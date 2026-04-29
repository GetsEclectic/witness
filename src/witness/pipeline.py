"""Post-meeting pipeline: render → fingerprint → summarize.

Usage:
    python -m witness <folder>                # full pipeline
    python -m witness <folder> --step render  # single step
    python -m witness <folder> --skip summarize

Each step is idempotent and safe to re-run. Failures in one step do not
block subsequent steps when run with `--continue-on-error` — rendering
always succeeds (pure text transform), so summary will attempt even if
fingerprinting dies.

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
import sys
from pathlib import Path

from . import render

log = logging.getLogger("witness")

STEPS = ["render", "fingerprint", "summarize"]


def run(folder: Path, steps: list[str] | None = None) -> int:
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

    if "render" in steps:
        try:
            out = render.render(folder)
            log.info("rendered %s", out)
        except Exception:
            log.exception("render failed")
            failures += 1

    if "fingerprint" in steps:
        try:
            from . import fingerprint
            fingerprint.resolve(folder)
            log.info("fingerprint resolved")
            # Re-render so transcript.md picks up newly resolved names.
            render.render(folder)
        except ImportError:
            log.info("fingerprint step skipped (pyannote not installed)")
        except Exception:
            log.exception("fingerprint failed")
            failures += 1

    if "summarize" in steps:
        try:
            from . import summarize
            out = summarize.summarize(folder)
            log.info("summarized %s", out)
        except Exception:
            log.exception("summarize failed")
            failures += 1

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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    steps = args.step or STEPS
    steps = [s for s in steps if s not in args.skip]
    return run(args.folder, steps)


if __name__ == "__main__":
    sys.exit(main())
