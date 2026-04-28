"""2-channel meeting capture: mic on ch0, system audio on ch1.

Produces three outputs from a single ffmpeg process:
  * audio.opus  — 2-channel Ogg/Opus on disk (canonical archive)
  * mic PCM     — 16kHz mono s16le → inherited fd (for live transcription)
  * system PCM  — 16kHz mono s16le → inherited fd (for live transcription)

The two PCM pipes drive per-channel Deepgram WebSocket streams. Mic-channel
utterances belong to the user (the local mic) without needing diarization;
system-channel is diarized by Deepgram (and later resolved to names by the
post-meeting fingerprint step).

Lifecycle:
    rec = start(slug, live=True)
    # use rec.mic_pcm_fd / rec.system_pcm_fd in asyncio readers
    ...
    interrupt(rec)
    wait_for_exit(rec)
    finalize(rec)

interrupt() is signal-handler-safe (just sends SIGINT to ffmpeg's process
group). wait_for_exit() and finalize() must NOT be called from a signal
handler that fires while someone is already inside rec.proc.wait() on the
same thread — subprocess's internal lock isn't reentrant.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import DEEPGRAM_SAMPLE_RATE, MEETINGS_ROOT


ECHO_CANCEL_SOURCE = "echo-cancel-source"
ECHO_CANCEL_SINK = "echo-cancel-sink"


def _pactl(*args: str) -> str:
    return subprocess.check_output(["pactl", *args], text=True).strip()


def _source_exists(name: str) -> bool:
    try:
        out = subprocess.check_output(
            ["pactl", "list", "sources", "short"], text=True
        )
    except subprocess.CalledProcessError:
        return False
    return any(line.split("\t", 2)[1] == name for line in out.splitlines() if "\t" in line)


def resolve_sources() -> tuple[str, str]:
    """Return (mic_source, system_monitor_source).

    Prefers the PipeWire echo-cancel virtual source/sink when loaded: the mic
    is captured post-AEC (laptop-speaker bleed subtracted) and system audio
    is captured pre-speaker from echo-cancel-sink.monitor. Falls back to the
    OS defaults when the AEC module isn't loaded.

    The full chain only works if apps actually play through echo-cancel-sink,
    which requires it to be the default sink. When the module is loaded but
    isn't the default sink, we still use it for mic and system capture — the
    mic will be a pass-through (no reference signal to cancel), which is no
    worse than not using it.
    """
    if _source_exists(ECHO_CANCEL_SOURCE):
        return ECHO_CANCEL_SOURCE, f"{ECHO_CANCEL_SINK}.monitor"
    mic = _pactl("get-default-source")
    sink = _pactl("get-default-sink")
    return mic, f"{sink}.monitor"


@dataclass
class Recording:
    slug: str
    folder: Path
    audio_path: Path
    metadata_path: Path
    mic_source: str
    system_source: str
    started_at: str
    proc: subprocess.Popen = field(repr=False)
    # Read-ends of PCM pipes for live transcription. None if live=False.
    mic_pcm_fd: int | None = None
    system_pcm_fd: int | None = None


def _ffmpeg_cmd(
    mic: str,
    system: str,
    out_path: Path,
    live: bool,
    mic_pcm_fd: int | None,
    system_pcm_fd: int | None,
) -> list[str]:
    # Downmix each stereo source to mono. For live mode, split each mono stream
    # into two copies — one for the 2-channel opus merge, one for raw PCM out.
    # asplit is required because a single labeled output can only be mapped once.
    if live:
        filter_chain = (
            "[0:a]pan=mono|c0=c0+c1,asplit=2[mic_a][mic_b];"
            "[1:a]pan=mono|c0=c0+c1,asplit=2[sys_a][sys_b];"
            "[mic_a][sys_a]amerge=inputs=2[merged]"
        )
    else:
        filter_chain = (
            "[0:a]pan=mono|c0=c0+c1[mic_a];"
            "[1:a]pan=mono|c0=c0+c1[sys_a];"
            "[mic_a][sys_a]amerge=inputs=2[merged]"
        )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-f", "pulse", "-i", mic,
        "-f", "pulse", "-i", system,
        "-filter_complex", filter_chain,
        # Opus archive output.
        "-map", "[merged]",
        "-ac", "2",
        "-c:a", "libopus",
        "-b:a", "48k",
        "-application", "voip",
        "-y",
        str(out_path),
    ]

    if live:
        assert mic_pcm_fd is not None and system_pcm_fd is not None
        # Raw PCM outputs for Deepgram. ffmpeg resamples to 16k via -ar.
        cmd += [
            "-map", "[mic_b]",
            "-f", "s16le", "-ar", str(DEEPGRAM_SAMPLE_RATE), "-ac", "1",
            f"pipe:{mic_pcm_fd}",
            "-map", "[sys_b]",
            "-f", "s16le", "-ar", str(DEEPGRAM_SAMPLE_RATE), "-ac", "1",
            f"pipe:{system_pcm_fd}",
        ]
    return cmd


def start(slug: str, root: Path = MEETINGS_ROOT, live: bool = True) -> Recording:
    folder = root / slug
    folder.mkdir(parents=True, exist_ok=True)
    audio_path = folder / "audio.opus"
    metadata_path = folder / "metadata.json"

    mic, system = resolve_sources()
    started_at = datetime.now(timezone.utc).isoformat()

    mic_pcm_fd: int | None = None
    system_pcm_fd: int | None = None
    pass_fds: tuple[int, ...] = ()
    # Pipes: parent reads r, ffmpeg writes w. The fd numbers in the child will
    # be different from ours; subprocess remaps pass_fds to their own numbers.
    # We pass OUR fd numbers in pipe:N args — pass_fds preserves those same
    # numbers in the child process.
    parent_reads: list[int] = []
    child_writes: list[int] = []
    if live:
        r_mic, w_mic = os.pipe()
        r_sys, w_sys = os.pipe()
        parent_reads = [r_mic, r_sys]
        child_writes = [w_mic, w_sys]
        mic_pcm_fd = w_mic
        system_pcm_fd = w_sys
        pass_fds = (w_mic, w_sys)

    cmd = _ffmpeg_cmd(mic, system, audio_path, live, mic_pcm_fd, system_pcm_fd)

    # start_new_session: own process group so we can SIGINT without hitting parent.
    proc = subprocess.Popen(cmd, start_new_session=True, pass_fds=pass_fds)

    # Close the child-owned ends in the parent so we only read, not write.
    for fd in child_writes:
        os.close(fd)

    metadata = {
        "slug": slug,
        "started_at": started_at,
        "ended_at": None,
        "audio": {
            "path": "audio.opus",
            "channels": {"0": "mic", "1": "system"},
            "codec": "opus",
            "container": "ogg",
        },
        "sources": {"mic": mic, "system": system},
        "ffmpeg_pid": proc.pid,
        "live_transcription": live,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2))

    return Recording(
        slug=slug,
        folder=folder,
        audio_path=audio_path,
        metadata_path=metadata_path,
        mic_source=mic,
        system_source=system,
        started_at=started_at,
        proc=proc,
        mic_pcm_fd=parent_reads[0] if parent_reads else None,
        system_pcm_fd=parent_reads[1] if parent_reads else None,
    )


def interrupt(rec: Recording) -> None:
    """Signal-handler-safe: send SIGINT to ffmpeg's process group."""
    if rec.proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(rec.proc.pid), signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        pass


def wait_for_exit(rec: Recording, hard_timeout_s: float = 15.0) -> int:
    """Block until ffmpeg exits. Escalate to SIGTERM/SIGKILL if it hangs."""
    try:
        return rec.proc.wait(timeout=hard_timeout_s)
    except subprocess.TimeoutExpired:
        rec.proc.terminate()
        try:
            return rec.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            rec.proc.kill()
            return rec.proc.wait()


def finalize(rec: Recording) -> None:
    """Close any open PCM pipes, update metadata.json with end time."""
    for fd in (rec.mic_pcm_fd, rec.system_pcm_fd):
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    meta = json.loads(rec.metadata_path.read_text())
    meta["ended_at"] = datetime.now(timezone.utc).isoformat()
    meta["exit_code"] = rec.proc.returncode
    rec.metadata_path.write_text(json.dumps(meta, indent=2))


def stop(rec: Recording) -> None:
    """Convenience: interrupt + wait + finalize. Do NOT call from a signal
    handler that can fire while another stack frame is in rec.proc.wait()."""
    interrupt(rec)
    wait_for_exit(rec)
    finalize(rec)
