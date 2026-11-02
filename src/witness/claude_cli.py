"""One completion through the local `claude` CLI.

A Claude Code subscription token reaches only Haiku 4.5 over the raw API;
Haiku 5.5, Sonnet 5.5 and Opus 5.5 answer 429. The CLI is Claude Code
itself, so it gets every model the subscription does on the same login, and
an `ANTHROPIC_API_KEY` in the environment still wins. Tools, MCP, settings
and session persistence are off: this is a single completion, not an agent.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from typing import Any

from witnessd import config

log = logging.getLogger("witness.claude_cli")


def complete(
    system: str,
    prompt: str,
    model: str,
    json_schema: dict[str, Any] | None = None,
    timeout: int = 600,
) -> dict[str, Any]:
    """Return the CLI's JSON result: `result` holds the text, and
    `structured_output` the object when `json_schema` is given."""
    cmd = [
        config.CLAUDE_BIN, "-p", "--model", model, "--system-prompt", system,
        "--tools", "", "--strict-mcp-config", "--setting-sources", "",
        "--disable-slash-commands", "--no-session-persistence",
        "--output-format", "json",
    ]
    if json_schema is not None:
        cmd += ["--json-schema", json.dumps(json_schema)]
    env = dict(os.environ)
    # An API-class setup token here would shadow the subscription login.
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
        cwd=tempfile.gettempdir(), env=env,
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"claude -p exited {proc.returncode}: {(proc.stderr or proc.stdout)[-500:]}"
        ) from None
    if out.get("is_error") or proc.returncode != 0:
        raise RuntimeError(f"claude -p failed: {str(out.get('result'))[:500]}")
    usage = out.get("usage") or {}
    log.info(
        "%s: $%.4f, %d in (%d cached), %d out",
        model, out.get("total_cost_usd") or 0,
        usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
        usage.get("cache_read_input_tokens", 0), usage.get("output_tokens", 0),
    )
    return out
