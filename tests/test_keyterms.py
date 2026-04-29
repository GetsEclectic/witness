"""load_keyterms cache + speaker-id filtering."""
from __future__ import annotations

import json
from pathlib import Path

from witnessd import config


def _make_speakers(root: Path, slug: str, mapping: dict[str, str]) -> None:
    folder = root / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "speakers.json").write_text(json.dumps(mapping))


def test_load_keyterms_harvests_real_names(tmp_meetings_root: Path):
    _make_speakers(tmp_meetings_root, "m1", {"system_speaker_0": "Alex"})
    _make_speakers(tmp_meetings_root, "m2", {"system_speaker_0": "Sam Wong"})
    terms = config.load_keyterms()
    assert "Alex" in terms
    assert "Sam Wong" in terms


def test_load_keyterms_skips_unresolved_ids(tmp_meetings_root: Path):
    _make_speakers(tmp_meetings_root, "m1", {
        "system_speaker_0": "unknown_a3f1",
        "system_speaker_1": "speaker_2",
    })
    terms = config.load_keyterms()
    assert "unknown_a3f1" not in terms
    assert "speaker_2" not in terms


def test_load_keyterms_caches_until_mtime_bumps(
    tmp_meetings_root: Path, monkeypatch
):
    import os

    _make_speakers(tmp_meetings_root, "m1", {"system_speaker_0": "Alex"})
    first = config.load_keyterms()
    assert "Alex" in first

    # Second call without dir-mtime change should hit the cache. Editing
    # speakers.json inside an existing folder doesn't change the parent
    # dir's mtime — assert the new name does NOT appear.
    (tmp_meetings_root / "m1" / "speakers.json").write_text(
        json.dumps({"system_speaker_0": "AddedLater"})
    )
    cached = config.load_keyterms()
    assert "AddedLater" not in cached  # cache hit

    # Bump the parent dir's mtime explicitly (creating m2 should do it, but
    # tests can run inside a single mtime tick on some filesystems — be
    # explicit so the test is deterministic).
    _make_speakers(tmp_meetings_root, "m2", {"system_speaker_0": "Sam"})
    st = tmp_meetings_root.stat()
    os.utime(tmp_meetings_root, (st.st_atime, st.st_mtime + 1))
    refreshed = config.load_keyterms()
    assert "Sam" in refreshed
    assert "AddedLater" in refreshed  # picked up on the refresh too
