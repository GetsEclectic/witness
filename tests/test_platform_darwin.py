"""Mac platform tests. Skipped on non-Mac systems.

Patches the three helpers that probe the system (_is_mic_running,
_running_meeting_app, _front_browser_meet) to avoid touching real
NSWorkspace / osascript / CoreAudio. The DarwinPlatform.detect_meeting
logic is what's under test — the helpers are exercised by hand on a
real Mac via the smoke test in mac/build.sh + scripts/install-mac.sh.
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
    """When no Zoom/Teams app is running but the front browser tab is on
    a Meet URL, we still detect a meeting and extract the room code."""
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_front_browser_meet",
                      return_value=("abc-defg-hij", 444)):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is not None
    assert det.platform == "meet"
    assert det.title == "Meet - abc-defg-hij"
    # Meet's .key extracts the room code so a tab reload doesn't rotate
    # the daemon session.
    assert det.key == "meet:abc-defg-hij"


def test_unknown_app_with_mic_returns_none(darwin_module):
    """Mic is active but neither a Zoom/Teams app nor a Meet tab — don't
    fire. Mirrors Linux ignoring random apps holding the mic."""
    with patch.object(darwin_module, "_is_mic_running", return_value=True), \
         patch.object(darwin_module, "_running_meeting_app", return_value=None), \
         patch.object(darwin_module, "_front_browser_meet", return_value=None):
        det = darwin_module.DarwinPlatform().detect_meeting()
    assert det is None


def test_meet_url_regex_extracts_room(darwin_module):
    """The MEET_URL pattern is what powers _front_browser_meet, so it's
    worth a direct test independent of AppleScript invocation."""
    m = darwin_module._MEET_URL.search("https://meet.google.com/xyz-abcd-efg?authuser=0")
    assert m is not None
    assert m.group(1) == "xyz-abcd-efg"
    assert darwin_module._MEET_URL.search("https://meet.google.com/") is None
    assert darwin_module._MEET_URL.search("https://example.com/meet/x") is None
