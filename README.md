# witness

Local-first meeting capture. Records audio + live transcript + post-meeting summary, with no bot joining the call. Captured data lives as plain files under `$WITNESS_MEETINGS_DIR` (default `~/meetings/`) so other tools — Claude Code skills, scripts, your own UI — can read it directly.

## Status

- **M1** (manual capture) ✅
- **M2** (live transcription + web UI) ✅
- **M3** (auto-trigger + tray) ✅
- **M4** (summaries) ✅
- **M5** (voice fingerprints) ✅
- **M6** (Claude Code skills) ✅

## Usage

```sh
uv sync
uv run witness daemon                       # auto-trigger: polls windows, starts on detect
uv run witness record-now "team standup"    # manual one-shot recording + web UI
# open http://127.0.0.1:7878 in a browser
# Ctrl+C to stop
uv run witness web                          # browse past meetings, no recording
uv run witness ls                           # CLI listing
```

Install as a systemd user service (autostart on login):

```sh
mkdir -p ~/.config/systemd/user
cp systemd/witnessd.service systemd/witnessd-tray.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now witnessd.service witnessd-tray.service
```

The shipped unit files reference the project as `%h/src/witness`. If your clone lives elsewhere, edit the `--project` path in both unit files before installing.

Each meeting becomes `$WITNESS_MEETINGS_DIR/<timestamp>-<slug>/` containing:
- `audio.opus` — 2-channel Ogg/Opus (ch0 = mic, ch1 = system audio)
- `transcript.jsonl` — one final utterance per line, from Deepgram streaming
- `transcript.md` — readable transcript with speaker labels + [MM:SS] offsets
- `summary.md` — Claude-generated TL;DR / decisions / action items
- `speakers.json` — `{speaker_0: "Name"}` (post-fingerprint, if enabled)
- `metadata.json` — start/end times, calendar event, detection trace
- `witness.log` — post-meeting pipeline log

## Configuration

| env var | default | purpose |
| --- | --- | --- |
| `WITNESS_MEETINGS_DIR` | `~/meetings` | where recordings + transcripts + voiceprints live |
| `WITNESS_GWS_BIN` | `gws` | path to the `gws` CLI used for Google Calendar lookups |
| `WITNESS_GWS_CONFIG_DIR` | `~/.config/gws` | single-account `gws` profile dir (encrypted token cache + client_secret.json) |
| `WITNESS_GWS_CONFIG_DIRS` | _unset_ | colon-separated list of `gws` profile dirs to query in parallel; takes precedence over `WITNESS_GWS_CONFIG_DIR`. Use this when one user is signed into multiple Google accounts and meetings can come from any of them. |

## Post-meeting pipeline

After a session ends the daemon spawns `python -m witness <folder>`, which runs:
1. **render** — `transcript.jsonl` → `transcript.md`
2. **fingerprint** (optional) — resolve `speaker_N` → real names via voiceprints
3. **summarize** — Claude OAuth call → `summary.md`

Re-run a single step with `witness redo <slug> --step summarize`.

Relabel speakers and update voiceprints: `witness relabel <slug> speaker_0 "Alex"`.

## Claude Code skills

Four skills under `skills/` read `$WITNESS_MEETINGS_DIR` directly:
- `meetings-search` — keyword/phrase search across transcripts + summaries
- `meetings-action-items` — aggregate open action items
- `meetings-on-topic` — narrative digest on a person / project / theme
- `meetings-week` — weekly recap

Install by copying or symlinking each into `~/.claude/skills/`:

```sh
for s in skills/meetings-*; do ln -s "$PWD/$s" ~/.claude/skills/; done
```

See `skills/README.md` for details.

## System dependencies

- PipeWire with pulseaudio compat (`pipewire-pulse`)
- `pulseaudio-utils` (`parec`, `pactl`)
- `ffmpeg`
- `xdotool`, `wmctrl` (M3+)
- For M5 voice fingerprints: `uv sync --extra fingerprint` + HF token at
  `~/.config/huggingface/token` with `pyannote/embedding` terms accepted.

## License

MIT — see [LICENSE](LICENSE).
