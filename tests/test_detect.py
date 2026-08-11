"""Pactl block parser + classifier — pure functions, no system calls."""
from __future__ import annotations

from witnessd.detect import (
    Detection,
    _classify,
    _is_live,
    _parse_pactl_blocks,
    teams_conference_ids,
    teams_conference_ids_from_tab,
    teams_id_kind,
)


# Both taken verbatim from a real EquipmentShare invite — every Teams invite
# names the same meeting twice, once per id space.
TEAMS_JOIN_URL = (
    "https://teams.microsoft.com/l/meetup-join/"
    "19%3ameeting_NWQ4ZmY0MjItYTEyMy00%40thread.v2/0"
    "?context=%7b%22Tid%22%3a%22abc%22%7d"
)
TEAMS_SHORT_URL = "https://teams.microsoft.com/meet/383440701254366?p=c75VTV1H9wWHwPLUEt"
TEAMS_ID = "19:meeting_nwq4zmy0mjityteymy00@thread.v2"
TEAMS_SHORT_ID = "383440701254366"


PACTL_OUTPUT = """\
Source Output #42
\tDriver: PipeWire
\tCorked: no
\tMute: no
\tProperties:
\t\tapplication.name = "Google Chrome"
\t\tapplication.process.binary = "chrome"
\t\tapplication.process.id = "12345"
\t\tmedia.name = "Meet - abc-defg-hij"
Source Output #43
\tDriver: PipeWire
\tCorked: yes
\tMute: no
\tProperties:
\t\tapplication.name = "Firefox"
\t\tmedia.name = "Some podcast"
"""


def test_parse_pactl_blocks_extracts_index_and_properties():
    blocks = _parse_pactl_blocks(PACTL_OUTPUT)
    assert len(blocks) == 2
    first = blocks[0]
    assert first["__index"] == "42"
    assert first["__corked"] == "no"
    assert first["__mute"] == "no"
    assert first["application.name"] == "Google Chrome"
    assert first["media.name"] == "Meet - abc-defg-hij"


def test_is_live_filters_corked_streams():
    blocks = _parse_pactl_blocks(PACTL_OUTPUT)
    assert _is_live(blocks[0]) is True
    assert _is_live(blocks[1]) is False  # corked


def test_is_live_filters_muted_streams():
    block = {"__corked": "no", "__mute": "yes"}
    assert _is_live(block) is False


def test_is_live_filters_classic_state_field():
    block = {"__state": "SUSPENDED"}
    assert _is_live(block) is False
    block = {"__state": "RUNNING"}
    assert _is_live(block) is True


def test_classify_recognizes_meet():
    block = {"media.name": "Meet - abc-defg-hij"}
    assert _classify(block) == ("meet", "Meet - abc-defg-hij")


def test_classify_recognizes_zoom_desktop():
    block = {"application.process.binary": "/opt/zoom/zoom"}
    assert _classify(block) == ("zoom", "Zoom Meeting")


def test_classify_recognizes_teams_web():
    block = {"media.name": "Microsoft Teams - Daily Standup"}
    assert _classify(block) == ("teams", "Microsoft Teams - Daily Standup")


def test_classify_returns_none_for_random_app():
    block = {"application.name": "Spotify", "media.name": "track"}
    assert _classify(block) is None


def test_detection_key_extracts_meet_room_code():
    d = Detection(
        platform="meet",
        title="Meet - abc-defg-hij",
        source="pactl",
    )
    assert d.key == "meet:abc-defg-hij"


def test_detection_key_uses_full_identity_for_non_meet():
    d = Detection(
        platform="zoom",
        title="Zoom Meeting",
        source="pactl",
        application_pid=99,
        source_output_index=7,
    )
    assert d.key == "zoom:Zoom Meeting:99:7"


def test_detection_key_prefers_conference_id():
    """A key built from pid/title rotates the session when the browser
    restarts or the tab title changes mid-call; the call's own ID doesn't."""
    d = Detection(
        platform="teams",
        title="Teams - nwq4zmy0mji",
        source="coreaudio",
        application_pid=99,
        conference_id=TEAMS_ID,
    )
    assert d.key == f"teams:{TEAMS_ID}"


def test_teams_ids_decode_percent_encoded_invite_url():
    """Calendar invites carry the thread id percent-encoded; the browser URL
    carries it decoded. Both must normalize to the same string or correlation
    never matches."""
    assert teams_conference_ids(TEAMS_JOIN_URL) == {TEAMS_ID}
    assert teams_conference_ids(TEAMS_ID) == {TEAMS_ID}
    assert teams_conference_ids("https://meet.google.com/abc-defg-hij") == frozenset()
    assert teams_conference_ids("") == frozenset()


def test_teams_invite_yields_both_id_spaces():
    """The bug that made every Teams event on the real calendar extract no id
    at all: an invite body names the meeting under a short `/meet/<digits>`
    link *and* a thread id, and only reading the first URL found gets whichever
    one Outlook happened to put first."""
    body = f"Join the meeting now {TEAMS_SHORT_URL} or {TEAMS_JOIN_URL}"
    assert teams_conference_ids(body) == {TEAMS_ID, TEAMS_SHORT_ID}


def test_teams_id_kind_separates_the_two_spaces():
    assert teams_id_kind(TEAMS_ID) == "thread"
    assert teams_id_kind(TEAMS_SHORT_ID) == "short"


def test_teams_tab_ids_require_a_teams_host():
    """A thread id quoted in a doc or an email in some other tab is not
    evidence that the user is sitting in that call."""
    assert teams_conference_ids_from_tab(TEAMS_JOIN_URL) == {TEAMS_ID}
    assert teams_conference_ids_from_tab(TEAMS_SHORT_URL) == {TEAMS_SHORT_ID}
    assert teams_conference_ids_from_tab(
        f"https://mail.google.com/#inbox/{TEAMS_ID}"
    ) == frozenset()


def test_teams_tab_ids_ignore_the_meeting_chat_view():
    """The meeting's chat tab carries the same thread id and routinely stays
    open for days after the call — matching it would start a recording every
    time anything else opened the mic."""
    chat = f"https://teams.microsoft.com/v2/#/conversations/{TEAMS_ID}?ctx=chat"
    assert teams_conference_ids_from_tab(chat) == frozenset()
    live = f"https://teams.microsoft.com/v2/?meetingjoin=true#/meet/{TEAMS_ID}"
    assert teams_conference_ids_from_tab(live) == {TEAMS_ID}
