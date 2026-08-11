"""Speaker label resolution + transcript.md round-trip."""
from __future__ import annotations

import json
from pathlib import Path

from witness.render import _speaker_label, render


def test_channels_are_the_speakers():
    assert _speaker_label({"channel": "mic", "text": "x"}) == "You"
    assert _speaker_label({"channel": "system", "text": "x"}) == "Remote"


def test_legacy_diarization_speaker_is_ignored():
    """Transcripts captured before diarization was removed still carry a
    per-utterance `speaker`. It attributed badly, so the channel wins."""
    assert _speaker_label({"channel": "mic", "speaker": "mic_speaker_3"}) == "You"
    assert (
        _speaker_label({"channel": "system", "speaker": "system_speaker_2"})
        == "Remote"
    )
    assert _speaker_label({"channel": "system", "speaker": "Alex"}) == "Remote"


def test_missing_channel_is_not_guessed():
    assert _speaker_label({"text": "x"}) == "?"


def test_render_groups_consecutive_same_speaker(tmp_path: Path):
    folder = tmp_path / "2026-04-28T1200-test"
    folder.mkdir()
    events = [
        {"channel": "mic", "is_final": True, "text": "hi",
         "ts_start": 0.0, "received_at": "2026-04-28T12:00:00+00:00"},
        {"channel": "mic", "is_final": True, "text": "how are you",
         "ts_start": 1.0, "received_at": "2026-04-28T12:00:01+00:00"},
        {"channel": "system", "is_final": True,
         "text": "good", "ts_start": 2.0,
         "received_at": "2026-04-28T12:00:02+00:00"},
    ]
    (folder / "transcript.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    out = render(folder)
    body = out.read_text()
    # Mic utterances grouped under one "You" header.
    assert body.count("**You**") == 1
    assert "**Remote**" in body
    assert "hi" in body and "how are you" in body and "good" in body


def test_render_interleaves_channels_by_timestamp(tmp_path: Path):
    """Both channels are transcribed separately and concatenated, so file
    order is per-channel. The rendered transcript has to be chronological or
    the conversation reads as two monologues."""
    folder = tmp_path / "2026-04-28T1200-test"
    folder.mkdir()
    events = [
        {"channel": "mic", "is_final": True, "text": "first", "ts_start": 0.0},
        {"channel": "mic", "is_final": True, "text": "third", "ts_start": 4.0},
        {"channel": "system", "is_final": True, "text": "second", "ts_start": 2.0},
    ]
    (folder / "transcript.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    body = render(folder).read_text()
    assert body.index("first") < body.index("second") < body.index("third")


def test_render_skips_empty_and_interim(tmp_path: Path):
    folder = tmp_path / "2026-04-28T1200-test"
    folder.mkdir()
    events = [
        {"channel": "mic", "is_final": False, "text": "in progress", "ts_start": 0},
        {"channel": "mic", "is_final": True, "text": "", "ts_start": 1},
        {"channel": "mic", "is_final": True, "text": "kept", "ts_start": 2},
    ]
    (folder / "transcript.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    out = render(folder)
    body = out.read_text()
    assert "in progress" not in body
    assert "kept" in body
