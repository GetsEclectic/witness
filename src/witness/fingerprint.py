"""Resolve Deepgram's diarized speaker labels to real names via voiceprints.

How this hooks into the rest of the system:
  * Live transcription tags each utterance with `speaker: "mic_speaker_N"` or
    `"system_speaker_N"` depending on channel (Deepgram diarizes both sides).
  * Post-meeting, this module extracts an ECAPA-TDNN embedding per cluster by
    re-slicing the appropriate channel of `audio.opus` against the utterance
    timings in `transcript.jsonl`, then cosine-matches against embeddings in
    `$WITNESS_MEETINGS_DIR/.voiceprints/<name>.npy` (defaults to
    `~/meetings/.voiceprints/`). Matches above `MATCH_THRESHOLD` become real
    names; the rest get a stable `unknown_<hash>` label whose embedding is
    written to `.voiceprints/` so the user can `witness relabel` later.

Writes `speakers.json`, e.g.:
    {"mic_speaker_0": "Alex", "mic_speaker_1": "Sam",
     "system_speaker_0": "Jordan Lee"}.

**Requires pyannote.audio.** Optional dep, not in pyproject by default —
install with `uv pip install pyannote.audio torch torchaudio soundfile` and
accept the HF model terms at https://hf.co/pyannote/embedding, then set
`HUGGINGFACE_TOKEN=$(cat ~/.config/huggingface/token)`.

Exposes:
  resolve(folder)           — full pipeline on a meeting
  enroll(name, audio_path)  — add a voiceprint from a sample clip
  load_voiceprints()        — list known identities + embeddings
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from witnessd.config import VOICEPRINTS_DIR

log = logging.getLogger("witness.fingerprint")

MATCH_THRESHOLD = 0.70  # cosine similarity; tune from real data
SAMPLE_RATE = 16000
MIN_CLUSTER_SECONDS = 3.0   # skip clusters too short for a reliable embedding


def _load_inference():
    """Lazy import so the rest of the pipeline runs without pyannote."""
    import torch
    from pyannote.audio import Model, Inference

    token = os.environ.get("HUGGINGFACE_TOKEN")
    if not token:
        tok_path = Path.home() / ".config" / "huggingface" / "token"
        if tok_path.exists():
            token = tok_path.read_text().strip()
    if not token:
        raise RuntimeError(
            "HUGGINGFACE_TOKEN not set and ~/.config/huggingface/token missing"
        )

    model = Model.from_pretrained("pyannote/embedding", token=token)
    # CPU is fine: ECAPA-TDNN embeds 30s of audio in ~1s on a modern CPU,
    # and avoids the prime-run / driver-version dance the dGPU needs.
    device = torch.device("cpu")
    inference = Inference(model, window="whole", device=device)
    return inference


# Only the system channel is diarized live (mic channel is post-AEC and is
# always the local user — see deepgram_live._build_url). `mic_speaker_` is
# kept here so legacy captures from before mic-diarization-was-disabled
# still resolve when re-running the pipeline; it routes to channel 0.
CHANNEL_PREFIXES = {"system_speaker_": 1, "mic_speaker_": 0, "speaker_": 1}


def _cluster_spans(folder: Path) -> dict[str, list[tuple[float, float]]]:
    """Group utterance (ts_start, ts_end) spans by raw speaker label.

    Returns both mic_speaker_N and system_speaker_N clusters. The caller
    routes each to the correct audio channel via `_channel_for_speaker`.
    """
    jsonl = folder / "transcript.jsonl"
    clusters: dict[str, list[tuple[float, float]]] = {}
    if not jsonl.exists():
        return clusters
    for line in jsonl.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not evt.get("is_final"):
            continue
        sp = evt.get("speaker") or ""
        if not any(sp.startswith(p) for p in CHANNEL_PREFIXES):
            continue
        start = evt.get("ts_start")
        end = evt.get("ts_end") or (start + 1.0 if start is not None else None)
        if start is None or end is None:
            continue
        clusters.setdefault(sp, []).append((float(start), float(end)))
    return clusters


def _channel_for_speaker(sp: str) -> int | None:
    for prefix, ch in CHANNEL_PREFIXES.items():
        if sp.startswith(prefix):
            return ch
    return None


def _decode_channel(audio_path: Path, channel: int):
    """Decode one channel of audio.opus to a 16kHz mono float32 numpy array.

    ffmpeg's `-map_channel 0.0.N` selects channel N of input 0 stream 0.
    soundfile/libsndfile don't have Opus support on Ubuntu so we shell out —
    fast and avoids a libsndfile rebuild.
    """
    import subprocess
    import numpy as np
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(audio_path),
            "-map_channel", f"0.0.{channel}",
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-f", "f32le", "-",
        ],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(proc.stdout, dtype="float32").copy()


def _embed_cluster(
    inference: Any,
    audio: "Any",  # numpy array, system channel @ SAMPLE_RATE
    spans: list[tuple[float, float]],
):
    """Concatenate the longest up-to-30s of a speaker's audio, embed it."""
    import numpy as np
    import soundfile as sf
    spans = sorted(spans, key=lambda s: s[1] - s[0], reverse=True)
    take: list[tuple[float, float]] = []
    total = 0.0
    for s, e in spans:
        dur = e - s
        if dur < 0.5:
            continue
        take.append((s, e))
        total += dur
        if total >= 30.0:
            break
    if total < MIN_CLUSTER_SECONDS:
        return None

    chunks = []
    for s, e in take:
        i0, i1 = int(s * SAMPLE_RATE), int(e * SAMPLE_RATE)
        chunks.append(audio[i0:i1])
    clip = np.concatenate(chunks) if chunks else None
    if clip is None or len(clip) < SAMPLE_RATE * MIN_CLUSTER_SECONDS:
        return None

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = Path(f.name)
    try:
        sf.write(tmp, clip, SAMPLE_RATE, subtype="PCM_16")
        emb = inference(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)
    vec = np.asarray(emb, dtype="float32").flatten()
    vec /= (np.linalg.norm(vec) + 1e-9)
    return vec


def load_voiceprints() -> dict[str, Any]:
    """Return {name: list_of_unit_vectors} for every .npy in the voiceprint dir."""
    import numpy as np
    out: dict[str, Any] = {}
    if not VOICEPRINTS_DIR.exists():
        return out
    for p in VOICEPRINTS_DIR.glob("*.npy"):
        vecs = np.load(p)
        if vecs.ndim == 1:
            vecs = vecs[None, :]
        norms = np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
        vecs = vecs / norms
        out[p.stem] = vecs
    return out


def _meta_path(name: str) -> Path:
    return VOICEPRINTS_DIR / f"{name}.meta.json"


def _load_meta(name: str) -> list[dict[str, Any]]:
    p = _meta_path(name)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _append_meta(name: str, entry: dict[str, Any]) -> None:
    """Append a row-metadata entry alongside <name>.npy. Each entry records
    when the embedding row was added and where it came from, so a poisoned
    relabel can be diagnosed and pruned later."""
    rows = _load_meta(name)
    rows.append(entry)
    VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    _meta_path(name).write_text(json.dumps(rows, indent=2))


def load_voiceprint_meta(name: str) -> list[dict[str, Any]]:
    """Public: read the metadata sidecar for `name` (empty list if none)."""
    return _load_meta(name)


def _match(vec, prints: dict[str, Any]) -> tuple[str | None, float]:
    import numpy as np
    best_name, best_score = None, -1.0
    for name, vecs in prints.items():
        sims = vecs @ vec
        s = float(sims.max())
        if s > best_score:
            best_score = s
            best_name = name
    return (best_name if best_score >= MATCH_THRESHOLD else None), best_score


def _unknown_label(vec) -> str:
    h = hashlib.sha1(vec.tobytes()).hexdigest()[:6]
    return f"unknown_{h}"


def resolve(folder: Path) -> dict[str, str]:
    """Write folder/speakers.json. Returns the resolved map."""
    import numpy as np

    audio = folder / "audio.opus"
    if not audio.exists():
        log.info("no audio.opus; skipping fingerprint")
        return {}

    clusters = _cluster_spans(folder)
    if not clusters:
        log.info("no diarized speakers; skipping fingerprint")
        return {}

    inference = _load_inference()
    prints = load_voiceprints()
    VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)

    # Decode only the channels we actually have clusters for.
    needed_channels = {
        ch for sp in clusters
        if (ch := _channel_for_speaker(sp)) is not None
    }
    decoded = {ch: _decode_channel(audio, ch) for ch in needed_channels}

    resolved: dict[str, str] = {}
    for sp, spans in clusters.items():
        ch = _channel_for_speaker(sp)
        if ch is None:
            continue
        vec = _embed_cluster(inference, decoded[ch], spans)
        if vec is None:
            log.info("%s: too little audio to embed", sp)
            continue
        name, score = _match(vec, prints)
        if name is None:
            name = _unknown_label(vec)
            # Store so `witness relabel` can bind it to a real person later.
            np.save(VOICEPRINTS_DIR / f"{name}.npy", vec[None, :])
            _append_meta(name, {
                "added": datetime.now(timezone.utc).isoformat(),
                "source": "resolve",
                "source_slug": folder.name,
                "speaker_id": sp,
            })
            log.info("%s → %s (unknown, stored)", sp, name)
        else:
            log.info("%s → %s (cos=%.3f)", sp, name, score)
        resolved[sp] = name

    (folder / "speakers.json").write_text(json.dumps(resolved, indent=2))
    return resolved


def enroll(name: str, audio_path: Path) -> Path:
    """Add (or append to) a voiceprint for `name` from a clean sample clip."""
    import numpy as np
    inference = _load_inference()
    emb = np.asarray(inference(str(audio_path)), dtype="float32").flatten()
    emb /= (np.linalg.norm(emb) + 1e-9)
    VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    out = VOICEPRINTS_DIR / f"{name}.npy"
    if out.exists():
        existing = np.load(out)
        if existing.ndim == 1:
            existing = existing[None, :]
        emb = np.vstack([existing, emb[None, :]])
    else:
        emb = emb[None, :]
    np.save(out, emb)
    _append_meta(name, {
        "added": datetime.now(timezone.utc).isoformat(),
        "source": "enroll",
        "audio_path": str(audio_path),
    })
    return out
