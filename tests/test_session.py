"""Session pause/resume/reattach behavior — no real ffmpeg, no real Deepgram.

`record.start` and friends are stubbed to write the folder + metadata
that Session needs to traverse, but never spawn a subprocess. The
Deepgram WS coroutine is replaced with a no-op so the session's
asyncio.create_task scaffolding stays intact without network I/O.

Uses asyncio.run rather than pytest-asyncio to avoid adding a dev dep
just for these tests.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from witnessd import record, session as session_mod
from witnessd.record import Recording
from witnessd.session import Session


class _FakeProc:
    """Minimal subprocess.Popen stand-in for record.Recording."""

    def __init__(self) -> None:
        self._exit_code: int | None = None

    def poll(self) -> int | None:
        return self._exit_code

    def wait(self, timeout: float | None = None) -> int:
        if self._exit_code is None:
            self._exit_code = 0
        return self._exit_code


def _fake_record_start(slug: str, root: Path = None, *, live: bool = True,
                       audio_path: Path | None = None,
                       write_metadata: bool = True, **_: Any) -> Recording:
    if root is None:
        from witnessd.config import MEETINGS_ROOT
        root = MEETINGS_ROOT
    folder = root / slug
    folder.mkdir(parents=True, exist_ok=True)
    if audio_path is None:
        audio_path = folder / "audio.opus"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"")  # placeholder; size==0 is fine for our scans
    metadata_path = folder / "metadata.json"
    started_at = "2026-04-30T12:00:00+00:00"
    if write_metadata:
        metadata_path.write_text(json.dumps({
            "slug": slug,
            "started_at": started_at,
            "ended_at": None,
        }, indent=2))
    # Session asserts both fds are not None when live=True. /dev/null is fine
    # since the deepgram stub never reads from them.
    mic_fd = os.open(os.devnull, os.O_RDONLY) if live else None
    sys_fd = os.open(os.devnull, os.O_RDONLY) if live else None
    return Recording(
        slug=slug,
        folder=folder,
        audio_path=audio_path,
        metadata_path=metadata_path,
        sources_metadata={"mic": "test", "system": "test"},
        started_at=started_at,
        proc=_FakeProc(),
        mic_pcm_fd=mic_fd,
        system_pcm_fd=sys_fd,
        aux_procs=[],
    )


@pytest.fixture
def stubbed_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace record.* + deepgram_run with no-ops."""
    monkeypatch.setattr(record, "start", _fake_record_start)
    monkeypatch.setattr(record, "interrupt", lambda rec: None)
    monkeypatch.setattr(record, "wait_for_exit", lambda rec, hard_timeout_s=60.0: 0)
    monkeypatch.setattr(record, "finalize", lambda rec, stamp_metadata=True: None)
    # `concat` is called from session paths we exercise; produce a stub file
    # so subsequent reads (e.g. fingerprint) wouldn't blow up — but in tests
    # nothing reads it, so empty bytes are fine.
    monkeypatch.setattr(record, "concat", lambda segments, out: out.write_bytes(b""))
    monkeypatch.setattr(record, "probe_duration_s", lambda path: 7.5)

    async def _noop_dg(*args: Any, **kwargs: Any) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(session_mod, "deepgram_run", _noop_dg)


def test_start_creates_folder_and_metadata(stubbed_record, tmp_meetings_root: Path):
    async def run():
        s = Session(slug="2026-04-30T1200-test", api_key="k", root=tmp_meetings_root)
        await s.start()
        folder = tmp_meetings_root / "2026-04-30T1200-test"
        assert folder.is_dir()
        meta = json.loads((folder / "metadata.json").read_text())
        assert meta["slug"] == "2026-04-30T1200-test"
        assert meta["ended_at"] is None
        await s.stop()

    asyncio.run(run())


def test_pause_resume_into_same_folder(stubbed_record, tmp_meetings_root: Path):
    """Pause/resume produces multiple segments in audio/NNN.opus pinned to one
    folder, with cumulative offset advanced for the post-resume segment."""
    async def run():
        s = Session(slug="2026-04-30T1200-multi", api_key="k", root=tmp_meetings_root)
        await s.start()
        folder = s.folder
        assert folder is not None
        assert (folder / "audio" / "000.opus").exists()
        assert s._offset_s == 0.0
        assert s._segment_index == 0

        await s.pause()
        assert s.is_paused
        meta = json.loads((folder / "metadata.json").read_text())
        assert meta["ended_at"] is not None
        # Offset advanced by the segment's wall-clock duration (small but
        # non-zero — _start_segment grabs time.monotonic() before pause does).
        assert s._offset_s >= 0.0

        await s.resume()
        assert not s.is_paused
        assert s._segment_index == 1
        assert (folder / "audio" / "001.opus").exists()
        meta = json.loads((folder / "metadata.json").read_text())
        assert meta["ended_at"] is None

        await s.stop()
        meta = json.loads((folder / "metadata.json").read_text())
        assert meta["segment_count"] == 2

    asyncio.run(run())


def test_reattach_resumes_into_existing_folder(stubbed_record, tmp_meetings_root: Path):
    """Pre-populate a folder that mimics an orphan: started_at present,
    ended_at null, two segments on disk. Session.start(reattach_folder=...)
    pins to it, computes offset, writes the next segment as 002.opus, and
    keeps the original metadata.
    """
    async def run():
        slug = "2026-04-30T1200-orphan"
        folder = tmp_meetings_root / slug
        folder.mkdir(parents=True)
        (folder / "audio").mkdir()
        (folder / "audio" / "000.opus").write_bytes(b"x" * 100)
        (folder / "audio" / "001.opus").write_bytes(b"x" * 100)
        original_meta = {
            "slug": slug,
            "started_at": "2026-04-30T11:30:00+00:00",
            "ended_at": None,
            "calendar_event": {"summary": "Original Meeting"},
            "detection": {"key": "meet:original-room", "platform": "meet"},
        }
        (folder / "metadata.json").write_text(json.dumps(original_meta, indent=2))

        s = Session(slug=slug, api_key="k", root=tmp_meetings_root)
        await s.start(reattach_folder=folder)

        assert s.folder == folder
        assert s._segment_index == 2
        assert s._offset_s == pytest.approx(15.0)
        assert (folder / "audio" / "002.opus").exists()
        meta = json.loads((folder / "metadata.json").read_text())
        assert meta["calendar_event"]["summary"] == "Original Meeting"
        assert meta["detection"]["key"] == "meet:original-room"
        assert meta["started_at"] == "2026-04-30T11:30:00+00:00"

        await s.stop()
        assert len(s._segment_paths) == 3

    asyncio.run(run())


def test_reattach_clears_stale_ended_at(stubbed_record, tmp_meetings_root: Path):
    """If a prior orphan-finalize had stamped ended_at, reattach clears it."""
    async def run():
        slug = "2026-04-30T1200-stale-end"
        folder = tmp_meetings_root / slug
        folder.mkdir(parents=True)
        (folder / "audio").mkdir()
        (folder / "audio" / "000.opus").write_bytes(b"x" * 100)
        (folder / "metadata.json").write_text(json.dumps({
            "slug": slug,
            "started_at": "2026-04-30T11:30:00+00:00",
            "ended_at": "2026-04-30T11:35:00+00:00",
        }, indent=2))

        s = Session(slug=slug, api_key="k", root=tmp_meetings_root)
        await s.start(reattach_folder=folder)
        meta = json.loads((folder / "metadata.json").read_text())
        assert meta["ended_at"] is None
        await s.stop()

    asyncio.run(run())
