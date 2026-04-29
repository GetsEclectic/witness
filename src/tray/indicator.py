"""System-tray indicator for the capture daemon.

Green icon   → daemon up, idle
Red icon     → daemon up, recording
Gray icon    → daemon down / can't reach UI

Left-click is the primary affordance: opens the live transcript UI at
localhost:7878 in the default browser. Right-click is a menu with the
same open action plus Quit (stopping a live recording gracefully
requires signalling the daemon, which lives in a separate process; the
daemon listens for SIGINT/SIGTERM).

Polls /api/status every few seconds. This is a UI-only process — it owns
no capture state.
"""
from __future__ import annotations

import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from json import loads
from time import sleep

import pystray
from PIL import Image, ImageDraw

from witnessd.config import WEBAPP_HOST, WEBAPP_PORT


def _hide_from_dock_and_switcher() -> None:
    """macOS only: tell NSApplication this is a menu-bar accessory so the
    Python host process doesn't appear in the Dock or in cmd+Tab. Must run
    before pystray initializes the NSApplication (i.e. before tray.run())."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory  # type: ignore[import-not-found]
    except ImportError:
        return
    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )

UI_URL = f"http://{WEBAPP_HOST}:{WEBAPP_PORT}/"
STATUS_URL = f"http://{WEBAPP_HOST}:{WEBAPP_PORT}/api/status"
POLL_S = 3.0


def _icon(color: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=color)
    return img


ICONS = {
    "idle":      _icon((80, 200, 120)),
    "recording": _icon((230, 70, 70)),
    "offline":   _icon((120, 120, 120)),
}


def _fetch_status() -> str:
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=1.5) as r:
            payload = loads(r.read().decode())
        return "recording" if payload.get("active") else "idle"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return "offline"


def main() -> None:
    state = {"mode": "offline"}

    def on_open(icon, item=None):  # noqa: ARG001
        webbrowser.open(UI_URL)

    def on_quit(icon, item=None):  # noqa: ARG001
        icon.stop()

    tray = pystray.Icon(
        "witness",
        icon=ICONS["offline"],
        title="witness: offline",
        menu=pystray.Menu(
            pystray.MenuItem("Open UI", on_open, default=True),
            pystray.MenuItem("Quit tray", on_quit),
        ),
    )

    def poller() -> None:
        while True:
            mode = _fetch_status()
            if mode != state["mode"]:
                state["mode"] = mode
                tray.icon = ICONS[mode]
                tray.title = f"witness: {mode}"
            sleep(POLL_S)

    t = threading.Thread(target=poller, daemon=True)
    t.start()
    _hide_from_dock_and_switcher()
    tray.run()


if __name__ == "__main__":
    main()
