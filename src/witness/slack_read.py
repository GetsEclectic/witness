"""Read-only Slack Web API calls for the follow-up worker.

The token behind this can also post, so the guard is here, not in Slack:
`call` refuses any method outside READ_METHODS, and there is no way to name
another one from the CLI. The worker reaches Slack only through
`witness slack ...`; send_guard keeps it from reading the token itself.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any, Iterator

from witnessd import config

API = "https://slack.com/api/"
READ_METHODS = frozenset({
    "auth.test",
    "search.messages",
    "conversations.history",
    "conversations.replies",
    "conversations.info",
    "users.info",
    "users.list",
    "users.lookupByEmail",
    "users.conversations",
})
USERS_CACHE_TTL = 24 * 3600


class SlackError(RuntimeError):
    pass


def token() -> str:
    path = config.SLACK_TOKEN_FILE
    try:
        tok = path.read_text().strip()
    except OSError:
        raise SlackError(f"no Slack token at {path}")
    if not tok:
        raise SlackError(f"{path} is empty")
    return tok


def call(method: str, **params: Any) -> dict[str, Any]:
    if method not in READ_METHODS:
        raise SlackError(f"{method} is not a read method")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(
        f"{API}{method}?{query}", headers={"Authorization": f"Bearer {token()}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    if not data.get("ok"):
        raise SlackError(f"{method}: {data.get('error')}")
    return data


def _pages(method: str, key: str, **params: Any) -> Iterator[dict[str, Any]]:
    cursor = None
    while True:
        data = call(method, cursor=cursor, **params)
        yield from data.get(key) or []
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return


def users() -> list[dict[str, Any]]:
    """Every active human in the workspace, cached for a day."""
    cache = config.ACTIONS_STATE_DIR / "slack-users.json"
    try:
        if time.time() - cache.stat().st_mtime < USERS_CACHE_TTL:
            return json.loads(cache.read_text())
    except (OSError, json.JSONDecodeError):
        pass
    out = [
        {"id": u["id"], "name": u.get("name"),
         "real_name": (u.get("profile") or {}).get("real_name") or u.get("real_name"),
         "title": (u.get("profile") or {}).get("title"),
         "email": (u.get("profile") or {}).get("email")}
        for u in _pages("users.list", "members", limit=1000)
        if not u.get("deleted") and not u.get("is_bot") and u.get("id") != "USLACKBOT"
    ]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out


def find_users(name: str) -> list[dict[str, Any]]:
    """Users whose handle or real name contains every word of `name`."""
    words = name.lower().split()
    return [
        u for u in users()
        if all(w in f"{u.get('name') or ''} {u.get('real_name') or ''}".lower() for w in words)
    ]


def user(who: str) -> dict[str, Any]:
    if "@" in who:
        return call("users.lookupByEmail", email=who)["user"]
    return call("users.info", user=who)["user"]


def dm_channel(user_id: str) -> str | None:
    for c in _pages("users.conversations", "channels", types="im", limit=1000):
        if c.get("user") == user_id:
            return c["id"]
    return None


def search(query: str, count: int = 20) -> list[dict[str, Any]]:
    data = call("search.messages", query=query, count=count, sort="timestamp")
    return (data.get("messages") or {}).get("matches") or []


def history(channel: str, limit: int = 30, oldest: str | None = None) -> list[dict[str, Any]]:
    return call("conversations.history", channel=channel, limit=limit, oldest=oldest)["messages"]


def thread(channel: str, ts: str) -> list[dict[str, Any]]:
    return call("conversations.replies", channel=channel, ts=ts, limit=200)["messages"]


def fmt_message(m: dict[str, Any]) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(m.get("ts") or 0)))
    who = m.get("username") or m.get("user") or "?"
    where = (m.get("channel") or {}).get("name") if isinstance(m.get("channel"), dict) else None
    head = f"{when}  {who}" + (f"  #{where}" if where else "") + f"  ts={m.get('ts')}"
    if m.get("permalink"):
        head += f"\n  {m['permalink']}"
    if m.get("reply_count"):
        head += f"  ({m['reply_count']} replies)"
    text = (m.get("text") or "").strip().replace("\n", "\n  ")
    return f"{head}\n  {text}"
