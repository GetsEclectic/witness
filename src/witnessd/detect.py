"""Detect whether the user is currently in a video call.

Signal: `pactl list source-outputs` — every app with the microphone
*actively open* shows up as a source-output. This works regardless of
which tab is foregrounded or which virtual desktop the call is on
(window titles only reflect the active tab, which the user may tab
away from during group calls — that's why window-title detection is
unsafe as a primary signal).

The detection is **mute-aware**: we require the source-output to be
in the RUNNING state. When the user mutes in Meet/Zoom/Teams, the
stream goes to CORKED/SUSPENDED, we return None, and the session
stops after the grace window — nothing is recorded while muted.

Identification:
  * Google Meet:   media.name starts with "Meet -" (Chrome/Firefox).
  * Zoom desktop:  application.binary matches "zoom".
  * Zoom web:      media.name contains "Zoom Meeting".
  * Teams desktop: application.name/binary matches "teams".
  * Teams web:     media.name contains "Microsoft Teams".
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


_MEET_TITLE = re.compile(r"^Meet\s*[-–—]\s*(.+)$")


@dataclass
class Detection:
    platform: str            # "meet" | "zoom" | "teams" | "unknown"
    title: str               # human-readable name (e.g. "Meet - abc-defg")
    source: str              # "pactl" | "window"
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


# --- pactl parsing ---

_BLOCK_HEADER = re.compile(r"^(?:Source Output|Sink Input) #(\d+)")
_KV = re.compile(r"^\s+([\w.\-]+)\s*=\s*\"(.*)\"\s*$")
# PipeWire's pactl emits `Corked: no/yes` and `Mute: no/yes` instead of the
# classic PulseAudio `State:` field. Both styles are handled.
_TOP_LEVEL = re.compile(r"^\s*(Corked|Mute|State):\s*(\S+)")


def _parse_pactl_blocks(output: str) -> list[dict[str, str]]:
    """Parse `pactl list source-outputs|sink-inputs` into a list of
    property dicts, one per block. Top-level fields like Corked/Mute/State
    are stored under keys prefixed with `__` to keep the namespace clean.
    `__index` holds the `Source Output #N` / `Sink Input #N` number."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in output.splitlines():
        if (m := _BLOCK_HEADER.match(line)):
            if current is not None:
                blocks.append(current)
            current = {"__index": m.group(1)}
            continue
        if current is None:
            continue
        if (m := _TOP_LEVEL.match(line)):
            current[f"__{m.group(1).lower()}"] = m.group(2)
            continue
        if (m := _KV.match(line)):
            current[m.group(1)] = m.group(2)
    if current is not None:
        blocks.append(current)
    return blocks


def _is_live(block: dict[str, str]) -> bool:
    """True iff this source-output is actively capturing (not muted/corked)."""
    if block.get("__mute") == "yes":
        return False
    if block.get("__corked") == "yes":
        return False
    # Classic PulseAudio path.
    state = block.get("__state")
    if state is not None and state != "RUNNING":
        return False
    return True


def _classify(block: dict[str, str]) -> tuple[str, str] | None:
    """Return (platform, display_title) if the block looks like an
    active call, else None."""
    media = block.get("media.name", "")
    app = block.get("application.name", "")
    binary = block.get("application.process.binary", "")

    if media.startswith("Meet -") or media.startswith("Meet –") or media.startswith("Meet —"):
        return "meet", media
    if "Zoom" in media or "zoom" in binary.lower() or app == "ZOOM VoiceEngine":
        title = media if media else "Zoom Meeting"
        return "zoom", title
    if "Microsoft Teams" in media or "teams" in binary.lower() or "Teams" in app:
        title = media if media else "Microsoft Teams"
        return "teams", title
    return None


def _pactl_detection() -> Detection | None:
    """Inspect who's holding the mic right now, via pactl."""
    try:
        out = subprocess.check_output(
            ["pactl", "list", "source-outputs"],
            text=True,
            timeout=3,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    for block in _parse_pactl_blocks(out):
        if not _is_live(block):
            continue
        hit = _classify(block)
        if hit is None:
            continue
        platform, title = hit
        pid_s = block.get("application.process.id")
        pid = int(pid_s) if pid_s and pid_s.isdigit() else None
        idx_s = block.get("__index")
        idx = int(idx_s) if idx_s and idx_s.isdigit() else None
        return Detection(
            platform=platform,
            title=title,
            source="pactl",
            application_pid=pid,
            application_name=block.get("application.name"),
            source_output_index=idx,
        )
    return None


def detect() -> Detection | None:
    """Scan the system once. Return a Detection if a call is in progress."""
    return _pactl_detection()
