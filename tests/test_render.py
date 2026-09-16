"""Speaker label resolution + transcript.md round-trip."""
from __future__ import annotations

import json
from pathlib import Path

from witness.render import (
    _speaker_label,
    display_name,
    read_metadata,
    recorder_name,
    render,
)
from witnessd import config


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


def test_display_name_from_email_local_part():
    assert display_name("ben.solwitz@equipmentshare.com") == "Ben Solwitz"
    assert display_name("angela_page@example.com") == "Angela Page"
    assert display_name("jay-kamm@example.com") == "Jay Kamm"
    assert display_name("randy@example.com") == "Randy"
    assert display_name("") == ""


def test_display_name_preserves_inner_capitals():
    """`O'Kane` and `McCoubrey` survive; a naive .capitalize() would not."""
    assert display_name("gary.O'Kane@example.com") == "Gary O'Kane"
    assert display_name("wilson.McCoubrey@example.com") == "Wilson McCoubrey"


def test_recorder_name_prefers_the_invite_over_config(monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", "Fallback Person")
    meta = {"calendar_event": {"self_email": "ben.solwitz@equipmentshare.com"}}
    assert recorder_name(meta) == "Ben Solwitz"


def test_recorder_name_falls_back_to_config(monkeypatch):
    """Roughly a third of recordings never matched an invite, so there is no
    self_email to read and the configured name is the only one available."""
    monkeypatch.setattr(config, "USER_NAME", "Ben Solwitz")
    assert recorder_name({}) == "Ben Solwitz"
    assert recorder_name(None) == "Ben Solwitz"
    assert recorder_name({"calendar_event": {"attendees": ["x@y.com"]}}) == "Ben Solwitz"


def test_recorder_name_is_none_when_nothing_knows_it(monkeypatch):
    monkeypatch.setattr(config, "USER_NAME", "")
    assert recorder_name({}) is None


def test_mic_channel_takes_the_recorders_name():
    assert _speaker_label({"channel": "mic"}, "Ben Solwitz") == "Ben Solwitz"
    # The remote side stays undifferentiated — there is no diarization to name it.
    assert _speaker_label({"channel": "system"}, "Ben Solwitz") == "Remote"


def test_render_labels_the_mic_with_the_recorders_name(tmp_path: Path):
    folder = tmp_path / "2026-04-28T1200-test"
    folder.mkdir()
    (folder / "metadata.json").write_text(json.dumps({
        "calendar_event": {"self_email": "ben.solwitz@equipmentshare.com"},
    }))
    (folder / "transcript.jsonl").write_text(json.dumps({
        "channel": "mic", "is_final": True, "text": "hi", "ts_start": 0.0,
    }) + "\n")
    body = render(folder).read_text()
    assert "**Ben Solwitz**" in body
    assert "**You**" not in body


def test_render_falls_back_to_you_with_no_metadata(monkeypatch, tmp_path: Path):
    """OSS installs with no calendar and no configured name keep the old label
    rather than getting an empty speaker header."""
    monkeypatch.setattr(config, "USER_NAME", "")
    folder = tmp_path / "2026-04-28T1200-test"
    folder.mkdir()
    (folder / "transcript.jsonl").write_text(json.dumps({
        "channel": "mic", "is_final": True, "text": "hi", "ts_start": 0.0,
    }) + "\n")
    assert "**You**" in render(folder).read_text()


def test_read_metadata_survives_corruption(tmp_path: Path):
    folder = tmp_path / "m"
    folder.mkdir()
    assert read_metadata(folder) == {}
    (folder / "metadata.json").write_text("{not json")
    assert read_metadata(folder) == {}
