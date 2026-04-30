"""Calendar correlate scoring — synthetic events, no gws calls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from witnessd.calendar import CalendarEvent, correlate


def _evt(summary: str, platform: str | None = "meet", start: datetime | None = None,
         minutes: int = 30, evt_id: str = "id1",
         conference_url: str | None = None) -> CalendarEvent:
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
        raw={},
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


def test_correlate_returns_none_when_zero_score():
    # No platform match (event=meet, active=unknown), no word overlap, and
    # the event is well outside "happening now" — every signal must fail
    # for correlate to return None.
    far_past = datetime.now(timezone.utc) - timedelta(hours=8)
    e = _evt("Standup", platform="meet", start=far_past, minutes=30)
    event, trace = correlate("Random unrelated tab", "unknown", [e])
    assert event is None
    assert len(trace["candidates"]) == 1
