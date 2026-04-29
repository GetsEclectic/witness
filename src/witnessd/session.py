"""One recording session: ffmpeg + both Deepgram WebSockets + EventBus.

Extracted from `cli/witness.py` so both `witness record-now` and the
auto-trigger daemon can use the same pipeline. Does NOT own the web UI —
that's run once at the daemon level and points at whichever session is current.

Usage:

    session = Session(slug, api_key)
    await session.start()
    ...
    await session.stop()

`.start()` is async so it can spin up the Deepgram tasks; it returns as
soon as ffmpeg is up. `.stop()` is idempotent.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger("witnessd.session")

from . import record
from .config import load_keyterms
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
        self._stopping = False
        self._stopped = asyncio.Event()
        # Set true if either Deepgram task raises during the session — the
        # recording itself still saves, but live transcript / post-meeting
        # render will be empty/partial. Surfaced via /api/status so the UI
        # can warn the user instead of silently producing a transcript-less
        # archive.
        self.transcription_failed: bool = False

    @property
    def started_at(self) -> str | None:
        return self.rec.started_at if self.rec else None

    @property
    def started_dt(self) -> datetime | None:
        if self.rec is None:
            return None
        try:
            return datetime.fromisoformat(self.rec.started_at)
        except ValueError:
            return None

    @property
    def folder(self) -> Path | None:
        return self.rec.folder if self.rec else None

    async def start(self) -> None:
        kwargs = {"live": True}
        if self._root is not None:
            kwargs["root"] = self._root  # type: ignore[assignment]
        rec = record.start(self.slug, **kwargs)
        assert rec.mic_pcm_fd is not None and rec.system_pcm_fd is not None
        self.rec = rec

        # Merge any extra metadata (e.g. calendar correlation trace) into
        # the metadata.json that record.start() wrote.
        if self._metadata_extra:
            meta = json.loads(rec.metadata_path.read_text())
            meta.update(self._metadata_extra)
            rec.metadata_path.write_text(json.dumps(meta, indent=2))

        self.bus = EventBus(rec.folder / "transcript.jsonl")

        async def on_event(evt: TranscriptEvent) -> None:
            assert self.bus is not None
            await self.bus.emit(evt)

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
            deepgram_run(rec.mic_pcm_fd, "mic", self._api_key, on_event, keyterms=keyterms),
            name=f"deepgram-mic[{self.slug}]",
        )
        sys_task = asyncio.create_task(
            deepgram_run(rec.system_pcm_fd, "system", self._api_key, on_event, keyterms=keyterms),
            name=f"deepgram-system[{self.slug}]",
        )
        for t in (mic_task, sys_task):
            t.add_done_callback(_dg_done)
        self._tasks = [
            mic_task,
            sys_task,
            asyncio.create_task(self._watch_ffmpeg(), name=f"ffmpeg-watch[{self.slug}]"),
        ]

    async def _watch_ffmpeg(self) -> None:
        """If ffmpeg dies on its own, fire stop()."""
        assert self.rec is not None
        while self.rec.proc.poll() is None:
            await asyncio.sleep(0.5)
        if not self._stopping:
            asyncio.create_task(self.stop())

    async def wait_stopped(self) -> None:
        await self._stopped.wait()

    async def stop(self) -> None:
        if self._stopping:
            await self._stopped.wait()
            return
        self._stopping = True
        assert self.rec is not None

        record.interrupt(self.rec)

        # Let the two Deepgram tasks flush their last words after EOF.
        dg_tasks = self._tasks[:2]
        if dg_tasks:
            try:
                await asyncio.wait(dg_tasks, timeout=5)
            except Exception:
                pass

        # Reap ffmpeg + write ended_at into metadata.
        record.wait_for_exit(self.rec)
        record.finalize(self.rec)

        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        if self.bus is not None:
            self.bus.close()

        self._stopped.set()
