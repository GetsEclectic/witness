"""Detect whether the user is currently in a video call.

This module exposes the shared `Detection` dataclass and the cross-platform
`detect()` dispatcher. Per-OS implementations live in `_platform_linux.py`
(pactl-based, the original strategy) and `_platform_darwin.py` (CoreAudio
mic-active probe + NSWorkspace + osascript).

The pactl parser/classifier helpers (`_parse_pactl_blocks`, `_is_live`,
`_classify`) are re-exported from `_platform_linux` here so existing tests
that import them from `witnessd.detect` keep working without modification.
They are pure functions and import cleanly on any platform.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote


_MEET_TITLE = re.compile(r"^Meet\s*[-–—]\s*(.+)$")

# Teams names a call two different ways and a single invite carries both:
#
#   short:  https://teams.microsoft.com/meet/383440701254366?p=<passcode>
#   thread: https://teams.microsoft.com/l/meetup-join/
#               19%3ameeting_<opaque>%40thread.v2/0?context=...
#
# Which one the browser ends up showing depends on which link was clicked and
# on the Teams release, so neither alone is enough to tie a detected tab back
# to its invite — we collect every id an invite mentions and match on any.
_TEAMS_HOSTS = r"teams\.(?:microsoft\.com|microsoft\.us|live\.com)"
TEAMS_THREAD_ID_RE = re.compile(r"19:meeting_[A-Za-z0-9_\-=]+@thread\.v2", re.I)
TEAMS_SHORT_ID_RE = re.compile(rf"{_TEAMS_HOSTS}/meet/(\d+)", re.I)

# Requiring a Teams host keeps a thread id quoted in some unrelated page (a
# doc, an email in a webmail tab) from being read as "the user is in this call".
_TEAMS_HOST_RE = re.compile(rf"https://{_TEAMS_HOSTS}/", re.I)


def teams_id_kind(conference_id: str) -> str:
    """Which of the two Teams id spaces `conference_id` belongs to.

    Correlation only treats a difference as a *contradiction* within one id
    space — a short id and a thread id are simply not comparable, and reading
    that as "different meetings" would disqualify the correct event.
    """
    return "thread" if TEAMS_THREAD_ID_RE.fullmatch(conference_id) else "short"


def teams_conference_ids(text: str) -> frozenset[str]:
    """Every Teams call id mentioned in `text` (a URL, or a whole invite body).

    Lowercased and percent-decoded so the browser-tab side and the
    calendar-invite side compare equal regardless of how each encoded it.
    """
    if not text:
        return frozenset()
    decoded = unquote(text)
    ids = {m.group(0).lower() for m in TEAMS_THREAD_ID_RE.finditer(decoded)}
    ids |= {m.group(1) for m in TEAMS_SHORT_ID_RE.finditer(decoded)}
    return frozenset(ids)


def teams_conference_ids_from_tab(url: str) -> frozenset[str]:
    """Same, for a browser tab URL: the tab must be on a Teams host and must
    not be the meeting's *chat* view.

    The chat view (`.../#/conversations/19:meeting_...@thread.v2`) carries the
    same thread id as the call itself and tends to be left open long after the
    meeting ends — treating it as "in this call" would start a recording every
    time some other app opened the mic. The in-call URL shape has changed
    across Teams releases (`/meet/`, `meetup-join`, `pre-join-calling`), so we
    exclude the one known-stale view rather than enumerate the live ones.
    """
    if not url or not _TEAMS_HOST_RE.search(url):
        return frozenset()
    if "/conversations/" in unquote(url).lower():
        return frozenset()
    return teams_conference_ids(url)


class ProbeFailed(Exception):
    """The OS probe couldn't determine current meeting state.

    Distinguishes "I have evidence no meeting is active" (None) from "I
    couldn't get evidence either way" (this exception). On macOS the
    osascript / audiotap subprocesses can stall under load and time out;
    treating the timeout as a clean None advances the daemon's window-gone
    timer and produces spurious pauses. Callers should preserve their
    last-known state instead of letting the gap accumulate.
    """


@dataclass
class Detection:
    platform: str            # "meet" | "zoom" | "teams" | "unknown"
    title: str               # human-readable name (e.g. "Meet - abc-defg")
    source: str              # "pactl" | "coreaudio" | "window"
    application_pid: int | None = None
    application_name: str | None = None
    source_output_index: int | None = None
    # The call's own ID, when the probe could read one off a join URL (Meet
    # room code, Teams thread id). Gives the session a key that survives a
    # tab reload or a window-title change, and lets `calendar.correlate`
    # match against the invite instead of guessing from the title.
    conference_id: str | None = None

    @property
    def key(self) -> str:
        """Stable identity for the active call. Two detections sharing a
        key are the same meeting; a key change is the daemon's signal to
        rotate the session (see Daemon._tick)."""
        if self.conference_id:
            return f"{self.platform}:{self.conference_id}"
        if self.platform == "meet":
            m = _MEET_TITLE.match(self.title)
            if m:
                return f"meet:{m.group(1).strip()}"
        return (
            f"{self.platform}:{self.title}:"
            f"{self.application_pid}:{self.source_output_index}"
        )


# Re-exports of pactl helpers for existing tests in tests/test_detect.py.
# Pure functions; importable on any platform.
from ._platform_linux import (  # noqa: E402
    _classify,
    _is_live,
    _parse_pactl_blocks,
)


def detect(active_key: str | None = None) -> Detection | None:
    """Scan the system once. Return a Detection if a call is in progress.

    `active_key` is the daemon's currently-recording session key (or None
    when idle). Platform implementations may use it to broaden detection
    for the in-progress meeting only — e.g. on macOS, accept "the Meet
    tab for *this* room is still open even though the front tab changed."
    Without it, only strict standalone signals fire.

    Raises `ProbeFailed` when the platform probe was inconclusive (e.g.
    osascript timeout) — callers should preserve their last-known state
    rather than treat it as "no detection."
    """
    from ._platform import get_platform
    return get_platform().detect_meeting(active_key=active_key)
