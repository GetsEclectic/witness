"""Batch transcription — channel split, event shape, skip-when-current.

The model itself is stubbed everywhere: loading Parakeet costs ~1.2GB of
download on a cold machine and seconds of GPU time per run, and none of what
this file guards is model behavior. The ffmpeg channel split IS exercised for
real against a generated 2-channel file, because getting mic and system
crossed is exactly the bug that would be invisible in a stub.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from witnessd import transcribe
from witnessd._platform import ffmpeg_path


@pytest.fixture
def stereo_audio(tmp_path: Path) -> Path:
    """A 2-channel opus file with a distinguishable tone per channel:
    440Hz on ch0 (mic), 880Hz on ch1 (system)."""
    out = tmp_path / "audio.opus"
    subprocess.run(
        [
            ffmpeg_path(), "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
            "-filter_complex", "[0:a][1:a]amerge=inputs=2[m]",
            "-map", "[m]", "-ac", "2", "-c:a", "libopus", "-y", str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


def _dominant_freq(wav: Path) -> float:
    """Peak frequency of a mono wav, via numpy's FFT."""
    import wave

    import numpy as np

    with wave.open(str(wav)) as fh:
        rate = fh.getframerate()
        frames = np.frombuffer(fh.readframes(fh.getnframes()), dtype=np.int16)
    spectrum = np.abs(np.fft.rfft(frames.astype(np.float64)))
    return float(np.fft.rfftfreq(len(frames), 1.0 / rate)[int(np.argmax(spectrum))])


def test_split_channels_keeps_mic_on_ch0_and_system_on_ch1(
    stereo_audio: Path, tmp_path: Path
):
    """The entire speaker-attribution scheme rests on this mapping. Crossing
    the two would silently label every utterance with the wrong person."""
    out_dir = tmp_path / "split"
    out_dir.mkdir()
    wavs = transcribe.split_channels(stereo_audio, out_dir)

    assert set(wavs) == {"mic", "system"}
    assert _dominant_freq(wavs["mic"]) == pytest.approx(440, abs=15)
    assert _dominant_freq(wavs["system"]) == pytest.approx(880, abs=15)


def _stub_model(monkeypatch: pytest.MonkeyPatch, per_channel: dict[str, list]) -> None:
    """Make transcribe_channel return canned utterances per channel."""
    def _fake(wav_path: Path, channel: str) -> list[transcribe.TranscriptEvent]:
        return [
            transcribe.TranscriptEvent(
                channel=channel,
                text=text,
                ts_start=start,
                ts_end=start + 1.0,
                received_at="2026-08-11T10:00:00+00:00",
            )
            for text, start in per_channel.get(channel, [])
        ]

    monkeypatch.setattr(transcribe, "transcribe_channel", _fake)


def test_transcribe_writes_both_channels_in_chronological_order(
    stereo_audio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _stub_model(monkeypatch, {
        "mic": [("mine first", 0.0), ("mine last", 4.0)],
        "system": [("theirs middle", 2.0)],
    })
    out = transcribe.transcribe(stereo_audio.parent)

    assert out == stereo_audio.parent / "transcript.jsonl"
    rows = [json.loads(l) for l in out.read_text().splitlines() if l.strip()]
    assert [(r["channel"], r["text"]) for r in rows] == [
        ("mic", "mine first"),
        ("system", "theirs middle"),
        ("mic", "mine last"),
    ]
    assert all(r["is_final"] for r in rows)
    assert "speaker" not in rows[0]


def test_transcribe_skips_when_transcript_is_already_current(
    stereo_audio: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The pipeline runs after every pause and again at the terminal stop.
    Re-transcribing an unchanged recording is pure waste."""
    _stub_model(monkeypatch, {"mic": [("once", 0.0)]})
    folder = stereo_audio.parent
    transcribe.transcribe(folder)
    original = (folder / "transcript.jsonl").read_text()

    _stub_model(monkeypatch, {"mic": [("should not appear", 0.0)]})
    transcribe.transcribe(folder)
    assert (folder / "transcript.jsonl").read_text() == original

    # ...unless explicitly forced.
    transcribe.transcribe(folder, force=True)
    assert "should not appear" in (folder / "transcript.jsonl").read_text()


def test_transcribe_reruns_when_audio_is_newer(
    stereo_audio: Path, monkeypatch: pytest.MonkeyPatch
):
    """A resumed meeting rewrites audio.opus with the next segment appended;
    the stale transcript must not survive that."""
    _stub_model(monkeypatch, {"mic": [("first segment", 0.0)]})
    folder = stereo_audio.parent
    transcribe.transcribe(folder)

    import os
    later = stereo_audio.stat().st_mtime + 60
    os.utime(stereo_audio, (later, later))

    _stub_model(monkeypatch, {"mic": [("both segments", 0.0)]})
    transcribe.transcribe(folder)
    assert "both segments" in (folder / "transcript.jsonl").read_text()


def test_transcribe_returns_none_without_audio(tmp_path: Path):
    assert transcribe.transcribe(tmp_path) is None
    assert not (tmp_path / "transcript.jsonl").exists()
