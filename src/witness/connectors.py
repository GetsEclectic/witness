"""Whether the follow-up worker's MCP connectors are signed in.

A connector's OAuth can lapse while the CLI login stays good; the worker
then runs anyway and quietly writes paste-in files instead of Slack drafts.
`check()` asks `claude mcp get` in the worker's environment and caches the
answer in `ACTIONS_STATE_DIR/connectors.json`, which the web UI reads to
show a banner.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from witnessd import config

log = logging.getLogger("witness.connectors")

SLACK = "plugin:slack:slack"
STALE_AFTER = timedelta(minutes=15)


def status_path() -> Path:
    return config.ACTIONS_STATE_DIR / "connectors.json"


def worker_env() -> dict[str, str]:
    env = dict(os.environ)
    # An API-class setup token here would shadow the login that carries the
    # Slack and calendar connectors.
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return env


def worker_cwd() -> Path:
    return config.FOLLOWUP_CWD if config.FOLLOWUP_CWD.is_dir() else Path.home()


def _probe(name: str) -> bool | None:
    """True when connected, False when `claude` says otherwise, None when
    the probe itself failed and says nothing about the connector."""
    try:
        out = subprocess.run(
            [config.CLAUDE_BIN, "mcp", "get", name],
            cwd=worker_cwd(), env=worker_env(), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.exception("claude mcp get %s failed", name)
        return None
    for line in out.stdout.splitlines():
        if line.strip().startswith("Status:"):
            return "Connected" in line
    return None


def load() -> dict[str, Any]:
    try:
        return json.loads(status_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def check() -> dict[str, Any]:
    """Probe Slack now and record it. A failed probe keeps the last answer."""
    prior = load()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ok = _probe(SLACK)
    if ok is None:
        return prior
    was = prior.get("slack") or {}
    slack = {"connected": ok, "checked_at": now,
             "since": was.get("since") if was.get("connected") == ok else now}
    data = {**prior, "slack": slack}
    path = status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)
    return data


def current() -> dict[str, Any]:
    """The cached status, re-probed when it is missing or stale."""
    data = load()
    checked = (data.get("slack") or {}).get("checked_at")
    try:
        fresh = checked and datetime.now(timezone.utc) - datetime.fromisoformat(checked) < STALE_AFTER
    except ValueError:
        fresh = False
    return data if fresh else check()
