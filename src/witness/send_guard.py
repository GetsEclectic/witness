"""PreToolUse hook for the follow-up worker: the only thing it may send is
one Slack DM to the recorder.

Permission rules match MCP tools by name only, and the digest and a
forbidden post differ only in `channel_id`. Exit 2 blocks the call.

Usage: send_guard.py <recorder-slack-user-id>  (event JSON on stdin)
"""
from __future__ import annotations

import json
import re
import sys

MATCHER = r"mcp__.*(send_message|schedule_message|reply|forward|create_event|update_event|delete_event|respond_to_event)$"
_SLACK_SEND = re.compile(r"slack_send_message$")


def blocked(event: dict, self_id: str) -> str | None:
    """Why this call must not run, or None to let it through."""
    name = event.get("tool_name") or ""
    if not re.search(MATCHER, name):
        return None
    inp = event.get("tool_input") or {}
    if _SLACK_SEND.search(name) and self_id and inp.get("channel_id") == self_id and not inp.get("thread_ts"):
        return None
    return (
        f"{name} is blocked for the witness follow-up worker. Everything it produces is a draft: "
        "Slack via slack_send_message_draft, documents as files under the meeting's followup/ "
        f"directory. The one send allowed is the digest, slack_send_message to channel_id {self_id}."
    )


def main() -> int:
    self_id = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    reason = blocked(event, self_id)
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
