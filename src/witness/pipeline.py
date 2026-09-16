"""Post-meeting pipeline: transcribe → render → summarize → prune.

Usage:
    python -m witness <folder>                # transcribe, render, summarize
    python -m witness <folder> --final        # …then delete the audio
    python -m witness <folder> --step render  # single step
    python -m witness <folder> --skip summarize

Each step is idempotent and safe to re-run — transcription rewrites
transcript.jsonl from audio.opus (and skips outright when the transcript is
already current), rendering is a pure text transform. A failing step doesn't
block the ones after it.

The audio is an input, not an archive. Once a transcript exists that covers
it, the recording is the one artifact here that carries wiretap, voiceprint
and discovery exposure while adding nothing the transcript doesn't already
say — so the `prune` step deletes it. It only runs on the terminal pipeline
invocation (`--final`), never after a mid-meeting pause, and it refuses
unless transcript.jsonl is non-empty and at least as new as the audio.

Pause/resume produces multiple pipeline invocations against the same
folder — once after every grace-pause and once at the terminal stop. We
serialize them with a blocking flock on `<folder>/.pipeline.lock` so a
late invocation that started while the prior one was still running just
queues; last writer wins on summary.md / transcript.md, which are
overwriting outputs anyway.
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import shutil
import subprocess
import sys
from pathlib import Path

from . import render

log = logging.getLogger("witness")

STEPS = ["transcribe", "render", "summarize", "prune"]
DEFAULT_STEPS = ["transcribe", "render", "summarize"]


def transcript_covers_audio(folder: Path) -> bool:
    """True when transcript.jsonl is non-empty and no older than the audio.

    The same test transcribe.py uses to skip a re-run, so "safe to delete
    the audio" and "nothing left to transcribe" can never disagree.
    """
    audio = folder / "audio.opus"
    transcript = folder / "transcript.jsonl"
    if not transcript.exists() or transcript.stat().st_size == 0:
        return False
    if not audio.exists():
        return True
    return transcript.stat().st_mtime >= audio.stat().st_mtime


def prune_audio(folder: Path) -> int:
    """Delete audio.opus and the audio/ segment directory once the
    transcript covers them. Returns the number of bytes freed; 0 when
    there was nothing to delete or the transcript isn't current."""
    audio = folder / "audio.opus"
    seg_dir = folder / "audio"
    if not audio.exists() and not seg_dir.exists():
        return 0
    if not transcript_covers_audio(folder):
        log.warning(
            "keeping audio for %s: transcript is missing, empty, or older than the recording",
            folder.name,
        )
        return 0
    freed = 0
    if audio.exists():
        freed += audio.stat().st_size
        audio.unlink()
    if seg_dir.is_dir():
        freed += sum(p.stat().st_size for p in seg_dir.glob("*.opus"))
        shutil.rmtree(seg_dir, ignore_errors=True)
    log.info("pruned audio for %s (%.1f MB)", folder.name, freed / 1024 / 1024)
    return freed


def _sanity_check_and_notify(folder: Path) -> None:
    """Fire a desktop notification if this meeting's archive looks broken.

    Runs at the end of every pipeline invocation. The pipeline runs once per
    pause and once at the terminal stop, so a multi-segment meeting that's
    been working fine since the first segment can pause-resume-pause without
    re-notifying. We dedupe by a per-folder `.notified` marker.

    A non-empty transcript means the meeting is safe, whatever became of the
    audio — the prune step deletes it on purpose. Two failure modes remain:
      * `audio.opus` is missing or zero-bytes — recording itself failed.
        This is the loud one: there's nothing to recover from.
      * `audio.opus` has bytes but `transcript.jsonl` is empty — recording
        worked, transcription didn't. The user can re-transcribe later from
        the on-disk audio, but they should know now so they don't go looking
        for a transcript that doesn't exist.

    On platforms without `osascript` (Linux dev boxes), the log warning still
    fires and the marker is still touched; only the GUI notification is
    skipped. That's fine — the daemon log is the primary record either way.
    """
    notified = folder / ".notified"
    if notified.exists():
        return

    audio = folder / "audio.opus"
    transcript = folder / "transcript.jsonl"
    audio_size = audio.stat().st_size if audio.exists() else 0
    transcript_size = transcript.stat().st_size if transcript.exists() else 0

    if transcript_size > 0:
        return
    if audio_size == 0:
        problem = "no audio captured (recording failed)"
    else:
        problem = "audio saved but transcript is empty (transcription failed)"

    title = "witness: meeting capture failed"
    body = f"{folder.name}: {problem}"
    log.warning("%s — %s", title, body)
    try:
        _macos_notify(title, body)
    except Exception:
        log.exception("failed to fire macOS notification")
    try:
        notified.touch()
    except OSError:
        log.warning("could not write %s; future runs may re-notify", notified)


def _macos_notify(title: str, body: str) -> None:
    """Display a macOS user notification.

    Use the `display notification` AppleScript verb via `osascript`. No new
    dependency (osascript ships with macOS), no permission prompt beyond the
    standard one Notification Center shows for any new sender.

    AppleScript string literals delimit with `"` and escape with `\\`. Both
    `\\` and `"` in the title/body must be escaped; nothing else is special.
    """
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    script = (
        f'display notification "{_esc(body)}" '
        f'with title "{_esc(title)}"'
    )
    subprocess.run(
        ["osascript", "-e", script],
        check=False,
        timeout=5,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run(
    folder: Path,
    steps: list[str] | None = None,
    force_transcribe: bool = False,
    final: bool = False,
) -> int:
    steps = list(steps or DEFAULT_STEPS)
    if final and "prune" not in steps:
        steps.append("prune")
    if not folder.exists():
        log.error("folder does not exist: %s", folder)
        return 2

    # Block on a per-folder exclusive lock so concurrent pipeline runs
    # against the same meeting serialize cleanly. Held for the life of
    # this process (file is closed when we return).
    lock_path = folder / ".pipeline.lock"
    lock_fp = lock_path.open("w")
    log.info("acquiring pipeline lock for %s", folder.name)
    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)

    failures = 0

    if "transcribe" in steps:
        try:
            # Imported lazily — it pulls MLX and the ASR model in with it,
            # which a `--step render` re-run has no business paying for.
            from witnessd import transcribe
            transcribe.transcribe(folder, force=force_transcribe)
        except Exception:
            log.exception("transcribe failed")
            failures += 1

    if "render" in steps:
        try:
            out = render.render(folder)
            log.info("rendered %s", out)
        except Exception:
            log.exception("render failed")
            failures += 1

    if "summarize" in steps:
        try:
            from . import summarize
            out = summarize.summarize(folder)
            log.info("summarized %s", out)
        except Exception:
            log.exception("summarize failed")
            failures += 1

    # Runs regardless of which steps were asked for: a summary with no
    # transcript behind it is a fabrication however it got there.
    try:
        from . import summarize
        summarize.quarantine_fabricated(folder)
    except Exception:
        log.exception("fabrication check failed")

    if "prune" in steps:
        try:
            prune_audio(folder)
        except Exception:
            log.exception("prune failed")
            failures += 1

    _sanity_check_and_notify(folder)

    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="witness")
    parser.add_argument("folder", type=Path, help="meeting folder")
    parser.add_argument(
        "--step",
        action="append",
        choices=STEPS,
        help="run only this step (repeatable); default: all",
    )
    parser.add_argument(
        "--skip",
        action="append",
        choices=STEPS,
        default=[],
        help="skip this step (repeatable)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-transcribe even when the existing transcript is current",
    )
    parser.add_argument(
        "--final",
        action="store_true",
        help="this is the last run for the meeting: delete the audio once transcribed",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    steps = args.step or DEFAULT_STEPS
    steps = [s for s in steps if s not in args.skip]
    return run(args.folder, steps, force_transcribe=args.force, final=args.final)


if __name__ == "__main__":
    sys.exit(main())
