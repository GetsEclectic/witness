"""FastAPI app: browse past meetings, and see what's recording right now.

Built as a factory so it can be used standalone (browsing mode) or embedded
inside the recording daemon, which supplies a live `status` provider.

There is no live transcript feed: transcription runs in the post-meeting
pipeline, so a meeting's transcript appears once that pipeline has run —
after every pause, and at the terminal stop.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .config import MEETINGS_ROOT

STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class RecordingStatus:
    active: bool
    slug: str | None
    started_at: str | None


StatusProvider = Callable[[], RecordingStatus]


def build_app(
    status: StatusProvider | None = None,
    meetings_root: Path = MEETINGS_ROOT,
) -> FastAPI:
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
            return {"active": False, "slug": None, "started_at": None}
        s = status()
        return {"active": s.active, "slug": s.slug, "started_at": s.started_at}

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
