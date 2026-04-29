"""Test fixtures: isolate every test from the user's real ~/meetings/ tree.

`tmp_meetings_root` builds a clean MEETINGS_ROOT under tmp_path and rebinds
the module-level constants so code that imported them at module load time
sees the override. Tests that don't need the rebind (pure-function checks
on parsers / scoring / labels) can ignore it.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_meetings_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "meetings"
    root.mkdir()
    monkeypatch.setenv("WITNESS_MEETINGS_DIR", str(root))
    # config.MEETINGS_ROOT is read at import time. For tests that import it
    # transitively (webapp, fingerprint), patch the bound names too.
    from witnessd import config
    monkeypatch.setattr(config, "MEETINGS_ROOT", root, raising=True)
    monkeypatch.setattr(config, "VOICEPRINTS_DIR", root / ".voiceprints", raising=True)
    monkeypatch.setattr(config, "STATE_DIR", root / ".state", raising=True)
    monkeypatch.setattr(config, "_KEYTERMS_CACHE", None, raising=False)
    return root
