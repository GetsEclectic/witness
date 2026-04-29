"""Auto-trigger daemon: poll windows, start/stop recordings on detection.

Runs one web UI continuously (so the user can keep a browser tab open
to localhost:7878) and at most one Session at a time.

State machine:
    IDLE       — no meeting window visible. Poll every POLL_INTERVAL_S.
    RECORDING  — active Session. Keep polling; once the window has been
                 gone for RECORDING_GRACE_S seconds, *pause* (not stop):
                 ffmpeg + deepgram wind down, audio.opus is concatenated,
                 the post-meeting pipeline is spawned. Folder + bus stay
                 open in case the same key reappears.
    PAUSED     — session paused, waiting up to RESUME_WINDOW_S for the
                 same key to reappear. Same key → resume into the same
                 folder as a new audio segment. Different key → finalize
                 current and start fresh. Window expires → finalize.

The calendar event end time is informational only — meetings commonly
finish well before their scheduled end, and gating stop on it once led
to Witness recording ambient audio for ~30 min after a call ended.

Triple-book disambiguation lives in detect.correlate — the trace is
persisted into metadata.json so we can tune it from real incidents.
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn

from . import detect
from .calendar import CalendarEvent, correlate, events_active_now
from .config import (
    LOG_PATH,
    MAX_RECORDING_S,
    MEETINGS_ROOT,
    POLL_INTERVAL_S,
    RECORDING_GRACE_S,
    RESUME_WINDOW_S,
    STATE_DIR,
    WEBAPP_HOST,
    WEBAPP_PORT,
    read_deepgram_key,
)
from .session import Session
from .webapp import RecordingStatus, build_app

log = logging.getLogger("witnessd.daemon")


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s or "meeting"


def _build_slug(event: CalendarEvent | None, detection_title: str) -> str:
    ts = datetime.now().strftime("%Y-%m-%dT%H%M")
    if event is not None:
        return f"{ts}-{_slugify(event.summary)}"
    # Strip trailing browser app suffix if we picked up a window title.
    clean = re.sub(
        r"\s*[-|\u2013\u2014]\s*(Google Chrome|Mozilla Firefox|Microsoft Teams).*$",
        "",
        detection_title,
    )
    return f"{ts}-{_slugify(clean)}"


class Daemon:
    def __init__(self) -> None:
        self.api_key = read_deepgram_key()
        self.session: Session | None = None
        self.current_event: CalendarEvent | None = None
        self._session_key: str | None = None
        # Last time the active session's window was *seen*. While recording,
        # this is now-on-each-tick. While paused, it freezes at the moment
        # the window disappeared, so RESUME_WINDOW_S is measured from there.
        self._last_match_at: datetime | None = None
        self._stop_flag = asyncio.Event()

    # --- providers for the webapp ---

    def bus_provider(self):
        return self.session.bus if self.session else None

    def status_provider(self) -> RecordingStatus:
        if self.session and self.session.rec:
            return RecordingStatus(
                active=True,
                slug=self.session.slug,
                started_at=self.session.started_at,
                transcription_failed=self.session.transcription_failed,
            )
        return RecordingStatus(
            active=False, slug=None, started_at=None, transcription_failed=False
        )

    # --- lifecycle ---

    async def run(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        MEETINGS_ROOT.mkdir(parents=True, exist_ok=True)

        # Recover any meeting folders left in limbo by a previous daemon
        # crash (started_at present, ended_at missing). Concat any segments,
        # stamp ended_at, and spawn the pipeline so they finalize cleanly.
        _sweep_orphans(MEETINGS_ROOT)

        app = build_app(
            bus=self.bus_provider,
            status=self.status_provider,
            meetings_root=MEETINGS_ROOT,
        )
        uvi_config = uvicorn.Config(
            app,
            host=WEBAPP_HOST,
            port=WEBAPP_PORT,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
        server = uvicorn.Server(uvi_config)
        server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_stop)

        web_task = asyncio.create_task(server.serve(), name="webapp")
        poll_task = asyncio.create_task(self._poll_loop(), name="poll")

        log.info("daemon up, ui at http://%s:%s/", WEBAPP_HOST, WEBAPP_PORT)
        try:
            await self._stop_flag.wait()
        finally:
            log.info("daemon shutting down")
            poll_task.cancel()
            if self.session is not None:
                await self.session.stop()
            server.should_exit = True
            await asyncio.gather(web_task, poll_task, return_exceptions=True)

    def _request_stop(self) -> None:
        if not self._stop_flag.is_set():
            log.info("stop signal received")
            self._stop_flag.set()

    # --- the poll loop ---

    async def _poll_loop(self) -> None:
        try:
            while not self._stop_flag.is_set():
                try:
                    await self._tick()
                except Exception:
                    log.exception("poll tick failed")
                try:
                    await asyncio.wait_for(
                        self._stop_flag.wait(), timeout=POLL_INTERVAL_S
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        # Pass the active key so platform implementations can broaden
        # detection for *this* meeting only — e.g. on macOS, accept the
        # Meet tab being open in any window once we're already recording
        # for that room. Idle ticks pass None and use only strict signals.
        window = detect.detect(active_key=self._session_key)

        if self.session is None:
            # Idle: look for a meeting to start.
            if window is None:
                return
            await self._start_for(window)
            return

        # Hard upper bound — protects against a wedged pactl source-output
        # reporting RUNNING after the call really ended. Applies whether
        # we're actively recording or just sitting paused.
        if self.session.started_dt is not None:
            elapsed = (now - self.session.started_dt).total_seconds()
            if elapsed >= MAX_RECORDING_S:
                log.warning(
                    "max recording duration %ds exceeded; finalizing",
                    MAX_RECORDING_S,
                )
                await self._finalize_current()
                return

        if window is not None:
            if window.key != self._session_key:
                # Different meeting started — finalize this one and pivot
                # immediately. Grace doesn't apply when the identity flipped.
                log.info(
                    "meeting key changed: %s → %s; rotating session",
                    self._session_key,
                    window.key,
                )
                await self._finalize_current()
                await self._start_for(window)
                return
            # Same meeting. If we were paused, resume; either way refresh
            # last-seen so the grace timer restarts.
            if self.session.is_paused:
                log.info("window returned for %s; resuming", window.key)
                await self.session.resume()
            self._last_match_at = now
            return

        # Window absent.
        last = self._last_match_at or now
        gap = (now - last).total_seconds()
        if not self.session.is_paused:
            if gap >= RECORDING_GRACE_S:
                log.info("window gone %ds; pausing session", RECORDING_GRACE_S)
                await self._pause_current()
        else:
            if gap >= RESUME_WINDOW_S:
                log.info(
                    "no resume after %ds; finalizing %s",
                    RESUME_WINDOW_S,
                    self._session_key,
                )
                await self._finalize_current()

    async def _start_for(self, window: detect.Detection) -> None:
        events = await asyncio.to_thread(events_active_now)
        event, trace = correlate(window.title, window.platform, events)
        slug = _build_slug(event, window.title)
        log.info(
            "detected %s via %s %r key=%s → slug=%s event=%s",
            window.platform,
            window.source,
            window.title,
            window.key,
            slug,
            event.summary if event else "(no calendar match)",
        )
        extra: dict[str, Any] = {
            "detection": {
                "platform": window.platform,
                "title": window.title,
                "source": window.source,
                "application_pid": window.application_pid,
                "application_name": window.application_name,
                "source_output_index": window.source_output_index,
                "key": window.key,
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "correlation": trace,
            }
        }
        if event is not None:
            extra["calendar_event"] = event.to_metadata()

        self.session = Session(slug, self.api_key, metadata_extra=extra)
        self.current_event = event
        self._session_key = window.key
        self._last_match_at = datetime.now(timezone.utc)
        try:
            await self.session.start()
        except Exception:
            log.exception("failed to start session")
            self.session = None
            self.current_event = None

    async def _pause_current(self) -> None:
        """Soft stop: ffmpeg + deepgram wind down, audio.opus is updated,
        pipeline is spawned. Session stays in self.session in the paused
        state so a return of the same key resumes into the same folder."""
        if self.session is None or self.session.is_paused:
            return
        folder = self.session.folder
        await self.session.pause()
        if folder is not None:
            _spawn_witness(folder)

    async def _finalize_current(self) -> None:
        """Terminal stop: closes the bus, marks session done, spawns the
        final pipeline run. After this, self.session is cleared."""
        if self.session is None:
            return
        folder = self.session.folder
        was_paused = self.session.is_paused
        await self.session.stop()
        self.session = None
        self.current_event = None
        self._session_key = None
        self._last_match_at = None
        # If we were already paused, the last pause already spawned the
        # pipeline against the same audio.opus. Skip the redundant run —
        # the flock would just queue it for no reason.
        if folder is not None and not was_paused:
            _spawn_witness(folder)


def _sweep_orphans(root: Path) -> None:
    """Finalize meeting folders that a previous daemon left in an in-progress
    state (started_at present, ended_at missing). Called once at daemon start.

    Concats any per-segment audio files into audio.opus, stamps ended_at +
    `recovered: true`, and spawns the pipeline. Folders without a started_at
    are presumed scratch and left alone — we don't try to be clever about
    detecting partial captures with no metadata.
    """
    if not root.exists():
        return
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    from . import record as _record
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        meta_path = folder / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = _json.loads(meta_path.read_text())
        except _json.JSONDecodeError:
            continue
        if not meta.get("started_at") or meta.get("ended_at"):
            continue
        log.info("recovering orphan meeting folder: %s", folder.name)
        seg_dir = folder / "audio"
        segments = sorted(seg_dir.glob("*.opus")) if seg_dir.is_dir() else []
        # Filter out empty segments (ffmpeg killed before writing anything).
        segments = [s for s in segments if s.stat().st_size > 0]
        out = folder / "audio.opus"
        if segments:
            try:
                _record.concat(segments, out)
            except Exception:
                log.exception("orphan concat failed for %s", folder.name)
        elif out.exists():
            # Pre-multi-segment recording — keep the file as-is.
            pass
        else:
            log.warning("orphan %s has no audio; finalizing without pipeline", folder.name)
        meta["ended_at"] = _dt.now(_tz.utc).isoformat()
        meta["recovered"] = True
        meta_path.write_text(_json.dumps(meta, indent=2))
        if out.exists():
            _spawn_witness(folder)


def _spawn_witness(folder: Path) -> None:
    """Kick off the post-meeting pipeline as a detached subprocess.

    Daemon continues polling; long-running summarize/fingerprint don't
    block the next recording. The pipeline writes its own logs into the
    meeting folder (witness.log) so failures are diagnosable after the fact.
    """
    import subprocess
    import sys
    logf = (folder / "witness.log").open("a")
    try:
        subprocess.Popen(
            [sys.executable, "-m", "witness", str(folder)],
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach from daemon's process group
            close_fds=True,
        )
        log.info("spawned witness for %s", folder.name)
    except Exception:
        log.exception("failed to spawn witness")
    finally:
        logf.close()


def main() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH)],
    )
    try:
        asyncio.run(Daemon().run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
