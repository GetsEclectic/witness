"""Calendar correlate scoring — synthetic events, no gws calls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from witnessd.calendar import CalendarEvent, correlate


def _evt(summary: str, platform: str | None = "meet", start: datetime | None = None,
         minutes: int = 30, evt_id: str = "id1",
         conference_url: str | None = None,
         raw: dict | None = None) -> CalendarEvent:
    start = start or datetime.now(timezone.utc)
    if conference_url is None:
        conference_url = "https://meet.google.com/x" if platform == "meet" else None
    return CalendarEvent(
        id=evt_id,
        summary=summary,
        start=start,
        end=start + timedelta(minutes=minutes),
        attendees=[],
        self_email=None,
        platform=platform,
        conference_url=conference_url,
        raw=raw or {},
    )


def test_correlate_returns_none_for_empty_event_list():
    event, trace = correlate("Meet - abc-defg", "meet", [])
    assert event is None
    assert trace["candidates"] == []


def test_correlate_picks_event_whose_summary_words_appear_in_title():
    a = _evt("Witness Triage", evt_id="a")
    b = _evt("Quarterly Planning", evt_id="b")
    event, _ = correlate("Witness Triage – Google Chrome", "meet", [a, b])
    assert event is a


def test_correlate_prefers_platform_match_over_word_overlap():
    # Same word overlap on both, but only `a` matches the active platform.
    a = _evt("Standup", platform="meet", evt_id="a")
    b = _evt("Standup", platform="zoom", evt_id="b")
    event, _ = correlate("Standup", "meet", [a, b])
    assert event is a


def test_correlate_breaks_ties_by_earliest_start():
    now = datetime.now(timezone.utc)
    a = _evt("1:1", start=now + timedelta(minutes=2), evt_id="a")
    b = _evt("1:1", start=now, evt_id="b")
    event, _ = correlate("1:1", "meet", [a, b])
    # Both score equally on word-overlap + happening-now + platform; earliest wins.
    assert event is b


def test_correlate_uses_meet_code_to_disambiguate_double_booking():
    # Two simultaneous Meet events tie on platform + happening-now. The window
    # title carries the joined call's specific Meet code; only `b`'s URL
    # contains it. `b` should win even though it'd otherwise tie or lose.
    a = _evt(
        "Costentory Scrum",
        evt_id="a",
        conference_url="https://meet.google.com/ysq-audy-hch",
    )
    b = _evt(
        "Ben/Gary 1:1",
        evt_id="b",
        conference_url="https://meet.google.com/qoy-mdvb-rzj",
    )
    event, trace = correlate("Meet - qoy-mdvb-rzj", "meet", [a, b])
    assert event is b
    reasons_b = next(c["reasons"] for c in trace["candidates"] if c["event_id"] == "b")
    assert "conference-id-match" in reasons_b


def test_correlate_excludes_event_with_mismatched_conference_id():
    # Real-world bug: an ad-hoc Meet (xxr-wefx-ytr) opened during the calendar
    # window of a different scheduled Meet (qgq-mgqy-wtb) was attributed to the
    # scheduled event because platform=meet + happening-now scored above zero.
    # Conference IDs are unique — a known mismatch disqualifies the event,
    # even when other signals would otherwise win.
    e = _evt(
        "Genie / EQS EDI ordering introduction",
        evt_id="genie",
        conference_url="https://meet.google.com/qgq-mgqy-wtb",
    )
    event, trace = correlate("Meet - xxr-wefx-ytr", "meet", [e])
    assert event is None
    reasons = trace["candidates"][0]["reasons"]
    assert "conference-id-mismatch" in reasons


TEAMS_ID = "19:meeting_nwq4zmy0mjityteymy00@thread.v2"
TEAMS_SHORT_ID = "383440701254366"
TEAMS_JOIN_URL = (
    "https://teams.microsoft.com/l/meetup-join/"
    "19%3ameeting_NWQ4ZmY0MjItYTEyMy00%40thread.v2/0"
    "?context=%7b%22Tid%22%3a%22abc%22%7d"
)
TEAMS_SHORT_URL = f"https://teams.microsoft.com/meet/{TEAMS_SHORT_ID}?p=c75VTV1H9wWHwPLUEt"


def _teams_evt(summary: str, *, urls: list[str], evt_id: str = "teams1") -> CalendarEvent:
    """A Teams event shaped like the real thing: the short link lands in
    `conference_url` (it comes first in the body) while the thread id is only
    reachable through the description."""
    body = " ".join(f"Join the meeting now {u}" for u in urls)
    return _evt(
        summary,
        platform="teams",
        evt_id=evt_id,
        conference_url=urls[0],
        raw={"description": body},
    )


def test_correlate_matches_teams_invite_under_either_id_space():
    """The real R&D Tax Credit invite names its meeting twice — short link and
    thread id — and which one the browser shows isn't ours to predict, so
    either has to match."""
    e = _teams_evt(
        "EquipmentShare 2025 - R&D Tax Credit Interview - Web Experience",
        urls=[TEAMS_SHORT_URL, TEAMS_JOIN_URL],
    )
    for detected in (TEAMS_ID, TEAMS_SHORT_ID):
        event, trace = correlate("Teams - nwq4zmy0mjit", "teams", [e], detected)
        assert event is e
        assert "conference-id-match" in trace["candidates"][0]["reasons"]


def test_correlate_excludes_teams_event_with_a_different_id():
    """Back-to-back Teams calls overlap in the ± window; without the id
    disqualification the second one is attributed to the first one's event,
    since platform + happening-now already score above zero."""
    e = _teams_evt(
        "Some other Teams call",
        urls=["https://teams.microsoft.com/meet/999888777666?p=zzz"],
        evt_id="other",
    )
    event, trace = correlate("Teams - nwq4zmy0mjit", "teams", [e], TEAMS_SHORT_ID)
    assert event is None
    assert "conference-id-mismatch" in trace["candidates"][0]["reasons"]


def test_correlate_does_not_disqualify_across_teams_id_spaces():
    """An invite that only quotes a short link says nothing about a tab
    showing a thread id — they're different id spaces, not different meetings.
    Reading that as a mismatch would throw away the correct event."""
    e = _teams_evt(
        "EquipmentShare 2025 - R&D Tax Credit Interview - Web Experience",
        urls=[TEAMS_SHORT_URL],
    )
    event, trace = correlate("Teams - nwq4zmy0mjit", "teams", [e], TEAMS_ID)
    assert event is e  # falls back to platform + happening-now
    reasons = trace["candidates"][0]["reasons"]
    assert "conference-id-mismatch" not in reasons
    assert "conference-id-match" not in reasons


def test_correlate_still_reads_the_id_from_the_title_when_not_passed():
    """Meet's room code lives in its window title, so the Linux pactl path —
    which has no conference_id to hand over — keeps working unchanged."""
    e = _evt("Standup", conference_url="https://meet.google.com/qgq-mgqy-wtb")
    event, trace = correlate("Meet - qgq-mgqy-wtb", "meet", [e])
    assert event is e
    assert trace["conference_id"] == "qgq-mgqy-wtb"


def test_correlate_returns_none_when_zero_score():
    # No platform match (event=meet, active=unknown), no word overlap, and
    # the event is well outside "happening now" — every signal must fail
    # for correlate to return None.
    far_past = datetime.now(timezone.utc) - timedelta(hours=8)
    e = _evt("Standup", platform="meet", start=far_past, minutes=30)
    event, trace = correlate("Random unrelated tab", "unknown", [e])
    assert event is None
    assert len(trace["candidates"]) == 1
