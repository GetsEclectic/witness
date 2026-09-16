"""Prompt assembly and the short-transcript guard.

Nothing here calls the API — `summarize()` is exercised only on the path that
returns before building a client.
"""
from __future__ import annotations

import json
from pathlib import Path

from witness.summarize import (
    MIN_TRANSCRIPT_WORDS,
    _SYSTEM_PROMPT,
    _user_prompt,
    summarize,
)
from witnessd import config


def _meta(**cal) -> dict:
    return {"calendar_event": cal} if cal else {}


def test_prompt_names_the_recorder(tmp_path: Path):
    prompt = _user_prompt(
        tmp_path / "2026-09-14T1229-x",
        "**Ben Solwitz** · [00:00]\nhello",
        _meta(self_email="ben.solwitz@equipmentshare.com"),
    )
    assert "Recorder (the person whose mic this is): Ben Solwitz" in prompt
    assert "`Ben Solwitz` is the person recording" in prompt


def test_prompt_gives_invitee_spellings_and_drops_the_recorder(tmp_path: Path):
    """The name the model gets wrong by ear is the one it should copy: Pair 2
    posted `Willie's` where the invite said Willy."""
    prompt = _user_prompt(
        tmp_path / "m",
        "transcript",
        _meta(
            self_email="ben.solwitz@equipmentshare.com",
            attendees=[
                "ben.solwitz@equipmentshare.com",
                "angela.page@equipmentshare.com",
                "gary.O'Kane@equipmentshare.com",
            ],
        ),
    )
    assert "Angela Page" in prompt
    assert "Gary O'Kane" in prompt
    # The recorder is named once, as the recorder — not again as an invitee.
    assert prompt.count("Ben Solwitz") == 2


def test_prompt_without_a_known_name_forbids_you(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config, "USER_NAME", "")
    prompt = _user_prompt(tmp_path / "m", "transcript", {})
    assert "Recorder (the person" not in prompt
    assert 'never as "You"' in prompt
    assert "the recorder" in prompt


def test_system_prompt_bans_the_channel_labels():
    assert 'never write the words "You"' in _SYSTEM_PROMPT
    # The checkbox does not survive a paste into Slack.
    assert "- [ ]" not in _SYSTEM_PROMPT
    assert "No checkbox markup" in _SYSTEM_PROMPT


def test_system_prompt_does_not_anchor_quotes_on_three():
    """`Up to 3` read as a quota: 499 of 697 summaries emitted exactly three."""
    assert "Up to 3" not in _SYSTEM_PROMPT
    assert "Usually none" in _SYSTEM_PROMPT


def test_short_transcript_writes_a_stub_without_calling_the_api(tmp_path: Path):
    """Left to the model this came back as prose apologising for the missing
    transcript, or asking the caller to paste one — 27 times in ~700 meetings.
    _build_client() is never reached, so this passes with no credentials."""
    folder = tmp_path / "2026-04-29T1027-meet-nux"
    folder.mkdir()
    (folder / "transcript.md").write_text("# x\n\n**Remote** · [00:00]\nuh\n")
    (folder / "metadata.json").write_text(json.dumps(
        {"calendar_event": {"summary": "NUX / PCVP"}}
    ))
    body = summarize(folder).read_text()
    assert body.startswith("# NUX / PCVP")
    assert "No usable transcript" in body
    assert "## TL;DR" in body


def test_stub_titles_itself_from_the_folder_without_a_calendar(tmp_path: Path):
    folder = tmp_path / "2026-04-29T1027-ad-hoc"
    folder.mkdir()
    (folder / "transcript.md").write_text("# x\n")
    assert "2026-04-29T1027-ad-hoc" in summarize(folder).read_text()


def test_a_real_transcript_is_not_stubbed(tmp_path: Path, monkeypatch):
    """The guard must not swallow a short but genuine meeting. Stops at the
    client build, which is as far as this test can go without credentials."""
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "transcript.md").write_text("word " * (MIN_TRANSCRIPT_WORDS + 1))

    built = []
    monkeypatch.setattr(
        "witness.summarize._build_client",
        lambda: built.append(1) or (_ for _ in ()).throw(RuntimeError("stop")),
    )
    try:
        summarize(folder)
    except RuntimeError:
        pass
    assert built, "short-but-real transcript should have reached the API path"
