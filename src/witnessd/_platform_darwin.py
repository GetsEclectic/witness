"""macOS capture + detection.

Detection: combine two signals to mirror the strictness Linux's pactl
source-outputs check enforces ("someone has the mic open *right now*"):
  1. Mic active — the bundled `witness-audiotap --probe-mic-running` exits
     0 when the default input device's IsRunningSomewhere is true. Cheap
     subprocess (~10ms).
  2. Meeting app present — NSWorkspace.runningApplications for Zoom/Teams,
     osascript over the open Chrome/Safari tabs to spot a Meet room or a
     Teams meeting URL (both platforms are routinely joined in the browser
     with no desktop app installed).

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

import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from AppKit import NSWorkspace  # type: ignore[import-not-found]

from ._platform import CapturePlan
from .detect import Detection, ProbeFailed, teams_conference_ids_from_tab

log = logging.getLogger("witnessd.platform")


# Path to the Swift binary, committed at <repo>/mac/witness-audiotap.
# This module lives at <repo>/src/witnessd/_platform_darwin.py.
_AUDIOTAP_BIN = Path(__file__).resolve().parent.parent.parent / "mac" / "witness-audiotap"

_MEET_URL = re.compile(r"meet\.google\.com/([a-z0-9\-]+)", re.IGNORECASE)

# The rate we ask the tap to *emit*. The tap resamples internally, so this is a
# fixed contract for the life of the process: the CoreAudio aggregate underneath
# may run at 16 kHz (Bluetooth headset in HFP mode) or 44.1 kHz, and may change
# mid-capture when the user's devices change, but stdout stays at this rate.
# That's what lets ffmpeg keep one -ar across a device swap.
_TAP_OUTPUT_RATE = 48000

# The tap echoes its output rate back on stderr once the graph is up. We parse
# it as a startup handshake (proof the tap got as far as running) and to catch a
# stale binary that predates the fixed-rate contract.
_TAP_RATE_RE = re.compile(rb"witness-audiotap: rate=(\d+)")


def _pump_tap_stderr(stderr, rate_holder: dict, rate_ready: threading.Event) -> None:
    """Forward the tap's stderr to our own (so it lands in the daemon log) and
    capture the reported output rate. Runs on a daemon thread for the life of
    the tap; setting `rate_ready` on the rate line OR on EOF unblocks the
    launcher so a tap that dies at startup doesn't hang capture forever."""
    try:
        for line in iter(stderr.readline, b""):
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()
            if not rate_ready.is_set():
                m = _TAP_RATE_RE.search(line)
                if m:
                    rate_holder["rate"] = int(m.group(1))
                    rate_ready.set()
    finally:
        stderr.close()
        rate_ready.set()


_MEETING_BUNDLES = {
    "us.zoom.xos": "zoom",
    "com.microsoft.teams2": "teams",
    "com.microsoft.teams": "teams",
}


def _is_mic_running() -> bool:
    """True when something currently owns the default input device.

    Raises ProbeFailed on subprocess timeout — distinguishes "audiotap says
    no" (return False) from "audiotap stalled" (raise) so the daemon's gap
    timer doesn't advance during transient probe stalls.
    """
    try:
        result = subprocess.run(
            [str(_AUDIOTAP_BIN), "--probe-mic-running"],
            timeout=2,
            capture_output=True,
        )
    except subprocess.TimeoutExpired as e:
        raise ProbeFailed("audiotap mic probe timed out") from e
    except FileNotFoundError:
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


_TAB_SCRIPT = '''tell application "{app}"
    if it is not running then return ""
    set out to ""
    repeat with w in windows
        repeat with t in tabs of w
            set out to out & (URL of t) & linefeed
        end repeat
    end repeat
    return out
end tell'''


def _browser_tab_urls() -> tuple[list[tuple[str, int]], bool]:
    """Return ([(url, browser_pid), ...], any_probe_timed_out) for every tab
    open in Chrome and Safari, Chrome first.

    One osascript round-trip per browser, shared by every URL matcher below
    — the alternative (a bespoke AppleScript per platform per lookup) costs
    a 3s-timeout subprocess each and the tick has to stay well under the
    daemon's poll interval.

    The timeout flag is returned rather than raised because "Safari stalled"
    only matters if nothing else matched: a hit from Chrome is still a hit.
    Callers raise ProbeFailed when they come up empty *and* a probe stalled,
    so an inconclusive tick doesn't read as "the meeting ended."
    """
    pairs: list[tuple[str, int]] = []
    timed_out = False
    for app_name in ("Google Chrome", "Safari"):
        try:
            out = subprocess.check_output(
                ["osascript", "-e", _TAB_SCRIPT.format(app=app_name)],
                text=True,
                timeout=3,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            continue
        except (subprocess.SubprocessError, FileNotFoundError):
            continue
        urls = [u.strip() for u in out.splitlines() if u.strip()]
        if not urls:
            continue
        pid = _bundle_pid(app_name)
        if pid is None:
            continue
        pairs.extend((u, pid) for u in urls)
    return pairs, timed_out


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
    pairs, timed_out = _browser_tab_urls()
    for url, pid in pairs:
        m = _MEET_URL.search(url)
        if m is not None:
            return m.group(1), pid
    if timed_out:
        # Every browser we tried timed out (or the only successful one
        # reported no Meet tab AND another timed out). We have no clean
        # evidence either way, so propagate the indeterminacy.
        raise ProbeFailed("osascript Meet-tab probe timed out")
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
    pairs, timed_out = _browser_tab_urls()
    room_lc = room.lower()
    for url, pid in pairs:
        m = _MEET_URL.search(url)
        if m is not None and m.group(1).lower() == room_lc:
            return pid
    if timed_out:
        raise ProbeFailed("osascript active-room probe timed out")
    return None


def _any_teams_meeting_open() -> tuple[str, int] | None:
    """Return (meeting_id, pid) for the first Teams *call* tab open in any
    Chrome/Safari window, else None.

    The browser is how Teams meetings actually get joined here — there's no
    desktop app in the picture — so without this a Teams call produces no
    detection at all and goes unrecorded. Same mic gating as Meet: the tab
    only counts while the browser holds the input device, so the post-call
    summary screen left open in a tab doesn't keep a session alive.
    """
    pairs, timed_out = _browser_tab_urls()
    for url, pid in pairs:
        ids = teams_conference_ids_from_tab(url)
        if ids:
            # A tab shows one id or the other, never both; `sorted` just keeps
            # the choice deterministic if that ever stops being true.
            return sorted(ids)[0], pid
    if timed_out:
        raise ProbeFailed("osascript Teams-tab probe timed out")
    return None


def _teams_meeting_open_anywhere(meeting_id: str) -> int | None:
    """Return the Chrome/Safari pid if a tab for the *specific* Teams meeting
    is open in any window, else None. The Meet continuity check's counterpart
    — see `_meet_room_open_anywhere` for why it's scoped to one call."""
    pairs, timed_out = _browser_tab_urls()
    for url, pid in pairs:
        if meeting_id in teams_conference_ids_from_tab(url):
            return pid
    if timed_out:
        raise ProbeFailed("osascript active-Teams-meeting probe timed out")
    return None


def _is_teams_call_id(call_id: str) -> bool:
    """Whether the tail of a session key is a Teams call id rather than the
    `Microsoft Teams:333:None` tail a desktop-app detection mints. Only a real
    call id can be looked up in the browser."""
    return call_id.startswith("19:meeting_") or call_id.isdigit()


def _teams_title(meeting_id: str) -> str:
    """Human-readable label for a Teams call, e.g. `Teams - nwq4zmy0mji`.

    Feeds the folder slug when no calendar event matches, so it has to be
    short and filesystem-clean — the raw thread id is ~60 opaque characters.
    Truncated ids are for display only; identity always uses the full id.
    """
    body = meeting_id.split("meeting_", 1)[-1].split("@", 1)[0]
    body = re.sub(r"[^a-z0-9]", "", body.lower())[:12]
    return f"Teams - {body}" if body else "Teams meeting"


def _bundle_pid(localized_name: str) -> int | None:
    workspace = NSWorkspace.sharedWorkspace()
    for app in workspace.runningApplications():
        if str(app.localizedName() or "") == localized_name:
            return int(app.processIdentifier())
    return None


def _meet_detection(room: str, pid: int) -> Detection:
    return Detection(
        platform="meet",
        title=f"Meet - {room}",
        source="coreaudio",
        application_pid=pid,
        application_name="Google Chrome",
        conference_id=room,
    )


def _teams_detection(meeting_id: str, pid: int) -> Detection:
    return Detection(
        platform="teams",
        title=_teams_title(meeting_id),
        source="coreaudio",
        application_pid=pid,
        application_name="Google Chrome",
        conference_id=meeting_id,
    )


@dataclass
class DarwinPlatform:
    def detect_meeting(self, active_key: str | None = None) -> Detection | None:
        """Return a Detection if a meeting is currently active.

        Detection is gated by `_is_mic_running()` — Chrome only holds the
        default input device while a call is actually live, so a stale
        Meet tab whose call ended doesn't trigger anything once the user
        leaves the call (mic releases within a second or two).

        Tab focus is irrelevant: we accept any Meet or Teams call tab open
        in any Chrome/Safari window. `active_key` is used to *prefer
        continuity* — if a session is already running for `meet:<room>` or
        `teams:<thread-id>` and that same call is still open somewhere, we
        keep emitting detections for it rather than letting tab iteration
        order pick a different call and trigger a session rotation.
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

        # Continuity: if we're already recording a browser call and its tab
        # is still open, return it directly. Skips the more general lookups
        # so tab iteration order can't quietly switch us to another call.
        # A pre-conference-id key (`teams:Microsoft Teams:333:None`, from the
        # desktop-app branch) isn't a browser call and falls through.
        platform_prefix, _, call_id = (active_key or "").partition(":")
        if platform_prefix == "meet" and call_id:
            pid = _meet_room_open_anywhere(call_id)
            if pid is not None:
                return _meet_detection(call_id, pid)
        elif platform_prefix == "teams" and _is_teams_call_id(call_id):
            pid = _teams_meeting_open_anywhere(call_id)
            if pid is not None:
                return _teams_detection(call_id, pid)

        # Startup, or the active call's tab disappeared: pick any call tab.
        # Meet before Teams — a Teams tab parked on a meeting the user has
        # already left is more likely to linger than a Meet one, and losing
        # that race would divert a live Meet recording.
        meet_hit = _any_meet_room_open()
        if meet_hit is not None:
            return _meet_detection(*meet_hit)

        teams_hit = _any_teams_meeting_open()
        if teams_hit is not None:
            return _teams_detection(*teams_hit)

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
                [str(_AUDIOTAP_BIN), "--rate", str(_TAP_OUTPUT_RATE)],
                stdout=w_fd,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except BaseException:
            os.close(r_fd)
            os.close(w_fd)
            raise
        os.close(w_fd)

        # Wait for the tap to confirm its output rate before launching ffmpeg.
        # This is a startup handshake, not a rate negotiation: the tap resamples
        # to _TAP_OUTPUT_RATE regardless of what the device underneath runs at,
        # so the answer is known — what we're really testing is that the tap got
        # its capture graph up at all. If it never reports (died at startup, TCC
        # denied, no input device), abort loudly instead of recording silence.
        rate_holder: dict = {}
        rate_ready = threading.Event()
        threading.Thread(
            target=_pump_tap_stderr,
            args=(tap_proc.stderr, rate_holder, rate_ready),
            daemon=True,
        ).start()
        rate_ready.wait(timeout=6.0)
        if "rate" not in rate_holder:
            tap_proc.terminate()
            os.close(r_fd)
            raise RuntimeError(
                "witness-audiotap did not report a capture rate within 6s — the "
                "audio device is likely misconfigured (see witness-audiotap "
                "output above). Restarting CoreAudio (sudo killall coreaudiod) "
                "usually clears this. Aborting capture rather than recording silence."
            )
        rate = rate_holder["rate"]
        if rate != _TAP_OUTPUT_RATE:
            # A binary predating the fixed-rate contract reports the *device*
            # rate here. Trust what it says over what we asked for — feeding
            # ffmpeg the wrong -ar pitch-shifts the whole recording — but say so,
            # because such a build also can't survive a mid-meeting device change.
            log.warning(
                "witness-audiotap reported rate=%d but %d was requested; using %d. "
                "Rebuild the tap (mac/build.sh) — this build predates fixed-rate "
                "output and will stop capturing if the audio device changes.",
                rate, _TAP_OUTPUT_RATE, rate,
            )

        return CapturePlan(
            ffmpeg_inputs=[
                "-f", "f32le", "-ar", str(rate), "-ac", "2", "-i", f"pipe:{r_fd}",
            ],
            extra_pass_fds=(r_fd,),
            aux_procs=[tap_proc],
            aux_fds_to_close_in_parent=[r_fd],
            sources_metadata={
                "mic": "coreaudio_default_input",
                "system": "coreaudio_tap",
                "binary": str(_AUDIOTAP_BIN),
            },
            # Single input, already 2 channels (mic, system) — the opus
            # output reads it directly, no filter needed.
            archive_filter="",
            archive_map=["-map", "0:a"],
        )
