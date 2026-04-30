"""LLM-based speaker resolver: map system_speaker_N → real names using calendar
invitees + utterance samples. Runs after fingerprint (so voiceprint matches win)
and before summarize. Skipped silently when nothing is unresolved or credentials
are unavailable.

Why this exists: Deepgram diarization on the system channel often (a) splits one
remote speaker into multiple clusters and (b) leaves them unnamed when no
voiceprint is enrolled. Claude can read 3-5 sample utterances per cluster
alongside the meeting's calendar invitee list and produce a `system_speaker_N
→ Name` map, collapsing aliases of the same person in the process.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .summarize import _build_client

log = logging.getLogger("witness")

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024
SAMPLES_PER_SPEAKER = 5
SAMPLE_MAX_CHARS = 240

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,40}$")
_UNRESOLVED_PREFIXES = ("system_speaker_", "unknown_")

_SYSTEM_PROMPT = """You map anonymized speaker IDs in a meeting transcript to real names.

Input: meeting title, calendar invitees (with email-derived first-name guesses), and
3-5 utterance samples per speaker ID.

Return ONLY a JSON object — no prose, no code fences — mapping each given ID to either:
  - a real name string (use the form from the invitee list, typically first name), OR
  - null, if you cannot identify the speaker confidently.

Rules:
  - Two IDs CAN map to the same name. Diarization frequently splits one person into
    multiple clusters; if two IDs sound like the same person (overlapping topics,
    complementary halves of sentences, identical first-person references), assign
    them the same name.
  - Only use names from the invitee list. Do not guess.
  - If only one non-user invitee exists, assign every system_speaker_* to that
    person.
  - Return null only when genuinely ambiguous.
"""


def _email_to_guess(email: str) -> str:
    """ben.solwitz@... → 'Ben'."""
    local = email.split("@", 1)[0]
    first = re.split(r"[._-]", local, 1)[0]
    return first.capitalize() if first else email


def _terminal(speaker: str, resolved: dict[str, str], depth: int = 8) -> str:
    """Walk the alias chain to its terminus (or last seen on cycle)."""
    seen: set[str] = set()
    cur = speaker
    while cur in resolved and cur not in seen and depth > 0:
        seen.add(cur)
        cur = resolved[cur]
        depth -= 1
    return cur


def _is_real_name(terminal: str) -> bool:
    return not terminal.startswith(_UNRESOLVED_PREFIXES)


def _gather_speaker_samples(events: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Up to N longest utterances per system_speaker_N — short ones are
    backchannels ('yeah', 'mhm') and aren't useful for ID."""
    by_speaker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        sp = e.get("speaker") or ""
        if e.get("channel") == "system" and sp.startswith("system_speaker_"):
            by_speaker[sp].append(e)
    samples: dict[str, list[str]] = {}
    for sp, evts in by_speaker.items():
        evts.sort(key=lambda e: -len((e.get("text") or "")))
        chosen: list[str] = []
        for e in evts:
            text = (e.get("text") or "").strip()
            if not text:
                continue
            if len(text) > SAMPLE_MAX_CHARS:
                text = text[:SAMPLE_MAX_CHARS].rsplit(" ", 1)[0] + "…"
            chosen.append(text)
            if len(chosen) >= SAMPLES_PER_SPEAKER:
                break
        if chosen:
            samples[sp] = chosen
    return samples


def _user_prompt(meta: dict[str, Any], samples: dict[str, list[str]]) -> str:
    cal = meta.get("calendar_event") or {}
    title = cal.get("summary") or "(no title)"
    attendees = cal.get("attendees") or []
    self_email = cal.get("self_email")

    lines = [f"Meeting title: {title}", "", "Invitees (the recording user is excluded):"]
    for a in attendees:
        if a == self_email:
            continue
        lines.append(f"  - {_email_to_guess(a)} <{a}>")
    lines.append("")
    lines.append("Speakers to identify (mic channel is the user, not shown):")
    for sp, utts in samples.items():
        lines.append(f"\n{sp}:")
        for u in utts:
            lines.append(f"  - {u}")
    lines.append("")
    lines.append('Return ONLY JSON, e.g. {"system_speaker_0": "Gareth", "system_speaker_1": "Gareth"}')
    return "\n".join(lines)


def _validate(raw: Any, valid_ids: set[str], valid_names: set[str]) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for sp, name in raw.items():
        if sp not in valid_ids or name is None:
            continue
        if not isinstance(name, str) or not _NAME_RE.match(name.strip()):
            log.warning("identify: dropping malformed name %r for %s", name, sp)
            continue
        n = name.strip()
        if n not in valid_names:
            log.warning("identify: %s → %r not in invitee list; dropping", sp, n)
            continue
        out[sp] = n
    return out


def resolve(folder: Path) -> Path | None:
    """Update folder/speakers.json with LLM-inferred names. Returns the path
    when a write happens, None when skipped."""
    jsonl = folder / "transcript.jsonl"
    meta_path = folder / "metadata.json"
    sp_path = folder / "speakers.json"
    if not jsonl.exists():
        log.info("identify: transcript.jsonl missing — skipping")
        return None

    events: list[dict[str, Any]] = []
    for line in jsonl.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            pass
    cal = meta.get("calendar_event") or {}
    attendees = cal.get("attendees") or []
    self_email = cal.get("self_email")
    remote_attendees = [a for a in attendees if a != self_email]
    if not remote_attendees:
        log.info("identify: no remote calendar attendees — skipping")
        return None

    resolved: dict[str, str] = {}
    if sp_path.exists():
        try:
            resolved = json.loads(sp_path.read_text())
        except json.JSONDecodeError:
            pass

    samples = _gather_speaker_samples(events)
    unresolved = {sp: u for sp, u in samples.items()
                  if not _is_real_name(_terminal(sp, resolved))}
    if not unresolved:
        log.info("identify: nothing to resolve — skipping")
        return None

    valid_names = {_email_to_guess(a) for a in remote_attendees}

    try:
        client = _build_client()
    except RuntimeError as e:
        log.info("identify: %s — skipping", e)
        return None

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_prompt(meta, unresolved)}],
    )
    body = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body)
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        log.warning("identify: non-JSON response, skipping. First 200 chars: %r", body[:200])
        return None

    new_map = _validate(raw, set(unresolved), valid_names)
    if not new_map:
        log.info("identify: no usable names produced")
        return None

    # Write each name to the *terminus* of its alias chain. If fingerprint had
    # produced `system_speaker_1 → unknown_5a3f1b`, we write `unknown_5a3f1b →
    # Gareth` rather than overwriting the system_speaker_1 pointer. Mirrors
    # `witness relabel` and keeps the embedding-hash → name lineage intact.
    for sp, name in new_map.items():
        resolved[_terminal(sp, resolved)] = name
    sp_path.write_text(json.dumps(resolved, indent=2, sort_keys=True) + "\n")
    log.info("identify: resolved %d speaker(s): %s", len(new_map), new_map)
    return sp_path
