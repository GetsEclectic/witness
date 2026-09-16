"""Render `transcript.jsonl` into a human-readable `transcript.md`.

Groups consecutive utterances from the same speaker into paragraphs and adds
[MM:SS] offsets at each speaker change. Idempotent: always overwrites
transcript.md.

Speakers are the two capture channels: the mic is the local user, system
audio is everyone else. Older transcripts also carry a per-utterance
`speaker` from the retired diarization path; it is ignored — that path
never attributed reliably, which is why it was removed.

The mic channel carries the recorder's own name, falling back to "You" only
when no invite and no `WITNESS_USER_NAME` can supply one.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from witnessd import config


def display_name(email: str) -> str:
    """`ben.solwitz@equipmentshare.com` → `Ben Solwitz`; a guess, not a lookup."""
    local = (email or "").split("@", 1)[0]
    parts = [p for p in local.replace("_", ".").replace("-", ".").split(".") if p]
    return " ".join(p[:1].upper() + p[1:] for p in parts)


def read_metadata(folder: Path) -> dict[str, Any]:
    """`metadata.json`, or `{}` when absent or unparseable. Never raises."""
    path = folder / "metadata.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def recorder_name(meta: dict[str, Any] | None) -> str | None:
    """The recorder's display name, or None if this meeting can't supply one."""
    cal = (meta or {}).get("calendar_event") or {}
    if self_email := cal.get("self_email"):
        return display_name(self_email)
    return config.USER_NAME or None


def _speaker_label(evt: dict[str, Any], me: str = "You") -> str:
    channel = evt.get("channel")
    if channel == "mic":
        return me
    if channel == "system":
        return "Remote"
    return "?"


def _fmt_clock(sec: float | None) -> str:
    if sec is None:
        return "??:??"
    s = int(sec)
    return f"{s // 60:02d}:{s % 60:02d}"


def render(folder: Path) -> Path:
    jsonl = folder / "transcript.jsonl"
    out = folder / "transcript.md"
    me = recorder_name(read_metadata(folder)) or "You"
    events: list[dict[str, Any]] = []
    if jsonl.exists():
        for line in jsonl.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # Keep only final utterances with text, sorted by ts_start (jsonl order
    # is ~chronological already, but system and mic channels interleave).
    events = [
        e for e in events
        if e.get("is_final") and (e.get("text") or "").strip()
    ]
    events.sort(key=lambda e: (e.get("ts_start") or 0, e.get("received_at") or ""))

    lines: list[str] = [f"# {folder.name}", ""]
    last_speaker: str | None = None
    for e in events:
        who = _speaker_label(e, me)
        text = e["text"].strip()
        if who != last_speaker:
            lines.append("")
            lines.append(f"**{who}** · [{_fmt_clock(e.get('ts_start'))}]")
            last_speaker = who
        lines.append(text)
    out.write_text("\n".join(lines).rstrip() + "\n")
    return out
