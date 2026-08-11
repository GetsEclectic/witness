"""Mac platform tests. Skipped on non-Mac systems.

Patches the helpers that probe the system (_is_mic_running,
_running_meeting_app, _browser_tab_urls and the per-platform matchers
built on it) to avoid touching real NSWorkspace / osascript / CoreAudio.
The DarwinPlatform.detect_meeting logic is what's under test — the
helpers are exercised by hand on a real Mac via the smoke test in
mac/build.sh + scripts/install-mac.sh.
"""
from __future__ import annotations

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
    with patch.object(darwin_module, "_is_mic_running", return_value=False):
        assert darwin_module.DarwinPlatform().detect_meeting() is None


def test_zoom_app_running_with_mic_active(darwin_module):
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
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
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
         patch.object(darwin_module, "_running_meeting_app",
                      return_value=("teams", "Microsoft Teams", 333)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "teams"
    assert det.application_pid == 333


def test_meet_tab_in_chrome_when_no_meeting_app(darwin_module):
    """No Zoom/Teams app but a Meet tab is open in some browser window —
    detect with that room. Tab focus is irrelevant."""
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
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
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
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
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
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
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_any_meet_room_open", return_value=None), \
         patch.object(darwin_module, "_any_teams_meeting_open", return_value=None):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is None


TEAMS_ID = "19:meeting_nwq4zmy0mjityteymy00@thread.v2"


def test_teams_tab_in_browser_when_no_meeting_app(darwin_module):
    """Teams meetings here are joined in the browser with no desktop app
    installed — before this path existed they produced no detection at all
    and went unrecorded."""
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
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
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
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
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
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
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_any_meet_room_open", return_value=None), \
         patch.object(darwin_module, "_teams_meeting_open_anywhere") as pin_mock, \
         patch.object(darwin_module, "_any_teams_meeting_open", return_value=None):
        det = darwin_module.DarwinPlatform().detect_meeting(
            active_key="teams:Microsoft Teams:333:None"
        )
    assert det is None
    pin_mock.assert_not_called()


def test_matchers_share_one_tab_scan(darwin_module):
    """Chrome and Safari are scanned once per tick and every matcher reads
    the same result — a per-matcher AppleScript costs a 3s-timeout
    subprocess each and the tick has to fit inside the poll interval."""
    tabs = [
        ("https://mail.google.com/", 100),
        (f"https://teams.microsoft.com/v2/#/meet/{TEAMS_ID}", 100),
        ("https://meet.google.com/abc-defg-hij?authuser=0", 100),
    ]
    with patch.object(darwin_module, "_browser_tab_urls",
                      return_value=(tabs, False)):
        assert darwin_module._any_meet_room_open() == ("abc-defg-hij", 100)
        assert darwin_module._any_teams_meeting_open() == (TEAMS_ID, 100)
        assert darwin_module._meet_room_open_anywhere("abc-defg-hij") == 100
        assert darwin_module._meet_room_open_anywhere("zzz-zzzz-zzz") is None
        assert darwin_module._teams_meeting_open_anywhere(TEAMS_ID) == 100
        assert darwin_module._teams_meeting_open_anywhere("19:meeting_x@thread.v2") is None


def test_no_match_plus_stalled_probe_is_inconclusive(darwin_module):
    """A stalled osascript with no match left is not evidence the call
    ended — ProbeFailed keeps the daemon on its last-known state instead of
    advancing the window-gone timer."""
    from witnessd.detect import ProbeFailed

    with patch.object(darwin_module, "_browser_tab_urls", return_value=([], True)):
        for probe in (
            lambda: darwin_module._any_meet_room_open(),
            lambda: darwin_module._any_teams_meeting_open(),
            lambda: darwin_module._meet_room_open_anywhere("abc-defg-hij"),
            lambda: darwin_module._teams_meeting_open_anywhere(TEAMS_ID),
        ):
            with pytest.raises(ProbeFailed):
                probe()


def test_a_hit_outranks_a_stalled_probe(darwin_module):
    """Chrome answered and Safari stalled: the Chrome hit stands rather
    than the tick going indeterminate."""
    tabs = [("https://meet.google.com/abc-defg-hij", 100)]
    with patch.object(darwin_module, "_browser_tab_urls", return_value=(tabs, True)):
        assert darwin_module._any_meet_room_open() == ("abc-defg-hij", 100)


def test_teams_title_is_short_enough_for_a_folder_name(darwin_module):
    """The title becomes the folder slug when no calendar event matches, and
    a raw thread id is ~60 opaque characters."""
    assert darwin_module._teams_title(TEAMS_ID) == "Teams - nwq4zmy0mjit"
    assert darwin_module._teams_title("19:meeting_@thread.v2") == "Teams meeting"


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
