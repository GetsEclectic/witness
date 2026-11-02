"""PreToolUse hook for the follow-up worker: it may not send anything.

Permission rules match MCP tools by name, and auto mode decides the rest by
judgement; this makes every send a hard stop instead. Slack drafts
(`slack_send_message_draft`) don't match and stay allowed. Exit 2 blocks
the call.

The Slack token behind `witness slack` can post, so for FILE_MATCHER tools
any input that names the token file, a raw token or the Web API is blocked
too: the worker reaches Slack only through that read-only command.

Usage: send_guard.py  (event JSON on stdin)
"""
from __future__ import annotations

import json
import os
import re
import sys

MATCHER = r"mcp__.*(send_message|schedule_message|reply|forward|create_event|update_event|delete_event|respond_to_event)$"
FILE_MATCHER = "Bash|Read|Grep|Glob|Write|Edit|NotebookEdit"
_TOKEN_MARKERS = ("slack-read-token", "xoxp-", "xoxb-", "xoxe", "slack.com/api")


def _token_markers() -> tuple[str, ...]:
    custom = os.environ.get("WITNESS_SLACK_TOKEN_FILE")
    return _TOKEN_MARKERS + ((os.path.basename(custom),) if custom else ())


def blocked(event: dict) -> str | None:
    """Why this call must not run, or None to let it through."""
    name = event.get("tool_name") or ""
    if re.fullmatch(FILE_MATCHER, name):
        raw = json.dumps(event.get("tool_input") or {})
        if any(m in raw for m in _token_markers()):
            return (
                f"{name} touches the Slack token or the Slack Web API, which is blocked for the "
                "witness follow-up worker. Use the read-only `witness slack` command instead."
            )
        return None
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
