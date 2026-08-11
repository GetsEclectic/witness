# Contributing to witness

Thanks for your interest. witness is a small, opinionated tool — issues and PRs are welcome, but please open an issue first for anything beyond a small fix so we can align before you write code.

## Dev setup

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/GetsEclectic/witness.git
cd witness
uv sync                          # core deps
uv run witness --help
```

System dependencies (see `README.md` for the full list): PipeWire with `pipewire-pulse`, `pulseaudio-utils`, `ffmpeg`, `xdotool`, `wmctrl`.

## Running the daemon during dev

```sh
uv run witness daemon            # foreground, Ctrl+C to stop
```

Or install the systemd user units (`systemd/witnessd.service`, `systemd/witnessd-tray.service`) and use `systemctl --user restart witnessd` after code changes — the long-running daemon imports from the project venv, so changes don't take effect until restart.

## Layout

- `src/witnessd/` — the long-running daemon (window detection, calendar correlation, recording session lifecycle, local ASR, FastAPI web UI)
- `src/witness/` — post-meeting pipeline modules (`render`, `summarize`)
- `src/cli/witness.py` — `witness` CLI (`daemon`, `record-now`, `web`, `ls`, `redo`, `show`)
- `src/tray/` — system tray indicator
- `skills/` — Claude Code skills that read `$WITNESS_MEETINGS_DIR`

## Tests

```sh
uv run pytest
```

The tests under `tests/` are pure-Python unit tests — they don't touch the real `~/meetings/` tree (the `tmp_meetings_root` fixture rebinds `config.MEETINGS_ROOT`), never load the ASR model, and don't call Anthropic. They do shell out to the bundled `ffmpeg` to build a synthetic 2-channel fixture.

## Style

- Type hints required on new public functions.
- Comments only when WHY is non-obvious — avoid restating WHAT.
- One commit per logical change.
- No fixtures with real meeting data — synthetic transcripts only.

## Reporting issues

Include: OS + version, `uv --version`, output of `uv run witness --help`, and `journalctl --user -u witnessd -n 100` if it's a daemon issue.
