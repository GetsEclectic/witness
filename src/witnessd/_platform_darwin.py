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


def _any_meet_room_open() -> tuple[str, int] | None:
    """Return (room, pid) for the first Meet tab found in any Chrome/Safari
    window, else None. Used at session-start when the daemon doesn't yet
    have an active room to anchor on.

    Combined with `_is_mic_running()` upstream: a Meet tab whose call has
    already ended doesn't trigger this because Chrome releases the mic
    when the call ends. Once a session is running, `_meet_room_open_anywhere`
    pins detection to *that specific room* so a stale tab from a finished
    call can't divert recording away from the active one.
    """
    chrome_script = '''tell application "Google Chrome"
        if it is not running then return ""
        repeat with w in windows
            repeat with t in tabs of w
                set u to URL of t
                if u contains "meet.google.com" then return u
            end repeat
        end repeat
        return ""
    end tell'''
    safari_script = '''tell application "Safari"
        if it is not running then return ""
        repeat with w in windows
            repeat with t in tabs of w
                set u to URL of t
                if u contains "meet.google.com" then return u
            end repeat
        end repeat
        return ""
    end tell'''
    for app_name, script in (("Google Chrome", chrome_script), ("Safari", safari_script)):
        try:
            out = subprocess.check_output(
                ["osascript", "-e", script],
                text=True,
                timeout=3,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
        m = _MEET_URL.search(out)
        if m is None:
            continue
        pid = _bundle_pid(app_name)
        if pid is None:
            continue
        return m.group(1), pid
    return None


def _meet_room_open_anywhere(room: str) -> int | None:
    """Return the Chrome/Safari pid if a tab for the *specific* meet room is
    open in any window, else None.

    Targeted lookup — we only consider this as "user tabbed away from a
    call already in progress" evidence, not as standalone detection.
    Looking for one specific room avoids false positives from stale Meet
    tabs left over from earlier calls (a generic "any meet.google.com tab"
    check picks those up and would keep the recording going forever).
    """
    # AppleScript string equality is case-insensitive by default for
    # `contains`; Meet codes are lowercase letters/dashes anyway.
    script_chrome = f'''tell application "Google Chrome"
        if it is not running then return ""
        repeat with w in windows
            repeat with t in tabs of w
                set u to URL of t
                if u contains "meet.google.com/{room}" then return u
            end repeat
        end repeat
        return ""
    end tell'''
    script_safari = f'''tell application "Safari"
        if it is not running then return ""
        repeat with w in windows
            repeat with t in tabs of w
                set u to URL of t
                if u contains "meet.google.com/{room}" then return u
            end repeat
        end repeat
        return ""
    end tell'''
    for app_name, script in (("Google Chrome", script_chrome), ("Safari", script_safari)):
        try:
            out = subprocess.check_output(
                ["osascript", "-e", script],
                text=True,
                timeout=3,
                stderr=subprocess.DEVNULL,
            ).strip()
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
        if not out:
            continue
        pid = _bundle_pid(app_name)
        if pid is not None:
            return pid
    return None


def _bundle_pid(localized_name: str) -> int | None:
    workspace = NSWorkspace.sharedWorkspace()
    for app in workspace.runningApplications():
        if str(app.localizedName() or "") == localized_name:
            return int(app.processIdentifier())
    return None


@dataclass
class DarwinPlatform:
    def detect_meeting(self, active_key: str | None = None) -> Detection | None:
        """Return a Detection if a meeting is currently active.

        Detection is gated by `_is_mic_running()` — Chrome only holds the
        default input device while a call is actually live, so a stale
        Meet tab whose call ended doesn't trigger anything once the user
        leaves the call (mic releases within a second or two).

        Tab focus is irrelevant: we accept any Meet tab open in any
        Chrome/Safari window. `active_key` is used to *prefer continuity*
        — if a session is already running for `meet:<room>` and that
        same room is still open somewhere, we keep emitting detections
        for it rather than letting AppleScript iteration order pick a
        different tab and trigger a session rotation.
        """
        if not _is_mic_running():
            return None

        # Try a desktop meeting app first (Zoom/Teams).
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

        # Continuity: if we're already recording a Meet room and that
        # room's tab is still open, return it directly. Skips the more
        # general lookup so AppleScript iteration order can't quietly
        # switch us to a different open Meet tab.
        if active_key and active_key.startswith("meet:"):
            room = active_key.split(":", 1)[1]
            pid = _meet_room_open_anywhere(room)
            if pid is not None:
                return Detection(
                    platform="meet",
                    title=f"Meet - {room}",
                    source="coreaudio",
                    application_pid=pid,
                    application_name="Google Chrome",
                )

        # Startup, or active room's tab disappeared: pick any Meet tab.
        match = _any_meet_room_open()
        if match is not None:
            room, pid = match
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
