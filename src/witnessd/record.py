"""2-channel meeting capture: mic on ch0, system audio on ch1.

Produces three outputs from a single ffmpeg process:
  * audio.opus  — 2-channel Ogg/Opus on disk (canonical archive)
  * mic PCM     — 16kHz mono s16le → inherited fd (for live transcription)
  * system PCM  — 16kHz mono s16le → inherited fd (for live transcription)

The two PCM pipes drive per-channel Deepgram WebSocket streams. Mic-channel
utterances belong to the user (the local mic) without needing diarization;
system-channel is diarized by Deepgram (and later resolved to names by the
post-meeting fingerprint step).

The ffmpeg input section is per-OS — see `_platform.get_platform().plan_capture()`.
The filter graph + opus output + live PCM tap are shared across platforms,
so the on-disk format (`audio.opus`, ch0=mic, ch1=system) is identical.

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

from ._platform import CapturePlan, ffmpeg_path, get_platform
from .config import DEEPGRAM_SAMPLE_RATE, MEETINGS_ROOT


@dataclass
class Recording:
    slug: str
    folder: Path
    audio_path: Path
    metadata_path: Path
    sources_metadata: dict[str, str]
    started_at: str
    proc: subprocess.Popen = field(repr=False)
    # Read-ends of PCM pipes for live transcription. None if live=False.
    mic_pcm_fd: int | None = None
    system_pcm_fd: int | None = None
    # Auxiliary processes (e.g. the macOS witness-audiotap) that need to
    # be torn down with the session. ffmpeg is rec.proc; everything else
    # is here.
    aux_procs: list[subprocess.Popen] = field(default_factory=list, repr=False)


def _ffmpeg_cmd(
    plan: CapturePlan,
    out_path: Path,
    live: bool,
    mic_pcm_fd: int | None,
    system_pcm_fd: int | None,
) -> list[str]:
    """Build the ffmpeg argv from a platform CapturePlan.

    Inputs and filter wiring are platform-specific (see _platform_linux /
    _platform_darwin). The shared shape is: one opus archive output plus
    optionally two raw 16kHz mono s16le PCM pipe outputs for live Deepgram.
    `-shortest` on every output makes ffmpeg wind down when any input
    closes, which is how shutdown is driven on macOS (where we close the
    witness-audiotap pipe to terminate).
    """
    cmd = [
        ffmpeg_path(),
        "-hide_banner",
        "-loglevel", "warning",
        *plan.ffmpeg_inputs,
    ]
    if plan.archive_filter:
        cmd += ["-filter_complex", plan.archive_filter]

    cmd += [
        *plan.archive_map,
        "-ac", "2",
        "-c:a", "libopus",
        "-b:a", "48k",
        "-application", "voip",
        "-shortest",
        "-y",
        str(out_path),
    ]

    if live:
        assert mic_pcm_fd is not None and system_pcm_fd is not None
        # Mic live PCM
        cmd += list(plan.mic_pcm_map)
        if plan.mic_pcm_af:
            cmd += ["-af", plan.mic_pcm_af]
        cmd += [
            "-f", "s16le", "-ar", str(DEEPGRAM_SAMPLE_RATE), "-ac", "1",
            "-shortest",
            f"pipe:{mic_pcm_fd}",
        ]
        # System live PCM
        cmd += list(plan.sys_pcm_map)
        if plan.sys_pcm_af:
            cmd += ["-af", plan.sys_pcm_af]
        cmd += [
            "-f", "s16le", "-ar", str(DEEPGRAM_SAMPLE_RATE), "-ac", "1",
            "-shortest",
            f"pipe:{system_pcm_fd}",
        ]
    return cmd


def start(slug: str, root: Path = MEETINGS_ROOT, live: bool = True) -> Recording:
    folder = root / slug
    folder.mkdir(parents=True, exist_ok=True)
    audio_path = folder / "audio.opus"
    metadata_path = folder / "metadata.json"

    plan: CapturePlan = get_platform().plan_capture()
    started_at = datetime.now(timezone.utc).isoformat()

    mic_pcm_fd: int | None = None
    system_pcm_fd: int | None = None
    parent_reads: list[int] = []
    child_writes: list[int] = []
    if live:
        r_mic, w_mic = os.pipe()
        r_sys, w_sys = os.pipe()
        parent_reads = [r_mic, r_sys]
        child_writes = [w_mic, w_sys]
        mic_pcm_fd = w_mic
        system_pcm_fd = w_sys

    pass_fds = (*child_writes, *plan.extra_pass_fds)
    cmd = _ffmpeg_cmd(plan, audio_path, live, mic_pcm_fd, system_pcm_fd)

    try:
        # start_new_session: own process group so we can SIGINT (Linux fallback
        # path in interrupt()) without hitting parent. On macOS, shutdown comes
        # from closing the audiotap pipe — see interrupt() for the rationale.
        proc = subprocess.Popen(cmd, start_new_session=True, pass_fds=pass_fds)
    except BaseException:
        # ffmpeg failed to spawn — clean up the aux procs the platform started
        # (e.g. witness-audiotap) so we don't leak them.
        for ap in plan.aux_procs:
            try:
                ap.terminate()
            except (ProcessLookupError, PermissionError):
                pass
        for fd in (*child_writes, *plan.aux_fds_to_close_in_parent):
            try:
                os.close(fd)
            except OSError:
                pass
        raise

    # Close fds the parent doesn't need: the write ends of our PCM pipes
    # (ffmpeg owns them), and any platform-supplied fds it asked us to drop.
    for fd in (*child_writes, *plan.aux_fds_to_close_in_parent):
        try:
            os.close(fd)
        except OSError:
            pass

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
        "sources": plan.sources_metadata,
        "ffmpeg_pid": proc.pid,
        "live_transcription": live,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2))

    return Recording(
        slug=slug,
        folder=folder,
        audio_path=audio_path,
        metadata_path=metadata_path,
        sources_metadata=plan.sources_metadata,
        started_at=started_at,
        proc=proc,
        mic_pcm_fd=parent_reads[0] if parent_reads else None,
        system_pcm_fd=parent_reads[1] if parent_reads else None,
        aux_procs=list(plan.aux_procs),
    )


def interrupt(rec: Recording) -> None:
    """Ask ffmpeg to wind down gracefully and write trailers.

    Order matters:
      1. SIGTERM auxiliary input processes (e.g. macOS witness-audiotap)
         FIRST. Their pipes close, ffmpeg sees EOF on those inputs, and
         the filter graph can drain its buffers.
      2. Then SIGINT ffmpeg's process group. SIGINT alone isn't enough on
         macOS because the avfoundation demuxer's read loop blocks
         indefinitely on its sample-buffer queue and won't unblock from a
         signal — the EOF on the pipe input is what lets the main loop
         move past the avfoundation read. Signal alone hangs; closed pipe
         alone hangs (avfoundation keeps producing); both together → ffmpeg
         exits cleanly with proper output trailers.

    Signal-handler-safe.
    """
    for ap in rec.aux_procs:
        if ap.poll() is None:
            try:
                ap.terminate()
            except (ProcessLookupError, PermissionError):
                pass

    # Only signal ffmpeg directly when there are no aux input procs whose
    # closure should drive shutdown (i.e. Linux, where ffmpeg pulls from
    # PulseAudio sockets that we can't close from outside).
    if not rec.aux_procs and rec.proc.poll() is None:
        try:
            os.killpg(os.getpgid(rec.proc.pid), signal.SIGINT)
        except (ProcessLookupError, PermissionError):
            pass


def wait_for_exit(rec: Recording, hard_timeout_s: float = 60.0) -> int:
    """Block until ffmpeg exits. Escalate to SIGTERM/SIGKILL if it hangs.
    Then reap any auxiliary processes.

    The default timeout is generous because ffmpeg's shutdown cost scales
    with the encoder backlog when there are several outputs draining at
    different rates. Sending SIGTERM mid-flush kicks ffmpeg into 'immediate
    exit' mode which skips trailer writes (leaving a 0-byte opus). The
    actual common case finishes in well under a second; we just don't want
    to escalate prematurely under the rare slow-flush case.
    """
    try:
        code = rec.proc.wait(timeout=hard_timeout_s)
    except subprocess.TimeoutExpired:
        rec.proc.terminate()
        try:
            code = rec.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rec.proc.kill()
            code = rec.proc.wait()

    for ap in rec.aux_procs:
        try:
            ap.wait(timeout=2)
        except subprocess.TimeoutExpired:
            ap.kill()
            try:
                ap.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    return code


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
