"""One-shot backfill: add `calendar_event.self_email` to existing meetings.

`self_email` (the recording user's address, taken from Google's
`attendee.self == true`) was added to CalendarEvent in commit cc8a8e1.
The post-meeting `identify` step uses it to drop the user from the
candidate name set so the LLM can't pick the user's own name for a
remote speaker cluster. Older recordings predate that field; this
script fills it in.

Usage:
    python scripts/backfill_self_email.py --self-email you@example.com
    python scripts/backfill_self_email.py --self-email you@example.com --dry-run

Idempotent: meetings whose metadata already has `self_email` are skipped.
Only acts when the email actually appears in the meeting's attendee list
(don't invent a self attendee for meetings the user wasn't on).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ROOT = Path.home() / "meetings"


def backfill_one(meta_path: Path, self_email: str, dry_run: bool) -> str:
    try:
        meta = json.loads(meta_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        return f"skip ({e})"
    cal = meta.get("calendar_event")
    if not isinstance(cal, dict):
        return "skip (no calendar_event)"
    if cal.get("self_email"):
        return "skip (already set)"
    attendees = cal.get("attendees") or []
    if self_email not in attendees:
        return f"skip (self_email {self_email!r} not in attendees)"
    cal["self_email"] = self_email
    if dry_run:
        return f"would set self_email={self_email}"
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    return f"set self_email={self_email}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--self-email", required=True, help="email to set as self_email")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help=f"meetings directory (default: {DEFAULT_ROOT})")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.root.exists():
        print(f"meetings root not found: {args.root}", file=sys.stderr)
        return 2

    metas = sorted(args.root.glob("*/metadata.json"))
    if not metas:
        print(f"no metadata.json files under {args.root}")
        return 0

    for meta_path in metas:
        result = backfill_one(meta_path, args.self_email, args.dry_run)
        print(f"{meta_path.parent.name}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
