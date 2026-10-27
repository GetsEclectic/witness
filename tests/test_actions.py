"""Action-item extraction, the tracker and its CLI, the follow-up brief,
and the worker's send guard. No network: the model call is monkeypatched,
and no worker process is ever started."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.witness import cli
from witness import actions, followup, pipeline
from witness.send_guard import blocked
from witnessd import config

SUMMARY = """# Hiring Sync

## TL;DR
Synthetic.

## Action items
- The recorder: Message Jarvis and Mona today for approval to hire Dee [00:34].
- The recorder: Write up what each team works on for the recruiters [03:10].
- The recorder: Interview the second candidate in person.
- Gale: Pull in another recruiter [02:00].

## Open questions
- (none)
"""

RAW = {
    "personal": False,
    "items": [
        {"owner": "The recorder", "text": "Message Jarvis and Mona today for approval to hire Dee",
         "people": ["Jarvis", "Mona", "Dee"], "due": "today", "transcript_ts": "00:34",
         "kind": "draft_message"},
        {"owner": "The recorder", "text": "Write up what each team works on for the recruiters",
         "people": [], "due": None, "transcript_ts": "[03:10]", "kind": "write_doc"},
        {"owner": "The recorder", "text": "Interview the second candidate in person",
         "people": [], "due": None, "transcript_ts": None, "kind": "human_only"},
        {"owner": "Gale", "text": "Pull in another recruiter", "people": [],
         "transcript_ts": "02:00", "kind": "draft_message"},
    ],
}


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "state"
    monkeypatch.setattr(config, "ACTIONS_STATE_DIR", d)
    monkeypatch.setattr(config, "USER_NAME", "")
    return d


@pytest.fixture
def meeting(tmp_meetings_root: Path) -> Path:
    folder = tmp_meetings_root / "2026-10-27T1000-meet-hiring"
    folder.mkdir()
    (folder / "summary.md").write_text(SUMMARY)
    (folder / "metadata.json").write_text(json.dumps({
        "started_at": "2026-10-27T14:00:00+00:00",
        "calendar_event": {
            "summary": "Hiring Sync",
            "self_email": "pat.recorder@example.com",
            "attendees": ["pat.recorder@example.com", "gale.smith@example.com"],
        },
    }))
    events = [
        {"channel": "mic", "text": "I'll ping Jarvis and Mona.", "ts_start": 30.0, "is_final": True},
        {"channel": "system", "text": "Great, thanks.", "ts_start": 40.0, "is_final": True},
        {"channel": "system", "text": "Unrelated, much later.", "ts_start": 900.0, "is_final": True},
        {"channel": "mic", "text": "partial", "ts_start": 41.0, "is_final": False},
    ]
    (folder / "transcript.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    return folder


def _fake_model(monkeypatch: pytest.MonkeyPatch, raw: dict = RAW) -> list[int]:
    calls: list[int] = []

    def fake(summary_md, meta):
        calls.append(1)
        return raw

    monkeypatch.setattr(actions, "_call_model", fake)
    return calls


def test_section_parsing():
    assert "The recorder: Message" in actions.action_items_section(SUMMARY)
    assert "Open questions" not in actions.action_items_section(SUMMARY)
    assert actions.action_items_section("# x\n\n## Action items\n(none)\n\n## Open questions\n") == ""
    assert actions.action_items_section("# x\n\n## TL;DR\nhi\n") == ""


def test_normalize_decides_ownership_and_cleans_fields():
    data = actions.normalize("slug", RAW, "Pat Recorder")
    items = data["items"]
    assert [i["is_mine"] for i in items] == [True, True, True, False]
    assert items[1]["transcript_ts"] == "03:10"
    assert items[0]["due"] == "today"
    assert len({i["id"] for i in items}) == 4


def test_normalize_matches_first_name_and_rejects_bad_kind():
    raw = {"items": [
        {"owner": "Pat", "text": "a", "people": [], "kind": "teleport", "transcript_ts": "soon"},
        {"owner": "Pat", "text": "a", "people": [], "kind": "lookup"},
    ]}
    items = actions.normalize("s", raw, "Pat Recorder")["items"]
    assert len(items) == 1  # duplicate text collapsed
    assert items[0]["is_mine"] and items[0]["kind"] == "human_only"
    assert items[0]["transcript_ts"] is None


def test_extract_is_keyed_on_the_summary(meeting, monkeypatch):
    calls = _fake_model(monkeypatch)
    first = actions.extract(meeting)
    assert actions.extract(meeting) == first
    assert len(calls) == 1
    (meeting / "summary.md").write_text(SUMMARY + "\n")
    actions.extract(meeting)
    assert len(calls) == 2


def test_extract_skips_the_model_without_items(meeting, monkeypatch):
    calls = _fake_model(monkeypatch)
    (meeting / "summary.md").write_text("# x\n\n## Action items\n(none)\n")
    assert actions.extract(meeting)["items"] == []
    (meeting / "summary.md").write_text("# x\n\n## TL;DR\nNo usable transcript — 3 words.\n")
    assert actions.extract(meeting) is None
    assert calls == []


def test_track_once_per_meeting_and_skips_personal(meeting, monkeypatch, state):
    _fake_model(monkeypatch)
    data = actions.extract(meeting)
    added = actions.track(data)
    assert len(added) == 3 and all(e["status"] == "open" for e in added)
    assert actions.track(data) == []
    assert len(actions.load()) == 3
    assert actions.track({**data, "slug": "other", "personal": True}) == []


def test_tracker_cli(meeting, monkeypatch, state):
    _fake_model(monkeypatch)
    added = actions.track(actions.extract(meeting))
    runner = CliRunner()
    first, second = added[0]["id"], added[1]["id"]

    out = runner.invoke(cli, ["actions", "list"])
    assert out.exit_code == 0 and first in out.output

    out = runner.invoke(cli, ["actions", "update", first[:6], "--status", "drafted",
                              "--artifact", "https://slack/x", "--note", "check spelling"])
    assert out.exit_code == 0, out.output
    assert runner.invoke(cli, ["actions", "done", first]).exit_code == 0
    assert runner.invoke(cli, ["actions", "drop", second]).exit_code == 0

    listed = runner.invoke(cli, ["actions", "list"]).output
    assert first not in listed and second not in listed
    assert first in runner.invoke(cli, ["actions", "list", "--all"]).output
    entry = next(e for e in actions.load() if e["id"] == first)
    assert entry["status"] == "done" and entry["artifacts"] == ["https://slack/x"]

    bad = runner.invoke(cli, ["actions", "done", "zzzz"])
    assert bad.exit_code != 0 and "0 items match" in bad.output


def test_transcript_slices_merge_and_window(meeting):
    slices = followup.transcript_slices(meeting, ["00:34", "00:40", None, "bogus"], "Pat")
    assert len(slices) == 1
    s = slices[0]
    assert "I'll ping Jarvis" in s and "**Pat**" in s and "**Remote**" in s
    assert "much later" not in s and "partial" not in s
    assert followup.ts_seconds("1:02:03") == 3723


def test_brief_renders_items_rules_and_slices(meeting, monkeypatch, state):
    _fake_model(monkeypatch)
    data, brief = followup.prepare(meeting)
    text = brief.read_text()
    worker = followup.worker_items(data)
    assert [i["kind"] for i in worker] == ["draft_message", "write_doc"]
    for i in worker:
        assert f"`{i['id']}`" in text
    assert "Interview the second candidate" in text.split("## Hard rules")[0]
    assert "Pull in another recruiter" not in text.split("## Summary")[0]
    assert "Gale Smith <gale.smith@example.com>" in text
    assert "slack_send_message_draft" in text and config.SLACK_SELF_ID in text
    assert "I'll ping Jarvis" in text
    assert actions.load() == []  # prepare is the dry run


def test_brief_without_an_invite_says_attendees_are_unknown(meeting, monkeypatch, state):
    _fake_model(monkeypatch)
    (meeting / "metadata.json").write_text(json.dumps({"detection": {"correlation": {
        "candidates": [{"summary": "Ben/Paddy 1:1"}]}}}))
    _, brief = followup.prepare(meeting)
    text = brief.read_text()
    assert "attendees are unknown" in text and "Ben/Paddy 1:1" in text


def test_run_spawns_once_and_never_raises(meeting, monkeypatch, state):
    _fake_model(monkeypatch)
    spawned: list[Path] = []

    class Proc:
        pid = 4242

    monkeypatch.setattr(followup, "spawn", lambda folder, brief: spawned.append(brief) or Proc())
    notes: list[tuple[str, str]] = []
    followup.run(meeting, notify=lambda t, b: notes.append((t, b)))
    followup.run(meeting, notify=lambda t, b: notes.append((t, b)))
    assert len(spawned) == 1 and len(notes) == 1
    assert (meeting / "followup" / ".spawned").read_text().strip() == "4242"

    monkeypatch.setattr(actions, "extract", lambda folder: 1 / 0)
    followup.run(meeting)


def test_run_respects_the_flag(meeting, monkeypatch, state):
    calls = _fake_model(monkeypatch)
    monkeypatch.setattr(config, "ACTION_FOLLOWUP", False)
    followup.run(meeting)
    assert calls == [] and not (meeting / "action_items.json").exists()


def test_pipeline_runs_followup_only_on_the_final_run(meeting, monkeypatch):
    ran: list[Path] = []
    monkeypatch.setattr(followup, "run", lambda folder, notify=None: ran.append(folder))
    monkeypatch.setattr(pipeline, "_macos_notify", lambda t, b: None)
    from witness import summarize
    monkeypatch.setattr(summarize, "summarize", lambda folder: folder / "summary.md")
    pipeline.run(meeting, steps=["render"], final=True)
    pipeline.run(meeting, steps=["summarize"], final=False)
    assert ran == []
    pipeline.run(meeting, steps=["summarize"], final=True)
    assert ran == [meeting]


def test_settings_scope_writes_and_install_the_guard(meeting):
    s = followup.worker_settings(meeting)
    assert s["permissions"]["defaultMode"] == "auto"
    assert f"Edit(/{meeting}/followup/**)" in s["permissions"]["allow"]
    assert isinstance(s["autoMode"]["environment"], list)
    hook = s["hooks"]["PreToolUse"][0]
    assert "send_guard.py" in hook["hooks"][0]["command"]


@pytest.mark.parametrize("name,inp,ok", [
    ("mcp__plugin_slack_slack__slack_send_message", {"channel_id": "USELF", "message": "x"}, True),
    ("mcp__plugin_slack_slack__slack_send_message", {"channel_id": "C123", "message": "x"}, False),
    ("mcp__plugin_slack_slack__slack_send_message", {"channel_id": "USELF", "thread_ts": "1"}, False),
    ("mcp__abc__slack_send_message_draft", {"channel_id": "C123"}, True),
    ("mcp__abc__slack_schedule_message", {"channel_id": "USELF"}, False),
    ("mcp__gmail__send_message", {}, False),
    ("mcp__cal__create_event", {}, False),
    ("mcp__abc__slack_search_public", {}, True),
])
def test_send_guard(name, inp, ok):
    assert (blocked({"tool_name": name, "tool_input": inp}, "USELF") is None) == ok
