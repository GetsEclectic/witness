"""find_similar_unknowns + auto-absorption on /unknowns bind."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from witnessd.webapp import RecordingStatus, build_app


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def _vec_at_cos(base: np.ndarray, target: float, rng: np.random.Generator) -> np.ndarray:
    """Unit vector with exactly `target` cosine to `base` (modulo float)."""
    rand = rng.standard_normal(len(base)).astype(np.float32)
    orth = rand - (rand @ base) * base
    orth = orth / np.linalg.norm(orth)
    return (target * base + np.sqrt(1.0 - target ** 2) * orth).astype(np.float32)


def _save(path: Path, vec: np.ndarray) -> None:
    np.save(path, vec[None, :].astype(np.float32))


def test_is_backchannel():
    from witness.fingerprint import _is_backchannel

    # Pure backchannels
    for s in ("Yeah.", "yeah", "Yes.", "Mhm.", "uh-huh", "Mm-hmm.",
              "Okay.", "Right.", "Totally.", "Yeah, yeah!", "Oh!", "Cool.",
              "Got it.", "I see.", "For sure.", ""):
        assert _is_backchannel(s), f"expected backchannel: {s!r}"

    # Real content (even with a backchannel mixed in)
    for s in (
        "but, yeah, I think we should ship it",
        "no, the migration script broke",
        "Yeah, let's regroup tomorrow.",
        "I have a question.",
    ):
        assert not _is_backchannel(s), f"expected NOT backchannel: {s!r}"


def test_cluster_spans_drops_backchannel_utterances(tmp_meetings_root: Path):
    from witness.fingerprint import _cluster_spans

    folder = tmp_meetings_root / "2026-04-30T1200-test"
    folder.mkdir()
    events = [
        {"is_final": True, "speaker": "system_speaker_0", "ts_start": 0.0,
         "ts_end": 0.5, "text": "Yeah."},
        {"is_final": True, "speaker": "system_speaker_0", "ts_start": 1.0,
         "ts_end": 1.4, "text": "Mhm."},
        {"is_final": True, "speaker": "system_speaker_0", "ts_start": 2.0,
         "ts_end": 6.0, "text": "but yeah I think we should redesign the auth flow"},
        {"is_final": True, "speaker": "system_speaker_1", "ts_start": 7.0,
         "ts_end": 7.3, "text": "Okay."},
    ]
    (folder / "transcript.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n"
    )

    clusters = _cluster_spans(folder)
    # speaker_0: only the long sentence survives.
    assert clusters["system_speaker_0"] == [(2.0, 6.0)]
    # speaker_1: nothing but a backchannel → cluster doesn't form at all.
    assert "system_speaker_1" not in clusters


def test_find_similar_unknowns_thresholds(
    tmp_meetings_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    from witness import fingerprint

    vp = tmp_meetings_root / ".voiceprints"
    vp.mkdir()
    monkeypatch.setattr(fingerprint, "VOICEPRINTS_DIR", vp, raising=True)

    rng = np.random.default_rng(42)
    base = _unit(rng.standard_normal(192).astype(np.float32))
    np.save(vp / "lissa-giedt.npy", np.stack([
        _vec_at_cos(base, 0.95, rng),
        _vec_at_cos(base, 0.92, rng),
    ]))
    for h in ("aaa111", "bbb222", "ccc333"):
        _save(vp / f"unknown_{h}.npy", _vec_at_cos(base, 0.75, rng))
    _save(vp / "unknown_far00.npy", _vec_at_cos(base, 0.20, rng))

    sim = fingerprint.find_similar_unknowns("lissa-giedt")
    labels = [s[0] for s in sim]
    assert {"unknown_aaa111", "unknown_bbb222", "unknown_ccc333"} <= set(labels)
    assert "unknown_far00" not in labels
    # Sorted by score descending.
    scores = [s[1] for s in sim]
    assert scores == sorted(scores, reverse=True)


def test_bind_absorbs_similar_unknowns(
    tmp_meetings_root: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Binding one unknown should fold in any other unknown that's clearly
    the same person (above MERGE_THRESHOLD)."""
    from witness import fingerprint
    from witnessd import webapp

    vp = tmp_meetings_root / ".voiceprints"
    vp.mkdir()
    monkeypatch.setattr(fingerprint, "VOICEPRINTS_DIR", vp, raising=True)
    monkeypatch.setattr(webapp, "VOICEPRINTS_DIR", vp, raising=True)

    rng = np.random.default_rng(7)
    base = _unit(rng.standard_normal(192).astype(np.float32))
    _save(vp / "unknown_aaa111.npy", _vec_at_cos(base, 0.85, rng))
    _save(vp / "unknown_bbb222.npy", _vec_at_cos(base, 0.80, rng))
    _save(vp / "unknown_zzzfar.npy", _vec_at_cos(base, 0.15, rng))

    # Two meetings: one references aaa111, the other bbb222. Both should
    # end up labeled "Lissa Giedt" after a single bind on aaa111.
    for slug, label in (
        ("2026-04-28T1200-a", "unknown_aaa111"),
        ("2026-04-29T1200-b", "unknown_bbb222"),
    ):
        folder = tmp_meetings_root / slug
        folder.mkdir()
        (folder / "metadata.json").write_text(json.dumps({"slug": slug}))
        (folder / "speakers.json").write_text(json.dumps({"system_speaker_0": label}))
        (folder / "transcript.jsonl").write_text(json.dumps({
            "is_final": True, "speaker": "system_speaker_0",
            "ts_start": 0.0, "ts_end": 1.0, "text": "hi",
        }) + "\n")
        # Stub transcript.md so render.render() can no-op cleanly.
        (folder / "transcript.md").write_text("")

    app = build_app(
        bus=None,
        status=lambda: RecordingStatus(False, None, None, False),
        meetings_root=tmp_meetings_root,
    )
    client = TestClient(app)
    resp = client.post(
        "/api/unknowns/aaa111/bind", json={"name": "Lissa Giedt"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Lissa Giedt"
    assert body["updated_meetings"] == ["2026-04-28T1200-a"]
    absorbed_labels = {a["label"] for a in body["absorbed"]}
    assert "unknown_bbb222" in absorbed_labels
    assert "unknown_zzzfar" not in absorbed_labels

    # Both meetings now point at "Lissa Giedt"; absorbed npys are gone.
    for slug in ("2026-04-28T1200-a", "2026-04-29T1200-b"):
        sp = json.loads((tmp_meetings_root / slug / "speakers.json").read_text())
        assert sp["system_speaker_0"] == "Lissa Giedt"
    assert not (vp / "unknown_aaa111.npy").exists()
    assert not (vp / "unknown_bbb222.npy").exists()
    assert (vp / "unknown_zzzfar.npy").exists()
    assert (vp / "lissa-giedt.npy").exists()
