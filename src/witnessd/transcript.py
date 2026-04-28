"""Transcript event bus: persist finalized utterances to disk + broadcast
all events (including interim ones) to connected web-UI clients.

Only `is_final=true` events are written to transcript.jsonl. Interim events
churn every few hundred ms and would bloat the file; the live UI handles
them by overwriting the same DOM node until a final arrives.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

from .deepgram_live import TranscriptEvent


def _event_to_dict(evt: TranscriptEvent) -> dict[str, Any]:
    return dataclasses.asdict(evt)


class EventBus:
    """Fan out transcript events to a JSONL file + a set of WebSocket subscribers."""

    def __init__(self, jsonl_path: Path) -> None:
        self.jsonl_path = jsonl_path
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        # Open in append mode, line-buffered, so `tail -f` works in real time.
        self._file = jsonl_path.open("a", buffering=1, encoding="utf-8")
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    async def emit(self, evt: TranscriptEvent) -> None:
        payload = _event_to_dict(evt)
        if evt.is_final:
            async with self._lock:
                self._file.write(json.dumps(payload) + "\n")
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow subscriber — drop the oldest to keep up.
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

    def close(self) -> None:
        self._file.close()
        # Wake any WebSocket handlers waiting on queue.get(). They'll see a
        # None sentinel and disconnect; the browser reconnects and picks up
        # whatever bus is current (or "no_bus" when the daemon is idle).
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._subscribers.clear()
