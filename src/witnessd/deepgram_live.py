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
import os
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal

import websockets

from .config import DEEPGRAM_MODEL, DEEPGRAM_SAMPLE_RATE

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


async def run(
    pcm_fd: int,
    channel: Channel,
    api_key: str,
    on_event: EventHandler,
    keyterms: list[str] | None = None,
) -> None:
    """Stream PCM from `pcm_fd` to Deepgram; await until EOF on the pipe."""
    url = _build_url(channel, keyterms=keyterms)
    headers = {"Authorization": f"Token {api_key}"}

    reader = await _open_pcm_reader(pcm_fd)

    async with websockets.connect(url, additional_headers=headers) as ws:

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
