"""Post-meeting pipeline: transcribe → render → summarize.

Usage:
    python -m witness <folder>                # full pipeline
    python -m witness <folder> --step render  # single step
    python -m witness <folder> --skip summarize

Each step is idempotent and safe to re-run — transcription rewrites
transcript.jsonl from audio.opus (and skips outright when the transcript is
already current), rendering is a pure text transform. A failing step doesn't
block the ones after it.

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
import subprocess
import sys
from pathlib import Path

from . import render

log = logging.getLogger("witness")

STEPS = ["transcribe", "render", "summarize"]


def _sanity_check_and_notify(folder: Path) -> None:
    """Fire a desktop notification if this meeting's archive looks broken.

    Runs at the end of every pipeline invocation. The pipeline runs once per
    pause and once at the terminal stop, so a multi-segment meeting that's
    been working fine since the first segment can pause-resume-pause without
    re-notifying. We dedupe by a per-folder `.notified` marker.

    Two failure modes worth surfacing:
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

    if audio_size == 0:
        problem = "no audio captured (recording failed)"
    elif transcript_size == 0:
        problem = "audio saved but transcript is empty (transcription failed)"
    else:
        return

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
) -> int:
    steps = steps or STEPS
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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    steps = args.step or STEPS
    steps = [s for s in steps if s not in args.skip]
    return run(args.folder, steps, force_transcribe=args.force)


if __name__ == "__main__":
    sys.exit(main())
