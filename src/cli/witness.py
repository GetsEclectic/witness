"""`witness` CLI — manual control of the capture pipeline.

Subcommands:
  record-now [name]   Start a live-transcribed recording. Ctrl+C to stop.
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
from witnessd.config import (
    DEEPGRAM_KEY_PATH,
    MEETINGS_ROOT,
    WEBAPP_HOST,
    WEBAPP_PORT,
    read_deepgram_key,
)
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
    """Start a live-transcribed recording. Ctrl+C to stop."""
    if not DEEPGRAM_KEY_PATH.exists():
        raise click.ClickException(
            f"Deepgram key not found at {DEEPGRAM_KEY_PATH}"
        )
    slug = _build_slug(name)
    click.echo(f"slug: {slug}")
    click.echo(f"UI:   http://{WEBAPP_HOST}:{WEBAPP_PORT}/")
    click.echo("Ctrl+C to stop.\n")
    asyncio.run(_record_and_serve(slug))


@cli.command("daemon")
def daemon_cmd() -> None:
    """Run the auto-trigger daemon: polls windows, starts/stops recordings."""
    if not DEEPGRAM_KEY_PATH.exists():
        raise click.ClickException(
            f"Deepgram key not found at {DEEPGRAM_KEY_PATH}"
        )
    witnessd_daemon.main()


@cli.command("web")
def web() -> None:
    """Serve the web UI (browse past meetings). No recording."""
    app = build_app(bus=None, status=lambda: RecordingStatus(False, None, None, False))
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
        if (folder / "summary.md").exists(): flags.append("summary")
        if (folder / "speakers.json").exists(): flags.append("speakers")
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
    speakers_path = folder / "speakers.json"
    if speakers_path.exists():
        click.echo("speakers:")
        import json
        for k, v in json.loads(speakers_path.read_text()).items():
            click.echo(f"  {k} → {v}")
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
    type=click.Choice(["render", "fingerprint", "summarize"]),
    help="run only this step (repeatable)",
)
def redo(slug: str, step: tuple[str, ...]) -> None:
    """Re-run the post-meeting pipeline for a meeting."""
    from witness import pipeline
    folder = _resolve_slug(MEETINGS_ROOT, slug)
    import logging
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    rc = pipeline.run(folder, list(step) if step else None)
    if rc != 0:
        raise click.ClickException("one or more steps failed — see logs")


@cli.command("relabel")
@click.argument("slug")
@click.argument("speaker_id")
@click.argument("name")
def relabel(slug: str, speaker_id: str, name: str) -> None:
    """Bind a speaker_id (e.g. `speaker_0` or `unknown_a3f1`) to a real name.

    Updates speakers.json for this meeting, re-renders transcript.md, and
    (if there's a voiceprint for the old id) renames the voiceprint to
    `name.npy` so future meetings auto-match.
    """
    import json
    folder = _resolve_slug(MEETINGS_ROOT, slug)
    sp_path = folder / "speakers.json"
    resolved = json.loads(sp_path.read_text()) if sp_path.exists() else {}

    prior = resolved.get(speaker_id)
    resolved[speaker_id] = name
    sp_path.write_text(json.dumps(resolved, indent=2))

    # Promote the voiceprint if this was an unknown_<hash> id or a previously
    # matched name that already has a stored embedding.
    from witnessd.config import VOICEPRINTS_DIR
    import numpy as np
    target_name = _slugify(name)
    target = VOICEPRINTS_DIR / f"{target_name}.npy"
    candidates = []
    if prior:
        candidates.append(VOICEPRINTS_DIR / f"{prior}.npy")
        candidates.append(VOICEPRINTS_DIR / f"{_slugify(prior)}.npy")
    candidates.append(VOICEPRINTS_DIR / f"{speaker_id}.npy")
    src = next((p for p in candidates if p.exists() and p != target), None)
    if src is not None and src != target:
        VOICEPRINTS_DIR.mkdir(parents=True, exist_ok=True)
        new = np.load(src)
        if new.ndim == 1:
            new = new[None, :]
        rows_added = int(new.shape[0])
        if target.exists():
            existing = np.load(target)
            if existing.ndim == 1:
                existing = existing[None, :]
            new = np.vstack([existing, new])
        np.save(target, new)
        # Record the promotion in the metadata sidecar so a wrong relabel is
        # diagnosable later. One entry per row added.
        from witness import fingerprint
        from datetime import datetime, timezone
        added_at = datetime.now(timezone.utc).isoformat()
        for _ in range(rows_added):
            fingerprint._append_meta(target_name, {
                "added": added_at,
                "source": "relabel",
                "source_slug": folder.name,
                "speaker_id": speaker_id,
                "promoted_from": src.name,
            })
        if src.name.startswith("unknown_"):
            src.unlink()
            # Drop the corresponding metadata sidecar too.
            (VOICEPRINTS_DIR / f"{src.stem}.meta.json").unlink(missing_ok=True)
        click.echo(f"voiceprint: {src.name} → {target.name}")

    from witness import render
    render.render(folder)
    click.echo(f"relabeled {speaker_id} → {name} in {folder.name}")


@cli.group("voiceprints")
def voiceprints() -> None:
    """Inspect or prune stored voiceprints."""


@voiceprints.command("inspect")
@click.argument("name", required=False)
def voiceprints_inspect(name: str | None) -> None:
    """Show voiceprint embeddings + per-row metadata.

    Without NAME, lists every voiceprint with row counts. With NAME, prints
    each row's metadata entry (when added, source meeting, promoted_from)."""
    from witnessd.config import VOICEPRINTS_DIR
    import numpy as np
    if not VOICEPRINTS_DIR.exists():
        click.echo(f"(no voiceprints at {VOICEPRINTS_DIR})")
        return
    if name is None:
        for p in sorted(VOICEPRINTS_DIR.glob("*.npy")):
            try:
                v = np.load(p)
                rows = v.shape[0] if v.ndim > 1 else 1
            except Exception:
                rows = "?"
            meta_count = len(_voiceprint_meta(p.stem))
            tag = f"{rows} row(s)"
            if meta_count != rows:
                tag += f" · {meta_count} meta"
            click.echo(f"{p.stem:30s}  {tag}")
        return
    npy = VOICEPRINTS_DIR / f"{_slugify(name)}.npy"
    if not npy.exists():
        raise click.ClickException(f"no voiceprint for {name!r}")
    v = np.load(npy)
    rows = v.shape[0] if v.ndim > 1 else 1
    meta = _voiceprint_meta(npy.stem)
    click.echo(f"{npy.stem}: {rows} row(s)")
    for i in range(rows):
        entry = meta[i] if i < len(meta) else {}
        click.echo(f"  [{i}] {entry or '(no metadata)'}")


@voiceprints.command("prune")
@click.argument("name")
@click.argument("row", type=int)
def voiceprints_prune(name: str, row: int) -> None:
    """Remove a single embedding row from NAME's voiceprint stack.

    Use after `witness voiceprints inspect <name>` identifies a poisoned row.
    Both the .npy and metadata sidecar are updated atomically."""
    from witnessd.config import VOICEPRINTS_DIR
    import numpy as np
    npy = VOICEPRINTS_DIR / f"{_slugify(name)}.npy"
    if not npy.exists():
        raise click.ClickException(f"no voiceprint for {name!r}")
    v = np.load(npy)
    if v.ndim == 1:
        v = v[None, :]
    if not (0 <= row < v.shape[0]):
        raise click.ClickException(f"row {row} out of range (0..{v.shape[0] - 1})")
    keep = np.delete(v, row, axis=0)
    if keep.shape[0] == 0:
        npy.unlink()
        (VOICEPRINTS_DIR / f"{npy.stem}.meta.json").unlink(missing_ok=True)
        click.echo(f"removed last row; deleted {npy.name}")
        return
    np.save(npy, keep)
    meta = _voiceprint_meta(npy.stem)
    if row < len(meta):
        meta.pop(row)
        (VOICEPRINTS_DIR / f"{npy.stem}.meta.json").write_text(
            __import__("json").dumps(meta, indent=2)
        )
    click.echo(f"pruned row {row} from {npy.name}")


@voiceprints.command("archive")
@click.argument("hash_id")
def voiceprints_archive(hash_id: str) -> None:
    """Hide an `unknown_<hash>` voiceprint from the identify-speakers UI.

    HASH_ID accepts either the bare 6-char hash or the full `unknown_xxxxxx`
    label. The npy + metadata sidecar move into a `.voiceprints/archived/`
    subdirectory; existing meeting `speakers.json` references are untouched.
    Archived voiceprints are also excluded from future audio matching.
    """
    bare = hash_id.removeprefix("unknown_")
    if not re.fullmatch(r"[0-9a-f]+", bare):
        raise click.ClickException(f"bad hash {hash_id!r}")
    from witness import fingerprint
    moved = fingerprint.archive_unknown(bare)
    if moved is None:
        raise click.ClickException(f"no unknown voiceprint for {bare}")
    click.echo(f"archived → {moved}")


@voiceprints.command("unarchive")
@click.argument("hash_id")
def voiceprints_unarchive(hash_id: str) -> None:
    """Restore an archived `unknown_<hash>` so it shows up in the UI again."""
    bare = hash_id.removeprefix("unknown_")
    if not re.fullmatch(r"[0-9a-f]+", bare):
        raise click.ClickException(f"bad hash {hash_id!r}")
    from witness import fingerprint
    restored = fingerprint.unarchive_unknown(bare)
    if restored is None:
        raise click.ClickException(f"no archived voiceprint for {bare}")
    click.echo(f"restored → {restored}")


@voiceprints.command("list-archived")
def voiceprints_list_archived() -> None:
    """List archived `unknown_<hash>` voiceprints."""
    from witness import fingerprint
    hashes = fingerprint.list_archived_unknowns()
    if not hashes:
        click.echo("(no archived voiceprints)")
        return
    for h in hashes:
        click.echo(f"unknown_{h}")


def _voiceprint_meta(stem: str) -> list:
    """Read metadata sidecar without importing fingerprint (which pulls torch)."""
    from witnessd.config import VOICEPRINTS_DIR
    import json as _json
    p = VOICEPRINTS_DIR / f"{stem}.meta.json"
    if not p.exists():
        return []
    try:
        data = _json.loads(p.read_text())
    except (OSError, _json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


@cli.command("enroll")
@click.argument("name")
@click.argument("audio", type=click.Path(path_type=Path, exists=True))
def enroll(name: str, audio: Path) -> None:
    """Add or append a voiceprint for NAME from a clean AUDIO sample (wav/opus).

    Requires the `fingerprint` extra: `uv sync --extra fingerprint`. The model
    auto-downloads to ~/.cache/witness/speechbrain/ on first use.
    """
    try:
        from witness import fingerprint
    except ImportError as e:
        raise click.ClickException(f"fingerprint extra not installed: {e}")
    out = fingerprint.enroll(_slugify(name), audio)
    click.echo(f"voiceprint written: {out}")


# --- record-now: single session + web UI, stopped by Ctrl+C ---

async def _record_and_serve(slug: str) -> None:
    api_key = read_deepgram_key()
    session = Session(slug, api_key)
    await session.start()

    def status_fn() -> RecordingStatus:
        return RecordingStatus(
            active=True,
            slug=session.slug,
            started_at=session.started_at,
            transcription_failed=session.transcription_failed,
        )

    app = build_app(bus=session.bus, status=status_fn)
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


if __name__ == "__main__":
    cli()
