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


class _FakeClosedSock:
    """Mimics socket._closed=True with no live fd — the tombstone shape a
    real socket carries after `_real_close` raises EBADF."""
    def __init__(self) -> None:
        self._closed = True


class _FakeOpenSock:
    def __init__(self) -> None:
        self._closed = False


class _FakeTransport:
    def __init__(self, sock) -> None:
        self._sock = sock


def test_sweep_stale_transports_evicts_zombies() -> None:
    """The leak we saw on 2026-05-14: a transport whose connection_lost
    callback crashed with EBADF leaves the entry in loop._transports
    forever. The sweep is what unblocks the next session's connect.

    Note: `loop._transports` is a WeakValueDictionary, so the test must
    keep strong references to the fake transports for the duration of
    the assertion — otherwise GC frees them between insertion and sweep
    and the test trivially "passes" without exercising the sweep at all.
    The production leak case stays in the dict because the real broken
    transport is reachable through the loop's selector/protocol graph.
    """
    async def go() -> None:
        loop = asyncio.get_running_loop()
        zombie_closed = _FakeTransport(_FakeClosedSock())
        zombie_no_sock = _FakeTransport(None)
        live = _FakeTransport(_FakeOpenSock())
        # Hold strong refs explicitly so the WeakValueDictionary keeps them.
        _refs = [zombie_closed, zombie_no_sock, live]  # noqa: F841
        try:
            loop._transports[9991] = zombie_closed
            loop._transports[9992] = live
            loop._transports[9993] = zombie_no_sock
            evicted = deepgram_live._sweep_stale_transports()
            assert evicted == 2
            assert 9991 not in loop._transports
            assert 9992 in loop._transports
            assert 9993 not in loop._transports
        finally:
            loop._transports.pop(9991, None)
            loop._transports.pop(9992, None)
            loop._transports.pop(9993, None)

    asyncio.run(go())


def test_sweep_stale_transports_handles_empty_loop() -> None:
    """No transports → no-op, returns 0 cleanly. The sweep runs before
    every connect attempt; cheap-path must not log/throw."""
    async def go() -> None:
        loop = asyncio.get_running_loop()
        assert deepgram_live._sweep_stale_transports() == 0
        assert loop._transports == {} or loop._transports  # whatever it was

    asyncio.run(go())


def test_drain_consumes_until_eof() -> None:
    """The drain keeps ffmpeg unblocked when Deepgram is dead. We feed a
    StreamReader some bytes then EOF; drain should return cleanly."""
    async def go() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"\x00" * 16000)
        reader.feed_data(b"\x01" * 4000)
        reader.feed_eof()
        await deepgram_live._drain(reader)  # should return promptly
        assert reader.at_eof()

    asyncio.run(go())


def test_drain_swallows_pipe_errors() -> None:
    """If the underlying pipe errors mid-drain, we still need to return so
    the deepgram task ends and the session can wind down."""
    class _ErroringReader:
        async def read(self, n: int) -> bytes:
            raise ConnectionError("pipe died")

    async def go() -> None:
        await deepgram_live._drain(_ErroringReader())  # no raise

    asyncio.run(go())


def test_run_drains_pcm_when_connect_fails(monkeypatch) -> None:
    """The bug that ate yesterday's afternoon: Deepgram connect failure
    aborted before draining, ffmpeg's PCM pipe filled, the whole ffmpeg
    process stalled, and audio.opus ended up at zero bytes. Run must drain
    the PCM pipe before re-raising so opus capture survives."""
    drained: list[bool] = []

    async def fake_open_pcm_reader(fd: int):
        r = asyncio.StreamReader()
        r.feed_data(b"\x00" * 1000)
        r.feed_eof()
        return r, None

    async def fake_connect(url, *, additional_headers):
        raise ConnectionError("deepgram unreachable")

    async def fake_drain(reader):
        drained.append(True)
        # Still consume to keep the contract realistic.
        while True:
            chunk = await reader.read(8192)
            if not chunk:
                return

    monkeypatch.setattr(deepgram_live, "_open_pcm_reader", fake_open_pcm_reader)
    monkeypatch.setattr(deepgram_live.websockets, "connect", fake_connect)
    monkeypatch.setattr(deepgram_live, "_drain", fake_drain)

    async def go() -> None:
        with pytest.raises(ConnectionError, match="deepgram unreachable"):
            await deepgram_live.run(
                pcm_fd=-1,
                channel="system",
                api_key="x",
                on_event=lambda evt: None,  # type: ignore[arg-type]
            )
        assert drained == [True], "drain must run before re-raising"

    asyncio.run(go())


def test_run_reexecs_on_unrecoverable_stale_transport(monkeypatch) -> None:
    """The specific case where the asyncio loop's _transports has a zombie
    that neither the retry nor the sweep can clear: re-exec the daemon so
    launchctl gives us a fresh interpreter. Without this, the daemon stays
    up but silently fails to capture meetings."""
    exit_calls: list[int] = []

    async def fake_open_pcm_reader(fd: int):
        r = asyncio.StreamReader()
        r.feed_eof()
        return r, None

    async def fake_connect(url, *, additional_headers):
        raise RuntimeError(
            "File descriptor 11 is used by transport <_SelectorSocketTransport>"
        )

    def fake_exit(code: int) -> None:
        exit_calls.append(code)
        # Raise to short-circuit — real os._exit doesn't return either.
        raise SystemExit(code)

    monkeypatch.setattr(deepgram_live, "_open_pcm_reader", fake_open_pcm_reader)
    monkeypatch.setattr(deepgram_live.websockets, "connect", fake_connect)
    monkeypatch.setattr(deepgram_live.os, "_exit", fake_exit)

    async def go() -> None:
        with pytest.raises(SystemExit) as ei:
            await deepgram_live.run(
                pcm_fd=-1,
                channel="mic",
                api_key="x",
                on_event=lambda evt: None,  # type: ignore[arg-type]
            )
        assert ei.value.code == 75
        assert exit_calls == [75]

    asyncio.run(go())


def test_open_pcm_reader_does_not_own_raw_fd() -> None:
    """The EBADF regression (2026-06-03): the pipe transport must NOT close
    the raw fd. `_open_pcm_reader` opens with closefd=False so record.finalize()
    stays the single owner. If the transport owned the fd, both it and finalize
    would os.close() the same integer; once the OS recycled that number for the
    next session's pipe, the late close corrupted it and the fresh segment died
    at os.fdopen with `OSError: [Errno 9] Bad file descriptor`."""
    import os

    async def go() -> None:
        r, w = os.pipe()
        reader, transport = await deepgram_live._open_pcm_reader(r)
        os.write(w, b"hello")
        os.close(w)  # EOF
        assert await reader.read(5) == b"hello"
        assert await reader.read(5) == b""

        # Tear the transport down the way run()'s finally does, then let the
        # loop run connection_lost / the file-object close.
        deepgram_live._close_pipe_transport(transport)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # The raw fd must still be open — the transport did not close it.
        # os.fstat raises OSError(EBADF) if it had. finalize owns the one close.
        os.fstat(r)
        os.close(r)

    asyncio.run(go())


def test_close_pipe_transport_tolerates_none_and_double_close() -> None:
    """run()'s finally calls this on a transport that may be None (open never
    reached) or already torn down; it must never raise."""
    import os

    async def go() -> None:
        deepgram_live._close_pipe_transport(None)
        r, w = os.pipe()
        _, transport = await deepgram_live._open_pcm_reader(r)
        os.close(w)
        deepgram_live._close_pipe_transport(transport)
        deepgram_live._close_pipe_transport(transport)  # idempotent
        await asyncio.sleep(0)
        os.close(r)

    asyncio.run(go())
