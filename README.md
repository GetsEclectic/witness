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

There's no echo cancellation on macOS (no equivalent to PipeWire's `module-echo-cancel`). The system channel still diarizes correctly because diarization runs on that channel only; the mic channel may have some speaker bleed when not using headphones.

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
| `WITNESS_WEBAPP_HOST` | `127.0.0.1` | bind address for the live UI |
| `WITNESS_WEBAPP_PORT` | `7878` | port for the live UI |
| `WITNESS_GWS_BIN` | `gws` | path to the `gws` CLI used for Google Calendar lookups |
| `WITNESS_GWS_CONFIG_DIR` | `~/.config/gws` | single-account `gws` profile dir (encrypted token cache + client_secret.json) |
| `WITNESS_GWS_CONFIG_DIRS` | _unset_ | colon-separated list of `gws` profile dirs to query in parallel; takes precedence over `WITNESS_GWS_CONFIG_DIR`. Use this when one user is signed into multiple Google accounts and meetings can come from any of them. |
| `ANTHROPIC_API_KEY` | _unset_ | Anthropic API key for summary generation. If unset, witness falls back to the local Claude Code OAuth token at `~/.claude/.credentials.json`. |

## Post-meeting pipeline

After a session ends the daemon spawns `python -m witness <folder>`, which runs:
1. **render** — `transcript.jsonl` → `transcript.md`
2. **fingerprint** (optional) — resolve `speaker_N` → real names via voiceprints
3. **summarize** — Claude OAuth call → `summary.md`

Re-run a single step with `witness redo <slug> --step summarize`.

Relabel speakers and update voiceprints: `witness relabel <slug> speaker_0 "Alex"`.

Inspect or prune voiceprint embeddings (per-row metadata records when each
embedding was added and from which meeting):

```sh
witness voiceprints inspect              # list all voiceprints with row counts
witness voiceprints inspect Alex         # show metadata for Alex's embeddings
witness voiceprints prune Alex 2         # drop a single poisoned row
```

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
- For M5 voice fingerprints: `uv sync --extra fingerprint`. The model
  (`speechbrain/spkrec-ecapa-voxceleb`) auto-downloads on first use to
  `~/.cache/witness/speechbrain/`; no HF token, no license acceptance.

**macOS** (14.2+):
- Nothing system-level. `ffmpeg` is bundled via `imageio-ffmpeg`; the
  CoreAudio tap binary ships in-repo at `mac/witness-audiotap`.
- For M5 voice fingerprints: `uv sync --extra fingerprint` (same as Linux).

## License

MIT — see [LICENSE](LICENSE).
