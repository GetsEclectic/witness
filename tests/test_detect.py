"""Pactl block parser + classifier — pure functions, no system calls."""
from __future__ import annotations

from witnessd.detect import (
    Detection,
    _classify,
    _is_live,
    _parse_pactl_blocks,
)


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
