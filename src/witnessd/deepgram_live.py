"""Deepgram streaming WebSocket client, one connection per audio channel.

Reads raw 16kHz mono s16le PCM from an OS file descriptor (pipe from ffmpeg),
streams it to Deepgram, and emits parsed utterance events via a callback.

Two instances run in parallel — one for the mic channel, one for system audio.
Both channels are diarized; speaker IDs are namespaced as `mic_speaker_N` /
`system_speaker_N` so in-room voices don't collide with remote participants.
Real names come from `speakers.json` via `witness relabel` or the post-meeting
fingerprint step.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal

import websockets

from .config import DEEPGRAM_MODEL, DEEPGRAM_SAMPLE_RATE

log = logging.getLogger("witnessd.deepgram_live")

Channel = Literal["mic", "system"]
EventHandler = Callable[["TranscriptEvent"], Awaitable[None]]

# Send audio in ~200ms chunks: 16000 samples/s * 2 bytes/sample * 0.2s = 6400 B.
# 8KiB is close enough; pipe reads don't have to land on chunk boundaries.
READ_CHUNK = 8192


@dataclass
class TranscriptEvent:
    channel: Channel
    is_final: bool
    text: str
    ts_start: float | None
    ts_end: float | None
    speaker: str  # "{channel}_speaker_N" from Deepgram's diarizer, "" when unknown
    received_at: str  # ISO8601 UTC


def _build_url(channel: Channel, keyterms: list[str] | None = None) -> str:
    params: dict[str, object] = {
        "model": DEEPGRAM_MODEL,
        "encoding": "linear16",
        "sample_rate": str(DEEPGRAM_SAMPLE_RATE),
        "channels": "1",
        "interim_results": "true",
        "punctuate": "true",
        "smart_format": "true",
        "utterance_end_ms": "1000",
        "vad_events": "true",
        # Diarize the system channel only — multiple remote speakers there.
        # The mic channel is post-AEC and always the local user; running
        # diarization on it produces spurious mic_speaker_1 clusters from
        # background-noise segments that then need manual relabeling.
        "diarize": "true" if channel == "system" else "false",
    }
    if keyterms:
        # Nova-3 keyterm prompting: repeated `keyterm=` query params, one per
        # phrase. urlencode(doseq=True) expands the list; spaces become '+'.
        params["keyterm"] = list(keyterms)
    return "wss://api.deepgram.com/v1/listen?" + urllib.parse.urlencode(params, doseq=True)


def _parse_results_message(
    msg: dict, channel: Channel
) -> TranscriptEvent | None:
    """Extract a TranscriptEvent from a Deepgram Results frame, or None if
    there's no transcript text in it (silence / filler)."""
    if msg.get("type") != "Results":
        return None
    alternatives = msg.get("channel", {}).get("alternatives") or []
    if not alternatives:
        return None
    alt = alternatives[0]
    text = (alt.get("transcript") or "").strip()
    if not text:
        return None

    start = msg.get("start")
    duration = msg.get("duration")
    ts_end = (start + duration) if (start is not None and duration is not None) else None

    # Speaker resolution: take the speaker of the first word with a tag.
    # Namespace by channel so mic's speaker_0 and system's speaker_0 don't
    # collide when one physical person is on each side.
    speaker = ""
    for word in alt.get("words") or []:
        if "speaker" in word:
            speaker = f"{channel}_speaker_{word['speaker']}"
            break

    return TranscriptEvent(
        channel=channel,
        is_final=bool(msg.get("is_final")),
        text=text,
        ts_start=start,
        ts_end=ts_end,
        speaker=speaker,
        received_at=datetime.now(timezone.utc).isoformat(),
    )


async def _open_pcm_reader(fd: int) -> asyncio.StreamReader:
    """Wrap a raw OS file descriptor as an asyncio StreamReader."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, os.fdopen(fd, "rb", 0))
    return reader


def _sweep_stale_transports() -> int:
    """Evict zombie entries from the running loop's `_transports` dict.

    Python 3.14's `_SelectorTransport._call_connection_lost` calls
    `self._sock.close()` from a `loop.call_soon` callback. If the underlying
    socket was already closed (e.g. SSL teardown closed the raw fd before
    asyncio's protocol-cleanup tick fired), `socket._real_close` raises
    `OSError: EBADF`, the callback aborts, and the rest of cleanup — which
    is where the transport gets removed from `loop._transports` — never
    runs. The next WS connect on a recycled fd then hits
    `_ensure_fd_no_transport` and raises `RuntimeError: File descriptor N
    is used by transport`. The retry-and-wait in `_connect_with_retry`
    only helps when the cleanup is genuinely *pending*; for a leaked
    transport (callback already crashed) it never resolves.

    `socket.close()` sets `_closed = True` before calling `_real_close`,
    so even when the close path raised EBADF the tombstone is reliable.
    We treat `_sock._closed` (or `_sock is None`) as "this transport is
    done; drop it from the registry."
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    transports = getattr(loop, "_transports", None)
    if not transports:
        return 0
    evicted = 0
    for fd in list(transports.keys()):
        t = transports.get(fd)
        if t is None:
            continue
        # _UnixReadPipeTransport has no _sock attribute; skip it.
        # Socket transports always have _sock (possibly None if cleared, or
        # with _closed=True when the socket closed before asyncio cleaned up).
        if not hasattr(t, "_sock"):
            continue
        sock = t._sock
        if sock is None or getattr(sock, "_closed", False):
            if transports.pop(fd, None) is not None:
                evicted += 1
    if evicted:
        log.warning(
            "swept %d stale transport(s) from loop._transports before connect",
            evicted,
        )
    return evicted


async def _connect_with_retry(url: str, headers: dict[str, str]):
    """Open the WS, retrying past a stale-transport RuntimeError.

    Two distinct race conditions trigger `_ensure_fd_no_transport`:

      1. A prior session's `connection_lost` callback hasn't run yet (the
         loop is one tick behind). The transport will clear itself on the
         next tick. Sleeping briefly and retrying recovers cleanly.
      2. A prior session's `connection_lost` callback already ran and
         raised EBADF, leaving a zombie transport in `loop._transports`
         that nothing else will ever clean up. `_sweep_stale_transports`
         catches this case before each attempt by walking `_transports`
         and dropping entries whose socket is already closed.

    Caller handles the post-retry failure: `run()` catches the
    re-raised RuntimeError and triggers a daemon re-exec, since at that
    point the asyncio loop is in a state we can't safely continue from.
    """
    attempt = 0
    while True:
        _sweep_stale_transports()
        try:
            return await websockets.connect(url, additional_headers=headers)
        except RuntimeError as e:
            if "is used by transport" not in str(e) or attempt >= 4:
                raise
            log.warning(
                "deepgram connect hit stale transport (attempt %d): %s", attempt + 1, e
            )
            await asyncio.sleep(0.1 * (attempt + 1))
            attempt += 1


async def _drain(reader: asyncio.StreamReader) -> None:
    """Read and discard PCM until the pipe closes.

    When Deepgram is unreachable (connect fails or the WS dies before EOF),
    nothing on our side reads the PCM pipe ffmpeg is writing into. ffmpeg's
    write blocks once the pipe buffer fills, which stalls every output it
    owns — including the canonical `audio.opus` archive. The transcript may
    be lost for this segment, but the on-disk archive is the artifact the
    user actually needs, so we keep the pipe drained until session wind-down
    closes ffmpeg's write end.
    """
    try:
        while True:
            chunk = await reader.read(READ_CHUNK)
            if not chunk:
                return
    except (ConnectionError, OSError, asyncio.IncompleteReadError):
        return


def _reexec_for_corrupted_loop(reason: str) -> None:
    """Hard-exit so the process supervisor (launchctl) hands us a fresh loop.

    When `_connect_with_retry` exhausts its budget on a stale-transport
    RuntimeError, the asyncio loop's `_transports` is in a state our sweep
    couldn't repair and every future Deepgram connect in this process will
    hit the same wall. The cheapest reliable recovery is to die and let
    `com.witness.daemon` (KeepAlive=SuccessfulExit:false) respawn us with
    a clean interpreter.

    Uses `os._exit` rather than `sys.exit` because the corrupted loop can't
    be trusted to run normal Python shutdown — interpreter teardown awaits
    on the same broken transports and would hang us indefinitely.

    Exit code 75 = `EX_TEMPFAIL` (sysexits.h): a transient failure, retry
    fine. Useful when grepping launchctl logs to distinguish this case
    from other crashes.
    """
    log.error("re-execing daemon: %s", reason)
    os._exit(75)


async def _close_safely(ws) -> None:
    """Close `ws` and wait for transport teardown, surviving cancellation.

    Without this, when our task is being cancelled (the session's
    wind-down path cancel+gathers us), the WS `close()` await is itself
    cancelled, the TLS/TCP teardown never completes, and the transport
    sticks around in the event loop's `_transports` dict — breaking the
    next session's connect on the recycled fd (Python 3.14 + websockets 16).
    """
    async def _do_close() -> None:
        try:
            await ws.close()
        except Exception:
            pass
        try:
            await ws.wait_closed()
        except Exception:
            pass

    close_task = asyncio.ensure_future(_do_close())
    cancelled = False
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError()


async def run(
    pcm_fd: int,
    channel: Channel,
    api_key: str,
    on_event: EventHandler,
    keyterms: list[str] | None = None,
) -> None:
    """Stream PCM from `pcm_fd` to Deepgram; await until EOF on the pipe.

    On transcription failure (Deepgram unreachable, auth bad, etc.) we drain
    the PCM pipe instead of letting it back up. ffmpeg shares one process
    across the opus archive and the two PCM tap outputs; if any tap blocks,
    the whole process stalls and `audio.opus` ends up at zero bytes. The
    canonical archive is the artifact that matters — losing the live
    transcript for one segment is recoverable (re-transcribe offline);
    losing the audio isn't.

    For the specific failure mode where `_connect_with_retry` exhausts its
    budget against `RuntimeError: ... is used by transport`, we re-exec
    the daemon: the asyncio loop is corrupted in a way that affects every
    future session, so dying here is strictly better than soldiering on
    and silently failing to capture meetings for hours.
    """
    url = _build_url(channel, keyterms=keyterms)
    headers = {"Authorization": f"Token {api_key}"}

    reader = await _open_pcm_reader(pcm_fd)

    try:
        ws = await _connect_with_retry(url, headers)
    except RuntimeError as e:
        if "is used by transport" in str(e):
            _reexec_for_corrupted_loop(
                f"{channel} deepgram connect: stale-transport leak unrecoverable: {e}"
            )
        log.error(
            "%s deepgram connect failed; draining pcm so opus capture survives "
            "(this segment's transcript will be empty): %s", channel, e,
        )
        await _drain(reader)
        raise
    except Exception as e:
        log.error(
            "%s deepgram connect failed; draining pcm so opus capture survives "
            "(this segment's transcript will be empty): %s", channel, e,
        )
        await _drain(reader)
        raise

    try:
        async def send_loop() -> None:
            try:
                while True:
                    chunk = await reader.read(READ_CHUNK)
                    if not chunk:
                        # ffmpeg closed the pipe — tell Deepgram to finish up.
                        await ws.send(json.dumps({"type": "CloseStream"}))
                        return
                    await ws.send(chunk)
            except (ConnectionError, websockets.ConnectionClosed):
                return

        async def recv_loop() -> None:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                evt = _parse_results_message(msg, channel)
                if evt is not None:
                    await on_event(evt)

        # Run both loops until the sender finishes (EOF on ffmpeg pipe); then
        # the Deepgram server closes the WS, which ends recv_loop.
        sender = asyncio.create_task(send_loop())
        receiver = asyncio.create_task(recv_loop())
        try:
            await sender
            await receiver
        finally:
            for t in (sender, receiver):
                if not t.done():
                    t.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
    finally:
        await _close_safely(ws)
    # If the WS closed before ffmpeg wrote EOF (Deepgram disconnected early,
    # session interrupted, etc.), the PCM pipe still has a writer. Without a
    # reader the asyncio StreamReader buffer fills to the 64KB high-water
    # mark, asyncio pauses the transport, the kernel pipe buffer fills, and
    # ffmpeg blocks on its PCM tap writes — stalling the opus archive write
    # too. Drain to EOF so ffmpeg can finish and audio.opus gets its bytes.
    await _drain(reader)
