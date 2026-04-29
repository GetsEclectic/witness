"""macOS capture + detection.

Detection: combine two signals to mirror the strictness Linux's pactl
source-outputs check enforces ("someone has the mic open *right now*"):
  1. Mic active — the bundled `witness-audiotap --probe-mic-running` exits
     0 when the default input device's IsRunningSomewhere is true. Cheap
     subprocess (~10ms).
  2. Meeting app present — NSWorkspace.runningApplications for Zoom/Teams,
     osascript for the front Chrome/Safari tab to spot a Meet URL.

Both must be true to fire. Without (1) we'd fire whenever Zoom is merely
launched; without (2) we'd fire on any random app holding the mic.

Capture: ffmpeg avfoundation reads the default input device for the mic
channel; the system channel comes from `mac/witness-audiotap`, a Swift
binary that creates a CoreAudio Process Tap (system-wide, excluding our
own PID) and pipes interleaved Float32 PCM to ffmpeg via an inherited fd.
The shared filter graph in record.py merges both into the same 2-channel
audio.opus the Linux path produces.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from AppKit import NSWorkspace  # type: ignore[import-not-found]

from ._platform import CapturePlan
from .detect import Detection


# Path to the Swift binary, committed at <repo>/mac/witness-audiotap.
# This module lives at <repo>/src/witnessd/_platform_darwin.py.
_AUDIOTAP_BIN = Path(__file__).resolve().parent.parent.parent / "mac" / "witness-audiotap"

_MEET_URL = re.compile(r"meet\.google\.com/([a-z0-9\-]+)", re.IGNORECASE)

_MEETING_BUNDLES = {
    "us.zoom.xos": "zoom",
    "com.microsoft.teams2": "teams",
    "com.microsoft.teams": "teams",
}


def _is_mic_running() -> bool:
    try:
        result = subprocess.run(
            [str(_AUDIOTAP_BIN), "--probe-mic-running"],
            timeout=2,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _running_meeting_app() -> tuple[str, str, int] | None:
    """Return (platform, title, pid) for a running Zoom/Teams app, else None."""
    workspace = NSWorkspace.sharedWorkspace()
    for app in workspace.runningApplications():
        bundle_id = app.bundleIdentifier()
        if bundle_id is None:
            continue
        platform = _MEETING_BUNDLES.get(str(bundle_id))
        if platform is None:
            continue
        title = str(app.localizedName() or platform.title())
        return platform, title, int(app.processIdentifier())
    return None


def _front_browser_meet() -> tuple[str, int] | None:
    """If the front Chrome/Safari window is on a meet.google.com tab, return
    (room_code, pid) for that browser. Else None.

    AppleScript-based — there's no public API for "URL of the active tab"
    on macOS without scripting bridges.
    """
    scripts = [
        # Chrome family (Chrome, Brave, Arc, Edge use similar dictionaries
        # but the bundle IDs vary). Try Chrome canonical first.
        ("Google Chrome", 'tell application "Google Chrome" to if it is running then return URL of active tab of front window'),
        ("Safari",        'tell application "Safari" to if it is running then return URL of front document'),
    ]
    for app_name, script in scripts:
        try:
            out = subprocess.check_output(
                ["osascript", "-e", script],
                text=True,
                timeout=2,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
        m = _MEET_URL.search(out)
        if m is None:
            continue
        # Resolve PID via NSWorkspace
        pid = _bundle_pid(app_name)
        if pid is None:
            continue
        return m.group(1), pid
    return None


def _bundle_pid(localized_name: str) -> int | None:
    workspace = NSWorkspace.sharedWorkspace()
    for app in workspace.runningApplications():
        if str(app.localizedName() or "") == localized_name:
            return int(app.processIdentifier())
    return None


@dataclass
class DarwinPlatform:
    def detect_meeting(self) -> Detection | None:
        if not _is_mic_running():
            return None

        # Try a desktop meeting app first.
        app_hit = _running_meeting_app()
        if app_hit is not None:
            platform, title, pid = app_hit
            return Detection(
                platform=platform,
                title=title,
                source="coreaudio",
                application_pid=pid,
                application_name=title,
            )

        # Fall back to a Meet tab in the front browser window.
        meet = _front_browser_meet()
        if meet is not None:
            room, pid = meet
            return Detection(
                platform="meet",
                title=f"Meet - {room}",
                source="coreaudio",
                application_pid=pid,
                application_name="Google Chrome",
            )

        return None

    def plan_capture(self) -> CapturePlan:
        if not _AUDIOTAP_BIN.exists() or not os.access(_AUDIOTAP_BIN, os.X_OK):
            raise RuntimeError(
                f"witness-audiotap binary missing or not executable at {_AUDIOTAP_BIN}. "
                f"Maintainer: run mac/build.sh to rebuild."
            )

        # witness-audiotap captures BOTH the default mic (sub-device) and
        # system audio (sub-tap) via a CoreAudio aggregate device, and
        # writes a single 2-channel float32 stream: ch0=mic, ch1=system.
        # This is the entire audio source for ffmpeg — no avfoundation,
        # which is critical because ffmpeg's avfoundation demuxer doesn't
        # respond to graceful shutdown signals (it hangs until SIGKILL,
        # losing the opus trailer and producing a 0-byte file).
        r_fd, w_fd = os.pipe()
        try:
            tap_proc = subprocess.Popen(
                [str(_AUDIOTAP_BIN), "--rate", "48000"],
                stdout=w_fd,
                start_new_session=True,
            )
        except BaseException:
            os.close(r_fd)
            os.close(w_fd)
            raise
        os.close(w_fd)

        return CapturePlan(
            ffmpeg_inputs=[
                "-f", "f32le", "-ar", "48000", "-ac", "2", "-i", f"pipe:{r_fd}",
            ],
            extra_pass_fds=(r_fd,),
            aux_procs=[tap_proc],
            aux_fds_to_close_in_parent=[r_fd],
            sources_metadata={
                "mic": "coreaudio_default_input",
                "system": "coreaudio_tap",
                "binary": str(_AUDIOTAP_BIN),
            },
            # Single input, already 2 channels (mic, system). Opus output
            # reads it directly; per-channel PCM uses pan to extract.
            archive_filter="",
            archive_map=["-map", "0:a"],
            mic_pcm_map=["-map", "0:a"],
            mic_pcm_af="pan=mono|c0=c0",
            sys_pcm_map=["-map", "0:a"],
            sys_pcm_af="pan=mono|c0=c1",
        )
