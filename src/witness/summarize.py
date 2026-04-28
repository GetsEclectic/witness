"""Generate `summary.md` for a meeting from its resolved `transcript.md`.

Uses the local Claude Code OAuth token at `~/.claude/.credentials.json` —
the same credential Claude Code itself uses, so no extra API billing for
users who already have Claude Code set up.

Output structure:
  # <meeting title>
  **When:** ...  **Attendees:** ...

  ## TL;DR
  one-paragraph recap
  ## Decisions
  - ...
  ## Action items
  - [ ] <owner>: <what>
  ## Open questions
  - ...

Idempotent: overwrites summary.md.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anthropic


MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 4096

_SYSTEM_PROMPT = """You summarize a meeting transcript for the user who recorded it.
The summary is for their own recall — not for sending to anyone else.

Produce a tight, skimmable summary. No throat-clearing, no "in this meeting"
framing. Use plain markdown. Keep it terse — the user can scroll the full
transcript if they want detail.

Output, in order:

# <title>
4–8 words describing what this meeting actually was (e.g. "Arlo Head of
Engineering Interview", "Witness Auto-Stop Bug Triage"). If a calendar title
is provided, use it verbatim. Otherwise generate one from the content.

Then these sections, all required (use "(none)" if empty):

## TL;DR
One short paragraph. What happened, what it means.

## Decisions
Bulleted. Each a single declarative line. Only actual decisions, not discussion.

## Action items
Bulleted, formatted as `- [ ] <owner>: <what> (due <when>)`. Owner is the
speaker who took it on. Omit "due ..." if not stated.

## Open questions
Bulleted. Anything raised but not resolved.

## Notable quotes
Up to 3, only if genuinely useful context. Format: `> "quote" — Speaker`.
"""


def _load_oauth_token() -> str:
    path = Path.home() / ".claude" / ".credentials.json"
    creds = json.loads(path.read_text())
    return creds["claudeAiOauth"]["accessToken"]


def _build_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(
        auth_token=_load_oauth_token(),
        default_headers={
            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
        },
    )


def _user_prompt(folder: Path, transcript_md: str, meta: dict[str, Any]) -> str:
    parts = [f"Meeting folder: `{folder.name}`"]
    cal = meta.get("calendar_event") or {}
    if cal.get("summary"):
        parts.append(f"Calendar title: {cal['summary']}")
    if cal.get("start") and cal.get("end"):
        parts.append(f"Scheduled: {cal['start']} → {cal['end']}")
    if cal.get("attendees"):
        parts.append("Invited: " + ", ".join(cal["attendees"]))
    started = meta.get("started_at")
    ended = meta.get("ended_at")
    if started and ended:
        parts.append(f"Recorded: {started} → {ended}")
    parts.append("")
    parts.append("Transcript follows. Speakers are named (post-fingerprint) "
                 "or labeled `Room N` / `Remote N` / `Spk N` when unresolved.")
    parts.append("")
    parts.append(transcript_md)
    return "\n".join(parts)


def summarize(folder: Path) -> Path:
    """Generate folder/summary.md. Returns the output path."""
    tmd = folder / "transcript.md"
    if not tmd.exists():
        raise FileNotFoundError(f"{tmd} missing — run render first")
    transcript_md = tmd.read_text()

    meta_path = folder / "metadata.json"
    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            pass

    client = _build_client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_prompt(folder, transcript_md, meta)}],
    )
    body = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    ).strip()

    out = folder / "summary.md"
    out.write_text(body + "\n")
    return out
