"""Render `transcript.jsonl` into a human-readable `transcript.md`.

Groups consecutive utterances from the same speaker into paragraphs and adds
[MM:SS] offsets at each speaker change. Idempotent: always overwrites
transcript.md.

Speakers are the two capture channels: the mic is the local user, system
audio is everyone else. Older transcripts also carry a per-utterance
`speaker` from the retired diarization path; it is ignored — that path
never attributed reliably, which is why it was removed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _speaker_label(evt: dict[str, Any]) -> str:
    channel = evt.get("channel")
    if channel == "mic":
        return "You"
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
        who = _speaker_label(e)
        text = e["text"].strip()
        if who != last_speaker:
            lines.append("")
            lines.append(f"**{who}** · [{_fmt_clock(e.get('ts_start'))}]")
            last_speaker = who
        lines.append(text)
    out.write_text("\n".join(lines).rstrip() + "\n")
    return out
