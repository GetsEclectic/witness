"""One recording session: ffmpeg + both Deepgram WebSockets + EventBus.

Extracted from `cli/witness.py` so both `witness record-now` and the
auto-trigger daemon can use the same pipeline. Does NOT own the web UI —
that's run once at the daemon level and points at whichever session is current.

Single-segment usage (e.g. `witness record-now`):

    session = Session(slug, api_key)
    await session.start()
    ...
    await session.stop()

Multi-segment usage (the daemon's auto-trigger flow):

    session = Session(slug, api_key)
    await session.start()
    ...
    await session.pause()       # ffmpeg + deepgram stop, bus stays open
    # (some time later, same key reappears)
    await session.resume()      # new ffmpeg into next segment
    ...
    await session.stop()        # terminal: closes bus, audio.opus is final

`pause()` concatenates all segments-so-far into `audio.opus` so the
post-meeting pipeline can run against a complete file even between segments.
The pipeline is idempotent (see witness/pipeline.py + the flock there);
running it after every pause is the user's chosen tradeoff over deferring
to a single terminal stop.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("witnessd.session")

from . import record
from .config import MEETINGS_ROOT, load_keyterms
from .deepgram_live import TranscriptEvent, run as deepgram_run
from .transcript import EventBus


class Session:
    def __init__(
        self,
        slug: str,
        api_key: str,
        root: Path | None = None,
        metadata_extra: dict[str, Any] | None = None,
    ) -> None:
        self.slug = slug
        self._api_key = api_key
        self._root = root
        self._metadata_extra = metadata_extra or {}
        self.rec: record.Recording | None = None
        self.bus: EventBus | None = None
        self._tasks: list[asyncio.Task] = []
        self._winding_down = False  # interrupt+wait in progress for current segment
        self._terminal = False  # stop() called; bus is closed
        self._stopped = asyncio.Event()
        self._folder: Path | None = None
        self._session_started_at: str | None = None
        # Multi-segment bookkeeping. Each pause/resume produces a new
        # segment file. _offset_s is the cumulative wall-clock duration of
        # all prior segments; we add it to ts_start/ts_end on transcript
        # events so render.py sees monotonic timestamps across segments.
        self._segment_index: int = 0
        self._offset_s: float = 0.0
        self._segment_started_mono: float | None = None
        self._segment_paths: list[Path] = []
        # Set true if either Deepgram task raises during the session — the
        # recording itself still saves, but live transcript / post-meeting
        # render will be empty/partial. Surfaced via /api/status so the UI
        # can warn the user instead of silently producing a transcript-less
        # archive.
        self.transcription_failed: bool = False

    # --- public state ---

    @property
    def started_at(self) -> str | None:
        return self._session_started_at

    @property
    def started_dt(self) -> datetime | None:
        if self._session_started_at is None:
            return None
        try:
            return datetime.fromisoformat(self._session_started_at)
        except ValueError:
            return None

    @property
    def folder(self) -> Path | None:
        return self._folder

    @property
    def is_paused(self) -> bool:
        return self._folder is not None and self.rec is None and not self._terminal

    @property
    def is_terminal(self) -> bool:
        return self._terminal

    # --- lifecycle ---

    async def start(self, *, reattach_folder: Path | None = None) -> None:
        """First segment + create the bus. Call once per Session.

        When `reattach_folder` is given, the session pins itself to an
        existing partially-recorded folder (used by the daemon's orphan
        sweep to resume a meeting whose previous daemon process died
        mid-recording). Existing audio segments under audio/NNN.opus are
        scanned to compute the cumulative offset so transcript timestamps
        from the new segment continue monotonically. metadata.json is
        preserved — the original calendar correlation and detection trace
        stay attached to this meeting.
        """
        assert self._folder is None, "Session.start called twice"
        if reattach_folder is not None:
            self._folder = reattach_folder
            self._metadata_path = reattach_folder / "metadata.json"
            seg_dir = reattach_folder / "audio"
            existing = sorted(seg_dir.glob("*.opus")) if seg_dir.is_dir() else []
            existing = [s for s in existing if s.stat().st_size > 0]
            self._segment_paths = list(existing)
            self._segment_index = len(existing)
            # Probe each existing segment for duration (ffmpeg null-decodes
            # in <100ms each, so a multi-segment recording adds ~0.5s to
            # daemon startup — acceptable for a path that only fires after
            # a real crash).
            self._offset_s = sum(
                await asyncio.to_thread(record.probe_duration_s, p)
                for p in existing
            )
            try:
                meta = json.loads(self._metadata_path.read_text())
            except (OSError, json.JSONDecodeError):
                meta = {}
            self._session_started_at = meta.get("started_at")
            # Defensive: a previous crash could have left a stale ended_at
            # if the sweep stamped one before we got here. Clear it so the
            # session is unambiguously recording again.
            if meta.get("ended_at"):
                meta["ended_at"] = None
                self._metadata_path.write_text(json.dumps(meta, indent=2))
            await self._start_segment(write_metadata=False, create_bus=True)
        else:
            await self._start_segment(write_metadata=True, create_bus=True)
            self._session_started_at = self.rec.started_at if self.rec else None

    async def pause(self) -> None:
        """Stop the current segment but keep the session open for resume.

        After return: ffmpeg is dead, deepgram tasks are flushed and
        cancelled, audio.opus reflects all segments-so-far. Bus is still
        open so subscribers (the live web UI) keep their connection.
        """
        if self.is_paused or self._terminal:
            return
        await self._wind_down_segment()
        record.concat(self._segment_paths, self._folder / "audio.opus")
        self._stamp_metadata_ended()

    async def resume(self) -> None:
        """Start the next segment in the same folder/bus."""
        assert self.is_paused, "Session.resume called outside paused state"
        # Clear ended_at — recording is in progress again.
        meta = json.loads(self._metadata_path.read_text())
        meta["ended_at"] = None
        self._metadata_path.write_text(json.dumps(meta, indent=2))
        self._segment_index += 1
        await self._start_segment(write_metadata=False, create_bus=False)

    async def stop(self) -> None:
        """Terminal stop. Idempotent."""
        if self._terminal:
            await self._stopped.wait()
            return
        if not self.is_paused:
            await self._wind_down_segment()
            if self._segment_paths:
                record.concat(self._segment_paths, self._folder / "audio.opus")
            self._stamp_metadata_ended()
        self._terminal = True
        if self.bus is not None:
            self.bus.close()
            self.bus = None
        self._stopped.set()

    async def wait_stopped(self) -> None:
        await self._stopped.wait()

    # --- providers / introspection ---

    def status_folder(self) -> Path | None:
        return self._folder

    # --- internal: segment bring-up / tear-down ---

    async def _start_segment(self, *, write_metadata: bool, create_bus: bool) -> None:
        base = self._folder or ((self._root or MEETINGS_ROOT) / self.slug)
        seg_path = base / "audio" / f"{self._segment_index:03d}.opus"
        kwargs: dict[str, Any] = {"live": True, "audio_path": seg_path, "write_metadata": write_metadata}
        if self._root is not None:
            kwargs["root"] = self._root
        rec = record.start(self.slug, **kwargs)
        assert rec.mic_pcm_fd is not None and rec.system_pcm_fd is not None
        self.rec = rec
        self._folder = rec.folder
        self._metadata_path = rec.metadata_path
        self._segment_paths.append(seg_path)
        self._segment_started_mono = time.monotonic()
        self._winding_down = False

        # First-segment-only: merge daemon-supplied extra metadata into the
        # freshly-written metadata.json. Resume segments don't write metadata
        # (write_metadata=False) so this branch wouldn't make sense for them.
        if write_metadata and self._metadata_extra:
            meta = json.loads(rec.metadata_path.read_text())
            meta.update(self._metadata_extra)
            rec.metadata_path.write_text(json.dumps(meta, indent=2))

        if create_bus:
            self.bus = EventBus(rec.folder / "transcript.jsonl")

        keyterms = load_keyterms()

        def _dg_done(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is None:
                return
            self.transcription_failed = True
            log.error("transcription task %s failed", t.get_name(), exc_info=exc)

        mic_task = asyncio.create_task(
            deepgram_run(rec.mic_pcm_fd, "mic", self._api_key, self._on_event, keyterms=keyterms),
            name=f"deepgram-mic[{self.slug}#{self._segment_index}]",
        )
        sys_task = asyncio.create_task(
            deepgram_run(rec.system_pcm_fd, "system", self._api_key, self._on_event, keyterms=keyterms),
            name=f"deepgram-system[{self.slug}#{self._segment_index}]",
        )
        for t in (mic_task, sys_task):
            t.add_done_callback(_dg_done)
        self._tasks = [
            mic_task,
            sys_task,
            asyncio.create_task(
                self._watch_ffmpeg(),
                name=f"ffmpeg-watch[{self.slug}#{self._segment_index}]",
            ),
        ]

    async def _watch_ffmpeg(self) -> None:
        """If ffmpeg dies on its own (not because we asked it to), trigger
        a terminal stop. Pause/resume sets `_winding_down` to suppress this
        when we're the ones killing ffmpeg."""
        assert self.rec is not None
        rec = self.rec
        while rec.proc.poll() is None:
            await asyncio.sleep(0.5)
        if not self._winding_down:
            asyncio.create_task(self.stop())

    async def _wind_down_segment(self) -> None:
        """Shared by pause and stop. Interrupt ffmpeg, flush deepgram,
        close pcm fds. Bumps _offset_s by this segment's wall-clock duration
        so the next segment's transcript timestamps continue monotonically.
        Does NOT close the bus or touch metadata's ended_at — callers do that."""
        assert self.rec is not None
        self._winding_down = True

        record.interrupt(self.rec)

        dg_tasks = self._tasks[:2]
        if dg_tasks:
            try:
                await asyncio.wait(dg_tasks, timeout=5)
            except Exception:
                pass

        record.wait_for_exit(self.rec)
        # Don't stamp ended_at — we may be pausing, not stopping.
        record.finalize(self.rec, stamp_metadata=False)

        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        if self._segment_started_mono is not None:
            self._offset_s += max(0.0, time.monotonic() - self._segment_started_mono)
            self._segment_started_mono = None

        self.rec = None
        self._tasks = []

    def _stamp_metadata_ended(self) -> None:
        if self._folder is None:
            return
        meta = json.loads(self._metadata_path.read_text())
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        meta["segment_count"] = self._segment_index + 1
        self._metadata_path.write_text(json.dumps(meta, indent=2))

    async def _on_event(self, evt: TranscriptEvent) -> None:
        if self.bus is None:
            return
        if self._offset_s and (evt.ts_start is not None or evt.ts_end is not None):
            evt = dataclasses.replace(
                evt,
                ts_start=(evt.ts_start + self._offset_s) if evt.ts_start is not None else None,
                ts_end=(evt.ts_end + self._offset_s) if evt.ts_end is not None else None,
            )
        await self.bus.emit(evt)
