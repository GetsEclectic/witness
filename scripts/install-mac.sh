#!/usr/bin/env bash
# witness Mac install: validate the prebuilt audio-tap binary, sync deps via
# uv (which pulls bundled ffmpeg + pyobjc), and install launchd agents for
# autostart on login.
#
# This is the end-user install path. The maintainer rebuilds the Swift
# binary via mac/build.sh; that's not part of normal install.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# --- Step 1: prebuilt audiotap binary -------------------------------------

AUDIOTAP="$PROJECT_DIR/mac/witness-audiotap"
if [[ ! -x "$AUDIOTAP" ]]; then
    echo "ERROR: $AUDIOTAP missing or not executable." >&2
    echo "Run mac/build.sh to rebuild (requires Xcode Command Line Tools)." >&2
    exit 1
fi
if ! codesign -dv "$AUDIOTAP" >/dev/null 2>&1; then
    echo "ERROR: $AUDIOTAP is not codesigned." >&2
    echo "Run mac/build.sh to re-sign." >&2
    exit 1
fi
echo "[1/4] audiotap binary OK: $AUDIOTAP"

# --- Step 2: uv sync (pulls bundled ffmpeg + pyobjc) ----------------------

UV_BIN=$(command -v uv || true)
if [[ -z "$UV_BIN" ]]; then
    echo "ERROR: uv not on PATH. Install with: brew install uv" >&2
    echo "       (or: curl -LsSf https://astral.sh/uv/install.sh | sh)" >&2
    exit 1
fi
# Resolve symlinks so launchd gets the actual binary path (avoids breakage
# if a brew upgrade rewrites the symlink).
UV_BIN=$(/usr/bin/python3 -c "import os, sys; print(os.path.realpath(sys.argv[1]))" "$UV_BIN")
echo "[2/4] uv sync ($UV_BIN)"
"$UV_BIN" sync

# --- Step 3: launchd agents ----------------------------------------------

AGENT_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/witness"
mkdir -p "$AGENT_DIR" "$LOG_DIR"

for plist in launchd/com.witness.daemon.plist launchd/com.witness.tray.plist; do
    base=$(basename "$plist")
    target="$AGENT_DIR/$base"
    sed -e "s|{HOME}|$HOME|g" \
        -e "s|{PROJECT_DIR}|$PROJECT_DIR|g" \
        -e "s|{UV_BIN}|$UV_BIN|g" \
        "$plist" > "$target"
    # Reload: bootout is the modern equivalent of unload (silent on first run).
    launchctl bootout "gui/$(id -u)/${base%.plist}" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$target"
    echo "  loaded: $target"
done
echo "[3/4] launchd agents installed"

# --- Step 4: permission checklist ----------------------------------------

cat <<'EOF'
[4/4] permission checklist:

  Microphone — System Settings ▸ Privacy & Security ▸ Microphone
    Grant access to your terminal (Terminal.app / iTerm) and to `uv`.
    Without this, ffmpeg avfoundation will fail to open the mic.

  Audio Capture (macOS 14.4+) — System Settings ▸ Privacy & Security ▸
    Audio Capture (sometimes labelled "System Audio Recording")
    Grant access on first record-now; the system prompts the first time
    witness-audiotap calls AudioHardwareCreateProcessTap.

To verify the agents are running:
    launchctl list | grep witness

To watch logs:
    tail -F ~/Library/Logs/witness/daemon.{out,err}.log

To uninstall later:
    launchctl bootout "gui/$(id -u)/com.witness.daemon"
    launchctl bootout "gui/$(id -u)/com.witness.tray"
    rm ~/Library/LaunchAgents/com.witness.{daemon,tray}.plist
EOF
