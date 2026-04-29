"""Speaker label resolution + transcript.md round-trip."""
from __future__ import annotations

import json
from pathlib import Path

from witness.render import _speaker_label, render


def test_mic_channel_always_resolves_to_you():
    assert _speaker_label({"channel": "mic", "speaker": ""}, {}) == "You"
    # Even if Deepgram somehow tagged a speaker, mic = You.
    assert _speaker_label({"channel": "mic", "speaker": "mic_speaker_3"}, {}) == "You"


def test_legacy_ben_tag_resolves_to_you():
    assert _speaker_label({"channel": "system", "speaker": "ben"}, {}) == "You"


def test_system_channel_indirection_chain():
    resolved = {"system_speaker_0": "unknown_a3f1", "unknown_a3f1": "Alex"}
    evt = {"channel": "system", "speaker": "system_speaker_0"}
    assert _speaker_label(evt, resolved) == "Alex"


def test_unresolved_system_speaker_falls_back_to_remote_n():
    evt = {"channel": "system", "speaker": "system_speaker_2"}
    assert _speaker_label(evt, {}) == "Remote 2"


def test_legacy_speaker_prefix_falls_back_to_spk():
    evt = {"channel": "system", "speaker": "speaker_1"}
    assert _speaker_label(evt, {}) == "Spk 1"


def test_speaker_chain_with_cycle_does_not_loop_forever():
    # If the resolved map contains a cycle, the walk breaks and `cur` ends
    # up back at `sp`; the label falls through to the unknown-prefix branch.
    # Behavior we care about: termination, not a specific label.
    resolved = {"system_speaker_0": "system_speaker_0"}
    label = _speaker_label(
        {"channel": "system", "speaker": "system_speaker_0"}, resolved
    )
    assert label == "Remote 0"


def test_render_groups_consecutive_same_speaker(tmp_path: Path):
    folder = tmp_path / "2026-04-28T1200-test"
    folder.mkdir()
    events = [
        {"channel": "mic", "speaker": "", "is_final": True, "text": "hi",
         "ts_start": 0.0, "received_at": "2026-04-28T12:00:00+00:00"},
        {"channel": "mic", "speaker": "", "is_final": True, "text": "how are you",
         "ts_start": 1.0, "received_at": "2026-04-28T12:00:01+00:00"},
        {"channel": "system", "speaker": "system_speaker_0", "is_final": True,
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
    assert "**Remote 0**" in body
    assert "hi" in body and "how are you" in body and "good" in body


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
