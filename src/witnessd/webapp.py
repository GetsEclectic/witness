"""FastAPI app: serves the live transcript UI + REST API for past meetings.

Built as a factory so it can be used standalone (browsing mode) or embedded
inside the recording daemon with an active EventBus subscription.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import MEETINGS_ROOT, VOICEPRINTS_DIR
from .transcript import EventBus

STATIC_DIR = Path(__file__).parent / "static"

# Names allowed in /api/unknowns/{hash}/bind. Mirrors witness.identify._NAME_RE
# so any string accepted there is accepted here. Drop bytes-y characters and
# control codes so we never write something like "../../etc" into a path.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'\-]{0,40}$")
# Pad before/after the longest utterance when extracting an audio clip. Gives
# the listener enough context to pick up cadence and accent.
_CLIP_PAD_S = 0.5
_CLIP_MAX_S = 20.0
_SAMPLES_PER_UNKNOWN = 3


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

    @app.get("/api/unknowns")
    async def list_unknowns() -> list[dict[str, Any]]:
        return _aggregate_unknowns(meetings_root)

    @app.get("/api/unknowns/{hash_id}/clip.mp3")
    async def get_unknown_clip(hash_id: str) -> Response:
        if not re.fullmatch(r"[0-9a-f]+", hash_id):
            raise HTTPException(400, "bad hash")
        target = _longest_span_for_unknown(meetings_root, hash_id)
        if target is None:
            raise HTTPException(404, "no audio span found")
        folder, spk_id, ts_start, ts_end = target
        channel = _channel_for_speaker_id(spk_id)
        audio_path = folder / "audio.opus"
        if not audio_path.exists():
            raise HTTPException(404, "audio.opus missing")
        # Pad slightly, cap to _CLIP_MAX_S so we don't stream a long monologue
        # (encoder-side cost + faster page UX). ffmpeg's -ss/-to are decode
        # timestamps; -ar resamples; pan picks one input channel.
        start = max(0.0, ts_start - _CLIP_PAD_S)
        end = min(start + _CLIP_MAX_S, ts_end + _CLIP_PAD_S)
        import subprocess
        from ._platform import ffmpeg_path
        proc = subprocess.run(
            [
                ffmpeg_path(), "-v", "error",
                "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                "-i", str(audio_path),
                "-af", f"pan=mono|c0=c{channel}",
                "-ac", "1", "-ar", "22050",
                "-c:a", "libmp3lame", "-q:a", "5",
                "-f", "mp3", "-",
            ],
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise HTTPException(500, f"ffmpeg failed: {proc.stderr.decode()[:200]}")
        return Response(
            content=proc.stdout,
            media_type="audio/mpeg",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/unknowns/{hash_id}/bind")
    async def bind_unknown(hash_id: str, body: dict[str, Any]) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f]+", hash_id):
            raise HTTPException(400, "bad hash")
        name = (body.get("name") or "").strip()
        if not _NAME_RE.match(name):
            raise HTTPException(400, "bad name")
        label = f"unknown_{hash_id}"

        # Lazy-import witness.* — fingerprint pulls torch via _decode_channel
        # imports etc.; render is light. Both live in the optional `witness`
        # package (vs. always-present `witnessd`).
        from witness import fingerprint, render

        slug_name = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower() or "adhoc"
        updated: list[str] = []
        first_speaker_id: str | None = None

        for folder in meetings_root.iterdir():
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            sp_path = folder / "speakers.json"
            if not sp_path.exists():
                continue
            try:
                sp = json.loads(sp_path.read_text())
            except json.JSONDecodeError:
                continue
            if not isinstance(sp, dict):
                continue
            # Rewrite every speaker_id whose alias chain passes through this
            # unknown to the bound name. "Passes through" rather than
            # "terminates at" so we also catch the case where identify.py
            # already wrote a wrong-LLM-guess (`unknown_<hash> → "Tony"`) on
            # top of the unknown — that whole chain points at the same audio
            # cluster, so it should rewrite to the correct name.
            changed = False
            for spk_id in list(sp.keys()):
                if not spk_id.startswith(
                    ("system_speaker_", "mic_speaker_", "speaker_")
                ):
                    continue
                if label not in _alias_chain(spk_id, sp):
                    continue
                if first_speaker_id is None:
                    first_speaker_id = spk_id
                sp[spk_id] = name
                changed = True
            if changed:
                # Drop the orphan `unknown_<hash>: <llm_guess>` entry — nothing
                # else should reference it after the rewrite.
                sp.pop(label, None)
                sp_path.write_text(json.dumps(sp, indent=2, sort_keys=True) + "\n")
                # /api/meetings cache reads mtime on the meetings dir, but
                # speakers.json edits don't bump that — invalidate manually.
                list_cache["mtime"] = None
                try:
                    render.render(folder)
                except Exception:
                    pass
                updated.append(folder.name)

        if not updated:
            raise HTTPException(404, "no meetings reference this unknown")

        # Promote the embedding now that all meetings are pointing at the new
        # name. If somebody else already had a voiceprint for this name, the
        # promotion stacks rows onto theirs.
        promoted = fingerprint.promote_voiceprint(
            label,
            slug_name,
            source_slug=updated[0],
            speaker_id=first_speaker_id,
        )
        return {
            "name": name,
            "voiceprint": promoted.name if promoted else None,
            "updated_meetings": updated,
        }

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


# --- unknown-speaker aggregation --------------------------------------------

def _alias_terminal(label: str, resolved: dict[str, str], depth: int = 8) -> str:
    """Walk the speakers.json alias chain to its terminus."""
    seen: set[str] = set()
    cur = label
    while cur in resolved and cur not in seen and depth > 0:
        seen.add(cur)
        cur = resolved[cur]
        depth -= 1
    return cur


def _alias_chain(label: str, resolved: dict[str, str], depth: int = 8) -> list[str]:
    """Return the full alias chain starting at `label`, [label, ..., terminal]."""
    seen: set[str] = set()
    chain = [label]
    cur = label
    while cur in resolved and cur not in seen and depth > 0:
        seen.add(cur)
        cur = resolved[cur]
        chain.append(cur)
        depth -= 1
    return chain


def _spans_for_speaker(folder: Path, speaker_id: str) -> list[tuple[float, float, str]]:
    """Return (ts_start, ts_end, text) for each final utterance by speaker_id."""
    out: list[tuple[float, float, str]] = []
    p = folder / "transcript.jsonl"
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not evt.get("is_final") or evt.get("speaker") != speaker_id:
            continue
        s, e, t = evt.get("ts_start"), evt.get("ts_end"), (evt.get("text") or "").strip()
        if s is None or e is None or not t:
            continue
        out.append((float(s), float(e), t))
    return out


def _candidate_names(meetings_root: Path, primary_folder: Path) -> list[str]:
    """Suggested bind targets: calendar invitees of the primary meeting (using
    email-derived first names) plus any already-bound voiceprint names."""
    seen: set[str] = set()
    names: list[str] = []
    meta_path = primary_folder / "metadata.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            meta = {}
        cal = meta.get("calendar_event") or {}
        self_email = cal.get("self_email")
        for email in cal.get("attendees") or []:
            if email == self_email:
                continue
            local = email.split("@", 1)[0]
            first = re.split(r"[._-]", local, 1)[0].capitalize()
            if first and first not in seen:
                seen.add(first)
                names.append(first)
    if VOICEPRINTS_DIR.exists():
        for npy in VOICEPRINTS_DIR.glob("*.npy"):
            if npy.stem.startswith("unknown_"):
                continue
            cap = npy.stem.replace("-", " ").title()
            if cap and cap not in seen:
                seen.add(cap)
                names.append(cap)
    names.sort()
    return names


def _aggregate_unknowns(meetings_root: Path) -> list[dict[str, Any]]:
    """Build the data the /unknowns page consumes.

    For every `unknown_<hash>.npy` voiceprint, find every meeting whose
    speakers.json alias-chain terminates at it. Pick the meeting with the
    most speaking time as "primary" — that's the source we play audio from
    and pull samples from. Voiceprints with no matching meeting (orphans
    from a deleted meeting) are dropped.
    """
    if not VOICEPRINTS_DIR.exists():
        return []
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for npy in VOICEPRINTS_DIR.glob("unknown_*.npy"):
        by_hash[npy.stem.removeprefix("unknown_")] = []
    if not by_hash:
        return []

    for folder in sorted(meetings_root.iterdir(), reverse=True):
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        sp_path = folder / "speakers.json"
        if not sp_path.exists():
            continue
        try:
            sp = json.loads(sp_path.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(sp, dict):
            continue
        for spk_id in [
            k for k in sp
            if k.startswith(("system_speaker_", "mic_speaker_", "speaker_"))
        ]:
            chain = _alias_chain(spk_id, sp)
            # An unknown_<hash> that has a follow-on label (e.g.
            # `unknown_ee84d4 → "Tony"` from a wrong identify-step LLM guess)
            # still indicates an unbound voiceprint as long as the .npy file
            # exists — surface it for correction.
            unknowns_in_chain = [c for c in chain if c.startswith("unknown_")]
            for unk in unknowns_in_chain:
                h = unk.removeprefix("unknown_")
                if h not in by_hash:
                    continue
                spans = _spans_for_speaker(folder, spk_id)
                if not spans:
                    continue
                seconds = sum(e - s for s, e, _ in spans)
                samples = [t for _, _, t in sorted(spans, key=lambda x: -(x[1] - x[0]))[:_SAMPLES_PER_UNKNOWN]]
                # Display name is the chain's terminus — useful so the user
                # sees the wrong-LLM-guess (if any) right next to the samples.
                terminal = chain[-1]
                current_label = terminal if not terminal.startswith("unknown_") else None
                by_hash[h].append({
                    "slug": folder.name,
                    "speaker_id": spk_id,
                    "seconds": seconds,
                    "samples": samples,
                    "current_label": current_label,
                })
                break  # only first unknown in chain — same audio cluster

    out: list[dict[str, Any]] = []
    for h, apps in by_hash.items():
        if not apps:
            continue
        apps.sort(key=lambda a: -a["seconds"])
        primary = apps[0]
        primary_folder = meetings_root / primary["slug"]
        primary_meta: dict[str, Any] = {}
        meta_path = primary_folder / "metadata.json"
        if meta_path.exists():
            try:
                primary_meta = json.loads(meta_path.read_text())
            except json.JSONDecodeError:
                pass
        out.append({
            "hash": h,
            "total_seconds": sum(a["seconds"] for a in apps),
            "n_meetings": len(apps),
            "current_label": primary.get("current_label"),
            "primary": {
                "slug": primary["slug"],
                "title": _extract_title(primary["slug"], primary_meta, ""),
                "started_at": primary_meta.get("started_at"),
                "speaker_id": primary["speaker_id"],
                "samples": primary["samples"],
            },
            "candidates": _candidate_names(meetings_root, primary_folder),
        })
    out.sort(key=lambda r: -r["total_seconds"])
    return out


def _longest_span_for_unknown(meetings_root: Path, unknown_hash: str) -> tuple[Path, str, float, float] | None:
    """Find the longest single utterance for unknown_<hash> across all
    meetings. Returns (folder, speaker_id, ts_start, ts_end) or None."""
    best: tuple[Path, str, float, float] | None = None
    best_dur = 0.0
    label = f"unknown_{unknown_hash}"
    for folder in meetings_root.iterdir():
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        sp_path = folder / "speakers.json"
        if not sp_path.exists():
            continue
        try:
            sp = json.loads(sp_path.read_text())
        except json.JSONDecodeError:
            continue
        if not isinstance(sp, dict):
            continue
        for spk_id in [
            k for k in sp
            if k.startswith(("system_speaker_", "mic_speaker_", "speaker_"))
        ]:
            if label not in _alias_chain(spk_id, sp):
                continue
            for s, e, _ in _spans_for_speaker(folder, spk_id):
                if e - s > best_dur:
                    best_dur = e - s
                    best = (folder, spk_id, s, e)
    return best


def _channel_for_speaker_id(speaker_id: str) -> int:
    if speaker_id.startswith("mic_speaker_"):
        return 0
    return 1  # system_speaker_, speaker_, or anything else routed to system
