"""Render `transcript.jsonl` into a human-readable `transcript.md`.

Groups consecutive utterances from the same speaker into paragraphs,
adds [MM:SS] offsets at each speaker change, and resolves speaker IDs
via `speakers.json` if present (so `Spk 2` becomes the resolved name
post-fingerprint). Idempotent: always overwrites transcript.md.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _speaker_label(evt: dict[str, Any], resolved: dict[str, str]) -> str:
    sp = evt.get("speaker") or ""
    # Legacy (pre-AEC meetings): mic utterances were hard-coded with the
    # speaker tag "ben". Kept for back-compat re-rendering of older
    # transcripts; new captures use mic_speaker_N instead.
    if sp == "ben":
        return "You"
    # Follow indirection: mic_speaker_0 → unknown_5285b8 → "Alex". The chain
    # is built by `witness relabel` adding new mappings without rewriting old
    # ones, which keeps the raw Deepgram → embedding-hash → name lineage debuggable.
    seen: set[str] = set()
    cur = sp
    while cur in resolved and cur not in seen:
        seen.add(cur)
        cur = resolved[cur]
    if cur != sp:
        return cur
    # Unresolved: produce a readable fallback with channel hint.
    for prefix, tag in (
        ("mic_speaker_", "Room"),
        ("system_speaker_", "Remote"),
        ("speaker_", "Spk"),  # legacy
    ):
        if sp.startswith(prefix):
            return f"{tag} {sp[len(prefix):]}"
    if evt.get("channel") == "system":
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

    resolved: dict[str, str] = {}
    sp_path = folder / "speakers.json"
    if sp_path.exists():
        try:
            resolved = json.loads(sp_path.read_text())
        except json.JSONDecodeError:
            pass

    lines: list[str] = [f"# {folder.name}", ""]
    last_speaker: str | None = None
    for e in events:
        who = _speaker_label(e, resolved)
        text = e["text"].strip()
        if who != last_speaker:
            lines.append("")
            lines.append(f"**{who}** · [{_fmt_clock(e.get('ts_start'))}]")
            last_speaker = who
        lines.append(text)
    out.write_text("\n".join(lines).rstrip() + "\n")
    return out
