"""Structured action items and the recorder's tracker.

`extract(folder)` turns summary.md's `## Action items` section into
`action_items.json`:

  {"slug", "summary_sha", "personal", "items": [
     {"id", "owner", "is_mine", "text", "people", "due", "transcript_ts", "kind"}]}

The summary is already the model's best reading of the transcript, so this is
a second, cheap Haiku call over summary.md plus the attendee list, not another
pass over the transcript. It is keyed on summary.md's hash: an unchanged
summary is never re-extracted, and a regenerated one always is.

The tracker is `ACTIONS_STATE_DIR/items.jsonl`, one line per item the
recorder owns, across every meeting. Lines are appended when a meeting is
first tracked and rewritten in place when an item's status changes.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from witnessd import config

from .render import display_name, read_metadata, recorder_name

log = logging.getLogger("witness.actions")

# Same constraint as summarize.MODEL: the default OAuth credential is
# entitled to Haiku only.
MODEL = os.environ.get("WITNESS_ACTIONS_MODEL") or "claude-haiku-4-5-20251001"

KINDS = ("draft_message", "write_doc", "code_change", "lookup", "schedule", "human_only")
STATUSES = ("open", "drafted", "needs_input", "done", "dropped")
RECORDER_ALIASES = {"the recorder", "recorder", "you", "me", "i"}

_SECTION_RE = re.compile(r"^## Action items\s*$(.*?)(?=^## |\Z)", re.M | re.S)
_TS_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")

_SYSTEM_PROMPT = """You convert the action items of a meeting summary into structured records.

Return one record per bullet under "## Action items", in order. Do not add items
that are not bullets there, and do not merge or split bullets.

Fields:
  owner: exactly as the bullet names it ("The recorder", a person, a team).
  text: the bullet's action, without the owner prefix or the [MM:SS] stamp.
  people: everyone the action involves besides the owner — whom to message,
    whose approval, who receives the document — spelled as the bullet spells
    them. The speech recognizer mangles names, so never correct a spelling.
  due: the deadline if one is stated ("today", "Friday", "2026-11-03"), else null.
  transcript_ts: the bullet's [MM:SS] or [H:MM:SS] stamp without brackets, else null.
  kind, the first that fits:
    draft_message — the action is mainly to message, ask, tell, or follow up with people.
    write_doc     — write up, document, draft materials, prepare a proposal.
    code_change   — change software, fix a bug, ship a feature.
    lookup        — find out, check, pull data, research a question.
    schedule      — set up or move a meeting.
    human_only    — needs the person themself: a decision, a conversation in
                    person, a phone call, an interview, anything physical.

Also set `personal` to true when the meeting itself is a personal call — family,
friends, medical, legal or financial matters of the recorder's own life — and
false for anything work-related."""

_TOOL = {
    "name": "record_action_items",
    "description": "Record the meeting's action items.",
    "input_schema": {
        "type": "object",
        "properties": {
            "personal": {"type": "boolean"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "text": {"type": "string"},
                        "people": {"type": "array", "items": {"type": "string"}},
                        "due": {"type": ["string", "null"]},
                        "transcript_ts": {"type": ["string", "null"]},
                        "kind": {"type": "string", "enum": list(KINDS)},
                    },
                    "required": ["owner", "text", "people", "kind"],
                },
            },
        },
        "required": ["personal", "items"],
    },
}


def action_items_section(summary_md: str) -> str:
    """The body of `## Action items`, or "" when the summary has none."""
    m = _SECTION_RE.search(summary_md)
    if not m:
        return ""
    body = m.group(1).strip()
    return "" if body.lower() in {"(none)", "none", "- (none)"} else body


def item_id(slug: str, text: str) -> str:
    return hashlib.sha1(f"{slug}\n{text}".encode()).hexdigest()[:8]


def owner_is_me(owner: str, me: str | None) -> bool:
    o = (owner or "").strip().lower()
    if o in RECORDER_ALIASES:
        return True
    if not me:
        return False
    me_l = me.lower()
    return o == me_l or o == me_l.split()[0]


def attendees(meta: dict[str, Any]) -> list[dict[str, str]]:
    """Invitees other than the recorder, as {name, email}. Empty when the
    recording never matched an invite."""
    cal = meta.get("calendar_event") or {}
    self_email = cal.get("self_email")
    return [
        {"name": display_name(a), "email": a}
        for a in cal.get("attendees") or []
        if a and a != self_email
    ]


def normalize(slug: str, raw: dict[str, Any], me: str | None) -> dict[str, Any]:
    """Validate the model's tool input into action_items.json's item list.

    `is_mine` is decided here, not by the model: it is the field that sends
    work to a worker, and the owner string is enough to decide it."""
    items = []
    seen: set[str] = set()
    for r in raw.get("items") or []:
        text = str(r.get("text") or "").strip()
        owner = str(r.get("owner") or "").strip()
        if not text or not owner:
            continue
        iid = item_id(slug, text)
        if iid in seen:
            continue
        seen.add(iid)
        kind = r.get("kind") if r.get("kind") in KINDS else "human_only"
        ts = str(r.get("transcript_ts") or "").strip("[] ") or None
        due = r.get("due")
        items.append({
            "id": iid,
            "owner": owner,
            "is_mine": owner_is_me(owner, me),
            "text": text,
            "people": [str(p).strip() for p in r.get("people") or [] if str(p).strip()],
            "due": str(due).strip() if due else None,
            "transcript_ts": ts if ts and _TS_RE.match(ts) else None,
            "kind": kind,
        })
    return {"personal": bool(raw.get("personal")), "items": items}


def _user_prompt(summary_md: str, meta: dict[str, Any]) -> str:
    me = recorder_name(meta)
    parts = [f"Recorder: {me}" if me else 'Recorder: unknown; the summary calls them "The recorder"']
    cal = meta.get("calendar_event") or {}
    if cal.get("summary"):
        parts.append(f"Calendar title: {cal['summary']}")
    if att := attendees(meta):
        parts.append("Invitees: " + ", ".join(a["name"] for a in att))
    parts += ["", summary_md]
    return "\n".join(parts)


def _call_model(summary_md: str, meta: dict[str, Any]) -> dict[str, Any]:
    from .summarize import _build_client
    client = _build_client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=_SYSTEM_PROMPT,
        tools=[_TOOL],
        tool_choice={"type": "tool", "name": _TOOL["name"]},
        messages=[{"role": "user", "content": _user_prompt(summary_md, meta)}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            return dict(block.input)
    raise RuntimeError("action-item extraction returned no tool call")


def extract(folder: Path) -> dict[str, Any] | None:
    """Write folder/action_items.json and return it; None without a summary."""
    from .summarize import STUB_MARKER
    summary = folder / "summary.md"
    if not summary.exists():
        return None
    summary_md = summary.read_text()
    if STUB_MARKER in summary_md:
        return None
    sha = hashlib.sha1(summary_md.encode()).hexdigest()
    out = folder / "action_items.json"
    if out.exists():
        try:
            prior = json.loads(out.read_text())
            if prior.get("summary_sha") == sha:
                return prior
        except (json.JSONDecodeError, OSError):
            pass

    meta = read_metadata(folder)
    if action_items_section(summary_md):
        parsed = normalize(folder.name, _call_model(summary_md, meta), recorder_name(meta))
    else:
        parsed = {"personal": False, "items": []}
    data = {"slug": folder.name, "summary_sha": sha, **parsed}
    out.write_text(json.dumps(data, indent=2) + "\n")
    return data


# --- tracker ---

def tracker_path() -> Path:
    return config.ACTIONS_STATE_DIR / "items.jsonl"


@contextmanager
def _locked() -> Iterator[Path]:
    path = tracker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".items.lock").open("w") as fp:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        yield path


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def load() -> list[dict[str, Any]]:
    return _read(tracker_path())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def track(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Append the recorder's items from one meeting. A meeting already in
    the tracker is left alone, so a re-run never duplicates or resets status.
    Returns the entries that were added."""
    mine = [i for i in data.get("items", []) if i.get("is_mine")]
    if data.get("personal") or not mine:
        return []
    slug = data["slug"]
    with _locked() as path:
        if any(e.get("slug") == slug for e in _read(path)):
            return []
        now = _now()
        added = [
            {**{k: i[k] for k in ("id", "owner", "text", "people", "due", "transcript_ts", "kind")},
             "slug": slug, "status": "open", "artifacts": [], "note": None,
             "created_at": now, "updated_at": now}
            for i in mine
        ]
        with path.open("a") as fp:
            for e in added:
                fp.write(json.dumps(e) + "\n")
    return added


def update(
    item: str,
    status: str | None = None,
    artifacts: list[str] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Change one entry, found by id or unique id prefix. Raises KeyError
    when nothing or more than one entry matches."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    with _locked() as path:
        entries = _read(path)
        hits = [e for e in entries if e.get("id", "").startswith(item)]
        if len(hits) != 1:
            raise KeyError(f"{len(hits)} items match {item!r}")
        e = hits[0]
        if status is not None:
            e["status"] = status
        for a in artifacts or []:
            if a not in e["artifacts"]:
                e["artifacts"].append(a)
        if note is not None:
            e["note"] = note
        e["updated_at"] = _now()
        tmp = path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(x) + "\n" for x in entries))
        tmp.replace(path)
    return e
