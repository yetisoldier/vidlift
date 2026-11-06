"""v0.4.16 (Karen remediation D-7/D-3) — the visibility spec's
T-1..T-6 ``transcription`` device-echo matrix (``_transcription_echo``,
the additive block on every per-job ``_status_payload``).

Karen's validation (probes 5/6) found only the S-8 idle-shape guard
existed, and T-5's absence is exactly why D-3 shipped: the manifest
echo arm read top-level ``manifest["subtitles"]`` while every real
manifest stores the subtitles block at ``manifest["run"]["subtitles"]``
(``_build_manifest``, run.py) — so Hermes's one-shot read of a
finished/failed run job always echoed ``transcription.device: null``.

Each T row here drives the REAL payload path (``_status_payload`` over
a real registry/record — the ONE shape the GUI's 1 Hz tick, the MCP
status tool, and the monitor ``/status`` all serve) with the honest
anti-lie rule pinned per spec:
  T-1 live run mid-upscale    -> BOTH keys null (honest not-yet)
  T-2 live run, subs carried  -> the recorded device, incl. the exact
                                 "cpu (cuda unavailable)" R1 wording
  T-3 subs terminal success   -> result.device + the requested pin
  T-4 subs foreign factory    -> "" reads as null (never empty string)
  T-5 terminal run via MANIFEST (no live state) -> the manifest echo
  T-6 run with no transcription (status "none") -> BOTH keys null
"""

from __future__ import annotations

import json

from vidlift.mcp_bridge import BridgeServices
from vidlift.mcp_bridge.registry import Registry
from vidlift.mcp_bridge.tools import _status_payload
from vidlift.run import RunState

_ECHO_SHAPE = {"device", "device_requested"}


def _services(tmp_path):
    registry = Registry(tmp_path / "state")
    services = BridgeServices(registry=registry, state_dir=tmp_path / "state")
    return registry, services


def _record(registry, kind, *, state="running", options=None):
    record = registry.enqueue(
        kind=kind, paths=["/m/movie.avi"], options=options or {}
    )
    registry.transition(record, "running")
    if state != "running":
        registry.transition(record, state)
    return record


def _live(record, run_state):
    record.set_live("state_box", {"state": run_state})
    return run_state


def test_t1_live_run_job_mid_upscale_both_keys_null(tmp_path):
    """T-1: a LIVE run job mid-upscale — device null is the CORRECT
    answer (not yet recorded, never guessed), device_requested null (a
    run job carries no subs options pin); the surrounding payload stays
    a running upscale with real frames progress."""
    registry, services = _services(tmp_path)
    record = _record(registry, "run")
    run_state = RunState(file_path="/m/movie.avi")
    run_state.current_phase = "upscale"
    run_state.progress = {"frames_done": 10, "frames_total": 100}
    _live(record, run_state)
    payload = _status_payload(services, record)
    assert payload["state"] == "running"
    assert payload["current_phase"] == "upscale"
    assert payload["progress"]["frames_done"] == 10
    assert payload["transcription"] == {
        "device": None,
        "device_requested": None,
    }


def test_t2_live_run_carried_records_device_incl_fallback_wording(tmp_path):
    """T-2: a live run job whose subtitles phase completed echoes the
    recorded device — including the EXACT v0.4.15.3 R1 fallback wording
    "cpu (cuda unavailable)" (pin the string)."""
    registry, services = _services(tmp_path)
    record = _record(registry, "run")
    for device, label in (
        ("cpu (cuda unavailable)", "the R1 fallback wording"),
        ("cuda", "the plain real-GPU wording"),
    ):
        run_state = RunState(file_path="/m/movie.avi")
        run_state.current_phase = "subtitles"
        run_state.subtitles = {
            "status": "carried",
            "transcription": {"device": device, "model": "/m"},
        }
        _live(record, run_state)
        payload = _status_payload(services, record)
        assert payload["transcription"]["device"] == device, label
        assert payload["transcription"]["device_requested"] is None
        # the block keeps its EXACT two-key shape (S-8's guarantee)
        assert set(payload["transcription"]) == _ECHO_SHAPE


def test_t3_subs_terminal_success_reports_result_and_pin(tmp_path):
    """T-3: a terminal subs job echoes ``record.result["device"]`` and
    the requested pin from the record's options (e.g. "auto")."""
    registry, services = _services(tmp_path)
    record = _record(
        registry,
        "subs",
        state="succeeded",
        options={"device": "auto"},
    )
    record.result = {"device": "cuda", "language": "en", "cue_count": 3}
    registry.save()
    payload = _status_payload(services, record)
    assert payload["state"] == "succeeded"
    assert payload["transcription"] == {
        "device": "cuda",
        "device_requested": "auto",
    }


def test_t4_subs_foreign_engine_empty_device_reads_null(tmp_path):
    """T-4: a terminal subs job whose foreign engine factory left
    ``result["device"] == ""`` echoes NULL — the honest unknown, never
    an empty string."""
    registry, services = _services(tmp_path)
    record = _record(
        registry,
        "subs",
        state="succeeded",
        options={},
    )
    record.result = {"device": ""}
    registry.save()
    payload = _status_payload(services, record)
    assert payload["transcription"] == {
        "device": None,
        "device_requested": None,
    }


def test_t5_terminal_run_manifest_echo_is_live(tmp_path):
    """T-5 (the D-3 pin): a FINISHED run job read with NO live state —
    exactly Hermes's one-shot read of a completed job — echoes the
    device the manifest recorded under ``manifest["run"]["subtitles"]``
    (the real layout ``_build_manifest`` writes). The pre-fix echo read
    a non-existent top-level ``manifest["subtitles"]`` and always
    returned null here."""
    registry, services = _services(tmp_path)
    record = _record(registry, "run", state="succeeded")
    # a real-shaped manifest: subtitles live INSIDE the "run" dict
    manifest = {
        "schema": 2,
        "run": {
            "input_file": "/m/movie.avi",
            "outcome": "succeeded",
            "subtitles": {
                "status": "carried",
                "transcription": {
                    "model": "/cache/whisper/models/medium",
                    "device": "cpu",
                    "language": "en",
                    "cue_count": 3,
                },
            },
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    record.manifest_path = str(path)

    payload = _status_payload(services, record)
    assert payload["transcription"]["device"] == "cpu"
    assert payload["transcription"]["device_requested"] is None
    # the echo's honest-null survives adversarial shapes: a failed run's
    # manifest whose subtitles block never recorded a device
    manifest["run"]["subtitles"]["transcription"]["device"] = ""
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    assert _status_payload(services, record)["transcription"]["device"] is None


def test_t5b_manifest_without_run_subtitles_stays_null(tmp_path):
    """T-5's negative arm: a real manifest from a run with NO
    transcription (``run.subtitles`` is None) echoes both keys null —
    and a manifest that only carried the OLD wrong top-level key (the
    pre-fix read path) stays null: the echo reads the REAL layout."""
    registry, services = _services(tmp_path)
    record = _record(registry, "run", state="succeeded")
    manifest = {
        "schema": 2,
        "run": {"outcome": "succeeded", "subtitles": None},
        # the wrong shape the pre-fix code read instead:
        "subtitles": {
            "status": "carried",
            "transcription": {"device": "cpu"},
        },
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    record.manifest_path = str(path)
    payload = _status_payload(services, record)
    assert payload["transcription"] == {
        "device": None,
        "device_requested": None,
    }


def test_t6_run_without_transcription_both_keys_null(tmp_path):
    """T-6: a run job with no transcription (subtitles status "none")
    echoes both keys null; the pre-existing payload keys are untouched
    (the S-8 additivity guard owns the key set; this row owns values)."""
    registry, services = _services(tmp_path)
    record = _record(registry, "run")
    run_state = RunState(file_path="/m/movie.avi")
    run_state.current_phase = "encode"
    run_state.progress = {"frames_done": 5, "frames_total": 50}
    run_state.subtitles = {"status": "none"}
    _live(record, run_state)
    payload = _status_payload(services, record)
    assert payload["transcription"] == {
        "device": None,
        "device_requested": None,
    }
    assert payload["state"] == "running"