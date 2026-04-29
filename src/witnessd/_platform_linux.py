"""Linux capture + detection: PipeWire/PulseAudio via pactl, ffmpeg -f pulse.

The pure-Python parsers (_parse_pactl_blocks, _is_live, _classify) are
re-exported by witnessd.detect for tests; they have no system deps and
import cleanly on any platform. The probe + capture methods shell out to
pactl and will only succeed on Linux.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from ._platform import CapturePlan
from .detect import Detection


ECHO_CANCEL_SOURCE = "echo-cancel-source"
ECHO_CANCEL_SINK = "echo-cancel-sink"


# --- pactl parsing (pure functions, used by tests) ---

_BLOCK_HEADER = re.compile(r"^(?:Source Output|Sink Input) #(\d+)")
_KV = re.compile(r"^\s+([\w.\-]+)\s*=\s*\"(.*)\"\s*$")
# PipeWire's pactl emits `Corked: no/yes` and `Mute: no/yes` instead of the
# classic PulseAudio `State:` field. Both styles are handled.
_TOP_LEVEL = re.compile(r"^\s*(Corked|Mute|State):\s*(\S+)")

_MEET_TITLE = re.compile(r"^Meet\s*[-–—]\s*(.+)$")


def _parse_pactl_blocks(output: str) -> list[dict[str, str]]:
    """Parse `pactl list source-outputs|sink-inputs` into a list of
    property dicts, one per block. Top-level fields like Corked/Mute/State
    are stored under keys prefixed with `__` to keep the namespace clean.
    `__index` holds the `Source Output #N` / `Sink Input #N` number."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in output.splitlines():
        if (m := _BLOCK_HEADER.match(line)):
            if current is not None:
                blocks.append(current)
            current = {"__index": m.group(1)}
            continue
        if current is None:
            continue
        if (m := _TOP_LEVEL.match(line)):
            current[f"__{m.group(1).lower()}"] = m.group(2)
            continue
        if (m := _KV.match(line)):
            current[m.group(1)] = m.group(2)
    if current is not None:
        blocks.append(current)
    return blocks


def _is_live(block: dict[str, str]) -> bool:
    """True iff this source-output is actively capturing (not muted/corked)."""
    if block.get("__mute") == "yes":
        return False
    if block.get("__corked") == "yes":
        return False
    state = block.get("__state")
    if state is not None and state != "RUNNING":
        return False
    return True


def _classify(block: dict[str, str]) -> tuple[str, str] | None:
    """Return (platform, display_title) if the block looks like an
    active call, else None."""
    media = block.get("media.name", "")
    app = block.get("application.name", "")
    binary = block.get("application.process.binary", "")

    if media.startswith("Meet -") or media.startswith("Meet –") or media.startswith("Meet —"):
        return "meet", media
    if "Zoom" in media or "zoom" in binary.lower() or app == "ZOOM VoiceEngine":
        title = media if media else "Zoom Meeting"
        return "zoom", title
    if "Microsoft Teams" in media or "teams" in binary.lower() or "Teams" in app:
        title = media if media else "Microsoft Teams"
        return "teams", title
    return None


# --- system probes (Linux-only, shell out to pactl) ---

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


def _resolve_sources() -> tuple[str, str]:
    """Return (mic_source, system_monitor_source).

    Prefers the PipeWire echo-cancel virtual source/sink when loaded: the mic
    is captured post-AEC (laptop-speaker bleed subtracted) and system audio
    is captured pre-speaker from echo-cancel-sink.monitor. Falls back to the
    OS defaults when the AEC module isn't loaded.
    """
    if _source_exists(ECHO_CANCEL_SOURCE):
        return ECHO_CANCEL_SOURCE, f"{ECHO_CANCEL_SINK}.monitor"
    mic = _pactl("get-default-source")
    sink = _pactl("get-default-sink")
    return mic, f"{sink}.monitor"


@dataclass
class LinuxPlatform:
    def detect_meeting(self) -> Detection | None:
        try:
            out = subprocess.check_output(
                ["pactl", "list", "source-outputs"],
                text=True,
                timeout=3,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            return None
        for block in _parse_pactl_blocks(out):
            if not _is_live(block):
                continue
            hit = _classify(block)
            if hit is None:
                continue
            platform, title = hit
            pid_s = block.get("application.process.id")
            pid = int(pid_s) if pid_s and pid_s.isdigit() else None
            idx_s = block.get("__index")
            idx = int(idx_s) if idx_s and idx_s.isdigit() else None
            return Detection(
                platform=platform,
                title=title,
                source="pactl",
                application_pid=pid,
                application_name=block.get("application.name"),
                source_output_index=idx,
            )
        return None

    def plan_capture(self) -> CapturePlan:
        mic, system = _resolve_sources()
        return CapturePlan(
            ffmpeg_inputs=[
                "-f", "pulse", "-i", mic,
                "-f", "pulse", "-i", system,
            ],
            sources_metadata={"mic": mic, "system": system},
            # Two stereo pulse inputs → downmix each to mono and merge into
            # a 2-channel [merged] stream (mic on ch0, system on ch1).
            archive_filter=(
                "[0:a]pan=mono|c0=c0+c1[mic_a];"
                "[1:a]pan=mono|c0=c0+c1[sys_a];"
                "[mic_a][sys_a]amerge=inputs=2[merged]"
            ),
            archive_map=["-map", "[merged]"],
            mic_pcm_map=["-map", "0:a"],
            sys_pcm_map=["-map", "1:a"],
        )
