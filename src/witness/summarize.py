"""Generate `summary.md` for a meeting from its resolved `transcript.md`.

Runs through the local `claude` CLI on whatever login Claude Code is using
(see claude_cli). `ANTHROPIC_API_KEY` always wins when set.

Output structure:
  # <meeting title>
  ## TL;DR
  one-paragraph recap, written to survive being pasted on its own
  ## Decisions
  - ...
  ## Action items
  - <owner>: <what> (due <when>)
  ## Open questions
  - ...
  ## Notable quotes
  > "..." — Speaker [MM:SS]

Idempotent: overwrites summary.md.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from . import claude_cli
from .render import display_name, read_metadata, recorder_name

log = logging.getLogger("witness.summarize")


# Haiku 5.5 invented surnames and acronym expansions and wrote "the group
# agreed"; Sonnet 5.5 did none of that on the same four meetings.
MODEL = os.environ.get("WITNESS_SUMMARY_MODEL") or "claude-sonnet-5-5"
MIN_TRANSCRIPT_WORDS = 50

_SYSTEM_PROMPT = """You summarize a meeting transcript for the person who recorded it.

Two readers, in priority order:
  1. The recorder, weeks later, who remembers none of it.
  2. A colleague who was NOT in the room, to whom the recorder forwards part
     of this — usually the TL;DR alone, pasted into Slack with nothing else.
Write so that no sentence has to be rewritten before it is forwarded.

Never address the reader in the second person, and never write the words "You"
or "Remote" — those are audio-channel labels, not names. The recorder's name is
given below; use it. When a `Remote` line cannot be pinned to a named attendee,
attribute it to a role ("the Genie rep") or leave it unattributed. Do not invent
a name and do not guess at a spelling: spell every name exactly as the attendee
list spells it, and if a name is only ever spoken aloud, leave it out.

Plain markdown.

Output, in order:

# <title>
4–8 words describing what this meeting actually was (e.g. "Arlo Head of
Engineering Interview", "Witness Auto-Stop Bug Triage"). If a calendar title
is provided, use it verbatim. Otherwise generate one from the content.

## TL;DR
One short paragraph, which will be read on its own with none of the sections
below it. So:
  * The first sentence states the most consequential outcome — the thing that
    is now true that was not true before. Not the agenda, and not what kind of
    meeting this was. Never open by narrating the meeting: no "Team discussed",
    "Team synced on", "Reviewed progress on", "Caught up on", "Brief standup
    on", "Casual catch-up", "Met with", "Informal conversation about".
  * Expand every acronym, product name and internal shorthand on first use.
    A reader who was not in the room must not have to ask what it means.
  * If something is blocked, name what is blocking it and who owns it.
Never "(none)" — every recording has an outcome, even if the outcome is that
nothing was settled. Never write "agreed", "decided" or "will" for anything
that is not listed under Decisions.

## Decisions
Bulleted, formatted as `- <what was decided> — "<the words that committed to
it>" [MM:SS]`. State the decision, not an appraisal of it — do not add
characterizations the speakers did not use. If no line in the transcript
commits to it, it is not a decision; leave it out.
A decision is a commitment the meeting made: someone said it will happen and
nobody disputed it. These are not decisions:
  * an idea floated, or something called "worth trying"
  * agreement that something "makes sense", "is reasonable" or "probably" works
  * one participant describing what their own team already does or plans to do
Those belong under Open questions or in the TL;DR as discussion. A `Remote`
line is one person, never the group: a proposal from `Remote` becomes a
decision only if someone else explicitly commits to it. Exploratory and
brainstorming meetings often decide nothing, and then "(none)" is correct.

## Action items
Bulleted, formatted as `- <owner>: <what> (due <when>)`. No checkbox markup —
it does not survive being pasted elsewhere. Owner is a named person, "Team", or
a named team. Omit "due ..." if not stated. Only work someone took on: a
suggestion nobody picked up is an open question, not a "Team" action item.
When the reason for an item would not be obvious to someone who was not in the
room, add one clause giving the mechanism — the constraint, system, or failure
that makes it necessary. An action item that cannot be acted on without
re-reading the transcript is a defect, so prefer one well-explained item over
three bare ones.

## Open questions
Bulleted. Anything raised but not resolved.

## Notable quotes
Usually none. Include one, at most two, and only where the exact wording is
itself the content — a commitment, a refusal, a number, or a position stated
more precisely than any paraphrase would manage. Never include a quote merely
because it is vivid or forceful. Trim filler lead-ins ("I honestly think it
is.", "I mean,", "I want to throw that out there that") down to the clause that
carries the meaning. Format: `> "quote" — Speaker [MM:SS]`.

"(none)" is allowed under Decisions, Action items, Open questions and Notable
quotes, and nowhere else.

Length scales with the meeting: roughly 8-10% of the transcript's length, and a
meeting covering several unrelated topics needs every topic represented rather
than averaged into one paragraph. Being terse is not the goal; being
forwardable without an edit is.
"""

STUB_MARKER = "No usable transcript"

_STUB = """# {title}

## TL;DR
No usable transcript — {words} words were captured from this recording.
"""


def _transcript_words(folder: Path) -> int:
    tmd = folder / "transcript.md"
    return len(tmd.read_text().split()) if tmd.exists() else 0


def _write_stub(folder: Path, words: int) -> Path:
    cal = (read_metadata(folder).get("calendar_event") or {})
    out = folder / "summary.md"
    out.write_text(_STUB.format(title=cal.get("summary") or folder.name, words=words))
    return out


def quarantine_fabricated(folder: Path) -> bool:
    """Replace a summary that has no transcript behind it with the stub.

    Before MIN_TRANSCRIPT_WORDS existed, an empty transcript produced a
    confident summary of a meeting that never happened, and those files are
    indistinguishable from real ones in a search. Returns True when a
    summary was replaced.
    """
    out = folder / "summary.md"
    if not out.exists():
        return False
    words = _transcript_words(folder)
    if words >= MIN_TRANSCRIPT_WORDS or STUB_MARKER in out.read_text():
        return False
    log.warning(
        "summary for %s has no transcript behind it (%d words); replacing with stub",
        folder.name, words,
    )
    _write_stub(folder, words)
    return True


def _user_prompt(folder: Path, transcript_md: str, meta: dict[str, Any]) -> str:
    parts = [f"Meeting folder: `{folder.name}`"]
    cal = meta.get("calendar_event") or {}
    if cal.get("summary"):
        parts.append(f"Calendar title: {cal['summary']}")
    if cal.get("start") and cal.get("end"):
        parts.append(f"Scheduled: {cal['start']} → {cal['end']}")

    me = recorder_name(meta)
    if me:
        parts.append(f"Recorder (the person whose mic this is): {me}")
    if cal.get("attendees"):
        self_email = cal.get("self_email")
        others = [
            display_name(a) for a in cal["attendees"]
            if a and a != self_email
        ]
        if others:
            parts.append(
                "Other invitees, with the spelling to use for each: "
                + ", ".join(others)
            )
    started = meta.get("started_at")
    ended = meta.get("ended_at")
    if started and ended:
        parts.append(f"Recorded: {started} → {ended}")
    parts.append("")

    speaker_note = (
        "Transcript follows. Speakers are attributed by audio channel, not by "
        "voice: "
    )
    if me:
        speaker_note += (
            f"`{me}` is the person recording. `Remote` is everyone else on the "
            "call — all of them, undifferentiated, so consecutive `Remote` "
            "paragraphs are often different people. "
        )
    else:
        speaker_note += (
            "`You` is the person recording — their name is not known for this "
            "meeting, so refer to them as \"the recorder\" and never as "
            "\"You\". `Remote` is everyone else on the call — all of them, "
            "undifferentiated, so consecutive `Remote` paragraphs are often "
            "different people. "
        )
    speaker_note += (
        "Attribute a `Remote` line to a named attendee only where the "
        "surrounding conversation makes it unambiguous; otherwise use a role "
        "or no attribution. Bracketed [MM:SS] offsets are timestamps."
    )
    parts.append(speaker_note)
    parts.append("")
    parts.append(transcript_md)
    return "\n".join(parts)


def summarize(folder: Path) -> Path:
    """Generate folder/summary.md. Returns the output path."""
    tmd = folder / "transcript.md"
    if not tmd.exists():
        raise FileNotFoundError(f"{tmd} missing — run render first")
    transcript_md = tmd.read_text()

    meta = read_metadata(folder)
    out = folder / "summary.md"

    # Asked to summarize nothing, the model invents a meeting instead.
    words = len(transcript_md.split())
    if words < MIN_TRANSCRIPT_WORDS:
        return _write_stub(folder, words)

    res = claude_cli.complete(
        _SYSTEM_PROMPT, _user_prompt(folder, transcript_md, meta), MODEL,
    )
    out.write_text(res["result"].strip() + "\n")
    return out
