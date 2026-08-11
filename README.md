# witness

Local-first meeting capture. Records audio, transcribes it on-device, and writes a post-meeting summary — with no bot joining the call. Captured data lives as plain files under `$WITNESS_MEETINGS_DIR` (default `~/meetings/`) so other tools — Claude Code skills, scripts, your own UI — can read it directly.

## Status

- **M1** (manual capture) ✅
- **M2** (transcription + web UI) ✅
- **M3** (auto-trigger + tray) ✅
- **M4** (summaries) ✅
- **M5** (local ASR — Parakeet on MLX) ✅
- **M6** (Claude Code skills) ✅

## Usage

```sh
uv sync
uv run witness daemon                       # auto-trigger: polls windows, starts on detect
uv run witness record-now "team standup"    # manual one-shot recording
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

### macOS

Requires macOS 14.2+ (Sequoia tested). Auto-trigger and capture both work without any third-party audio drivers — system audio is captured via a CoreAudio Process Tap.

```sh
git clone <this repo>
cd witness
uv sync
scripts/install-mac.sh
```

`uv sync` pulls a bundled `ffmpeg` (via the `imageio-ffmpeg` wheel) and `pyobjc`, so you don't need `brew install ffmpeg`. The Swift system-audio tap binary at `mac/witness-audiotap` is committed prebuilt (universal arm64+x86_64, ad-hoc signed); no build step at install. Maintainers rebuild via `mac/build.sh` after editing the Swift source.

`scripts/install-mac.sh` installs two launchd agents (`com.witness.daemon`, `com.witness.tray`) into `~/Library/LaunchAgents/` and prompts you to grant two permissions:

- **Microphone** (System Settings → Privacy & Security → Microphone) for your terminal / `uv` — required for ffmpeg to open the mic.
- **Audio Capture** (System Settings → Privacy & Security → Audio Capture, macOS 14.4+) prompts on first record — required for the CoreAudio tap.

Logs land in `~/Library/Logs/witness/{daemon,tray}.{out,err}.log`.

There's no echo cancellation on macOS (no equivalent to PipeWire's `module-echo-cancel`). Speaker attribution comes from the channel layout rather than diarization, so it holds up regardless — but without headphones the mic channel picks up some bleed from the far end, which can show up as a remote utterance attributed to you.

Each meeting becomes `$WITNESS_MEETINGS_DIR/<timestamp>-<slug>/` containing:
- `audio.opus` — 2-channel Ogg/Opus (ch0 = mic, ch1 = system audio)
- `transcript.jsonl` — one utterance per line, tagged with its capture channel
- `transcript.md` — readable transcript with You/Remote labels + [MM:SS] offsets
- `summary.md` — Claude-generated TL;DR / decisions / action items
- `metadata.json` — start/end times, calendar event, detection trace
- `witness.log` — post-meeting pipeline log

## Configuration

| env var | default | purpose |
| --- | --- | --- |
| `WITNESS_MEETINGS_DIR` | `~/meetings` | where recordings + transcripts live |
| `WITNESS_WEBAPP_HOST` | `127.0.0.1` | bind address for the web UI |
| `WITNESS_WEBAPP_PORT` | `7878` | port for the web UI |
| `WITNESS_ASR_MODEL` | `mlx-community/parakeet-tdt-0.6b-v2` | HuggingFace id of the local ASR model |
| `WITNESS_GWS_BIN` | `gws` | path to the `gws` CLI used for Google Calendar lookups |
| `WITNESS_GWS_CONFIG_DIR` | `~/.config/gws` | single-account `gws` profile dir (encrypted token cache + client_secret.json) |
| `WITNESS_GWS_CONFIG_DIRS` | _unset_ | colon-separated list of `gws` profile dirs to query in parallel; takes precedence over `WITNESS_GWS_CONFIG_DIR`. Use this when one user is signed into multiple Google accounts and meetings can come from any of them. |
| `ANTHROPIC_API_KEY` | _unset_ | Anthropic API key for summary generation. If unset, witness falls back to the local Claude Code OAuth token at `~/.claude/.credentials.json`. |

## Post-meeting pipeline

After a session ends the daemon spawns `python -m witness <folder>`, which runs:
1. **transcribe** — `audio.opus` → `transcript.jsonl`, one channel at a time
2. **render** — `transcript.jsonl` → `transcript.md`
3. **summarize** — Claude OAuth call → `summary.md`

Re-run a single step with `witness redo <slug> --step summarize`, or force a
fresh transcription with `witness redo <slug> --force`.

The daemon also runs the pipeline after every pause, not only at the end, so a
long meeting that pauses partway gets a transcript partway. Transcription runs
at roughly 60x realtime on an M-series Mac — an hour of audio takes about a
minute — so re-running it is cheap.

### Speakers

There are two: **You** (the mic channel) and **Remote** (system audio, i.e.
everyone else). Individual remote speakers are not separated. Diarization plus
voice fingerprinting used to attempt that and never attributed reliably, so it
was removed in favor of the channel split, which is always right about the one
distinction that matters most.

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

**Linux:**
- PipeWire with pulseaudio compat (`pipewire-pulse`)
- `pulseaudio-utils` (`parec`, `pactl`)
- `ffmpeg`

Note that local ASR runs on MLX, which is Apple-Silicon-only — transcription
does not currently work on Linux.

**macOS** (14.2+):
- Nothing system-level. `ffmpeg` is bundled via `imageio-ffmpeg`; the
  CoreAudio tap binary ships in-repo at `mac/witness-audiotap`.
- The ASR model (~1.2GB) downloads from HuggingFace on first transcription
  into `~/.cache/huggingface/`. No token, no license acceptance.

## License

MIT — see [LICENSE](LICENSE).
