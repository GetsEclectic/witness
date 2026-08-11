"""One recording session: an ffmpeg process and the folder it writes into.

Extracted from `cli/witness.py` so both `witness record-now` and the
auto-trigger daemon can use the same pipeline. Does NOT own the web UI —
that's run once at the daemon level and points at whichever session is current.

Transcription is not part of a session. `audio.opus` is the deliverable; the
post-meeting pipeline (witness/pipeline.py) transcribes it afterwards, and the
daemon runs that pipeline after every pause as well as at the terminal stop.

Single-segment usage (e.g. `witness record-now`):

    session = Session(slug)
    await session.start()
    ...
    await session.stop()

Multi-segment usage (the daemon's auto-trigger flow):

    session = Session(slug)
    await session.start()
    ...
    await session.pause()       # ffmpeg stops, folder stays open
    # (some time later, same key reappears)
    await session.resume()      # new ffmpeg into next segment
    ...
    await session.stop()        # terminal: audio.opus is final

`pause()` concatenates all segments-so-far into `audio.opus` so the
post-meeting pipeline can run against a complete file even between segments.
The pipeline is idempotent (see witness/pipeline.py + the flock there);
running it after every pause is the user's chosen tradeoff over deferring
to a single terminal stop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("witnessd.session")

from . import record
from .config import MEETINGS_ROOT

# A live capture whose segment file stops growing for this long has lost its
# audio source. ffmpeg can't see this itself — it stays alive blocked on an
# idle input, so `_watch_ffmpeg` never fires — which is how a 42-minute meeting
# once recorded 5:45 and ran the remaining 36 minutes into a void. Opus writes
# pages even through silence, so no growth at all means no samples arriving,
# not a quiet room.
STALL_TIMEOUT_S = 120.0
_STALL_POLL_S = 10.0


class Session:
    def __init__(
        self,
        slug: str,
        root: Path | None = None,
        metadata_extra: dict[str, Any] | None = None,
    ) -> None:
        self.slug = slug
        self._root = root
        self._metadata_extra = metadata_extra or {}
        self.rec: record.Recording | None = None
        self._tasks: list[asyncio.Task] = []
        self._winding_down = False  # interrupt+wait in progress for current segment
        self._terminal = False  # stop() called
        self._stopped = asyncio.Event()
        self._folder: Path | None = None
        self._session_started_at: str | None = None
        # Multi-segment bookkeeping. Each pause/resume produces a new
        # segment file; `_segment_paths` is what gets concatenated into
        # audio.opus, and the transcript is derived from that concatenation
        # so timestamps are inherently continuous across segments.
        self._segment_index: int = 0
        self._segment_paths: list[Path] = []

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
        """First segment. Call once per Session.

        When `reattach_folder` is given, the session pins itself to an
        existing partially-recorded folder (used by the daemon's orphan
        sweep to resume a meeting whose previous daemon process died
        mid-recording). Existing audio segments under audio/NNN.opus are
        picked up so the next concat includes them. metadata.json is
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
            await self._start_segment(write_metadata=False)
        else:
            await self._start_segment(write_metadata=True)
            self._session_started_at = self.rec.started_at if self.rec else None

    async def pause(self) -> None:
        """Stop the current segment but keep the session open for resume.

        After return: ffmpeg is dead and audio.opus reflects all
        segments-so-far, so the pipeline the daemon spawns next sees a
        complete recording.
        """
        if self.is_paused or self._terminal:
            return
        await self._wind_down_segment()
        record.concat(self._segment_paths, self._folder / "audio.opus")
        self._stamp_metadata_ended()

    async def resume(self) -> None:
        """Start the next segment in the same folder."""
        assert self.is_paused, "Session.resume called outside paused state"
        # Clear ended_at — recording is in progress again.
        meta = json.loads(self._metadata_path.read_text())
        meta["ended_at"] = None
        self._metadata_path.write_text(json.dumps(meta, indent=2))
        self._segment_index += 1
        await self._start_segment(write_metadata=False)

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
        self._stopped.set()

    async def wait_stopped(self) -> None:
        await self._stopped.wait()

    # --- providers / introspection ---

    def status_folder(self) -> Path | None:
        return self._folder

    # --- internal: segment bring-up / tear-down ---

    async def _start_segment(self, *, write_metadata: bool) -> None:
        base = self._folder or ((self._root or MEETINGS_ROOT) / self.slug)
        seg_path = base / "audio" / f"{self._segment_index:03d}.opus"
        kwargs: dict[str, Any] = {"audio_path": seg_path, "write_metadata": write_metadata}
        if self._root is not None:
            kwargs["root"] = self._root
        rec = record.start(self.slug, **kwargs)
        self.rec = rec
        self._folder = rec.folder
        self._metadata_path = rec.metadata_path
        self._segment_paths.append(seg_path)
        self._winding_down = False

        # First-segment-only: merge daemon-supplied extra metadata into the
        # freshly-written metadata.json. Resume segments don't write metadata
        # (write_metadata=False) so this branch wouldn't make sense for them.
        if write_metadata and self._metadata_extra:
            meta = json.loads(rec.metadata_path.read_text())
            meta.update(self._metadata_extra)
            rec.metadata_path.write_text(json.dumps(meta, indent=2))

        self._tasks = [
            asyncio.create_task(
                self._watch_ffmpeg(),
                name=f"ffmpeg-watch[{self.slug}#{self._segment_index}]",
            ),
            asyncio.create_task(
                self._watch_stall(seg_path),
                name=f"stall-watch[{self.slug}#{self._segment_index}]",
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

    async def _watch_stall(self, seg_path: Path) -> None:
        """Trip a terminal stop when the segment file stops growing.

        Salvages the folder so the daemon's bounded-restart path can start
        fresh capture, rather than letting the rest of the meeting run into a
        source that has quietly stopped delivering samples.
        """
        last_size = -1
        last_growth = time.monotonic()
        while not self._winding_down and not self._terminal:
            await asyncio.sleep(_STALL_POLL_S)
            try:
                size = seg_path.stat().st_size
            except OSError:
                size = 0
            if size > last_size:
                last_size = size
                last_growth = time.monotonic()
                continue
            if time.monotonic() - last_growth >= STALL_TIMEOUT_S:
                log.error(
                    "no audio written to %s for %.0fs — the capture source has "
                    "stalled; stopping session so the daemon can restart it",
                    seg_path.name, STALL_TIMEOUT_S,
                )
                asyncio.create_task(self.stop())
                return

    async def _wind_down_segment(self) -> None:
        """Shared by pause and stop. Interrupt ffmpeg and reap the watchers.
        Does NOT touch metadata's ended_at — callers do that."""
        assert self.rec is not None
        self._winding_down = True

        record.interrupt(self.rec)
        await asyncio.to_thread(record.wait_for_exit, self.rec)
        # Don't stamp ended_at — we may be pausing, not stopping.
        record.finalize(self.rec, stamp_metadata=False)

        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        self.rec = None
        self._tasks = []

    def _stamp_metadata_ended(self) -> None:
        if self._folder is None:
            return
        meta = json.loads(self._metadata_path.read_text())
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        meta["segment_count"] = self._segment_index + 1
        self._metadata_path.write_text(json.dumps(meta, indent=2))
