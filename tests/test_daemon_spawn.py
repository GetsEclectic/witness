"""Which pipeline invocation the daemon spawns at each session transition.

A pause run must not delete audio (the recording may still grow); the
terminal stop must. When the stop arrives on an already-paused session the
audio has already been transcribed by the pause run, so only the prune step
is spawned — a full re-run would bill another summary for nothing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from witnessd import daemon as daemon_mod
from witnessd.daemon import Daemon


class _Session:
    def __init__(self, folder: Path, paused: bool) -> None:
        self.folder = folder
        self.is_paused = paused
        self.stopped = False

    async def pause(self) -> None:
        self.is_paused = True

    async def stop(self) -> None:
        self.stopped = True


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, tuple[str, ...]]]:
    spawned: list[tuple[Path, tuple[str, ...]]] = []
    monkeypatch.setattr(
        daemon_mod, "_spawn_witness",
        lambda folder, *args: spawned.append((folder, args)),
    )
    return spawned


def test_pause_spawns_a_plain_run(tmp_path: Path, monkeypatch) -> None:
    spawned = _capture(monkeypatch)
    d = Daemon()
    d.session = _Session(tmp_path, paused=False)

    asyncio.run(d._pause_current())

    assert spawned == [(tmp_path, ())]


def test_stop_from_recording_spawns_a_final_run(tmp_path: Path, monkeypatch) -> None:
    spawned = _capture(monkeypatch)
    d = Daemon()
    d.session = _Session(tmp_path, paused=False)

    asyncio.run(d._finalize_current())

    assert spawned == [(tmp_path, ("--final",))]
    assert d.session is None


def test_stop_from_paused_spawns_prune_only(tmp_path: Path, monkeypatch) -> None:
    spawned = _capture(monkeypatch)
    d = Daemon()
    d.session = _Session(tmp_path, paused=True)

    asyncio.run(d._finalize_current())

    assert spawned == [(tmp_path, ("--step", "prune"))]


def test_spawn_passes_args_through_to_the_pipeline(tmp_path: Path, monkeypatch) -> None:
    import subprocess
    seen: list[list[str]] = []

    class _Popen:
        def __init__(self, cmd, **kwargs) -> None:
            seen.append(cmd)

    monkeypatch.setattr(subprocess, "Popen", _Popen)

    daemon_mod._spawn_witness(tmp_path, "--final")

    assert seen[0][-2:] == [str(tmp_path), "--final"]
    assert seen[0][1:3] == ["-m", "witness"]
