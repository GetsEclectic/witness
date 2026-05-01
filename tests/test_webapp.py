"""Webapp endpoints — path-traversal guard, list cache, dedup behavior."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from witnessd.transcript import EventBus
from witnessd.webapp import RecordingStatus, build_app


def _build(meetings_root: Path) -> TestClient:
    app = build_app(
        bus=None,
        status=lambda: RecordingStatus(False, None, None, False),
        meetings_root=meetings_root,
    )
    return TestClient(app)


def _make_meeting(root: Path, slug: str, summary: str = "## TL;DR\nshort") -> Path:
    folder = root / slug
    folder.mkdir()
    (folder / "metadata.json").write_text(json.dumps({
        "slug": slug,
        "started_at": "2026-04-28T12:00:00+00:00",
        "ended_at": "2026-04-28T12:30:00+00:00",
    }))
    (folder / "summary.md").write_text(f"# {slug}\n{summary}\n")
    return folder


def test_path_traversal_dotdot_rejected(tmp_meetings_root: Path):
    client = _build(tmp_meetings_root)
    # FastAPI normalizes some path-segment cases at the routing layer; we
    # exercise the resolver directly via an obviously-escaping slug.
    resp = client.get("/api/meetings/..%2Fetc%2Fpasswd/metadata")
    assert resp.status_code in (400, 404)


def test_path_traversal_absolute_rejected(tmp_meetings_root: Path):
    client = _build(tmp_meetings_root)
    resp = client.get("/api/meetings/%2Ftmp/metadata")
    assert resp.status_code in (400, 404)


def test_list_meetings_returns_recent_first(tmp_meetings_root: Path):
    _make_meeting(tmp_meetings_root, "2026-04-01T0900-old")
    _make_meeting(tmp_meetings_root, "2026-04-28T1200-new")
    client = _build(tmp_meetings_root)
    resp = client.get("/api/meetings")
    assert resp.status_code == 200
    items = resp.json()
    assert [m["slug"] for m in items] == [
        "2026-04-28T1200-new",
        "2026-04-01T0900-old",
    ]


def test_list_meetings_skips_dot_directories(tmp_meetings_root: Path):
    (tmp_meetings_root / ".voiceprints").mkdir()
    _make_meeting(tmp_meetings_root, "2026-04-28T1200-new")
    client = _build(tmp_meetings_root)
    items = client.get("/api/meetings").json()
    assert [m["slug"] for m in items] == ["2026-04-28T1200-new"]


def test_meeting_detail_aggregate_endpoint(tmp_meetings_root: Path):
    _make_meeting(tmp_meetings_root, "2026-04-28T1200-test")
    client = _build(tmp_meetings_root)
    resp = client.get("/api/meetings/2026-04-28T1200-test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["slug"] == "2026-04-28T1200-test"
    assert body["has_summary"] is True
    assert body["has_audio"] is False
    assert body["duration_minutes"] == 30
    assert body["tldr"] == "short"


def test_speakers_endpoint_returns_map(tmp_meetings_root: Path):
    folder = _make_meeting(tmp_meetings_root, "2026-04-28T1200-test")
    (folder / "speakers.json").write_text(json.dumps({
        "system_speaker_0": "Aaron",
        "system_speaker_1": "unknown_8e9b7d",
    }))
    client = _build(tmp_meetings_root)
    resp = client.get("/api/meetings/2026-04-28T1200-test/speakers")
    assert resp.status_code == 200
    assert resp.json() == {
        "system_speaker_0": "Aaron",
        "system_speaker_1": "unknown_8e9b7d",
    }


def test_speakers_endpoint_returns_empty_when_missing(tmp_meetings_root: Path):
    _make_meeting(tmp_meetings_root, "2026-04-28T1200-test")
    client = _build(tmp_meetings_root)
    resp = client.get("/api/meetings/2026-04-28T1200-test/speakers")
    assert resp.status_code == 200
    assert resp.json() == {}


def test_summary_endpoint_404s_without_summary(tmp_meetings_root: Path):
    folder = tmp_meetings_root / "2026-04-28T1200-test"
    folder.mkdir()
    (folder / "metadata.json").write_text("{}")
    client = _build(tmp_meetings_root)
    assert client.get("/api/meetings/2026-04-28T1200-test/summary").status_code == 404


def test_ws_flushes_backlog_for_active_meeting(tmp_meetings_root: Path):
    # Contract the UI's active-meeting view relies on: /ws replays the
    # current transcript.jsonl as "event" messages, then sends "live".
    slug = "2026-04-30T1200-active"
    folder = tmp_meetings_root / slug
    folder.mkdir()
    transcript_path = folder / "transcript.jsonl"
    backlog = [
        {"channel": "mic", "speaker": "mic_speaker_0", "text": "hello",
         "is_final": True, "ts_start": 0.0,
         "received_at": "2026-04-30T12:00:01.000000+00:00"},
        {"channel": "system", "speaker": "system_speaker_0", "text": "hi back",
         "is_final": True, "ts_start": 1.0,
         "received_at": "2026-04-30T12:00:02.000000+00:00"},
    ]
    transcript_path.write_text("\n".join(json.dumps(e) for e in backlog) + "\n")

    bus = EventBus(transcript_path)
    try:
        app = build_app(
            bus=bus,
            status=lambda: RecordingStatus(
                True, slug, "2026-04-30T12:00:00+00:00", False,
            ),
            meetings_root=tmp_meetings_root,
        )
        client = TestClient(app)
        with client.websocket_connect("/ws") as ws:
            m1 = ws.receive_json()
            m2 = ws.receive_json()
            live = ws.receive_json()
        assert m1["type"] == "event" and m1["text"] == "hello"
        assert m2["type"] == "event" and m2["text"] == "hi back"
        assert live == {"type": "live"}
    finally:
        bus.close()


def test_unknowns_candidates_per_meeting_context(
    tmp_meetings_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The /unknowns dropdown surfaces names from every meeting the voiceprint
    appears in: calendar invitees, slug-derived names, and prior LLM labels.
    Globally-bound voiceprint names are NOT seeded (would repeat on every card)."""
    from witnessd import webapp

    vp_dir = tmp_meetings_root / ".voiceprints"
    vp_dir.mkdir()
    monkeypatch.setattr(webapp, "VOICEPRINTS_DIR", vp_dir, raising=True)
    (vp_dir / "unknown_abc123.npy").touch()
    (vp_dir / "lissa-giedt.npy").touch()  # global bound name; should NOT show as a chip

    def _make(slug: str, attendees: list[str], chain_terminus: str, utterance_count: int):
        folder = tmp_meetings_root / slug
        folder.mkdir()
        (folder / "metadata.json").write_text(json.dumps({
            "slug": slug,
            "started_at": "2026-04-28T12:00:00+00:00",
            "calendar_event": {
                "self_email": "ben.solwitz@gmail.com",
                "attendees": attendees,
            } if attendees else None,
        }))
        sp = ({"system_speaker_0": chain_terminus} if chain_terminus == "unknown_abc123"
              else {"system_speaker_0": "unknown_abc123", "unknown_abc123": chain_terminus})
        (folder / "speakers.json").write_text(json.dumps(sp))
        events = "\n".join(
            json.dumps({
                "is_final": True, "speaker": "system_speaker_0",
                "ts_start": float(i), "ts_end": float(i) + 1.0, "text": f"hi {i}",
            })
            for i in range(utterance_count)
        )
        (folder / "transcript.jsonl").write_text(events + "\n")

    # Primary (most speaking time): calendar with Alex + Jordan
    _make(
        "2026-04-28T1200-strategy-review",
        ["alex@example.com", "jordan@example.com", "ben.solwitz@gmail.com"],
        "unknown_abc123",
        utterance_count=10,
    )
    # Secondary: no calendar match, slug carries a compound name + LLM guess "Tony"
    _make(
        "2026-04-27T1200-ben-solwitz-and-sam-lee",
        [],
        "Tony",
        utterance_count=3,
    )
    # Tertiary: pure conferencing ID — slug should yield nothing
    _make(
        "2026-04-26T1100-meet-hvb-jfgx-gmf",
        [],
        "unknown_abc123",
        utterance_count=1,
    )

    client = _build(tmp_meetings_root)
    rows = client.get("/api/unknowns").json()
    assert len(rows) == 1
    candidates = rows[0]["candidates"]

    # Primary invitees first (ben filtered)
    assert candidates[:2] == ["Alex", "Jordan"]
    assert "Ben" not in candidates
    # Slug-derived names from primary then secondary
    assert "Strategy Review" in candidates
    assert "Ben Solwitz" in candidates and "Sam Lee" in candidates
    # LLM-guessed terminal label surfaced
    assert "Tony" in candidates
    # Conferencing-ID slug contributes nothing
    assert not any("Hvb" in c or "Jfgx" in c for c in candidates)
    # Globally-bound names appear at the end as recurring-contact suggestions
    assert "Lissa Giedt" in candidates
    assert candidates.index("Alex") < candidates.index("Lissa Giedt")


def test_ws_skips_backlog_when_status_idle(tmp_meetings_root: Path):
    bus = EventBus(tmp_meetings_root / ".staging" / "transcript.jsonl")
    try:
        app = build_app(
            bus=bus,
            status=lambda: RecordingStatus(False, None, None, False),
            meetings_root=tmp_meetings_root,
        )
        client = TestClient(app)
        with client.websocket_connect("/ws") as ws:
            msg = ws.receive_json()
        assert msg == {"type": "live"}
    finally:
        bus.close()
