"""PreToolUse hook for the follow-up worker: it may not send anything.

Permission rules match MCP tools by name, and auto mode decides the rest by
judgement; this makes every send a hard stop instead. Slack drafts
(`slack_send_message_draft`) don't match and stay allowed. Exit 2 blocks
the call.

Usage: send_guard.py  (event JSON on stdin)
"""
from __future__ import annotations

import json
import re
import sys

MATCHER = r"mcp__.*(send_message|schedule_message|reply|forward|create_event|update_event|delete_event|respond_to_event)$"


def blocked(event: dict) -> str | None:
    """Why this call must not run, or None to let it through."""
    name = event.get("tool_name") or ""
    if not re.search(MATCHER, name):
        return None
    return (
        f"{name} is blocked for the witness follow-up worker. Everything it produces is a draft: "
        "Slack via slack_send_message_draft, documents as files under the meeting's followup/ "
        "directory. Report back through `witness actions update`, not a message."
    )


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    reason = blocked(event)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
