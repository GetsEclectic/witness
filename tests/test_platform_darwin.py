"""Mac platform tests. Skipped on non-Mac systems.

Patches the helpers that probe the system (_probe_mic, _running_meeting_app,
_browser_tabs and the per-platform matchers built on it) to avoid touching
real NSWorkspace / CoreAudio. Audio capture is exercised by hand on a real
Mac via the smoke test in mac/build.sh + scripts/install-mac.sh, and the
browser probe runs for real once at the bottom of this file.
"""
from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import patch

import pytest


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Mac platform module requires pyobjc (darwin only)",
)


@pytest.fixture
def darwin_module():
    from witnessd import _platform_darwin
    return _platform_darwin


def test_no_mic_means_no_detection(darwin_module):
    with patch.object(darwin_module, "_probe_mic", return_value=(False, None)):
        assert darwin_module.DarwinPlatform().detect_meeting() is None


def test_zoom_app_running_with_mic_active(darwin_module):
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app",
                      return_value=("zoom", "zoom.us", 222)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "zoom"
    assert det.title == "zoom.us"
    assert det.application_pid == 222
    assert det.source == "coreaudio"
    # Daemon dedup uses .key — confirm shape matches what test_detect.py
    # asserts for Linux non-Meet detections.
    assert det.key == "zoom:zoom.us:222:None"


def test_teams_app_running_classified_as_teams(darwin_module):
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app",
                      return_value=("teams", "Microsoft Teams", 333)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "teams"
    assert det.application_pid == 333


def test_meet_tab_in_chrome_when_no_meeting_app(darwin_module):
    """No Zoom/Teams app but a Meet tab is open in some browser window —
    detect with that room. Tab focus is irrelevant."""
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_any_meet_room_open",
                      return_value=("abc-defg-hij", 444)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "meet"
    assert det.title == "Meet - abc-defg-hij"
    # Meet's .key extracts the room code so a tab reload doesn't rotate
    # the daemon session.
    assert det.key == "meet:abc-defg-hij"


def test_active_room_pinned_when_still_open(darwin_module):
    """If a session is already running for meet:<room> and that room is
    still open somewhere, prefer it over whatever _any_meet_room_open
    would return — keeps a session locked to the original room when
    multiple Meet tabs exist."""
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_meet_room_open_anywhere", return_value=555), \
         patch.object(darwin_module, "_any_meet_room_open",
                      return_value=("other-room-zzz", 444)) as any_mock:
        det = darwin_module.DarwinPlatform().detect_meeting(
            active_key="meet:abc-defg-hij"
        )
    assert det is not None
    assert det.title == "Meet - abc-defg-hij"
    assert det.application_pid == 555
    # _any_meet_room_open should not have been consulted at all when
    # the active room was found.
    any_mock.assert_not_called()


def test_active_room_gone_falls_back_to_any_tab(darwin_module):
    """Active room's tab was closed; fall back to whatever Meet tab is
    open. Daemon will see the key change and rotate the session."""
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_meet_room_open_anywhere", return_value=None), \
         patch.object(darwin_module, "_any_meet_room_open",
                      return_value=("new-room-xyz", 444)):
        det = darwin_module.DarwinPlatform().detect_meeting(
            active_key="meet:old-room-abc"
        )
    assert det is not None
    assert det.title == "Meet - new-room-xyz"


def test_unknown_app_with_mic_returns_none(darwin_module):
    """Mic is active but neither a Zoom/Teams app nor a Meet/Teams tab —
    don't fire. Mirrors Linux ignoring random apps holding the mic."""
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_any_meet_room_open", return_value=None), \
         patch.object(darwin_module, "_any_teams_meeting_open", return_value=None), \
         patch.object(darwin_module, "_any_teams_call_by_title", return_value=None):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is None


TEAMS_ID = "19:meeting_nwq4zmy0mjityteymy00@thread.v2"


def test_teams_tab_in_browser_when_no_meeting_app(darwin_module):
    """Teams meetings here are joined in the browser with no desktop app
    installed — before this path existed they produced no detection at all
    and went unrecorded."""
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_any_meet_room_open", return_value=None), \
         patch.object(darwin_module, "_any_teams_meeting_open",
                      return_value=(TEAMS_ID, 666)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "teams"
    assert det.application_pid == 666
    assert det.conference_id == TEAMS_ID
    # Identity is the full thread id; the title is only a display label.
    assert det.key == f"teams:{TEAMS_ID}"
    assert det.title == "Teams - nwq4zmy0mjit"


def test_meet_wins_over_a_teams_tab(darwin_module):
    """Both a live Meet call and a lingering Teams meeting tab are open.
    Meet is checked first so a stale Teams tab can't divert the recording."""
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_any_meet_room_open",
                      return_value=("abc-defg-hij", 444)), \
         patch.object(darwin_module, "_any_teams_meeting_open",
                      return_value=(TEAMS_ID, 666)) as teams_mock:
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "meet"
    teams_mock.assert_not_called()


def test_active_teams_meeting_pinned_when_still_open(darwin_module):
    """Continuity for Teams mirrors Meet: stay locked to the call we're
    already recording rather than re-picking from whatever tabs exist."""
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_teams_meeting_open_anywhere",
                      return_value=777), \
         patch.object(darwin_module, "_any_teams_meeting_open",
                      return_value=("19:meeting_other@thread.v2", 666)) as any_mock:
        det = darwin_module.DarwinPlatform().detect_meeting(
            active_key=f"teams:{TEAMS_ID}"
        )
    assert det is not None
    assert det.conference_id == TEAMS_ID
    assert det.application_pid == 777
    any_mock.assert_not_called()


def test_legacy_teams_desktop_key_does_not_trigger_browser_continuity(darwin_module):
    """Keys minted by the desktop-app branch look like
    `teams:Microsoft Teams:333:None` — the part after the first colon is not
    a thread id, so the browser continuity lookup must not run on it."""
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_any_meet_room_open", return_value=None), \
         patch.object(darwin_module, "_teams_meeting_open_anywhere") as pin_mock, \
         patch.object(darwin_module, "_teams_app_open_anywhere") as app_mock, \
         patch.object(darwin_module, "_any_teams_meeting_open", return_value=None), \
         patch.object(darwin_module, "_any_teams_call_by_title", return_value=None):
        det = darwin_module.DarwinPlatform().detect_meeting(
            active_key="teams:Microsoft Teams:333:None"
        )
    assert det is None
    pin_mock.assert_not_called()
    # Nor the title-path continuity: `Microsoft Teams` is the app naming
    # itself, not a meeting subject, so this key has no subject to resume.
    app_mock.assert_not_called()


def test_matchers_share_one_tab_scan(darwin_module):
    """Every browser instance is scanned once per tick and every matcher reads
    the same result — a per-matcher lookup costs a timeout-bounded subprocess
    each and the tick has to fit inside the poll interval."""
    tabs = [
        ("https://mail.google.com/", "Inbox (42)", 100),
        (f"https://teams.microsoft.com/v2/#/meet/{TEAMS_ID}", "Sync | Microsoft Teams", 100),
        ("https://meet.google.com/abc-defg-hij?authuser=0", "Meet", 100),
    ]
    with patch.object(darwin_module, "_browser_tabs",
                      return_value=(tabs, False)):
        assert darwin_module._any_meet_room_open() == ("abc-defg-hij", 100)
        assert darwin_module._any_teams_meeting_open() == (TEAMS_ID, 100)
        assert darwin_module._meet_room_open_anywhere("abc-defg-hij") == 100
        assert darwin_module._meet_room_open_anywhere("zzz-zzzz-zzz") is None
        assert darwin_module._teams_meeting_open_anywhere(TEAMS_ID) == 100
        assert darwin_module._teams_meeting_open_anywhere("19:meeting_x@thread.v2") is None
        assert darwin_module._any_teams_call_by_title() == ("Sync", 100)
        assert darwin_module._teams_app_open_anywhere() == 100


def test_no_match_plus_stalled_probe_is_inconclusive(darwin_module):
    """A probe that answered nothing, with no match left, is not evidence the
    call ended — ProbeFailed keeps the daemon on its last-known state instead
    of advancing the window-gone timer."""
    from witnessd.detect import ProbeFailed

    with patch.object(darwin_module, "_browser_tabs", return_value=([], True)):
        for probe in (
            lambda: darwin_module._any_meet_room_open(),
            lambda: darwin_module._any_teams_meeting_open(),
            lambda: darwin_module._meet_room_open_anywhere("abc-defg-hij"),
            lambda: darwin_module._teams_meeting_open_anywhere(TEAMS_ID),
            lambda: darwin_module._any_teams_call_by_title(),
            lambda: darwin_module._teams_app_open_anywhere(),
        ):
            with pytest.raises(ProbeFailed):
                probe()


def test_a_hit_outranks_a_stalled_probe(darwin_module):
    """One browser answered and another did not: the hit stands rather than
    the tick going indeterminate."""
    tabs = [("https://meet.google.com/abc-defg-hij", "Meet", 100)]
    with patch.object(darwin_module, "_browser_tabs", return_value=(tabs, True)):
        assert darwin_module._any_meet_room_open() == ("abc-defg-hij", 100)


def test_teams_title_is_short_enough_for_a_folder_name(darwin_module):
    """The title becomes the folder slug when no calendar event matches, and
    a raw thread id is ~60 opaque characters."""
    assert darwin_module._teams_title(TEAMS_ID) == "Teams - nwq4zmy0mjit"
    assert darwin_module._teams_title("19:meeting_@thread.v2") == "Teams meeting"


# Captured off a real call: an invite-link join leaves the tab at `/v2/` with
# the id nowhere in the URL, so the title is the only evidence it exists.
SPA_URL = "https://teams.microsoft.com/v2/"
SPA_TITLE = "(1) GitHub <> EquipmentShare | Microsoft Teams"
SPA_KEY = "teams:GitHub <> EquipmentShare:100:None"


def test_teams_spa_call_detected_by_tab_title(darwin_module):
    """The reason this path exists: an invite-link join that shows no id
    anywhere produced no detection at all and went unrecorded."""
    tabs = [(SPA_URL, SPA_TITLE, 100)]
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_browser_tabs", return_value=(tabs, False)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "teams"
    assert det.application_pid == 100
    assert det.title == "GitHub <> EquipmentShare"
    # No id to be had — correlation has to score this on the subject, and a
    # synthetic id would disqualify the matching invite outright.
    assert det.conference_id is None
    assert det.source == "window"
    assert det.key == SPA_KEY


def test_teams_id_in_the_url_outranks_the_title(darwin_module):
    """A title is a guess; an id in a URL is proof. When a call exposes one,
    identity must come from the id so the key survives a title change."""
    tabs = [
        (f"https://teams.microsoft.com/v2/?meetingjoin=true#/meet/{TEAMS_ID}",
         SPA_TITLE, 100),
    ]
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_browser_tabs", return_value=(tabs, False)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.conference_id == TEAMS_ID
    assert det.key == f"teams:{TEAMS_ID}"


def test_meet_outranks_a_title_only_teams_tab(darwin_module):
    """Same precedence the id path gets: a live Meet call is not diverted by
    a Teams tab that merely looks like a meeting."""
    tabs = [
        (SPA_URL, SPA_TITLE, 100),
        ("https://meet.google.com/abc-defg-hij", "Meet", 100),
    ]
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_browser_tabs", return_value=(tabs, False)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "meet"


def test_title_keyed_session_survives_a_mid_call_tab_change(darwin_module):
    """Clicking Chat during a call retitles the same tab, which a title-strict
    check would read as the meeting ending."""
    tabs = [(SPA_URL, "(2) Chat | Microsoft Teams", 100)]
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_browser_tabs", return_value=(tabs, False)):
        det = darwin_module.DarwinPlatform().detect_meeting(active_key=SPA_KEY)
    assert det is not None
    assert det.title == "GitHub <> EquipmentShare"
    assert det.key == SPA_KEY


def test_title_keyed_continuity_ends_when_teams_is_gone(darwin_module):
    """The looser continuity check still needs Teams on screen — once the tab
    is closed there is nothing to resume, whatever holds the mic."""
    tabs = [("https://mail.google.com/", "Inbox", 100)]
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_browser_tabs", return_value=(tabs, False)):
        det = darwin_module.DarwinPlatform().detect_meeting(active_key=SPA_KEY)
    assert det is None


def test_subject_with_a_colon_survives_the_key_round_trip(darwin_module):
    """Peel fields off the right, or a subject with colons rotates the session
    every tick."""
    assert darwin_module._teams_subject_from_key(
        "Q3 Planning: phase 2:100:None"
    ) == "Q3 Planning: phase 2"


MEET_TAB = ("https://meet.google.com/abc-defg-hij", "Meet")


def _instance(pid, tabs, bundle_id="com.google.Chrome", errors=()):
    return {
        "pid": pid,
        "bundle_id": bundle_id,
        "tabs": [list(t) for t in tabs],
        "errors": list(errors),
    }


def _probe_result(instances, returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=["python", "-m", "witnessd._browser_probe"],
        returncode=returncode,
        stdout=json.dumps({"instances": instances}),
        stderr=stderr,
    )


def test_tabs_come_from_every_browser_instance_not_just_one(darwin_module):
    """Instances are not candidates to choose between: every one gets asked,
    and each tab carries the pid of the instance it came from."""
    headless = _instance(54229, [("http://localhost:9333/json", "devtools")])
    real = _instance(75258, [MEET_TAB])
    with patch.object(darwin_module.subprocess, "run",
                      return_value=_probe_result([headless, real])):
        tabs, inconclusive = darwin_module._browser_tabs()
    assert inconclusive is False
    assert (*MEET_TAB, 75258) in tabs
    assert ("http://localhost:9333/json", "devtools", 54229) in tabs


def test_a_meeting_in_the_second_instance_still_fires_a_detection(darwin_module):
    """End to end: the detection fires and is attributed to the browser that
    actually holds the tab."""
    headless = _instance(54229, [("http://localhost:9333/json", "devtools")])
    real = _instance(75258, [MEET_TAB])
    with patch.object(darwin_module, "_probe_mic", return_value=(True, None)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module.subprocess, "run",
                      return_value=_probe_result([headless, real])):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "meet"
    assert det.conference_id == "abc-defg-hij"
    assert det.application_pid == 75258


def test_a_probe_that_crashes_is_not_read_as_no_meeting(darwin_module):
    """A dead probe used to return the same empty list as an idle machine."""
    crashed = _probe_result([], returncode=1, stderr="ModuleNotFoundError")
    with patch.object(darwin_module.subprocess, "run", return_value=crashed):
        assert darwin_module._browser_tabs() == ([], True)


def test_a_probe_timeout_is_inconclusive(darwin_module):
    with patch.object(darwin_module.subprocess, "run",
                      side_effect=subprocess.TimeoutExpired("probe", 4)):
        assert darwin_module._browser_tabs() == ([], True)


def test_unparseable_probe_output_is_inconclusive(darwin_module):
    garbage = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="<html>proxy error</html>", stderr="")
    with patch.object(darwin_module.subprocess, "run", return_value=garbage):
        assert darwin_module._browser_tabs() == ([], True)


def test_no_browser_running_is_a_conclusive_no(darwin_module):
    """Zero instances is an answer, not a failure: calling it indeterminate
    would leave a finished session unfinalizable."""
    with patch.object(darwin_module.subprocess, "run",
                      return_value=_probe_result([])):
        assert darwin_module._browser_tabs() == ([], False)


def test_one_silent_instance_does_not_discard_the_one_that_answered(darwin_module):
    """Otherwise one permanently unhappy browser wedges detection for good."""
    mute = _instance(54229, [], errors=["pid 54229 windows: errAEEventNotPermitted"])
    real = _instance(75258, [MEET_TAB])
    with patch.object(darwin_module.subprocess, "run",
                      return_value=_probe_result([mute, real])):
        tabs, inconclusive = darwin_module._browser_tabs()
    assert inconclusive is False
    assert (*MEET_TAB, 75258) in tabs


def test_every_instance_failing_is_inconclusive(darwin_module):
    """Learning nothing differs from learning that no meeting is open."""
    chrome = _instance(54229, [], errors=["denied"])
    safari = _instance(75258, [], errors=["denied"], bundle_id="com.apple.Safari")
    with patch.object(darwin_module.subprocess, "run",
                      return_value=_probe_result([chrome, safari])):
        assert darwin_module._browser_tabs() == ([], True)


def test_a_title_holding_control_characters_is_now_inert(darwin_module):
    """A title that used to be able to close its own record and name a Meet
    room the user never joined is, with JSON framing, just text."""
    hostile = "Inbox\x1f\x1ehttps://meet.google.com/zzz-zzzz-zzz"
    with patch.object(darwin_module.subprocess, "run",
                      return_value=_probe_result(
                          [_instance(100, [("https://mail.google.com/", hostile)])])):
        tabs, inconclusive = darwin_module._browser_tabs()
    assert inconclusive is False
    assert tabs == [("https://mail.google.com/", hostile, 100)]


class _FakeTab:
    def __init__(self, url, title):
        self._url, self._title = url, title

    def URL(self):
        return self._url

    def title(self):
        return self._title

    def name(self):
        return self._title


class _FakeWindow:
    def __init__(self, tabs, broken=False):
        self._tabs, self._broken = tabs, broken

    def tabs(self):
        if self._broken:
            raise RuntimeError("window closed mid-enumeration")
        return self._tabs


class _FakeApp:
    def __init__(self, windows):
        self._windows = windows
        self.timeout = None

    def setTimeout_(self, ticks):
        self.timeout = ticks

    def windows(self):
        return self._windows


class _FakeSB:
    def __init__(self, by_pid):
        self._by_pid = by_pid

    def applicationWithProcessIdentifier_(self, pid):
        return self._by_pid.get(pid)


def test_browsers_are_addressed_by_pid_with_their_own_title_property():
    """Chrome's tab property is `title` and Safari's is `name`; the wrong one
    fails only at runtime, which reads as an empty browser."""
    from witnessd import _browser_probe

    assert _browser_probe.BROWSER_BUNDLES == {
        "com.google.Chrome": "title",
        "com.apple.Safari": "name",
    }
    tab = _FakeTab("https://example.com/", "Example")
    for title_prop in ("title", "name"):
        tabs, errors = _browser_probe._probe_instance(
            _FakeSB({100: _FakeApp([_FakeWindow([tab])])}), 100, title_prop)
        assert tabs == [["https://example.com/", "Example"]]
        assert errors == []


def test_one_bad_window_does_not_cost_an_instance_its_other_tabs():
    """Safari's download panel has no `tabs`, and any window can close between
    the enumeration and the read."""
    from witnessd import _browser_probe

    good = _FakeWindow([_FakeTab(*MEET_TAB)])
    app = _FakeApp([_FakeWindow([], broken=True), good])
    tabs, errors = _browser_probe._probe_instance(_FakeSB({100: app}), 100, "title")
    assert tabs == [list(MEET_TAB)]
    assert len(errors) == 1


def test_an_instance_with_no_scripting_interface_is_reported_not_raised():
    from witnessd import _browser_probe

    tabs, errors = _browser_probe._probe_instance(_FakeSB({}), 54229, "title")
    assert tabs == []
    assert errors and "54229" in errors[0]


def test_each_instance_gets_a_bounded_apple_event_timeout(darwin_module):
    """A wedged browser must not starve the instances behind it."""
    from witnessd import _browser_probe

    app = _FakeApp([])
    _browser_probe._probe_instance(_FakeSB({100: app}), 100, "title")
    assert app.timeout == _browser_probe.AE_TIMEOUT_TICKS
    assert _browser_probe.AE_TIMEOUT_TICKS / 60 < darwin_module._PROBE_TIMEOUT_S


def test_the_probe_module_actually_runs_on_this_mac():
    """The fakes above cannot catch a missing ScriptingBridge."""
    out = subprocess.run(
        [sys.executable, "-m", "witnessd._browser_probe"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert isinstance(payload["instances"], list)
    for inst in payload["instances"]:
        assert inst["bundle_id"] in {"com.google.Chrome", "com.apple.Safari"}


def test_meet_url_regex_extracts_room(darwin_module):
    """The MEET_URL pattern is shared across the Meet-tab probes, so it's
    worth a direct test independent of AppleScript invocation."""
    m = darwin_module._MEET_URL.search("https://meet.google.com/xyz-abcd-efg?authuser=0")
    assert m is not None
    assert m.group(1) == "xyz-abcd-efg"
    assert darwin_module._MEET_URL.search("https://meet.google.com/") is None
    assert darwin_module._MEET_URL.search("https://example.com/meet/x") is None


def test_pump_tap_stderr_extracts_rate(darwin_module):
    """The stderr pump forwards the tap's diagnostics and captures the
    reported sample rate — the value plan_capture feeds to ffmpeg's -ar."""
    import io
    import threading

    stderr = io.BytesIO(
        b"witness-audiotap: warning: could not set aggregate rate to 48000.0 "
        b"(OSStatus=1852797029); falling back to device rate 44100.0\n"
        b"witness-audiotap: rate=44100\n"
        b"witness-audiotap: aggregate input streams - buffers=2 [0:ch=1] [1:ch=2]\n"
    )
    holder: dict = {}
    ready = threading.Event()
    darwin_module._pump_tap_stderr(stderr, holder, ready)
    assert ready.is_set()
    assert holder["rate"] == 44100


def test_pump_tap_stderr_unblocks_without_rate(darwin_module):
    """If the tap dies before reporting a rate, EOF still sets the event so
    the launcher's wait() returns and it can abort instead of hanging."""
    import io
    import threading

    stderr = io.BytesIO(b"witness-audiotap: FATAL no audio frames 3s after start\n")
    holder: dict = {}
    ready = threading.Event()
    darwin_module._pump_tap_stderr(stderr, holder, ready)
    assert ready.is_set()
    assert "rate" not in holder


def test_pump_tap_stderr_prefers_output_rate_over_device_rate(darwin_module):
    """The tap reports two rates: `rate=` is the fixed rate it emits (what
    ffmpeg's -ar must be) and `devrate=` is the device's own rate, which is
    diagnostic and can change mid-capture. Parsing `devrate=` as the output
    rate would pitch-shift every recording made on a 16 kHz Bluetooth mic, so
    pin the distinction."""
    import io
    import threading

    stderr = io.BytesIO(
        b"witness-audiotap: rate=48000\n"
        b"witness-audiotap: devrate=16000 (resampling to 48000)\n"
    )
    holder: dict = {}
    ready = threading.Event()
    darwin_module._pump_tap_stderr(stderr, holder, ready)
    assert holder["rate"] == darwin_module._TAP_OUTPUT_RATE == 48000


def test_devrate_line_alone_is_not_read_as_a_rate(darwin_module):
    """Order-independence for the above: a `devrate=` line must never satisfy
    the handshake on its own, even if it arrives first."""
    import io
    import threading

    stderr = io.BytesIO(b"witness-audiotap: devrate=16000\n")
    holder: dict = {}
    ready = threading.Event()
    darwin_module._pump_tap_stderr(stderr, holder, ready)
    assert "rate" not in holder


CHROME_PID = 75258
OTHER_BROWSER_PID = 41000
CHROME_HOLDERS = frozenset({"com.google.chrome.helper"})


def _bundles(pid):
    return "com.google.chrome" if pid == CHROME_PID else "com.apple.safari"


def test_stale_meet_tab_loses_to_the_browser_holding_the_mic(darwin_module):
    """The 2026-09-17 misattribution, in its cross-browser form: a Meet tab
    left open from an earlier call, while a live Teams call in a different
    browser holds the input device. Meet still matches first, so only the
    mic can break the tie."""
    with patch.object(darwin_module, "_probe_mic",
                      return_value=(True, CHROME_HOLDERS)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_bundle_id_for_pid", side_effect=_bundles), \
         patch.object(darwin_module, "_any_meet_room_open",
                      return_value=("sau-vorr-qsz", OTHER_BROWSER_PID)), \
         patch.object(darwin_module, "_any_teams_meeting_open",
                      return_value=(TEAMS_ID, CHROME_PID)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "teams"
    assert det.conference_id == TEAMS_ID


def test_title_only_teams_call_outranks_a_stale_meet_tab(darwin_module):
    """The same tie, resolved for the id-less Teams tab from b499b33 — the
    path that made this misattribution reachable in the first place."""
    with patch.object(darwin_module, "_probe_mic",
                      return_value=(True, CHROME_HOLDERS)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_bundle_id_for_pid", side_effect=_bundles), \
         patch.object(darwin_module, "_any_meet_room_open",
                      return_value=("sau-vorr-qsz", OTHER_BROWSER_PID)), \
         patch.object(darwin_module, "_any_teams_meeting_open", return_value=None), \
         patch.object(darwin_module, "_any_teams_call_by_title",
                      return_value=("Roadmap sync", CHROME_PID)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "teams"
    assert det.title == "Roadmap sync"


def test_live_meet_tab_still_wins_when_its_browser_holds_the_mic(darwin_module):
    """The tiebreak must not invert the common case: a real Meet call plus a
    lingering Teams tab still records the Meet call."""
    with patch.object(darwin_module, "_probe_mic",
                      return_value=(True, CHROME_HOLDERS)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_bundle_id_for_pid", side_effect=_bundles), \
         patch.object(darwin_module, "_any_meet_room_open",
                      return_value=("pyf-kzrd-gsx", CHROME_PID)), \
         patch.object(darwin_module, "_any_teams_meeting_open",
                      return_value=(TEAMS_ID, OTHER_BROWSER_PID)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "meet"
    assert det.conference_id == "pyf-kzrd-gsx"


def test_no_candidate_holds_the_mic_keeps_the_first_match(darwin_module):
    """Something unsupported owns the input device — a voice memo, say.
    Nothing is attributable, and we keep the old first-match answer rather
    than returning None: a mislabeled recording can be repaired afterwards
    and a meeting we declined to record cannot."""
    with patch.object(darwin_module, "_probe_mic",
                      return_value=(True, frozenset({"com.apple.voicememos"}))), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_bundle_id_for_pid", side_effect=_bundles), \
         patch.object(darwin_module, "_any_meet_room_open",
                      return_value=("sau-vorr-qsz", CHROME_PID)), \
         patch.object(darwin_module, "_any_teams_meeting_open", return_value=None), \
         patch.object(darwin_module, "_any_teams_call_by_title", return_value=None):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "meet"


def test_same_browser_stale_meet_tab_is_still_unresolved(darwin_module):
    """Known limitation, pinned so a future fix visibly flips it. Chrome mixes
    every tab's audio in one helper process, so when the stale Meet tab and
    the live Teams call are both in Chrome, CoreAudio attributes them
    identically and the Meet tab still wins on order. Closing this needs a
    per-tab signal, which no CoreAudio property carries."""
    with patch.object(darwin_module, "_probe_mic",
                      return_value=(True, CHROME_HOLDERS)), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_bundle_id_for_pid",
                      return_value="com.google.chrome"), \
         patch.object(darwin_module, "_any_meet_room_open",
                      return_value=("sau-vorr-qsz", CHROME_PID)), \
         patch.object(darwin_module, "_any_teams_meeting_open", return_value=None), \
         patch.object(darwin_module, "_any_teams_call_by_title",
                      return_value=("Live Teams call", CHROME_PID)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "meet"


def test_chrome_helper_bundle_resolves_to_the_browser(darwin_module):
    """Chrome captures in `com.google.Chrome.helper`, never in the pid
    NSWorkspace reports, so a pid equality test would answer False for
    every real Chrome call."""
    with patch.object(darwin_module, "_bundle_id_for_pid",
                      return_value="com.google.chrome"):
        assert darwin_module._browser_holds_input(CHROME_PID, CHROME_HOLDERS)


def test_safari_is_never_attributed(darwin_module):
    """Safari's audio runs in a launchd XPC service shared by every WebKit
    client, naming neither Safari nor the tab, so it stays unattributed and
    leaves the matcher order to decide."""
    with patch.object(darwin_module, "_bundle_id_for_pid",
                      return_value="com.apple.safari"):
        assert not darwin_module._browser_holds_input(
            999, frozenset({"com.apple.webkit.gpu"})
        )


SLACK_HOLDERS = frozenset({"com.tinyspeck.slackmacgap.helper"})
SLACK_PID = 812


def _huddle_patches(darwin_module, holders=SLACK_HOLDERS, app=None, meet=None):
    return (
        patch.object(darwin_module, "_probe_mic", return_value=(True, holders)),
        patch.object(darwin_module, "_slack_pid", return_value=SLACK_PID),
        patch.object(darwin_module, "_running_meeting_app", return_value=app),
        patch.object(darwin_module, "_bundle_id_for_pid", side_effect=_bundles),
        patch.object(darwin_module, "_any_meet_room_open", return_value=meet),
        patch.object(darwin_module, "_meet_room_open_anywhere",
                     return_value=meet[1] if meet else None),
        patch.object(darwin_module, "_any_teams_meeting_open", return_value=None),
        patch.object(darwin_module, "_any_teams_call_by_title", return_value=None),
    )


def _detect(darwin_module, active_key=None, **kw):
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in _huddle_patches(darwin_module, **kw):
            stack.enter_context(p)
        return darwin_module.DarwinPlatform().detect_meeting(active_key=active_key)


def test_slack_holding_the_mic_is_a_huddle(darwin_module):
    det = _detect(darwin_module)
    assert det is not None
    assert det.platform == "slack"
    assert det.application_pid == SLACK_PID
    assert det.key == f"slack:Slack huddle:{SLACK_PID}:None"


def test_slack_without_the_mic_is_not_a_huddle(darwin_module):
    """Slack is always open; only its input stream means a call."""
    assert _detect(darwin_module, holders=CHROME_HOLDERS) is None


def test_idle_zoom_does_not_claim_a_huddle(darwin_module):
    """The Zoom branch fires on Zoom merely running, which it nearly always is."""
    det = _detect(darwin_module, app=("zoom", "zoom.us", 222))
    assert det.platform == "slack"


def test_zoom_holding_the_mic_keeps_the_call(darwin_module):
    det = _detect(
        darwin_module,
        holders=SLACK_HOLDERS | {"us.zoom.cpthost"},
        app=("zoom", "zoom.us", 222),
    )
    assert det.platform == "zoom"


def test_a_running_huddle_survives_zoom_taking_the_mic_too(darwin_module):
    """Joining Zoom mid-huddle must not rotate the huddle's folder."""
    det = _detect(
        darwin_module,
        active_key=f"slack:Slack huddle:{SLACK_PID}:None",
        holders=SLACK_HOLDERS | {"us.zoom.xos"},
        app=("zoom", "zoom.us", 222),
    )
    assert det.platform == "slack"


def test_stale_meet_tab_does_not_claim_a_huddle(darwin_module):
    det = _detect(darwin_module, meet=("sau-vorr-qsz", CHROME_PID))
    assert det.platform == "slack"


def test_meet_session_ends_when_the_mic_moves_to_a_huddle(darwin_module):
    """Continuity pins a Meet room only while its browser still holds input."""
    det = _detect(
        darwin_module,
        active_key="meet:sau-vorr-qsz",
        meet=("sau-vorr-qsz", CHROME_PID),
    )
    assert det.platform == "slack"


def test_live_meet_call_beats_a_concurrent_huddle(darwin_module):
    det = _detect(
        darwin_module,
        active_key="meet:sau-vorr-qsz",
        holders=SLACK_HOLDERS | CHROME_HOLDERS,
        meet=("sau-vorr-qsz", CHROME_PID),
    )
    assert det.platform == "meet"
