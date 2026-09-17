"""Mac platform tests. Skipped on non-Mac systems.

Patches the helpers that probe the system (_probe_mic,
_running_meeting_app, _browser_tabs and the per-platform matchers
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
    """Chrome and Safari are scanned once per tick and every matcher reads
    the same result — a per-matcher AppleScript costs a 3s-timeout
    subprocess each and the tick has to fit inside the poll interval."""
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
    """A stalled osascript with no match left is not evidence the call
    ended — ProbeFailed keeps the daemon on its last-known state instead of
    advancing the window-gone timer."""
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
    """Chrome answered and Safari stalled: the Chrome hit stands rather
    than the tick going indeterminate."""
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


def test_the_script_strips_delimiters_out_of_page_controlled_text(darwin_module):
    """A title holding the record delimiter would close its own record, and the
    remainder would be read as another tab's URL — a page naming a Meet room
    the user never joined. Only the script can fix it."""
    script = darwin_module._TAB_SCRIPT.format(app="Google Chrome", title_prop="title")
    assert "my scrub(URL of t)" in script
    assert "my scrub(title of t)" in script
    # Both delimiters, replaced in one pass over the text items.
    assert "{(ASCII character 30), (ASCII character 31)}" in script


def test_scrub_removes_both_delimiters_in_real_applescript(darwin_module):
    """Only as good as AppleScript's multi-delimiter `text items`."""
    import subprocess

    script = darwin_module._TAB_SCRIPT.format(app="Finder", title_prop="name")
    handler = script.split("tell application")[0]
    probe = (
        'set probe to "a" & (ASCII character 31) & "b" '
        '& (ASCII character 30) & "c"\nreturn my scrub(probe)'
    )
    out = subprocess.check_output(
        ["osascript", "-e", handler + probe], text=True, timeout=10,
    )
    assert "\x1f" not in out and "\x1e" not in out
    assert out.strip() == "a b c"


def test_a_malformed_record_is_dropped_not_read_as_a_url(darwin_module):
    """Belt to the scrub's braces: a record with no field separator is
    malformed, not a tab."""
    script_out = f"{SPA_URL}\x1f{SPA_TITLE}\x1ehttps://meet.google.com/zzz-zzzz-zzz\x1e"
    with patch.object(darwin_module.subprocess, "check_output",
                      return_value=script_out), \
         patch.object(darwin_module, "_bundle_pid", return_value=100):
        tabs, timed_out = darwin_module._browser_tabs()
    assert timed_out is False
    assert {url for url, _title, _pid in tabs} == {SPA_URL}


def test_browser_tabs_parses_url_and_title_pairs(darwin_module):
    """The osascript contract: US between a tab's URL and title, RS between
    tabs, and a trailing RS from the last tab is not an empty tab."""
    script_out = (
        f"{SPA_URL}\x1f{SPA_TITLE}\x1e"
        "https://meet.google.com/abc-defg-hij\x1fMeet\x1e"
    )
    with patch.object(darwin_module.subprocess, "check_output",
                      return_value=script_out), \
         patch.object(darwin_module, "_bundle_pid", return_value=100):
        tabs, _ = darwin_module._browser_tabs()
    assert (SPA_URL, SPA_TITLE, 100) in tabs
    assert ("https://meet.google.com/abc-defg-hij", "Meet", 100) in tabs


def test_each_browser_is_asked_for_its_own_title_property(darwin_module):
    """One word for both compiles fine and fails only at runtime, where a
    failed probe is indistinguishable from "no tabs"."""
    assert darwin_module._TAB_TITLE_PROP["Google Chrome"] == "title"
    assert darwin_module._TAB_TITLE_PROP["Safari"] == "name"
    for app, prop in darwin_module._TAB_TITLE_PROP.items():
        script = darwin_module._TAB_SCRIPT.format(app=app, title_prop=prop)
        assert f"({prop} of t)" in script
        assert f'application "{app}"' in script


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
    """Something unsupported owns the input device — a Slack huddle, say.
    Nothing is attributable, and we keep the old first-match answer rather
    than returning None: a mislabeled recording can be repaired afterwards
    and a meeting we declined to record cannot."""
    with patch.object(darwin_module, "_probe_mic",
                      return_value=(True, frozenset({"com.tinyspeck.slackmacgap"}))), \
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
