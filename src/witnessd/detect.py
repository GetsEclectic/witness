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


_MEET_TITLE = re.compile(r"^Meet\s*[-–—]\s*(.+)$")


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

    @property
    def key(self) -> str:
        """Stable identity for the active call. Two detections sharing a
        key are the same meeting; a key change is the daemon's signal to
        rotate the session (see Daemon._tick)."""
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
