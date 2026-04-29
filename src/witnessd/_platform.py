"""Per-OS audio capture + meeting detection.

Linux uses PipeWire/pactl + system ffmpeg. macOS uses a CoreAudio Process
Tap (via the bundled `mac/witness-audiotap` Swift binary) for system audio,
ffmpeg avfoundation for the mic, and pyobjc + osascript for detection.

Shared code in record.py builds the same filter graph and on-disk
`audio.opus` (ch0=mic, ch1=system) on both platforms; the platform module
only owns input resolution and any auxiliary processes (e.g. the audio
tap) that need to live for the duration of the recording.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .detect import Detection


@dataclass
class CapturePlan:
    """Per-platform inputs + filter wiring for a single recording session.

    The shared record.start() builds an ffmpeg invocation by splicing
    `ffmpeg_inputs` into a fixed argv frame, passes `extra_pass_fds` so
    ffmpeg can read from pipes that auxiliary processes are writing into,
    and tracks `aux_procs` for cleanup at stop time.

    The filter / mapping fields tell record.py how to produce the three
    output streams (opus archive, optional mic-mono PCM, optional system-
    mono PCM) from whatever input layout the platform delivers:

      * Linux: two pulse inputs at the same rate. archive_filter merges
        them into a 2-channel [merged] stream; per-channel mic/sys PCM
        maps directly off the original input streams.
      * macOS: one 2-channel pipe input from witness-audiotap (ch0 = mic,
        ch1 = system, already mixed in Swift). The opus output reads the
        input directly; live PCM uses pan filters to extract each channel.
    """
    ffmpeg_inputs: list[str]
    extra_pass_fds: tuple[int, ...] = ()
    aux_procs: list[subprocess.Popen] = field(default_factory=list)
    aux_fds_to_close_in_parent: list[int] = field(default_factory=list)
    sources_metadata: dict[str, str] = field(default_factory=dict)

    # filter_complex string producing labeled outputs the maps reference,
    # or empty when no -filter_complex is needed.
    archive_filter: str = ""
    # -map argument(s) for the opus archive output.
    archive_map: list[str] = field(default_factory=lambda: ["-map", "0:a"])

    # -map and per-output -af for the two live PCM streams (used only when
    # live=True). The -af is the audio filter chain applied to that output;
    # output-format args (-f s16le -ar 16000 -ac 1) are added by record.py.
    mic_pcm_map: list[str] = field(default_factory=lambda: ["-map", "0:a"])
    mic_pcm_af: str = ""
    sys_pcm_map: list[str] = field(default_factory=lambda: ["-map", "1:a"])
    sys_pcm_af: str = ""


class Platform(Protocol):
    def detect_meeting(self) -> "Detection | None": ...
    def plan_capture(self) -> CapturePlan: ...


def get_platform() -> Platform:
    if sys.platform == "darwin":
        from . import _platform_darwin
        return _platform_darwin.DarwinPlatform()
    from . import _platform_linux
    return _platform_linux.LinuxPlatform()


def ffmpeg_path() -> str:
    """Resolve the ffmpeg binary. Mac uses the bundled imageio-ffmpeg
    wheel (so users don't need brew); Linux uses the system ffmpeg
    documented as a system dep in README."""
    if sys.platform == "darwin":
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    return "ffmpeg"
