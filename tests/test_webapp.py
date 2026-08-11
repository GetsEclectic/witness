"""Webapp endpoints — path-traversal guard, list cache, transcript shape."""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from witnessd.webapp import RecordingStatus, build_app


def _build(meetings_root: Path) -> TestClient:
    app = build_app(
        status=lambda: RecordingStatus(False, None, None),
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
    (tmp_meetings_root / ".state").mkdir()
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


def test_summary_endpoint_404s_without_summary(tmp_meetings_root: Path):
    folder = tmp_meetings_root / "2026-04-28T1200-test"
    folder.mkdir()
    (folder / "metadata.json").write_text("{}")
    client = _build(tmp_meetings_root)
    assert client.get("/api/meetings/2026-04-28T1200-test/summary").status_code == 404


def test_transcript_endpoint_returns_events_in_file_order(tmp_meetings_root: Path):
    """What the meeting view renders: one JSON object per utterance, each
    carrying the channel the UI turns into You/Remote."""
    folder = _make_meeting(tmp_meetings_root, "2026-04-28T1200-test")
    events = [
        {"channel": "mic", "text": "hello", "is_final": True, "ts_start": 0.0,
         "ts_end": 1.0, "received_at": "2026-04-28T12:00:01+00:00"},
        {"channel": "system", "text": "hi back", "is_final": True, "ts_start": 1.5,
         "ts_end": 2.5, "received_at": "2026-04-28T12:00:01+00:00"},
    ]
    (folder / "transcript.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )
    client = _build(tmp_meetings_root)
    rows = client.get("/api/meetings/2026-04-28T1200-test/transcript").json()
    assert [(r["channel"], r["text"]) for r in rows] == [
        ("mic", "hello"),
        ("system", "hi back"),
    ]


def test_transcript_endpoint_empty_when_not_yet_transcribed(tmp_meetings_root: Path):
    """A meeting whose pipeline hasn't run yet — the UI shows the summary and
    audio and an empty transcript rather than erroring."""
    _make_meeting(tmp_meetings_root, "2026-04-28T1200-test")
    client = _build(tmp_meetings_root)
    resp = client.get("/api/meetings/2026-04-28T1200-test/transcript")
    assert resp.status_code == 200
    assert resp.json() == []
