"""FastAPI app: serves the live transcript UI + REST API for past meetings.

Built as a factory so it can be used standalone (browsing mode) or embedded
inside the recording daemon with an active EventBus subscription.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import MEETINGS_ROOT
from .transcript import EventBus

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class RecordingStatus:
    active: bool
    slug: str | None
    started_at: str | None
    transcription_failed: bool = False


StatusProvider = Callable[[], RecordingStatus]
BusProvider = Callable[[], EventBus | None]


def build_app(
    bus: EventBus | BusProvider | None,
    status: StatusProvider | None = None,
    meetings_root: Path = MEETINGS_ROOT,
) -> FastAPI:
    # Accept either a bare EventBus (single-recording mode, as used by
    # `witness record-now`) or a provider callable (daemon mode, where the
    # current bus changes between meetings).
    if isinstance(bus, EventBus) or bus is None:
        _const: EventBus | None = bus
        bus_provider: BusProvider = lambda: _const
    else:
        bus_provider = bus
    app = FastAPI(title="witness")

    # /api/meetings caches its response keyed on meetings_root's mtime.
    # Listing every folder + reading each summary.md is O(n) disk I/O; without
    # a cache, every page load hits it again. The directory mtime bumps when
    # new meeting folders are created (post-pipeline) so the cache invalidates
    # naturally at the right moments.
    list_cache: dict[str, Any] = {"mtime": None, "value": None}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text())

    @app.get("/api/status")
    async def get_status() -> dict[str, Any]:
        if status is None:
            return {
                "active": False,
                "slug": None,
                "started_at": None,
                "transcription_failed": False,
            }
        s = status()
        return {
            "active": s.active,
            "slug": s.slug,
            "started_at": s.started_at,
            "transcription_failed": s.transcription_failed,
        }

    @app.get("/api/meetings")
    async def list_meetings() -> list[dict[str, Any]]:
        if not meetings_root.exists():
            return []
        try:
            mtime = meetings_root.stat().st_mtime
        except OSError:
            mtime = None
        if list_cache["mtime"] == mtime and list_cache["value"] is not None:
            return list_cache["value"]
        out = []
        for folder in sorted(meetings_root.iterdir(), reverse=True):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            out.append(_meeting_summary(folder))
        list_cache["mtime"] = mtime
        list_cache["value"] = out
        return out

    @app.get("/api/meetings/{slug}")
    async def get_meeting(slug: str) -> dict[str, Any]:
        folder = _resolve_folder(meetings_root, slug)
        return _meeting_summary(folder)

    @app.get("/api/meetings/{slug}/transcript")
    async def get_transcript(slug: str) -> list[dict[str, Any]]:
        folder = _resolve_folder(meetings_root, slug)
        path = folder / "transcript.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    @app.get("/api/meetings/{slug}/metadata")
    async def get_meta(slug: str) -> dict[str, Any]:
        folder = _resolve_folder(meetings_root, slug)
        path = folder / "metadata.json"
        if not path.exists():
            raise HTTPException(404)
        return json.loads(path.read_text())

    @app.get("/api/meetings/{slug}/summary")
    async def get_summary(slug: str) -> dict[str, str]:
        folder = _resolve_folder(meetings_root, slug)
        path = folder / "summary.md"
        if not path.exists():
            raise HTTPException(404)
        return {"markdown": path.read_text()}

    @app.get("/api/meetings/{slug}/audio")
    async def get_audio(slug: str) -> FileResponse:
        folder = _resolve_folder(meetings_root, slug)
        path = folder / "audio.opus"
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(path, media_type="audio/ogg")

    @app.websocket("/ws")
    async def ws_live(ws: WebSocket) -> None:
        await ws.accept()
        current_bus = bus_provider()
        if current_bus is None:
            await ws.send_text(json.dumps({"type": "no_bus"}))
            await ws.close()
            return

        # Subscribe first so we don't miss events between backlog-send and
        # live-stream. Then flush the on-disk backlog (already-final
        # utterances from earlier in this meeting — so a mid-meeting browser
        # refresh doesn't show a blank pane).
        queue = current_bus.subscribe()
        # EventBus.emit writes to disk *then* puts on queues, so an event that
        # arrives during the backlog read can land in both the file and the
        # queue. Track received_at strings from the backlog and drop the next
        # few queue events that match — that's the only window where collision
        # is possible. (received_at is microsecond-precision UTC ISO.)
        seen_received_at: set[str] = set()
        try:
            if status is not None:
                s = status()
                if s.active and s.slug:
                    backlog_path = meetings_root / s.slug / "transcript.jsonl"
                    if backlog_path.exists():
                        for line in backlog_path.read_text().splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                evt = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if (ra := evt.get("received_at")):
                                seen_received_at.add(ra)
                            await ws.send_text(
                                json.dumps({"type": "event", **evt})
                            )
            await ws.send_text(json.dumps({"type": "live"}))

            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    await ws.send_text(json.dumps({"type": "ping"}))
                    continue
                if payload is None:
                    # Bus closed (session ended). Tell the client and drop;
                    # the browser's auto-reconnect will rebind to whatever
                    # is current.
                    await ws.send_text(json.dumps({"type": "session_end"}))
                    await ws.close()
                    return
                if seen_received_at:
                    ra = payload.get("received_at")
                    if ra and ra in seen_received_at:
                        seen_received_at.discard(ra)
                        continue
                await ws.send_text(json.dumps({"type": "event", **payload}))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            current_bus.unsubscribe(queue)

    return app


def _meeting_summary(folder: Path) -> dict[str, Any]:
    """Build the same dict shape `/api/meetings` and `/api/meetings/{slug}` return."""
    meta_path = folder / "metadata.json"
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            pass
    summary_path = folder / "summary.md"
    summary_text = summary_path.read_text() if summary_path.exists() else ""
    return {
        "slug": folder.name,
        "title": _extract_title(folder.name, meta, summary_text),
        "tldr": _extract_tldr(summary_text),
        "started_at": meta.get("started_at"),
        "ended_at": meta.get("ended_at"),
        "duration_minutes": _duration_minutes(
            meta.get("started_at"), meta.get("ended_at")
        ),
        "has_summary": summary_path.exists(),
        "has_audio": (folder / "audio.opus").exists(),
    }


def _extract_title(slug: str, meta: dict[str, Any], summary_text: str) -> str:
    if cal := meta.get("calendar_event", {}):
        if summary := cal.get("summary", ""):
            return summary
    for line in summary_text.splitlines():
        if line.startswith("# "):
            candidate = line[2:].strip()
            if candidate != slug:
                return candidate
    return slug


def _extract_tldr(summary_text: str) -> str | None:
    in_tldr = False
    lines: list[str] = []
    for line in summary_text.splitlines():
        if line.startswith("## TL;DR"):
            in_tldr = True
            continue
        if in_tldr:
            if line.startswith("##"):
                break
            if line.strip():
                lines.append(line.strip())
            elif lines:
                break
    return " ".join(lines) if lines else None


def _duration_minutes(started_at: str | None, ended_at: str | None) -> int | None:
    if not started_at or not ended_at:
        return None
    from datetime import datetime, timezone
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
        return max(0, round((end - start).total_seconds() / 60))
    except ValueError:
        return None


def _resolve_folder(root: Path, slug: str) -> Path:
    """Resolve a meeting folder, blocking any path that escapes `root`.

    `Path.resolve` collapses `..`, symlinks, and redundant separators, so we
    can do a single is_relative_to check after instead of stringly-banning
    `/`, `..`, etc. (which misses platform-specific tricks like Windows
    backslashes or symlink-out attacks)."""
    folder = (root / slug).resolve()
    root_resolved = root.resolve()
    if not folder.is_relative_to(root_resolved):
        raise HTTPException(400, "bad slug")
    if folder == root_resolved or not folder.is_dir():
        raise HTTPException(404)
    return folder
