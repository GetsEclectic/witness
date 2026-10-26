"""macOS capture + detection.

Detection: combine two signals to mirror the strictness Linux's pactl
source-outputs check enforces ("someone has the mic open *right now*"):
  1. Mic active — the bundled `witness-audiotap --probe-mic-running` exits
     0 when any input device's IsRunningSomewhere is true — not just the
     default's, since a call app can pick its own mic. Cheap subprocess
     (~10ms).
  2. Meeting app present — NSWorkspace.runningApplications for Zoom/Teams,
     Slack itself holding input (a huddle),
     a sweep of the open tabs of *every* running Chrome and Safari instance
     (`_browser_probe`) to spot a Meet room or a Teams meeting URL (both
     platforms are routinely joined in the browser with no desktop app
     installed), and failing that a Teams tab whose *title* names a meeting
     — all the Teams web app exposes of a joined call.

Both must be true to fire. Without (1) we'd fire whenever Zoom is merely
launched; without (2) we'd fire on any random app holding the mic.

Capture: `mac/witness-audiotap`, a Swift binary, aggregates the input device
in use (the default when nothing is) with a CoreAudio Process Tap
(system-wide, excluding our own PID) and pipes interleaved Float32 PCM to
ffmpeg via an inherited fd.
The shared filter graph in record.py merges both into the same 2-channel
audio.opus the Linux path produces.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from AppKit import NSWorkspace  # type: ignore[import-not-found]

from ._platform import CapturePlan
from .detect import (
    TEAMS_APP_SECTIONS,
    Detection,
    ProbeFailed,
    is_teams_app_url,
    meet_call_from_url,
    teams_conference_ids_from_tab,
    teams_subject_from_tab,
)

log = logging.getLogger("witnessd.platform")


# Path to the Swift binary, committed at <repo>/mac/witness-audiotap.
# This module lives at <repo>/src/witnessd/_platform_darwin.py.
_AUDIOTAP_BIN = Path(__file__).resolve().parent.parent.parent / "mac" / "witness-audiotap"

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

# A call app's audio can run in a helper with its own bundle id (Zoom's
# CptHost, Teams' WebView), so ownership of an input stream is by prefix.
_APP_INPUT_PREFIXES = {
    "zoom": "us.zoom.",
    "teams": "com.microsoft.teams",
}

# Slack is always running, so unlike Zoom its presence says nothing; only an
# input stream does. The huddle audio runs in `com.tinyspeck.slackmacgap.helper`.
_SLACK_BUNDLE = "com.tinyspeck.slackmacgap"


_PROBE_PID_RE = re.compile(r"^pid (\d+) (.*)$")


def _probe_mic() -> tuple[bool, frozenset[str] | None]:
    """(input device in use, bundle ids holding input, or None if unknown)."""
    try:
        result = subprocess.run(
            [str(_AUDIOTAP_BIN), "--probe-mic-running"],
            timeout=2,
            capture_output=True,
        )
    except subprocess.TimeoutExpired as e:
        raise ProbeFailed("audiotap mic probe timed out") from e
    except FileNotFoundError:
        return False, None
    holders = set()
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        m = _PROBE_PID_RE.match(line.strip())
        if m is not None and m.group(2) != "-":
            holders.add(m.group(2).strip().lower())
    return result.returncode == 0, frozenset(holders) or None


def _bundle_id_for_pid(pid: int) -> str | None:
    workspace = NSWorkspace.sharedWorkspace()
    for app in workspace.runningApplications():
        if int(app.processIdentifier()) == pid:
            bundle_id = app.bundleIdentifier()
            return str(bundle_id).lower() if bundle_id else None
    return None


def _browser_holds_input(browser_pid: int, holders: frozenset[str]) -> bool:
    """Whether the browser owning `browser_pid` has a live input stream."""
    bundle_id = _bundle_id_for_pid(browser_pid)
    if bundle_id is None:
        return False
    return any(h == bundle_id or h.startswith(bundle_id + ".") for h in holders)


def _holds_input(prefix: str, holders: frozenset[str] | None) -> bool:
    return holders is not None and any(h.startswith(prefix) for h in holders)


def _slack_pid() -> int | None:
    workspace = NSWorkspace.sharedWorkspace()
    for app in workspace.runningApplications():
        if str(app.bundleIdentifier() or "").lower() == _SLACK_BUNDLE:
            return int(app.processIdentifier())
    return None


# Holds the Accessibility grant, so it runs as its own app via `open`: TCC
# would otherwise charge the request to the daemon's Python.
_SLACK_PROBE_APP = _AUDIOTAP_BIN.parent / "WitnessSlackProbe.app"

# A probe costs ~1.6s; the huddle's channel changes far less often than that.
_HUDDLE_NAME_TTL_S = 30.0

_huddle_name_cache: tuple[float, str | None] | None = None
_slack_probe_error: str | None = None


def huddle_name_from_titles(titles: list[str]) -> str | None:
    """The huddle's channel or people, from Slack's window titles.

    A huddle gets its own window, `<channel or names> - <workspace> - Slack`;
    the main window carries `[Main]` and tracks whatever channel is open."""
    for title in titles:
        if "[Main]" in title:
            continue
        parts = title.rsplit(" - ", 2)
        if len(parts) == 3 and parts[2] == "Slack" and parts[0].strip():
            return parts[0].strip()
    return None


def _probe_slack_window_titles() -> list[str] | None:
    global _slack_probe_error
    with tempfile.TemporaryDirectory(prefix="witness-slackprobe-") as tmp:
        out = Path(tmp) / "out"
        try:
            subprocess.run(
                ["open", "-n", "-W", "-g", "--stdout", str(out),
                 str(_SLACK_PROBE_APP), "--args", "--max-depth", "0"],
                timeout=6, capture_output=True,
            )
            lines = out.read_text().splitlines() if out.exists() else []
        except (subprocess.TimeoutExpired, OSError) as e:
            lines, error = [], f"slack probe failed: {e}"
        else:
            error = None
    titles = []
    for line in lines:
        try:
            node = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in node:
            error = f"slack probe: {node['error']}"
        elif node.get("role") == "AXWindow" and node.get("title"):
            titles.append(node["title"])
    if error != _slack_probe_error:
        if error:
            log.warning("%s; huddles keep the generic title", error)
        _slack_probe_error = error
    return None if error else titles


def _huddle_name() -> str | None:
    global _huddle_name_cache
    now = time.monotonic()
    if _huddle_name_cache and now - _huddle_name_cache[0] < _HUDDLE_NAME_TTL_S:
        return _huddle_name_cache[1]
    titles = _probe_slack_window_titles()
    name = huddle_name_from_titles(titles) if titles is not None else None
    _huddle_name_cache = (now, name)
    return name


def _slack_huddle(holders: frozenset[str] | None) -> Detection | None:
    """A Detection for a Slack huddle if Slack has a live input stream.

    Named and keyed by the huddle window's title when the probe can read it,
    so back-to-back huddles in different channels get their own folders."""
    if not _holds_input(_SLACK_BUNDLE, holders):
        return None
    name = _huddle_name()
    return Detection(
        platform="slack",
        title=f"Huddle - {name}" if name else "Slack huddle",
        source="coreaudio",
        application_pid=_slack_pid(),
        application_name="Slack",
        conference_id=name.lower() if name else None,
    )


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


_PROBE_MODULE = "witnessd._browser_probe"

# Covers every browser instance together, and has to finish inside the
# daemon's 5s poll interval. Each instance is separately bounded at 1s, so
# this only trips if the interpreter itself cannot start.
_PROBE_TIMEOUT_S = 4

_browser_census: str | None = None


def _note_census(instances: list[dict]) -> None:
    """Log the running browser instances whenever the set of them changes."""
    global _browser_census
    census = "; ".join(
        f"{inst['bundle_id']}:{inst['pid']}({len(inst['tabs'])} tabs)"
        for inst in sorted(instances, key=lambda i: i["pid"])
    ) or "none"
    if census != _browser_census:
        log.info("browser instances: %s", census)
        _browser_census = census


def _browser_tabs() -> tuple[list[tuple[str, str, int]], bool]:
    """Return ([(url, title, browser_pid), ...], inconclusive) for every tab
    open in every running Chrome and Safari instance.

    One subprocess round-trip, shared by every URL matcher below.

    `inconclusive` means we learned *nothing* — the probe died, or every
    instance it found refused to answer. Callers raise ProbeFailed only when
    they also have no match, so a bad tick doesn't read as "the meeting
    ended". No browser running at all is a conclusive "no tabs".
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", _PROBE_MODULE],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        log.warning("browser tab probe timed out after %ss", _PROBE_TIMEOUT_S)
        return [], True
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("browser tab probe could not run: %r", e)
        return [], True
    if proc.returncode != 0:
        log.warning(
            "browser tab probe exited %s: %s",
            proc.returncode, proc.stderr.strip()[-400:],
        )
        return [], True
    try:
        instances = json.loads(proc.stdout)["instances"]
    except (ValueError, TypeError, KeyError):
        log.warning("browser tab probe wrote no usable JSON: %r", proc.stdout[:200])
        return [], True

    _note_census(instances)
    tabs: list[tuple[str, str, int]] = []
    answered = False
    for inst in instances:
        if inst["errors"]:
            log.warning(
                "browser instance %s (%s) did not fully answer: %s",
                inst["pid"], inst["bundle_id"], "; ".join(inst["errors"]),
            )
        else:
            answered = True
        pid = int(inst["pid"])
        tabs.extend((url, title, pid) for url, title in inst["tabs"])
    return tabs, bool(instances) and not answered


def _any_meet_room_open() -> tuple[str, int] | None:
    """Return (room, pid) for the first Meet tab found in any Chrome/Safari
    window, else None. Used at session-start when the daemon doesn't yet
    have an active room to anchor on.

    A match is a candidate, not a verdict: `detect_meeting` asks which
    browser holds the mic. Continuity pins a running session to one room.
    """
    tabs, timed_out = _browser_tabs()
    for url, _title, pid in tabs:
        room = meet_call_from_url(url)
        if room is not None:
            return room, pid
    if timed_out:
        # Every browser we tried timed out (or the only successful one
        # reported no Meet tab AND another timed out). We have no clean
        # evidence either way, so propagate the indeterminacy.
        raise ProbeFailed("browser Meet-tab probe inconclusive")
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
    tabs, timed_out = _browser_tabs()
    room_lc = room.lower()
    for url, _title, pid in tabs:
        if meet_call_from_url(url) == room_lc:
            return pid
    if timed_out:
        raise ProbeFailed("browser active-room probe inconclusive")
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
    tabs, timed_out = _browser_tabs()
    for url, _title, pid in tabs:
        ids = teams_conference_ids_from_tab(url)
        if ids:
            # A tab shows one id or the other, never both; `sorted` just keeps
            # the choice deterministic if that ever stops being true.
            return sorted(ids)[0], pid
    if timed_out:
        raise ProbeFailed("browser Teams-tab probe inconclusive")
    return None


def _teams_meeting_open_anywhere(meeting_id: str) -> int | None:
    """Return the Chrome/Safari pid if a tab for the *specific* Teams meeting
    is open in any window, else None. The Meet continuity check's counterpart
    — see `_meet_room_open_anywhere` for why it's scoped to one call."""
    tabs, timed_out = _browser_tabs()
    for url, _title, pid in tabs:
        if meeting_id in teams_conference_ids_from_tab(url):
            return pid
    if timed_out:
        raise ProbeFailed("browser active-Teams-meeting probe inconclusive")
    return None


def _any_teams_call_by_title() -> tuple[str, int] | None:
    """Return (subject, pid) for the first Teams tab whose title names a
    meeting, else None — the web app's id-less shell."""
    tabs, timed_out = _browser_tabs()
    for url, title, pid in tabs:
        subject = teams_subject_from_tab(url, title)
        if subject is not None:
            return subject, pid
    if timed_out:
        raise ProbeFailed("browser Teams-title probe inconclusive")
    return None


def _teams_app_open_anywhere() -> int | None:
    """Return the Chrome/Safari pid if any Teams web-app tab is open, whatever
    its title, else None — title-keyed continuity, where clicking Chat mid-call
    retitles the tab and must not read as the meeting ending."""
    tabs, timed_out = _browser_tabs()
    for url, _title, pid in tabs:
        if is_teams_app_url(url):
            return pid
    if timed_out:
        raise ProbeFailed("browser Teams-app probe inconclusive")
    return None


def _is_teams_call_id(call_id: str) -> bool:
    """Whether the tail of a session key is a Teams call id rather than the
    `Microsoft Teams:333:None` tail a desktop-app detection mints. Only a real
    call id can be looked up in the browser."""
    return call_id.startswith("19:meeting_") or call_id.isdigit()


def _teams_subject_from_key(call_id: str) -> str | None:
    """The subject out of a title-keyed Teams session key
    (`teams:<subject>:<pid>:<index>`), or None if the key isn't one. Fields peel
    off the right, so a subject with its own colons survives."""
    subject = call_id.rpartition(":")[0].rpartition(":")[0].strip()
    if not subject or subject.lower() in TEAMS_APP_SECTIONS:
        return None
    return subject


def _teams_title(meeting_id: str) -> str:
    """Human-readable label for a Teams call, e.g. `Teams - nwq4zmy0mji`.

    Feeds the folder slug when no calendar event matches, so it has to be
    short and filesystem-clean — the raw thread id is ~60 opaque characters.
    Truncated ids are for display only; identity always uses the full id.
    """
    body = meeting_id.split("meeting_", 1)[-1].split("@", 1)[0]
    body = re.sub(r"[^a-z0-9]", "", body.lower())[:12]
    return f"Teams - {body}" if body else "Teams meeting"


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


def _teams_subject_detection(subject: str, pid: int) -> Detection:
    """A Teams call identified only by its tab title. `conference_id` stays None
    so correlation scores the subject: a synthetic id would disqualify the
    matching invite as a different call."""
    return Detection(
        platform="teams",
        title=subject,
        source="window",
        application_pid=pid,
        application_name="Google Chrome",
    )


@dataclass
class DarwinPlatform:
    def detect_meeting(self, active_key: str | None = None) -> Detection | None:
        """Return a Detection if a meeting is currently active.

        Detection is gated on the input device being in use, which is a
        system-wide fact; which browser uses it breaks ties between tabs.

        Tab focus is irrelevant: we accept any Meet or Teams call tab open
        in any Chrome/Safari window. `active_key` is used to *prefer
        continuity* — if a session is already running for `meet:<room>` or
        `teams:<thread-id>` and that same call is still open somewhere, we
        keep emitting detections for it rather than letting tab iteration
        order pick a different call and trigger a session rotation.
        """
        mic_running, holders = _probe_mic()
        if not mic_running:
            return None

        # A huddle only ever shows up as Slack holding input, so it is weighed
        # against the other calls by who holds the mic, not by what is open:
        # Zoom idling in the dock or a stale Meet tab must not claim it.
        huddle = _slack_huddle(holders)
        if huddle is not None and active_key == huddle.key:
            return huddle

        def holds_mic(browser_pid: int) -> bool:
            return huddle is None or _browser_holds_input(browser_pid, holders)

        # Try a desktop meeting app first (Zoom/Teams).
        app_hit = _running_meeting_app()
        if app_hit is not None and (
            huddle is None
            or _holds_input(_APP_INPUT_PREFIXES[app_hit[0]], holders)
        ):
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
            if pid is not None and holds_mic(pid):
                return _meet_detection(call_id, pid)
        elif platform_prefix == "teams" and _is_teams_call_id(call_id):
            pid = _teams_meeting_open_anywhere(call_id)
            if pid is not None and holds_mic(pid):
                return _teams_detection(call_id, pid)
        elif platform_prefix == "teams":
            # Re-emitting the stored subject keeps the key identical.
            subject = _teams_subject_from_key(call_id)
            if subject is not None:
                pid = _teams_app_open_anywhere()
                if pid is not None and holds_mic(pid):
                    return _teams_subject_detection(subject, pid)

        # Startup, or the active call's tab disappeared: pick any call tab.
        # The browser holding the mic wins outright, whatever the order here.
        # Unattributable ticks keep the old priority: Meet, id-Teams, title.
        fallback: Detection | None = None
        for find_hit, build in (
            (_any_meet_room_open, _meet_detection),
            (_any_teams_meeting_open, _teams_detection),
            (_any_teams_call_by_title, _teams_subject_detection),
        ):
            hit = find_hit()
            if hit is None:
                continue
            detection = build(*hit)
            if holders is None or _browser_holds_input(hit[1], holders):
                return detection
            if fallback is None:
                fallback = detection
        return huddle or fallback

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
                "mic": "coreaudio_live_input",
                "system": "coreaudio_tap",
                "binary": str(_AUDIOTAP_BIN),
            },
            # Single input, already 2 channels (mic, system) — the opus
            # output reads it directly, no filter needed.
            archive_filter="",
            archive_map=["-map", "0:a"],
        )
