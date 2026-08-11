"""`witness` CLI — manual control of the capture pipeline.

Subcommands:
  record-now [name]   Record until Ctrl+C, then transcribe and summarize.
  daemon              Run the auto-trigger daemon (window polling + web UI).
  web                 Serve the webapp without recording (browse past meetings).
  ls                  List past meetings.
"""
from __future__ import annotations

import asyncio
import re
import signal
from datetime import datetime
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
def redo(slug: str, step: tuple[str, ...], force: bool) -> None:
    """Re-run the post-meeting pipeline for a meeting."""
    from witness import pipeline
    folder = _resolve_slug(MEETINGS_ROOT, slug)
    import logging
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    rc = pipeline.run(folder, list(step) if step else None, force_transcribe=force)
    if rc != 0:
        raise click.ClickException("one or more steps failed — see logs")


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
