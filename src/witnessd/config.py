import os
from pathlib import Path

# Override with WITNESS_MEETINGS_DIR (e.g. for OSS users who want their
# recordings under XDG_DATA_HOME). Default keeps the original ~/meetings/
# path so existing installs migrate transparently.
MEETINGS_ROOT = Path(
    os.environ.get("WITNESS_MEETINGS_DIR") or (Path.home() / "meetings")
).expanduser()
STATE_DIR = MEETINGS_ROOT / ".state"
LOG_PATH = STATE_DIR / "witness.log"

WEBAPP_HOST = os.environ.get("WITNESS_WEBAPP_HOST") or "127.0.0.1"
WEBAPP_PORT = int(os.environ.get("WITNESS_WEBAPP_PORT") or 7878)

# Display name for whoever is recording. Names the mic channel in
# transcript.md and the recorder in summary.md, so neither says "You" —
# a document that says "You" can't be forwarded to anyone.
#
# Normally derived per-meeting from the invite's `self: true` attendee, which
# is what `calendar_event.self_email` holds. This is the fallback for the
# recordings that never matched an invite (about a third of them), where no
# name is otherwise knowable.
USER_NAME = os.environ.get("WITNESS_USER_NAME") or ""

# Window-detection poll interval (seconds). Tight enough that back-to-back
# meeting switches rotate the session within a few seconds; pactl is cheap.
POLL_INTERVAL_S = 5

# The first inconclusive detection tick is always logged; a continuing run
# gets one more line per this many ticks.
PROBE_FAILURE_LOG_EVERY = 60

# Recording pauses this many seconds after detection disappears. Pause is
# soft: ffmpeg stops and the audio segment is finalized, but the folder stays
# open. If the same key reappears within RESUME_WINDOW_S, recording resumes
# into the same folder as a new segment.
RECORDING_GRACE_S = 30

# How long a paused session waits for the same key to reappear before
# being finalized. After this, the folder gets its terminal `ended_at` and
# any subsequent same-key detection produces a fresh folder.
RESUME_WINDOW_S = 30 * 60

# A key with no conference id behind it (a desktop app's pid, a Meet URL the
# probe couldn't read a room code from) can't tell two calls apart, so after
# a gap this long a reappearing key is taken as a new call, not a resume.
WEAK_KEY_RESUME_S = 5 * 60

# Hard upper bound on a single recording. If detection wedges (RUNNING but
# the call actually ended), this caps the damage at one bounded archive
# rather than letting opus accumulate indefinitely.
MAX_RECORDING_S = 6 * 3600  # 6 hours

# Speech recognition. Runs locally on the GPU via MLX; the model is pulled
# from HuggingFace on first use (~1.2GB, cached under ~/.cache/huggingface).
# Parakeet TDT v2 is English-only — swap in `parakeet-tdt-0.6b-v3` for the
# multilingual variant if that ever matters.
ASR_MODEL = os.environ.get("WITNESS_ASR_MODEL") or "mlx-community/parakeet-tdt-0.6b-v2"
ASR_SAMPLE_RATE = 16000  # mono; what the model's frontend expects
# Long audio is transcribed in chunks to bound encoder memory. The overlap is
# what keeps a sentence straddling a chunk boundary intact.
ASR_CHUNK_S = 120.0
ASR_OVERLAP_S = 15.0

# Google Calendar via the `gws` CLI. Each config dir holds the encrypted
# token cache + client_secret.json for one Google account. To query multiple
# calendars (e.g. personal + work), set WITNESS_GWS_CONFIG_DIRS to a
# colon-separated list. WITNESS_GWS_CONFIG_DIR (singular) is the
# single-account shorthand.
GWS_BIN = os.environ.get("WITNESS_GWS_BIN") or "gws"


def _parse_gws_dirs() -> list[Path]:
    multi = os.environ.get("WITNESS_GWS_CONFIG_DIRS")
    if multi:
        return [Path(p).expanduser() for p in multi.split(":") if p.strip()]
    single = os.environ.get("WITNESS_GWS_CONFIG_DIR")
    if single:
        return [Path(single).expanduser()]
    return [Path.home() / ".config" / "gws"]


GWS_CONFIG_DIRS: list[Path] = _parse_gws_dirs()
# Back-compat: callers that only need one account can still import GWS_CONFIG_DIR.
GWS_CONFIG_DIR = GWS_CONFIG_DIRS[0]

# Action-item follow-up (witness/followup.py); WITNESS_ACTION_FOLLOWUP=0 turns it off.
ACTION_FOLLOWUP = (os.environ.get("WITNESS_ACTION_FOLLOWUP") or "1") != "0"
ACTIONS_STATE_DIR = Path(
    os.environ.get("WITNESS_ACTIONS_DIR")
    or (Path.home() / ".local" / "state" / "witness-actions")
).expanduser()
CLAUDE_BIN = os.environ.get("WITNESS_CLAUDE_BIN") or "claude"
FOLLOWUP_MODEL = os.environ.get("WITNESS_FOLLOWUP_MODEL") or "opus"
FOLLOWUP_CWD = Path(
    os.environ.get("WITNESS_FOLLOWUP_CWD") or (Path.home() / "src")
).expanduser()
# The only conversation the worker may send to: the recorder's self-DM.
SLACK_SELF_ID = os.environ.get("WITNESS_SLACK_SELF_ID") or "U06CXKA0ZRQ"
FACTORY_DIR = Path(
    os.environ.get("WITNESS_FACTORY_DIR")
    or (Path.home() / "src" / "nam-workbench" / "factory")
).expanduser()
