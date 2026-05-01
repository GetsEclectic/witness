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

**Requires speechbrain.** Optional extra in pyproject; install with:
    uv sync --extra fingerprint

The model (`speechbrain/spkrec-ecapa-voxceleb`) is ungated — first call
auto-downloads it to `~/.cache/witness/speechbrain/`, no HF token needed.

Exposes:
  resolve(folder)           — full pipeline on a meeting
  enroll(name, audio_path)  — add a voiceprint from a sample clip
  load_voiceprints()        — list known identities + embeddings
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from witnessd.config import VOICEPRINTS_DIR

log = logging.getLogger("witness.fingerprint")

MATCH_THRESHOLD = 0.55  # cosine similarity; ECAPA self-sim across cross-session
                        # audio routinely lands ~0.6–0.7 in this corpus, so 0.7
                        # was strict enough that even a person's own samples
                        # missed each other. 0.55 catches recurring contacts
                        # without false-merging different speakers.
MERGE_THRESHOLD = 0.65  # cos similarity above which a /unknowns bind also
                        # absorbs other unknown_*.npy that look like the same
                        # person (e.g. Lissa split into 4 clusters across
                        # meetings). Stricter than MATCH so explicit binds
                        # don't accidentally chain into a different person.
SAMPLE_RATE = 16000
MIN_CLUSTER_SECONDS = 3.0   # skip clusters too short for a reliable embedding


_MODEL_CACHE_DIR = Path.home() / ".cache" / "witness" / "speechbrain" / "spkrec-ecapa-voxceleb"


def _load_inference():
    """Lazy import so the rest of the pipeline runs without speechbrain."""
    from speechbrain.inference.speaker import EncoderClassifier
    _MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # CPU is fine: ECAPA-TDNN embeds 30s of audio in ~1s on a modern CPU,
    # and avoids the prime-run / driver-version dance the dGPU needs.
    return EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str(_MODEL_CACHE_DIR),
        run_opts={"device": "cpu"},
    )


# Only the system channel is diarized live (mic channel is post-AEC and is
# always the local user — see deepgram_live._build_url). `mic_speaker_` is
# kept here so legacy captures from before mic-diarization-was-disabled
# still resolve when re-running the pipeline; it routes to channel 0.
CHANNEL_PREFIXES = {"system_speaker_": 1, "mic_speaker_": 0, "speaker_": 1}

# Filler words / backchannels that, alone, identify "person who said yeah"
# rather than the person. A cluster of nothing but these gets a noisy ECAPA
# embedding even when total duration is generous (e.g. 180s of 'Yeah.' from
# one diarized speaker_id). Drop those events outright; if an utterance
# contains one of these plus other content it stays.
_BACKCHANNELS = frozenset({
    "yeah", "yes", "yep", "yup", "ya", "no", "nope", "nah",
    "ok", "okay", "okie", "alright",
    "mhm", "mmhm", "mhmm", "mm", "mmm", "uhhuh", "huh",
    "right", "sure", "true", "correct",
    "totally", "exactly", "absolutely", "definitely",
    "gotit", "gotcha", "isee", "forsure",
    "oh", "ohh", "wow", "nice", "cool", "great", "awesome", "perfect",
    "hmm", "hm", "uh", "um", "er", "ah", "ahh",
    "thanks", "thanksomuch", "thankyou", "ty",
})


def _is_backchannel(text: str) -> bool:
    """True iff every alphabetic token in `text` is a known backchannel.
    Punctuation, hyphens, and whitespace are squashed before the check, so
    'Uh-huh.', 'mm-hmm,', 'yeah, yeah!' all qualify."""
    if not text:
        return True
    squashed = re.sub(r"[^a-z]+", "", text.lower())
    if not squashed:
        return True
    if squashed in _BACKCHANNELS:
        return True
    tokens = [t for t in re.split(r"[^a-z]+", text.lower()) if t]
    return bool(tokens) and all(t in _BACKCHANNELS for t in tokens)


def _cluster_spans(folder: Path) -> dict[str, list[tuple[float, float]]]:
    """Group utterance (ts_start, ts_end) spans by raw speaker label.

    Returns both mic_speaker_N and system_speaker_N clusters. The caller
    routes each to the correct audio channel via `_channel_for_speaker`.
    Backchannel-only utterances are dropped here — see `_is_backchannel`.
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
        if _is_backchannel(evt.get("text") or ""):
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

    Uses the `pan` audio filter to select channel N (`-map_channel` was
    removed in ffmpeg 7). soundfile/libsndfile don't have Opus support on
    Ubuntu so we shell out — fast and avoids a libsndfile rebuild.
    """
    import subprocess
    import numpy as np
    from witnessd._platform import ffmpeg_path
    proc = subprocess.run(
        [
            ffmpeg_path(), "-v", "error", "-i", str(audio_path),
            "-af", f"pan=mono|c0=c{channel}",
            "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-f", "f32le", "-",
        ],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(proc.stdout, dtype="float32").copy()


def _decode_mono(audio_path: Path):
    """Decode any audio file to a 16kHz mono float32 numpy array."""
    import subprocess
    import numpy as np
    from witnessd._platform import ffmpeg_path
    proc = subprocess.run(
        [
            ffmpeg_path(), "-v", "error", "-i", str(audio_path),
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
    import torch
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

    waveform = torch.from_numpy(clip).unsqueeze(0)  # (batch=1, time)
    with torch.no_grad():
        emb = inference.encode_batch(waveform)  # (1, 1, D)
    vec = emb.squeeze().cpu().numpy().astype("float32")
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


def promote_voiceprint(
    src_name: str,
    target_name: str,
    *,
    source_slug: str | None = None,
    speaker_id: str | None = None,
) -> Path | None:
    """Move embeddings from `src_name.npy` onto `target_name.npy`, merging if
    target already exists. Each promoted row gets a new metadata entry. The
    source `.npy` and `.meta.json` are removed iff `src_name` starts with
    `unknown_`. Returns the target path, or None if `src_name` had no file.

    Used by `witness relabel` and the web app's bind endpoint to share one
    code path for the rename → embedding-merge → meta-log flow.
    """
    import numpy as np
    src = VOICEPRINTS_DIR / f"{src_name}.npy"
    if not src.exists():
        return None
    target = VOICEPRINTS_DIR / f"{target_name}.npy"
    if src == target:
        return target

    new = np.load(src)
    if new.ndim == 1:
        new = new[None, :]
    rows_added = int(new.shape[0])
    if target.exists():
        existing = np.load(target)
        if existing.ndim == 1:
            existing = existing[None, :]
        new = np.vstack([existing, new])
    VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
    np.save(target, new)

    added_at = datetime.now(timezone.utc).isoformat()
    for _ in range(rows_added):
        _append_meta(target_name, {
            "added": added_at,
            "source": "relabel",
            "source_slug": source_slug,
            "speaker_id": speaker_id,
            "promoted_from": src.name,
        })
    if src_name.startswith("unknown_"):
        src.unlink()
        _meta_path(src_name).unlink(missing_ok=True)
    return target


def find_similar_unknowns(
    target_name: str, threshold: float = MERGE_THRESHOLD,
) -> list[tuple[str, float]]:
    """Return unknown voiceprints that look like the same person as
    `target_name`, sorted by cos-sim desc. Used by the /unknowns bind path
    to fold same-person clusters into one explicit name without making the
    user click through every duplicate."""
    import numpy as np
    target_path = VOICEPRINTS_DIR / f"{target_name}.npy"
    if not target_path.exists() or not VOICEPRINTS_DIR.exists():
        return []
    target = np.load(target_path)
    if target.ndim == 1:
        target = target[None, :]
    out: list[tuple[str, float]] = []
    for npy in VOICEPRINTS_DIR.glob("unknown_*.npy"):
        if npy.stem == target_name:
            continue
        try:
            vecs = np.load(npy)
        except Exception:
            continue
        if vecs.ndim == 1:
            vecs = vecs[None, :]
        sim = float((target @ vecs.T).max())
        if sim >= threshold:
            out.append((npy.stem, sim))
    out.sort(key=lambda x: -x[1])
    return out


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
    import torch
    inference = _load_inference()
    clip = _decode_mono(audio_path)
    if len(clip) < SAMPLE_RATE * MIN_CLUSTER_SECONDS:
        raise RuntimeError(
            f"audio too short for enrollment: {len(clip)/SAMPLE_RATE:.1f}s "
            f"(need >= {MIN_CLUSTER_SECONDS:.0f}s)"
        )
    waveform = torch.from_numpy(clip).unsqueeze(0)
    with torch.no_grad():
        emb_t = inference.encode_batch(waveform)
    emb = emb_t.squeeze().cpu().numpy().astype("float32")
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
