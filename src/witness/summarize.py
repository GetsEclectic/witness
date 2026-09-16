"""Generate `summary.md` for a meeting from its resolved `transcript.md`.

Reuses whatever credential local Claude Code is already using:
  * Linux: OAuth token at `~/.claude/.credentials.json` (Pro/Max users) or
    a literal `sk-ant-...` key in the same file.
  * macOS: Claude Code stores credentials in the login Keychain instead of
    a file. We read service `Claude Code-credentials` for an OAuth
    `accessToken`, and fall back to service `Claude Code` for users on
    API-key auth.
`ANTHROPIC_API_KEY` always wins when set.

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

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import anthropic

from .render import display_name, read_metadata, recorder_name


# A Claude Code Pro/Max OAuth token — the default credential — is entitled to
# Haiku only here; Sonnet 5 and Opus 5 both answer 429. Overriding the model
# therefore also means supplying ANTHROPIC_API_KEY.
MODEL = os.environ.get("WITNESS_SUMMARY_MODEL") or "claude-haiku-4-5-20251001"
# Haiku 4.5 predates adaptive thinking and rejects the parameter.
THINKING = MODEL != "claude-haiku-4-5-20251001"
MAX_TOKENS = 16000
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
nothing was settled.

## Decisions
Bulleted. Each a single declarative line: what was decided. State the decision,
not an appraisal of it — do not add characterizations the speakers did not use.
Only actual decisions, not discussion and not status.

## Action items
Bulleted, formatted as `- <owner>: <what> (due <when>)`. No checkbox markup —
it does not survive being pasted elsewhere. Owner is a named person, "Team", or
a named team. Omit "due ..." if not stated.
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

_STUB = """# {title}

## TL;DR
No usable transcript — {words} words were captured from this recording.
"""


def _load_oauth_token_file() -> str | None:
    """Linux: read claudeAiOauth.accessToken from ~/.claude/.credentials.json."""
    path = Path.home() / ".claude" / ".credentials.json"
    if not path.exists():
        return None
    try:
        creds = json.loads(path.read_text())
        return creds["claudeAiOauth"]["accessToken"]
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _read_keychain(service: str) -> str | None:
    """Return the password for a generic Keychain entry, or None if absent.
    macOS-only — caller checks sys.platform."""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def _load_mac_oauth_token() -> str | None:
    """macOS: pull claudeAiOauth.accessToken out of the Keychain entry that
    Claude Code uses for Pro/Max subscription auth."""
    raw = _read_keychain("Claude Code-credentials")
    if raw is None:
        return None
    try:
        return json.loads(raw)["claudeAiOauth"]["accessToken"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _load_mac_api_key() -> str | None:
    """macOS: Claude Code stores a literal sk-ant-... API key under service
    'Claude Code' when the user signed in with an Anthropic API key instead
    of the subscription. Same billing as their CC usage."""
    raw = _read_keychain("Claude Code")
    if raw and raw.startswith("sk-ant-"):
        return raw
    return None


def _build_client() -> anthropic.Anthropic:
    """Auth resolution, in priority order:
      1. ANTHROPIC_API_KEY env var (OSS / explicit override).
      2. Linux Claude Code OAuth file.
      3. macOS Keychain Claude Code OAuth (Pro/Max users).
      4. macOS Keychain Claude Code API key (API-key users).
    Raises a friendly error if none of the above produces a credential."""
    if api_key := os.environ.get("ANTHROPIC_API_KEY"):
        return anthropic.Anthropic(api_key=api_key)

    if token := _load_oauth_token_file():
        return anthropic.Anthropic(
            auth_token=token,
            default_headers={
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
            },
        )

    if sys.platform == "darwin":
        if token := _load_mac_oauth_token():
            return anthropic.Anthropic(
                auth_token=token,
                default_headers={
                    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                },
            )
        if api_key := _load_mac_api_key():
            return anthropic.Anthropic(api_key=api_key)

    raise RuntimeError(
        "no Anthropic credentials found: set ANTHROPIC_API_KEY, install "
        "Claude Code (Linux: ~/.claude/.credentials.json; macOS: keychain "
        "entries 'Claude Code-credentials' or 'Claude Code')"
    )


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
        cal = meta.get("calendar_event") or {}
        out.write_text(_STUB.format(
            title=cal.get("summary") or folder.name, words=words,
        ))
        return out

    client = _build_client()
    kwargs: dict[str, Any] = {}
    if THINKING:
        kwargs["thinking"] = {"type": "adaptive"}
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_prompt(folder, transcript_md, meta)}],
        **kwargs,
    )
    body = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    ).strip()

    out.write_text(body + "\n")
    return out
