"""Webapp endpoints — path-traversal guard, list cache, transcript shape."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
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


def _track(state_dir: Path, entries: list[dict]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "items.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))


def _entry(iid: str, slug: str, created: str, **kw) -> dict:
    return {"id": iid, "owner": "The recorder", "text": f"item {iid}", "people": [],
            "due": None, "transcript_ts": None, "kind": "lookup", "slug": slug,
            "status": "open", "artifacts": [], "note": None,
            "created_at": created, "updated_at": created, **kw}


@pytest.fixture
def actions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from witnessd import config
    d = tmp_path / "witness-actions"
    monkeypatch.setattr(config, "ACTIONS_STATE_DIR", d)
    return d


def test_actions_newest_first_with_titles_and_links(tmp_meetings_root: Path, actions_dir: Path):
    old = _make_meeting(tmp_meetings_root, "2026-04-01T0900-old")
    (old / "followup").mkdir()
    doc = old / "followup" / "teams.md"
    doc.write_text("# teams\n")
    _make_meeting(tmp_meetings_root, "2026-04-28T1200-new")
    _track(actions_dir, [
        _entry("aaaa0001", old.name, "2026-04-01T13:00:00+00:00", status="drafted",
               artifacts=["https://slack.example/archives/C1", str(doc), "/elsewhere/x.md"]),
        _entry("aaaa0002", old.name, "2026-04-01T13:00:00+00:00"),
        _entry("bbbb0001", "2026-04-28T1200-new", "2026-04-28T13:00:00+00:00"),
        _entry("cccc0001", "gone", "2026-03-01T13:00:00+00:00"),
    ])
    rows = _build(tmp_meetings_root).get("/api/actions").json()
    assert [r["id"] for r in rows] == ["bbbb0001", "aaaa0001", "aaaa0002", "cccc0001"]
    assert rows[1]["meeting_title"] == old.name and rows[1]["meeting_exists"]
    assert rows[3]["meeting_title"] == "gone" and not rows[3]["meeting_exists"]
    links = rows[1]["links"]
    assert links[0]["href"] == "https://slack.example/archives/C1"
    assert links[1]["href"] == f"/api/meetings/{old.name}/files/followup/teams.md"
    assert links[2]["href"] is None


def test_meeting_files_serves_inside_the_folder_only(tmp_meetings_root: Path):
    folder = _make_meeting(tmp_meetings_root, "2026-04-28T1200-test")
    (folder / "followup").mkdir()
    (folder / "followup" / "brief.md").write_text("hello")
    (tmp_meetings_root / "secret.txt").write_text("no")
    client = _build(tmp_meetings_root)
    resp = client.get(f"/api/meetings/{folder.name}/files/followup/brief.md")
    assert resp.status_code == 200 and resp.text == "hello"
    assert resp.headers["content-type"].startswith("text/plain")
    assert client.get(f"/api/meetings/{folder.name}/files/..%2Fsecret.txt").status_code == 404


def test_set_action_status(tmp_meetings_root: Path, actions_dir: Path):
    _track(actions_dir, [_entry("aaaa0001", "s", "2026-04-01T13:00:00+00:00", status="needs_input")])
    client = _build(tmp_meetings_root)
    for status in ("done", "open", "dropped"):
        resp = client.post("/api/actions/aaaa0001", json={"status": status})
        assert resp.status_code == 200 and resp.json()["status"] == status
    assert client.get("/api/actions").json()[0]["status"] == "dropped"


@pytest.mark.parametrize("item,status,code", [
    ("aaaa0001", "drafted", 400),
    ("aaaa0001", "needs_input", 400),
    ("aaaa0001", "bogus", 400),
    ("aaaa", "done", 404),
    ("zzzz0001", "done", 404),
])
def test_set_action_status_rejects(tmp_meetings_root: Path, actions_dir: Path, item, status, code):
    _track(actions_dir, [_entry("aaaa0001", "s", "2026-04-01T13:00:00+00:00")])
    resp = _build(tmp_meetings_root).post(f"/api/actions/{item}", json={"status": status})
    assert resp.status_code == code
    assert json.loads((actions_dir / "items.jsonl").read_text())["status"] == "open"


def test_meeting_detail_includes_action_items(tmp_meetings_root: Path):
    folder = _make_meeting(tmp_meetings_root, "2026-04-28T1200-test")
    client = _build(tmp_meetings_root)
    assert client.get(f"/api/meetings/{folder.name}").json()["action_items"] is None
    (folder / "action_items.json").write_text(json.dumps({"items": [{"id": "x", "text": "t"}]}))
    assert client.get(f"/api/meetings/{folder.name}").json()["action_items"] == [{"id": "x", "text": "t"}]


def test_static_assets_revalidate_every_load(tmp_meetings_root: Path):
    r = _build(tmp_meetings_root).get("/static/style.css")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
