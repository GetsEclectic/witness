"""Daemon state-machine tests via direct _tick() drive.

We don't run the asyncio poll loop or any subprocess — we construct a
Daemon, mock the side-effecting hooks (_start_for, _pause_current,
_finalize_current, _reattach_for), drive _tick() with a controlled
detect.detect, and inspect the call ledger.

The aim is fast, deterministic coverage of:
  - 7s blip: detection-None for one tick, then back. No pause.
  - 30s+ blip: pauses, then resume on the same key returning.
  - 30+ minute blip: finalizes after RESUME_WINDOW_S.
  - Key change: finalize then start.
  - ProbeFailed: state preserved, no timer advance.
  - Orphan reattach: pending orphan key matches → reattach (not start).
  - Orphan finalized after RECOVERY_WINDOW_S without a match.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from witnessd import daemon as daemon_mod, detect
from witnessd.daemon import Daemon, RECOVERY_WINDOW_S
from witnessd.config import RECORDING_GRACE_S, RESUME_WINDOW_S


@dataclass
class _Calls:
    starts: list[detect.Detection] = field(default_factory=list)
    pauses: int = 0
    finalizes: int = 0
    reattaches: list[tuple[detect.Detection, str]] = field(default_factory=list)
    orphan_finalizes: list[Path] = field(default_factory=list)


def _make_daemon(monkeypatch: pytest.MonkeyPatch) -> tuple[Daemon, _Calls]:
    """Build a Daemon with side-effects stubbed. The session's actual lifecycle
    isn't exercised — we only verify the state machine routes detection
    events to the right hook."""
    monkeypatch.setattr(daemon_mod, "read_deepgram_key", lambda: "k")
    d = Daemon()
    d._daemon_started_at = datetime.now(timezone.utc)
    calls = _Calls()

    class _SessionStub:
        is_paused = False
        rec = object()  # truthy; daemon checks self.session.rec is not None

        @property
        def started_dt(self) -> datetime | None:
            return datetime.now(timezone.utc) - timedelta(minutes=2)

        async def resume(self) -> None:
            self.is_paused = False

    async def _stub_start(window: detect.Detection) -> None:
        calls.starts.append(window)
        d.session = _SessionStub()
        d._session_key = window.key
        d._last_match_at = datetime.now(timezone.utc)

    async def _stub_pause() -> None:
        calls.pauses += 1
        if d.session is not None:
            d.session.is_paused = True
            d.session.rec = None

    async def _stub_finalize() -> None:
        calls.finalizes += 1
        d.session = None
        d._session_key = None
        d._last_match_at = None

    async def _stub_reattach(window: detect.Detection, oc: Any) -> None:
        calls.reattaches.append((window, oc.folder.name))
        d.session = _SessionStub()
        d._session_key = window.key
        d._last_match_at = datetime.now(timezone.utc)

    monkeypatch.setattr(d, "_start_for", _stub_start)
    monkeypatch.setattr(d, "_pause_current", _stub_pause)
    monkeypatch.setattr(d, "_finalize_current", _stub_finalize)
    monkeypatch.setattr(d, "_reattach_for", _stub_reattach)
    monkeypatch.setattr(daemon_mod, "_finalize_orphan",
                        lambda folder: calls.orphan_finalizes.append(folder))
    return d, calls


def _drive_tick(d: Daemon, monkeypatch: pytest.MonkeyPatch,
                detection: detect.Detection | None | type[detect.ProbeFailed],
                advance_s: float = 0.0) -> None:
    """Run one _tick() with the given controlled detection. `advance_s` shifts
    the wall clock backwards on the daemon's internal timestamps so subsequent
    ticks see the simulated gap as if real time had passed."""
    if advance_s:
        # Move 'last seen' marker into the past so the next tick computes
        # the gap as if `advance_s` seconds passed. We don't advance now()
        # because that would also push _daemon_started_at and break the
        # orphan-recovery-window math.
        if d._last_match_at is not None:
            d._last_match_at -= timedelta(seconds=advance_s)
        if d._daemon_started_at is not None:
            # Pull daemon-start back the same amount only if there's no
            # active session — orphan-window expiry is measured from start.
            if d.session is None:
                d._daemon_started_at -= timedelta(seconds=advance_s)

    def _fake_detect(active_key: str | None = None) -> detect.Detection | None:
        if detection is detect.ProbeFailed:
            raise detect.ProbeFailed("stub timeout")
        return detection  # type: ignore[return-value]

    monkeypatch.setattr(detect, "detect", _fake_detect)
    asyncio.run(d._tick())


def _meet(room: str = "abc-defg-hij") -> detect.Detection:
    return detect.Detection(
        platform="meet",
        title=f"Meet - {room}",
        source="coreaudio",
        application_pid=100,
        application_name="Google Chrome",
    )


# ----- the actual tests --------------------------------------------------


def test_idle_with_no_window_does_nothing(monkeypatch):
    d, calls = _make_daemon(monkeypatch)
    _drive_tick(d, monkeypatch, None)
    assert calls.starts == []
    assert d.session is None


def test_first_detection_starts_session(monkeypatch):
    d, calls = _make_daemon(monkeypatch)
    det = _meet()
    _drive_tick(d, monkeypatch, det)
    assert len(calls.starts) == 1
    assert calls.starts[0] is det
    assert d._session_key == "meet:abc-defg-hij"


def test_short_blip_does_not_pause(monkeypatch):
    """One missed tick with the same key returning is well under
    RECORDING_GRACE_S=30s. No pause."""
    d, calls = _make_daemon(monkeypatch)
    _drive_tick(d, monkeypatch, _meet())
    # One tick of "no window," only ~7s gap on the next probe (POLL=5).
    _drive_tick(d, monkeypatch, None, advance_s=7)
    _drive_tick(d, monkeypatch, _meet())
    assert calls.pauses == 0
    assert calls.finalizes == 0
    assert d.session is not None


def test_grace_blip_pauses_then_resumes_on_same_key(monkeypatch):
    """Window absent past RECORDING_GRACE_S → pause. Same key returning
    inside RESUME_WINDOW_S → resume (not finalize, not new start)."""
    d, calls = _make_daemon(monkeypatch)
    _drive_tick(d, monkeypatch, _meet())
    _drive_tick(d, monkeypatch, None, advance_s=RECORDING_GRACE_S + 1)
    assert calls.pauses == 1
    assert calls.finalizes == 0

    _drive_tick(d, monkeypatch, _meet())
    # Same-key return doesn't take the start path; resume happens inside
    # the session stub's resume() and the session stays attached.
    assert calls.starts == [calls.starts[0]]  # no second start
    assert d.session is not None
    assert d.session.is_paused is False


def test_resume_window_expiry_finalizes(monkeypatch):
    """Window absent past RESUME_WINDOW_S → finalize."""
    d, calls = _make_daemon(monkeypatch)
    _drive_tick(d, monkeypatch, _meet())
    _drive_tick(d, monkeypatch, None, advance_s=RECORDING_GRACE_S + 1)
    assert calls.pauses == 1
    _drive_tick(d, monkeypatch, None, advance_s=RESUME_WINDOW_S + 1)
    assert calls.finalizes == 1
    assert d.session is None


def test_key_change_finalizes_and_starts(monkeypatch):
    """Different meeting key while we're already running → finalize then
    start, with no grace window."""
    d, calls = _make_daemon(monkeypatch)
    _drive_tick(d, monkeypatch, _meet("room-a"))
    _drive_tick(d, monkeypatch, _meet("room-b"))
    assert calls.finalizes == 1
    assert len(calls.starts) == 2
    assert calls.starts[1].title == "Meet - room-b"


def test_probe_failed_preserves_state(monkeypatch):
    """ProbeFailed ticks should NOT trigger pause or finalize even if many
    fire in succession during what would otherwise be a long gap.

    Pre-fix behavior: each timeout returned None, the gap timer accumulated
    past RECORDING_GRACE_S, and the session paused mid-meeting. The fix
    raises ProbeFailed instead — the daemon's _tick early-returns before
    the gap-timer code is reached, so no pause/finalize fires regardless
    of how much wall-clock time has passed.
    """
    d, calls = _make_daemon(monkeypatch)
    _drive_tick(d, monkeypatch, _meet())
    # Several ProbeFailed ticks "during" what would have been a 35+ second
    # gap if those ticks had returned None.
    _drive_tick(d, monkeypatch, detect.ProbeFailed)
    _drive_tick(d, monkeypatch, detect.ProbeFailed, advance_s=20)
    _drive_tick(d, monkeypatch, detect.ProbeFailed, advance_s=20)
    assert calls.pauses == 0
    assert calls.finalizes == 0
    assert d.session is not None


def test_orphan_reattach_when_detection_matches(monkeypatch, tmp_meetings_root: Path):
    """An orphan with detection.key matches the next live detection → reattach
    into that folder rather than start a new one."""
    d, calls = _make_daemon(monkeypatch)
    folder = tmp_meetings_root / "2026-04-30T1100-orphan"
    folder.mkdir(parents=True)
    (folder / "metadata.json").write_text(json.dumps({
        "slug": folder.name,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": None,
        "detection": {"key": "meet:abc-defg-hij"},
    }))
    oc = daemon_mod._OrphanCandidate(
        folder=folder, key="meet:abc-defg-hij",
        started_at=datetime.now(timezone.utc),
    )
    d._pending_orphans = {oc.key: oc}

    _drive_tick(d, monkeypatch, _meet("abc-defg-hij"))
    assert calls.starts == []
    assert calls.reattaches == [(_meet("abc-defg-hij"), folder.name)]
    assert d._pending_orphans == {}


def test_orphan_finalized_after_recovery_window(monkeypatch, tmp_meetings_root: Path):
    """No detection arrives within RECOVERY_WINDOW_S → orphan finalizes."""
    d, calls = _make_daemon(monkeypatch)
    folder = tmp_meetings_root / "2026-04-30T1100-orphan"
    folder.mkdir(parents=True)
    oc = daemon_mod._OrphanCandidate(
        folder=folder, key="meet:abc-defg-hij",
        started_at=datetime.now(timezone.utc),
    )
    d._pending_orphans = {oc.key: oc}

    # Idle tick well past RECOVERY_WINDOW_S → should finalize.
    _drive_tick(d, monkeypatch, None, advance_s=RECOVERY_WINDOW_S + 5)
    assert calls.orphan_finalizes == [folder]
    assert d._pending_orphans == {}


def test_orphan_held_when_key_does_not_match(monkeypatch, tmp_meetings_root: Path):
    """Detection arrives but for a different key → don't reattach; orphan
    stays pending so a later matching tick can still recover it."""
    d, calls = _make_daemon(monkeypatch)
    folder = tmp_meetings_root / "2026-04-30T1100-orphan"
    folder.mkdir(parents=True)
    oc = daemon_mod._OrphanCandidate(
        folder=folder, key="meet:other-room",
        started_at=datetime.now(timezone.utc),
    )
    d._pending_orphans = {oc.key: oc}

    _drive_tick(d, monkeypatch, _meet("abc-defg-hij"))
    assert calls.reattaches == []
    assert len(calls.starts) == 1  # new session for the unrelated key
    assert d._pending_orphans == {oc.key: oc}  # orphan still held


def test_collect_orphans_separates_resumable_from_stale(tmp_meetings_root: Path):
    """_collect_orphans returns recent + key-bearing folders as resumable;
    others go to stale for immediate finalize."""
    now = datetime.now(timezone.utc)

    # Resumable: started 5 minutes ago, has detection.key.
    fresh = tmp_meetings_root / "fresh-orphan"
    fresh.mkdir()
    (fresh / "metadata.json").write_text(json.dumps({
        "started_at": (now - timedelta(minutes=5)).isoformat(),
        "ended_at": None,
        "detection": {"key": "meet:fresh-room"},
    }))

    # Stale: missing detection.key.
    no_key = tmp_meetings_root / "no-key-orphan"
    no_key.mkdir()
    (no_key / "metadata.json").write_text(json.dumps({
        "started_at": (now - timedelta(minutes=5)).isoformat(),
        "ended_at": None,
    }))

    # Stale: too old (older than MAX_RECORDING_S).
    old = tmp_meetings_root / "old-orphan"
    old.mkdir()
    (old / "metadata.json").write_text(json.dumps({
        "started_at": (now - timedelta(hours=10)).isoformat(),
        "ended_at": None,
        "detection": {"key": "meet:ancient"},
    }))

    # Already-finalized: not an orphan, ignored.
    done = tmp_meetings_root / "done-meeting"
    done.mkdir()
    (done / "metadata.json").write_text(json.dumps({
        "started_at": (now - timedelta(minutes=5)).isoformat(),
        "ended_at": now.isoformat(),
    }))

    resumable, stale = daemon_mod._collect_orphans(tmp_meetings_root, now)
    assert [oc.folder.name for oc in resumable] == ["fresh-orphan"]
    assert sorted(p.name for p in stale) == ["no-key-orphan", "old-orphan"]
