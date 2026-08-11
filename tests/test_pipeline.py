"""Tests for the post-meeting pipeline's sanity-check notification.

Pure file-level checks — we don't exercise the whole transcribe/render/
summarize chain here (other test files cover those steps individually).
What this file guards is the failure-mode signal we added after the
2026-05-14 incident, when 21 hours of meetings produced empty audio
files and the user only noticed by accident: an end-of-pipeline check
that fires a desktop notification when the archive looks broken.
"""
from __future__ import annotations

from witness import pipeline


def _meeting(folder, audio_bytes: bytes, transcript_text: str) -> None:
    """Create a meeting folder with the given audio + transcript contents."""
    (folder / "audio.opus").write_bytes(audio_bytes)
    (folder / "transcript.jsonl").write_text(transcript_text)


def test_sanity_check_flags_zero_byte_audio(tmp_path, monkeypatch) -> None:
    """The catastrophic case: ffmpeg blocked, audio.opus is 0 bytes. The
    notification body must mention recording failure, not transcription,
    so the user knows there's nothing to recover from."""
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pipeline,
        "_macos_notify",
        lambda title, body: notifications.append((title, body)),
    )
    _meeting(tmp_path, audio_bytes=b"", transcript_text="")

    pipeline._sanity_check_and_notify(tmp_path)

    assert len(notifications) == 1
    title, body = notifications[0]
    assert "witness" in title.lower()
    assert "no audio" in body.lower()
    assert (tmp_path / ".notified").exists()


def test_sanity_check_flags_empty_transcript_with_audio(tmp_path, monkeypatch) -> None:
    """The 'audio saved, transcript lost' case: recording worked but the
    transcribe step failed. The audio is on disk so `witness redo` can
    recover it — but the user should know now, not when they go looking."""
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pipeline,
        "_macos_notify",
        lambda title, body: notifications.append((title, body)),
    )
    _meeting(tmp_path, audio_bytes=b"\x00" * 4096, transcript_text="")

    pipeline._sanity_check_and_notify(tmp_path)

    assert len(notifications) == 1
    _, body = notifications[0]
    assert "transcript" in body.lower()


def test_sanity_check_is_silent_when_healthy(tmp_path, monkeypatch) -> None:
    """A normal meeting — audio captured, transcript has events. No
    notification, no marker file (so a future regression in the same
    folder would re-notify if needed)."""
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pipeline,
        "_macos_notify",
        lambda title, body: notifications.append((title, body)),
    )
    _meeting(
        tmp_path,
        audio_bytes=b"\x00" * 4096,
        transcript_text='{"text": "hello"}\n',
    )

    pipeline._sanity_check_and_notify(tmp_path)

    assert notifications == []
    assert not (tmp_path / ".notified").exists()


def test_sanity_check_dedupes_across_pipeline_runs(tmp_path, monkeypatch) -> None:
    """Pause/resume meetings invoke the pipeline once per pause. The user
    should get ONE notification per broken meeting, not one per pause."""
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pipeline,
        "_macos_notify",
        lambda title, body: notifications.append((title, body)),
    )
    _meeting(tmp_path, audio_bytes=b"", transcript_text="")

    pipeline._sanity_check_and_notify(tmp_path)
    pipeline._sanity_check_and_notify(tmp_path)
    pipeline._sanity_check_and_notify(tmp_path)

    assert len(notifications) == 1


def test_sanity_check_survives_notification_failure(tmp_path, monkeypatch) -> None:
    """Notification dispatch failing (no osascript, sandbox refusal, etc.)
    must not crash the pipeline. The log warning is the durable record;
    the GUI notification is best-effort."""
    def boom(title: str, body: str) -> None:
        raise RuntimeError("osascript missing")

    monkeypatch.setattr(pipeline, "_macos_notify", boom)
    _meeting(tmp_path, audio_bytes=b"", transcript_text="")

    # Must not raise — and we should still drop the dedupe marker so
    # subsequent runs don't keep re-trying the broken notifier.
    pipeline._sanity_check_and_notify(tmp_path)
    assert (tmp_path / ".notified").exists()


def test_sanity_check_handles_missing_files(tmp_path, monkeypatch) -> None:
    """If the pipeline runs before either output exists (early-failure
    case in render), missing files should be treated as 'no audio'."""
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pipeline,
        "_macos_notify",
        lambda title, body: notifications.append((title, body)),
    )

    pipeline._sanity_check_and_notify(tmp_path)

    assert len(notifications) == 1
    _, body = notifications[0]
    assert "no audio" in body.lower()
