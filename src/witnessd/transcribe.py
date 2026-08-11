"""Batch ASR: turn a finished `audio.opus` into `transcript.jsonl`, locally.

Speaker attribution is the channel layout, not diarization. `audio.opus` is
2-channel by construction (see record.py): ch0 is the mic — that is the user —
and ch1 is system audio — that is everyone else on the call. Splitting the two
and transcribing each separately gives "You" vs "Remote" for free, and gives it
correctly, which is more than the diarize-then-fingerprint path it replaces
ever managed.

Transcription runs on Parakeet (NVIDIA's TDT model) through MLX, so it uses the
Mac's GPU and never leaves the machine. The model is ~1.2GB and is fetched from
HuggingFace on first use into ~/.cache/huggingface.

Batch, not streaming: this runs from the post-meeting pipeline, which the daemon
already invokes after every pause as well as at the terminal stop. A meeting
that pauses partway through therefore gets a transcript partway through — a
periodic pipeline run is all "live" transcription would need, and there is no
socket to keep alive in the meantime.

Idempotent: `transcript.jsonl` is rewritten from scratch each run, and a run
whose output is already newer than the audio is skipped entirely (the pipeline
fires again at terminal stop even when the last pause added no audio).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._platform import ffmpeg_path
from .config import ASR_CHUNK_S, ASR_MODEL, ASR_OVERLAP_S, ASR_SAMPLE_RATE

log = logging.getLogger("witnessd.transcribe")

# ch0 = mic (the local user), ch1 = system (everyone else). Named here so the
# ffmpeg pan filters and the emitted `channel` field can't drift apart.
CHANNELS: tuple[tuple[str, int], ...] = (("mic", 0), ("system", 1))


@dataclass(frozen=True)
class TranscriptEvent:
    """One utterance. Written as a line of transcript.jsonl.

    `is_final` is always True and exists only so the field survives for
    readers of older transcripts, which interleaved interim events from the
    streaming path. Nothing emits interim events any more.
    """
    channel: str  # "mic" | "system"
    text: str
    ts_start: float | None
    ts_end: float | None
    received_at: str
    is_final: bool = True

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _ensure_ffmpeg_on_path(shim_dir: Path) -> None:
    """Make `ffmpeg` resolvable by name for parakeet_mlx.

    parakeet_mlx decodes audio by shelling out to whatever `ffmpeg` PATH
    resolves to, and refuses to run when that is nothing. Ours is the
    imageio-ffmpeg wheel's binary, which is version-stamped
    (`ffmpeg-macos-aarch64-v7.1`) and deliberately not installed system-wide —
    so we hand it a directory containing a symlink under the name it looks for.
    No-op on Linux, where ffmpeg is a documented system dependency and already
    on PATH.
    """
    if shutil.which("ffmpeg"):
        return
    real = Path(ffmpeg_path())
    if not real.is_absolute():
        return  # nothing we can point at
    link = shim_dir / "ffmpeg"
    if not link.exists():
        link.symlink_to(real)
    os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"


_MODEL: Any = None


def load_model() -> Any:
    """Load (and cache) the Parakeet model.

    Held for the life of the process. The pipeline runs as a short-lived
    subprocess per meeting, so this is one load per meeting; the daemon itself
    never imports this module.
    """
    global _MODEL
    if _MODEL is None:
        from parakeet_mlx import from_pretrained

        log.info("loading ASR model %s", ASR_MODEL)
        _MODEL = from_pretrained(ASR_MODEL)
    return _MODEL


def split_channels(audio_path: Path, out_dir: Path) -> dict[str, Path]:
    """Decode `audio.opus` into one 16kHz mono WAV per channel.

    One ffmpeg pass with two outputs — decoding the opus twice would double
    the only part of this that isn't the model. Returns {channel: wav_path},
    omitting any channel that came out empty (a mono recording from a
    half-failed capture still transcribes on the channel it has).
    """
    paths = {name: out_dir / f"{name}.wav" for name, _ in CHANNELS}
    filters = ";".join(f"[0:a]pan=mono|c0=c{idx}[{name}]" for name, idx in CHANNELS)
    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "warning",
        "-i", str(audio_path),
        "-filter_complex", filters,
    ]
    for name, _ in CHANNELS:
        cmd += [
            "-map", f"[{name}]",
            "-ar", str(ASR_SAMPLE_RATE),
            "-ac", "1",
            "-y", str(paths[name]),
        ]
    subprocess.run(cmd, check=True, capture_output=True)
    return {n: p for n, p in paths.items() if p.exists() and p.stat().st_size > 44}


def transcribe_channel(wav_path: Path, channel: str) -> list[TranscriptEvent]:
    """Run the model over one channel and return its utterances.

    `chunk_duration` bounds encoder memory — an hour of audio in one pass
    would not fit — and the overlap is what lets a sentence straddling a chunk
    boundary come back whole. Timestamps are absolute across chunks.
    """
    model = load_model()
    received_at = datetime.now(timezone.utc).isoformat()

    def _progress(pos: float, total: float) -> None:
        log.info("%s channel: %.0f/%.0fs", channel, pos, total)

    result = model.transcribe(
        wav_path,
        chunk_duration=ASR_CHUNK_S,
        overlap_duration=ASR_OVERLAP_S,
        chunk_callback=_progress,
    )
    events: list[TranscriptEvent] = []
    for sentence in result.sentences:
        text = sentence.text.strip()
        if not text:
            continue
        events.append(
            TranscriptEvent(
                channel=channel,
                text=text,
                ts_start=sentence.start,
                ts_end=sentence.end,
                received_at=received_at,
            )
        )
    return events


def transcribe(folder: Path, force: bool = False) -> Path | None:
    """Transcribe `folder/audio.opus` into `folder/transcript.jsonl`.

    Returns the transcript path, or None when there was nothing to do (no
    audio, or an existing transcript already covers this audio). Pass
    `force=True` to re-transcribe regardless — e.g. after changing models.
    """
    audio = folder / "audio.opus"
    out = folder / "transcript.jsonl"
    if not audio.exists() or audio.stat().st_size == 0:
        log.warning("no audio to transcribe in %s", folder.name)
        return None
    if (
        not force
        and out.exists()
        and out.stat().st_size > 0
        and out.stat().st_mtime >= audio.stat().st_mtime
    ):
        log.info("transcript for %s is already current; skipping", folder.name)
        return out

    with tempfile.TemporaryDirectory(prefix="witness-asr-") as tmp:
        _ensure_ffmpeg_on_path(Path(tmp))
        wavs = split_channels(audio, Path(tmp))
        if not wavs:
            log.warning("no decodable audio channels in %s", folder.name)
            return None
        events: list[TranscriptEvent] = []
        for channel, wav in wavs.items():
            events.extend(transcribe_channel(wav, channel))

    # Interleave the two channels into one chronological stream so
    # transcript.jsonl reads in the order the conversation happened.
    events.sort(key=lambda e: (e.ts_start if e.ts_start is not None else 0.0))

    tmp_out = out.with_suffix(".jsonl.tmp")
    with tmp_out.open("w", encoding="utf-8") as fh:
        for evt in events:
            fh.write(json.dumps(evt.to_dict()) + "\n")
    tmp_out.replace(out)
    log.info("transcribed %s: %d utterances", folder.name, len(events))
    return out
