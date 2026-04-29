"""Google Calendar access via the `gws` CLI.

The daemon queries the calendar live on every meeting-detection event — no
caching (live systems are source of truth). `gws` handles OAuth, token
refresh, and encrypted storage.

Multi-account support: `events_active_now()` queries each config dir in
`GWS_CONFIG_DIRS` (one per Google account), unions the results, and
dedupes events that appear on more than one account (same id+start).
Each event records which `gws_account` it came from for debug visibility.

All functions degrade gracefully: missing / unauthenticated / offline
accounts contribute zero events; window-title detection still works.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import GWS_BIN, GWS_CONFIG_DIRS

log = logging.getLogger("witnessd.calendar")

# Conference-link patterns in event descriptions / locations / hangoutLink.
_MEET_RE = re.compile(r"https://meet\.google\.com/[a-z0-9\-]+", re.I)
_ZOOM_RE = re.compile(r"https://[\w.\-]*zoom\.us/j/\d+[^\s]*", re.I)
_TEAMS_RE = re.compile(r"https://teams\.microsoft\.com/[^\s]+", re.I)


@dataclass
class CalendarEvent:
    id: str
    summary: str
    start: datetime
    end: datetime
    attendees: list[str]
    platform: str | None  # "meet" | "zoom" | "teams" | None
    conference_url: str | None
    raw: dict[str, Any]
    gws_account: str = ""  # config dir basename (e.g. "gws-personal") for debug

    def to_metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "attendees": self.attendees,
            "platform": self.platform,
            "conference_url": self.conference_url,
            "gws_account": self.gws_account,
        }


def _parse_event(raw: dict[str, Any]) -> CalendarEvent | None:
    start_s = (raw.get("start") or {}).get("dateTime")
    end_s = (raw.get("end") or {}).get("dateTime")
    if not start_s or not end_s:
        return None  # all-day / date-only; not a joinable meeting
    try:
        start = datetime.fromisoformat(start_s)
        end = datetime.fromisoformat(end_s)
    except ValueError:
        return None

    haystack = " ".join(
        [
            raw.get("hangoutLink", "") or "",
            raw.get("location", "") or "",
            raw.get("description", "") or "",
        ]
    )
    platform = None
    url = None
    if m := _MEET_RE.search(haystack):
        platform, url = "meet", m.group(0)
    elif m := _ZOOM_RE.search(haystack):
        platform, url = "zoom", m.group(0)
    elif m := _TEAMS_RE.search(haystack):
        platform, url = "teams", m.group(0)

    attendees = [
        a.get("email", "")
        for a in (raw.get("attendees") or [])
        if a.get("email")
    ]

    return CalendarEvent(
        id=raw.get("id", ""),
        summary=raw.get("summary", "(no title)"),
        start=start,
        end=end,
        attendees=attendees,
        platform=platform,
        conference_url=url,
        raw=raw,
    )


def _query_one(config_dir: Path, params: str, timeout_s: int = 10) -> list[dict[str, Any]]:
    """Run `gws calendar events list` against one account; return raw items."""
    env = os.environ.copy()
    env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(config_dir)
    try:
        out = subprocess.run(
            [GWS_BIN, "calendar", "events", "list", "--params", params],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("gws query failed for %s: %s", config_dir, e)
        return []
    if out.returncode != 0:
        log.warning("gws non-zero exit for %s: %s", config_dir, out.stderr.strip())
        return []
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    return payload.get("items", []) or []


def events_active_now(window_min: int = 5) -> list[CalendarEvent]:
    """Events whose [start, end] interval overlaps `now ± window_min minutes`,
    unioned across every configured gws account.

    Dedupe rule: same `id` + same `start` → keep the first occurrence; later
    accounts that report the same event are dropped (the same Google Calendar
    event ID is shared across attendee accounts). Empty list on total failure.
    """
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(minutes=window_min)).isoformat()
    time_max = (now + timedelta(minutes=window_min)).isoformat()
    params = json.dumps(
        {
            "calendarId": "primary",
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 20,
        }
    )
    seen: set[tuple[str, str]] = set()
    events: list[CalendarEvent] = []
    for config_dir in GWS_CONFIG_DIRS:
        account = config_dir.name
        for raw in _query_one(config_dir, params):
            if raw.get("status") == "cancelled":
                continue
            evt = _parse_event(raw)
            if not evt:
                continue
            if not (evt.start <= now + timedelta(minutes=window_min) and evt.end >= now - timedelta(minutes=window_min)):
                continue
            key = (evt.id, evt.start.isoformat())
            if key in seen:
                continue
            seen.add(key)
            evt.gws_account = account
            events.append(evt)
    events.sort(key=lambda e: e.start)
    return events


def correlate(
    window_title: str,
    platform: str,
    events: list[CalendarEvent],
) -> tuple[CalendarEvent | None, dict[str, Any]]:
    """Pick the event best matching the active window.

    Returns (event_or_None, trace_dict) — the trace is logged to metadata.json
    so we can debug misattribution after the fact.

    Scoring:
      +10 platform match (event's conference URL matches detected platform)
      +5  any word from event summary appears in window title (substring)
      +2  summary word count overlaps window title
      +1  event is happening *right now* (not in ± window)
    Ties → earliest-starting event.
    """
    now = datetime.now(timezone.utc)
    scored: list[tuple[int, CalendarEvent, dict[str, Any]]] = []
    title_lc = window_title.lower()
    for evt in events:
        score = 0
        reasons: list[str] = []
        if evt.platform == platform:
            score += 10
            reasons.append(f"platform={platform}")
        summary_words = [w for w in re.findall(r"\w+", evt.summary.lower()) if len(w) > 2]
        if any(w in title_lc for w in summary_words):
            score += 5
            reasons.append("summary-word-in-title")
        if evt.start <= now <= evt.end:
            score += 1
            reasons.append("happening-now")
        scored.append((score, evt, {"event_id": evt.id, "summary": evt.summary, "gws_account": evt.gws_account, "score": score, "reasons": reasons}))

    scored.sort(key=lambda t: (-t[0], t[1].start))
    trace = {
        "window_title": window_title,
        "platform": platform,
        "candidates": [t[2] for t in scored],
    }
    if not scored or scored[0][0] == 0:
        return None, trace
    return scored[0][1], trace
