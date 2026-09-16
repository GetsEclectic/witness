"""The audio is deleted once — and only once — a transcript covers it.

The recording is the one artifact with legal exposure the transcript
doesn't share, so the terminal pipeline run deletes it. What these tests
guard is the "only once a transcript covers it" half: a prune that fires
against an empty or stale transcript destroys the only copy of a meeting.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from witness import pipeline, summarize


def _meeting(folder: Path, *, audio: bytes = b"\x00" * 4096, transcript: str | None = None) -> None:
    (folder / "audio.opus").write_bytes(audio)
    seg = folder / "audio"
    seg.mkdir()
    (seg / "000.opus").write_bytes(audio)
    if transcript is not None:
        (folder / "transcript.jsonl").write_text(transcript)


def _touch_newer(path: Path, than: Path) -> None:
    t = than.stat().st_mtime + 5
    os.utime(path, (t, t))


def test_prune_deletes_audio_and_segments_when_transcript_is_current(tmp_path: Path) -> None:
    _meeting(tmp_path, transcript='{"text": "hello"}\n')
    _touch_newer(tmp_path / "transcript.jsonl", tmp_path / "audio.opus")

    freed = pipeline.prune_audio(tmp_path)

    assert freed == 8192
    assert not (tmp_path / "audio.opus").exists()
    assert not (tmp_path / "audio").exists()
    assert (tmp_path / "transcript.jsonl").exists()


def test_prune_keeps_audio_when_transcript_is_empty(tmp_path: Path) -> None:
    _meeting(tmp_path, transcript="")

    assert pipeline.prune_audio(tmp_path) == 0
    assert (tmp_path / "audio.opus").exists()
    assert (tmp_path / "audio" / "000.opus").exists()


def test_prune_keeps_audio_when_transcript_is_missing(tmp_path: Path) -> None:
    _meeting(tmp_path)

    assert pipeline.prune_audio(tmp_path) == 0
    assert (tmp_path / "audio.opus").exists()


def test_prune_keeps_audio_when_recording_grew_after_transcription(tmp_path: Path) -> None:
    """A resume after the last pipeline run appends audio the transcript
    hasn't seen. The transcript is now older than the recording — keep it."""
    _meeting(tmp_path, transcript='{"text": "hello"}\n')
    _touch_newer(tmp_path / "audio.opus", tmp_path / "transcript.jsonl")

    assert pipeline.prune_audio(tmp_path) == 0
    assert (tmp_path / "audio.opus").exists()


def test_prune_is_a_no_op_without_audio(tmp_path: Path) -> None:
    (tmp_path / "transcript.jsonl").write_text('{"text": "hello"}\n')
    assert pipeline.prune_audio(tmp_path) == 0


def test_run_prunes_only_when_final(tmp_path: Path, monkeypatch) -> None:
    """The daemon runs the pipeline after every pause and once at the end.
    Only the final run may delete; a pause run has a recording that may
    still grow."""
    monkeypatch.setattr(pipeline, "_macos_notify", lambda t, b: None)
    _meeting(tmp_path, transcript='{"text": "hello"}\n')
    (tmp_path / "transcript.md").write_text("# m\n" + "word " * 60)
    (tmp_path / "summary.md").write_text("# m\n\n## TL;DR\nfine\n")
    _touch_newer(tmp_path / "transcript.jsonl", tmp_path / "audio.opus")

    assert pipeline.run(tmp_path, ["render"]) == 0
    assert (tmp_path / "audio.opus").exists()

    assert pipeline.run(tmp_path, ["render"], final=True) == 0
    assert not (tmp_path / "audio.opus").exists()


def test_sanity_check_is_silent_after_prune(tmp_path: Path, monkeypatch) -> None:
    """Missing audio used to mean 'recording failed'. Now it is the normal
    end state of a healthy meeting, and must not page the user."""
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(pipeline, "_macos_notify", lambda t, b: notifications.append((t, b)))
    (tmp_path / "transcript.jsonl").write_text('{"text": "hello"}\n')

    pipeline._sanity_check_and_notify(tmp_path)

    assert notifications == []


def test_quarantine_replaces_summary_with_no_transcript(tmp_path: Path) -> None:
    """The pre-guard fabrications: an empty transcript and a confident
    summary of a meeting that never happened."""
    (tmp_path / "transcript.jsonl").write_text("")
    (tmp_path / "transcript.md").write_text("# meeting\n")
    (tmp_path / "summary.md").write_text(
        "# Transcript\n\n**You**: Let's kick off the Q3 planning session.\n" * 20
    )

    assert summarize.quarantine_fabricated(tmp_path) is True
    text = (tmp_path / "summary.md").read_text()
    assert summarize.STUB_MARKER in text
    assert "Q3 planning" not in text


def test_quarantine_leaves_a_real_summary_alone(tmp_path: Path) -> None:
    (tmp_path / "transcript.md").write_text("# meeting\n" + "word " * 60)
    (tmp_path / "summary.md").write_text("# m\n\n## TL;DR\nreal\n")

    assert summarize.quarantine_fabricated(tmp_path) is False
    assert (tmp_path / "summary.md").read_text().endswith("real\n")


def test_quarantine_leaves_an_existing_stub_alone(tmp_path: Path) -> None:
    (tmp_path / "transcript.md").write_text("# meeting\n")
    stub = f"# m\n\n## TL;DR\n{summarize.STUB_MARKER} — 2 words were captured from this recording.\n"
    (tmp_path / "summary.md").write_text(stub)

    assert summarize.quarantine_fabricated(tmp_path) is False
    assert (tmp_path / "summary.md").read_text() == stub


def test_quarantine_without_a_summary_is_a_no_op(tmp_path: Path) -> None:
    assert summarize.quarantine_fabricated(tmp_path) is False
    assert not (tmp_path / "summary.md").exists()
