"""Unit tests for the deepgram_live cancellation-safe close + retry helpers.

The actual streaming `run()` isn't exercised here — it needs a real
WebSocket peer (or a substantial fake) and a pipe-style fd, and the
session integration tests stub it out entirely. What this file covers is
the failure-mode plumbing that broke recording on 2026-05-11:

  - `_close_safely` must let the WS transport finish tearing down even
    when its own task is being cancelled (otherwise asyncio retains the
    transport in its `_transports` dict and the next session's connect
    hits `RuntimeError: File descriptor N is used by transport`).
  - `_connect_with_retry` must transparently recover from that specific
    RuntimeError if it shows up at connect time (defense-in-depth in case
    cleanup races with the next session opening).

Uses `asyncio.run` rather than pytest-asyncio to stay consistent with the
rest of the test suite.
"""
from __future__ import annotations

import asyncio

import pytest

from witnessd import deepgram_live


class _FakeWS:
    def __init__(self, *, close_delay: float = 0.0) -> None:
        self.close_called = False
        self.wait_closed_called = False
        self._close_delay = close_delay

    async def close(self) -> None:
        if self._close_delay:
            await asyncio.sleep(self._close_delay)
        self.close_called = True

    async def wait_closed(self) -> None:
        self.wait_closed_called = True


def test_close_safely_awaits_both_close_and_wait_closed() -> None:
    async def go() -> None:
        ws = _FakeWS()
        await deepgram_live._close_safely(ws)
        assert ws.close_called
        assert ws.wait_closed_called

    asyncio.run(go())


def test_close_safely_completes_close_when_cancelled_mid_flight() -> None:
    """The regression we're guarding: cancel the close_safely task while
    ws.close() is still in flight. The close must run to completion (so the
    transport is removed from the loop's _transports dict) before
    CancelledError surfaces to the caller."""
    async def go() -> None:
        ws = _FakeWS(close_delay=0.05)
        task = asyncio.create_task(deepgram_live._close_safely(ws))
        # Yield long enough for _do_close to start its ws.close() sleep,
        # but cancel before that sleep would naturally complete.
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ws.close_called, "ws.close() must complete despite cancellation"
        assert ws.wait_closed_called, "ws.wait_closed() must run after close"

    asyncio.run(go())


def test_close_safely_swallows_close_exceptions() -> None:
    """ws.close() failing for non-cancellation reasons should not prevent
    the caller from continuing — the transport is in some bad state but
    we've done what we can."""
    class _RaisingWS:
        async def close(self) -> None:
            raise ConnectionError("peer reset")

        async def wait_closed(self) -> None:
            return None

    async def go() -> None:
        await deepgram_live._close_safely(_RaisingWS())

    asyncio.run(go())


def test_connect_with_retry_recovers_from_stale_transport(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_connect(url, *, additional_headers):
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeError(
                "File descriptor 14 is used by transport "
                "<_SelectorSocketTransport fd=14 read=idle write=<idle, bufsize=0>>"
            )
        return _FakeWS()

    monkeypatch.setattr(deepgram_live.websockets, "connect", fake_connect)

    async def go() -> None:
        ws = await deepgram_live._connect_with_retry("wss://x", {})
        assert isinstance(ws, _FakeWS)
        assert len(calls) == 2

    asyncio.run(go())


def test_connect_with_retry_does_not_swallow_unrelated_runtime_errors(
    monkeypatch,
) -> None:
    async def fake_connect(url, *, additional_headers):
        raise RuntimeError("something else entirely")

    monkeypatch.setattr(deepgram_live.websockets, "connect", fake_connect)

    async def go() -> None:
        with pytest.raises(RuntimeError, match="something else entirely"):
            await deepgram_live._connect_with_retry("wss://x", {})

    asyncio.run(go())


def test_connect_with_retry_gives_up_after_max_attempts(monkeypatch) -> None:
    """Persistent FD-conflict — the underlying state never clears. We retry
    a bounded number of times then propagate so the session surfaces
    transcription_failed instead of hanging forever."""
    calls: list[str] = []

    async def fake_connect(url, *, additional_headers):
        calls.append(url)
        raise RuntimeError(
            "File descriptor 14 is used by transport <_SelectorSocketTransport>"
        )

    monkeypatch.setattr(deepgram_live.websockets, "connect", fake_connect)

    async def go() -> None:
        with pytest.raises(RuntimeError, match="is used by transport"):
            await deepgram_live._connect_with_retry("wss://x", {})
        # _connect_with_retry caps at 5 total attempts (initial + 4 retries).
        assert len(calls) == 5

    asyncio.run(go())
