"""Tabs of every running Chrome and Safari instance, addressed by pid.

`tell application "Google Chrome"` names a program, not a process, so with two
copies running AppleScript reaches only the one macOS picks. ScriptingBridge
can address a pid, which is the only identifier that tells them apart.

Runs as a subprocess because an Apple event to a wedged application blocks,
and a process is the only thing the daemon can abandon. One JSON object::

    {"instances": [{"pid": int, "bundle_id": str,
                    "tabs": [[url, title], ...], "errors": [str]}]}
"""
from __future__ import annotations

import json
import sys

BROWSER_BUNDLES = {
    "com.google.Chrome": "title",
    "com.apple.Safari": "name",
}

# Apple-event timeout per instance, in ticks (1/60 s) — well inside the
# subprocess timeout covering the whole sweep.
AE_TIMEOUT_TICKS = 60


def _tabs_of_window(window, title_prop: str) -> list[list[str]]:
    tabs = []
    for tab in window.tabs() or []:
        url = tab.URL()
        if not url:
            continue
        title = tab.name() if title_prop == "name" else tab.title()
        tabs.append([str(url), str(title or "")])
    return tabs


def _probe_instance(sb_class, pid: int, title_prop: str) -> tuple[list, list]:
    """One instance's tabs, plus whatever went wrong getting them.

    Nothing here raises: a window that answers must not be lost to one that
    doesn't, nor an instance to another instance.
    """
    tabs: list[list[str]] = []
    errors: list[str] = []
    app = sb_class.applicationWithProcessIdentifier_(pid)
    if app is None:
        return tabs, [f"pid {pid} exposes no scripting interface"]
    try:
        app.setTimeout_(AE_TIMEOUT_TICKS)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"pid {pid} timeout not settable: {exc!r}")
    try:
        windows = app.windows() or []
    except Exception as exc:  # noqa: BLE001
        return tabs, errors + [f"pid {pid} windows: {exc!r}"]
    for window in windows:
        try:
            tabs.extend(_tabs_of_window(window, title_prop))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pid {pid} window: {exc!r}")
    return tabs, errors


def probe() -> dict:
    """Every running browser instance, listed even when it has no tabs."""
    from AppKit import NSWorkspace  # type: ignore[import-not-found]
    from ScriptingBridge import SBApplication  # type: ignore[import-not-found]

    instances = []
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        bundle_id = app.bundleIdentifier()
        if bundle_id is None:
            continue
        bundle_id = str(bundle_id)
        title_prop = BROWSER_BUNDLES.get(bundle_id)
        if title_prop is None:
            continue
        pid = int(app.processIdentifier())
        tabs, errors = _probe_instance(SBApplication, pid, title_prop)
        instances.append({
            "pid": pid,
            "bundle_id": bundle_id,
            "tabs": tabs,
            "errors": errors,
        })
    return {"instances": instances}


def main() -> int:
    json.dump(probe(), sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
