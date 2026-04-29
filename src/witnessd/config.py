import json
import os
from pathlib import Path

# Override with WITNESS_MEETINGS_DIR (e.g. for OSS users who want their
# recordings under XDG_DATA_HOME). Default keeps the original ~/meetings/
# path so existing installs migrate transparently.
MEETINGS_ROOT = Path(
    os.environ.get("WITNESS_MEETINGS_DIR") or (Path.home() / "meetings")
).expanduser()
VOICEPRINTS_DIR = MEETINGS_ROOT / ".voiceprints"
STATE_DIR = MEETINGS_ROOT / ".state"
LOG_PATH = STATE_DIR / "witness.log"

WEBAPP_HOST = os.environ.get("WITNESS_WEBAPP_HOST") or "127.0.0.1"
WEBAPP_PORT = int(os.environ.get("WITNESS_WEBAPP_PORT") or 7878)

# Window-detection poll interval (seconds). Tight enough that back-to-back
# meeting switches rotate the session within a few seconds; pactl is cheap.
POLL_INTERVAL_S = 5

# Recording pauses this many seconds after detection disappears. Pause is
# soft: ffmpeg stops, audio segment is finalized, but the folder/bus stay
# open. If the same key reappears within RESUME_WINDOW_S, recording resumes
# into the same folder as a new segment.
RECORDING_GRACE_S = 30

# How long a paused session waits for the same key to reappear before
# being finalized. After this, the folder gets its terminal `ended_at` and
# any subsequent same-key detection produces a fresh folder.
RESUME_WINDOW_S = 30 * 60

# Hard upper bound on a single recording. If detection wedges (RUNNING but
# the call actually ended), this caps the damage at one bounded archive
# rather than letting opus accumulate indefinitely.
MAX_RECORDING_S = 6 * 3600  # 6 hours

# Deepgram
DEEPGRAM_KEY_PATH = Path.home() / ".config" / "deepgram" / "key"
DEEPGRAM_SAMPLE_RATE = 16000  # mono s16le; plenty for speech
DEEPGRAM_MODEL = "nova-3"

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


def read_deepgram_key() -> str:
    return DEEPGRAM_KEY_PATH.read_text().strip()


# Proper nouns for Deepgram Nova-3 keyterm prompting. Extend this list when a
# name, company, or product consistently gets mistranscribed. Runtime merges
# this with names harvested from past meetings' speakers.json (load_keyterms).
KEYTERMS: list[str] = []

# Prefixes used for unresolved speaker IDs or test fixtures — skip these
# when harvesting real names from speakers.json.
_SPEAKER_ID_PREFIXES = (
    "speaker_",
    "mic_speaker_",
    "system_speaker_",
    "unknown_",
    "espeak-",  # scripts/fake-remote-speakers.sh test voices
)


# Cache for load_keyterms(). Invalidated when MEETINGS_ROOT's mtime changes —
# new meetings (which create new subfolders) bump it; in-place edits to
# existing speakers.json files do NOT, so a relabel needs the daemon restart
# anyway to reload Deepgram. This caches the cost of globbing N folders on
# each session start.
_KEYTERMS_CACHE: tuple[float, list[str]] | None = None


def load_keyterms() -> list[str]:
    """Static KEYTERMS unioned with real names from past speakers.json files.

    Harvested names lose the `unknown_<hash>` / `*speaker_N` chain links
    (those aren't human names). Dedup is case-sensitive; "Alex" and
    "alex" would both ship, which is fine — Deepgram treats them as
    distinct boosts.
    """
    global _KEYTERMS_CACHE
    try:
        mtime = MEETINGS_ROOT.stat().st_mtime if MEETINGS_ROOT.exists() else 0.0
    except OSError:
        mtime = 0.0
    if _KEYTERMS_CACHE is not None and _KEYTERMS_CACHE[0] == mtime:
        return list(_KEYTERMS_CACHE[1])

    seen: set[str] = set()
    result: list[str] = []
    for term in KEYTERMS:
        if term and term not in seen:
            seen.add(term)
            result.append(term)
    for sp_path in sorted(MEETINGS_ROOT.glob("*/speakers.json")):
        try:
            data = json.loads(sp_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for value in data.values():
            if not isinstance(value, str):
                continue
            value = value.strip()
            if not value or value in seen:
                continue
            if value.startswith(_SPEAKER_ID_PREFIXES):
                continue
            seen.add(value)
            result.append(value)
    _KEYTERMS_CACHE = (mtime, list(result))
    return result
