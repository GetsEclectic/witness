"""`witness` CLI — manual control of the capture pipeline.

Subcommands:
  record-now [name]   Record until Ctrl+C, then transcribe and summarize.
  daemon              Run the auto-trigger daemon (window polling + web UI).
  web                 Serve the webapp without recording (browse past meetings).
  ls                  List past meetings.
  redo <slug>         Re-run the post-meeting pipeline (deletes audio after).
  prune               Delete audio for every meeting whose transcript covers it.
"""
from __future__ import annotations

import asyncio
import re
import signal
from datetime import datetime, timezone
from pathlib import Path

import click
import uvicorn

from witnessd import daemon as witnessd_daemon
from witnessd.config import MEETINGS_ROOT, WEBAPP_HOST, WEBAPP_PORT
from witnessd.session import Session
from witnessd.webapp import RecordingStatus, build_app


def _default_slug() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H%M-adhoc")


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s or "adhoc"


def _build_slug(name: str | None) -> str:
    if not name:
        return _default_slug()
    return f"{datetime.now().strftime('%Y-%m-%dT%H%M')}-{_slugify(name)}"


@click.group()
def cli() -> None:
    """Local meeting capture."""


@cli.command("record-now")
@click.argument("name", required=False)
def record_now(name: str | None) -> None:
    """Record until Ctrl+C, then transcribe and summarize."""
    slug = _build_slug(name)
    click.echo(f"slug: {slug}")
    click.echo(f"UI:   http://{WEBAPP_HOST}:{WEBAPP_PORT}/")
    click.echo("Ctrl+C to stop.\n")
    asyncio.run(_record_and_serve(slug))


@cli.command("daemon")
def daemon_cmd() -> None:
    """Run the auto-trigger daemon: polls windows, starts/stops recordings."""
    witnessd_daemon.main()


@cli.command("web")
def web() -> None:
    """Serve the web UI (browse past meetings). No recording."""
    app = build_app(status=lambda: RecordingStatus(False, None, None))
    config = uvicorn.Config(
        app, host=WEBAPP_HOST, port=WEBAPP_PORT, log_level="warning"
    )
    uvicorn.Server(config).run()


@cli.command("ls")
@click.option("--root", type=click.Path(path_type=Path), default=MEETINGS_ROOT)
def ls_meetings(root: Path) -> None:
    """List recorded meetings."""
    if not root.exists():
        click.echo(f"(no meetings yet at {root})")
        return
    folders = sorted(
        p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    for folder in folders:
        audio = folder / "audio.opus"
        size = (
            f"{audio.stat().st_size / 1024 / 1024:.1f}MB"
            if audio.exists() else "—"
        )
        flags = []
        transcript = folder / "transcript.jsonl"
        if transcript.exists() and transcript.stat().st_size > 0:
            flags.append("transcript")
        if (folder / "summary.md").exists(): flags.append("summary")
        tag = (" [" + ",".join(flags) + "]") if flags else ""
        click.echo(f"{folder.name:50s}  {size}{tag}")


def _resolve_slug(root: Path, slug: str) -> Path:
    folder = root / slug
    if folder.is_dir():
        return folder
    # Allow prefix match for convenience.
    matches = [p for p in root.iterdir() if p.is_dir() and p.name.startswith(slug)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise click.ClickException(
            f"ambiguous slug {slug!r}: {', '.join(p.name for p in matches)}"
        )
    raise click.ClickException(f"no meeting matching {slug!r}")


@cli.command("show")
@click.argument("slug")
def show(slug: str) -> None:
    """Print summary + metadata for a past meeting."""
    folder = _resolve_slug(MEETINGS_ROOT, slug)
    click.echo(f"# {folder.name}\n")
    meta_path = folder / "metadata.json"
    if meta_path.exists():
        import json
        meta = json.loads(meta_path.read_text())
        cal = meta.get("calendar_event") or {}
        if cal.get("summary"):
            click.echo(f"title:      {cal['summary']}")
        if cal.get("attendees"):
            click.echo(f"attendees:  {', '.join(cal['attendees'])}")
        if meta.get("started_at"):
            click.echo(f"started:    {meta['started_at']}")
        if meta.get("ended_at"):
            click.echo(f"ended:      {meta['ended_at']}")
    summary_path = folder / "summary.md"
    if summary_path.exists():
        click.echo("\n" + summary_path.read_text())
    else:
        click.echo("\n(no summary yet — run `witness redo <slug>`)")


@cli.command("redo")
@click.argument("slug")
@click.option(
    "--step",
    multiple=True,
    type=click.Choice(["transcribe", "render", "summarize"]),
    help="run only this step (repeatable)",
)
@click.option(
    "--force",
    is_flag=True,
    help="re-transcribe even when the existing transcript is current",
)
@click.option(
    "--keep-audio",
    is_flag=True,
    help="leave audio.opus in place instead of deleting it once transcribed",
)
def redo(slug: str, step: tuple[str, ...], force: bool, keep_audio: bool) -> None:
    """Re-run the post-meeting pipeline for a meeting."""
    from witness import pipeline
    folder = _resolve_slug(MEETINGS_ROOT, slug)
    import logging
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    rc = pipeline.run(
        folder,
        list(step) if step else None,
        force_transcribe=force,
        final=not keep_audio and not step,
    )
    if rc != 0:
        raise click.ClickException("one or more steps failed — see logs")


def _settled(folder: Path, now: datetime) -> bool:
    """A meeting whose recording can't grow any more: ended_at is stamped
    and the resume window has passed, so a paused session won't come back
    and concatenate onto segments we've deleted."""
    import json
    from datetime import timedelta
    from witnessd.config import RESUME_WINDOW_S
    meta_path = folder / "metadata.json"
    if not meta_path.exists():
        return True
    try:
        ended = json.loads(meta_path.read_text()).get("ended_at")
    except (OSError, json.JSONDecodeError):
        return False
    if not ended:
        return False
    try:
        ended_dt = datetime.fromisoformat(ended)
    except ValueError:
        return False
    if ended_dt.tzinfo is None:
        ended_dt = ended_dt.replace(tzinfo=timezone.utc)
    return now - ended_dt > timedelta(seconds=RESUME_WINDOW_S)


def _covered_once_settled(folder: Path) -> bool:
    """The pipeline's currency test, plus the one case it can't see.

    Before batch ASR (2026-08-07) the transcript was streamed live, so it
    was written seconds *before* the final audio.opus and fails the mtime
    test forever, even though it covers the whole recording. That holds
    whenever nothing was appended after the transcript was written — i.e.
    for a single-segment meeting. Multi-segment ones stay on the strict
    rule: a resume may have added audio the live transcript never saw.
    """
    import json
    from witness import pipeline
    if pipeline.transcript_covers_audio(folder):
        return True
    transcript = folder / "transcript.jsonl"
    if not transcript.exists() or transcript.stat().st_size == 0:
        return False
    try:
        segments = json.loads((folder / "metadata.json").read_text()).get("segment_count")
    except (OSError, json.JSONDecodeError):
        return False
    return (segments or 1) <= 1


@cli.command("prune")
@click.option("--dry-run", is_flag=True, help="report what would be deleted; delete nothing")
def prune(dry_run: bool) -> None:
    """Delete audio for every finished meeting whose transcript covers it.

    The pipeline does this on its own at the end of each meeting; this
    command catches up the backlog recorded before it did. Meetings with
    no transcript, or one that may not cover their audio, keep their
    recording. Also replaces any summary that has no transcript behind it
    with the stub, so a fabricated meeting can't sit in the archive.
    """
    from witness import pipeline, summarize
    if not MEETINGS_ROOT.exists():
        click.echo(f"(no meetings yet at {MEETINGS_ROOT})")
        return
    now = datetime.now(timezone.utc)
    freed = pruned = kept_live = kept_untranscribed = quarantined = 0
    for folder in sorted(p for p in MEETINGS_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")):
        if not dry_run and summarize.quarantine_fabricated(folder):
            quarantined += 1
            click.echo(f"stub  {folder.name}  (summary had no transcript behind it)")
        audio = folder / "audio.opus"
        seg_dir = folder / "audio"
        if not audio.exists() and not seg_dir.exists():
            continue
        if not _settled(folder, now):
            kept_live += 1
            click.echo(f"keep  {folder.name}  (recording or within resume window)")
            continue
        if not _covered_once_settled(folder):
            kept_untranscribed += 1
            click.echo(f"keep  {folder.name}  (no transcript covering the audio)")
            continue
        size = audio.stat().st_size if audio.exists() else 0
        if seg_dir.is_dir():
            size += sum(p.stat().st_size for p in seg_dir.glob("*.opus"))
        if dry_run:
            click.echo(f"would  {folder.name}  ({size / 1024 / 1024:.1f}MB)")
        else:
            size = _delete_audio(folder)
        pruned += 1
        freed += size
    verb = "would free" if dry_run else "freed"
    click.echo(
        f"{pruned} meeting(s) pruned, {verb} {freed / 1024 / 1024 / 1024:.2f} GB; "
        f"kept {kept_untranscribed} without a covering transcript, {kept_live} still live; "
        f"{quarantined} fabricated summar{'y' if quarantined == 1 else 'ies'} replaced"
    )


def _delete_audio(folder: Path) -> int:
    """Delete audio.opus and audio/ for a folder `_covered_once_settled`
    has already vetted. Same deletion as pipeline.prune_audio, without
    the strict mtime test that live-era transcripts can't pass."""
    import shutil
    freed = 0
    audio = folder / "audio.opus"
    seg_dir = folder / "audio"
    if audio.exists():
        freed += audio.stat().st_size
        audio.unlink()
    if seg_dir.is_dir():
        freed += sum(p.stat().st_size for p in seg_dir.glob("*.opus"))
        shutil.rmtree(seg_dir, ignore_errors=True)
    return freed


# --- record-now: single session + web UI, stopped by Ctrl+C ---

async def _record_and_serve(slug: str) -> None:
    session = Session(slug)
    await session.start()

    def status_fn() -> RecordingStatus:
        return RecordingStatus(
            active=True,
            slug=session.slug,
            started_at=session.started_at,
        )

    app = build_app(status=status_fn)
    config = uvicorn.Config(
        app,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    stop_evt = asyncio.Event()
    loop = asyncio.get_running_loop()

    def handle_signal() -> None:
        if stop_evt.is_set():
            return
        click.echo("\nstopping…", err=True)
        stop_evt.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    web_task = asyncio.create_task(server.serve(), name="webapp")
    watch_task = asyncio.create_task(session.wait_stopped(), name="session-watch")

    try:
        done, _ = await asyncio.wait(
            [asyncio.create_task(stop_evt.wait()), watch_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        await session.stop()
        server.should_exit = True
        for t in (web_task, watch_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(web_task, watch_task, return_exceptions=True)
        if session.folder is not None:
            click.echo(f"saved {session.folder}")
            # Transcribe + summarize inline rather than detaching like the
            # daemon does — an interactive `record-now` should leave the user
            # with a finished meeting, not a pending one.
            click.echo("transcribing…")
            from witness import pipeline
            pipeline.run(session.folder)


if __name__ == "__main__":
    cli()
