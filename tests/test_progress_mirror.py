"""v0.4.9 progress-mirror tests (design §12 M1-M5)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vidlift.mcp_bridge.jobs import PROGRESS_MIRROR_SECONDS, JobExecutor
from vidlift.mcp_bridge.registry import JobRecord, Registry
from vidlift.mcp_bridge.tools import _status_payload


class FakeRunState:
    """The shape of run.RunState the mirror reads (progress seam)."""

    def __init__(self, progress: dict, current_phase: str = "upscale"):
        self.progress = progress
        self.current_phase = current_phase
        self.last_activity_at = time.time()


def _running_record(tmp_path: Path, snapshot: dict | None = None) -> JobRecord:
    record = JobRecord(
        job_id="vl-test-mirror",
        kind="run",
        paths=["/tmp/whatever.avi"],
        options={},
    )
    record.state = "running"
    record.progress_snapshot = snapshot
    return record


def test_m1_mirror_copies_state_into_record_within_cadence(tmp_path):
    """M1: one _mirror_running_jobs pass copies the live state's
    progress/current_phase/last_activity into record.progress_snapshot;
    the throttle gate (maybe_mirror) honrors PROGRESS_MIRROR_SECONDS."""
    registry = Registry(tmp_path / "state")
    record = _running_record(tmp_path)
    registry._jobs[record.job_id] = record
    record.set_live(
        "state_box",
        {"state": FakeRunState({"step": "chunk", "frames_done": 5})},
    )

    services = SimpleNamespace(registry=registry)

    executor = JobExecutor(services, executor_fn=lambda *_: None)
    mirrored = executor._mirror_running_jobs()
    assert mirrored == 1
    snap = record.progress_snapshot
    assert snap["progress"]["frames_done"] == 5
    assert snap["current_phase"] == "upscale"
    assert snap["last_activity_at"] is not None
    # the save happened (jobs.json exists and carries the snapshot)
    data = json.loads((tmp_path / "state" / "jobs.json").read_text())
    row = [j for j in data["jobs"] if j["job_id"] == record.job_id][0]
    assert row["progress_snapshot"]["progress"]["frames_done"] == 5


def test_m2_payload_falls_back_to_snapshot_without_state_box(tmp_path):
    """M2: a record with NO live state_box (another process's writer)
    but a persisted progress_snapshot serves the mirror's progress."""
    registry = Registry(tmp_path / "state")
    record = _running_record(
        tmp_path,
        snapshot={
            "progress": {"step": "chunk", "frames_done": 9, "frames_total": 52},
            "current_phase": "upscale",
            "last_activity_at": 1234.5,
        },
    )
    registry._jobs[record.job_id] = record

    services = SimpleNamespace(registry=registry)

    payload = _status_payload(services, record)
    assert payload["progress"]["frames_done"] == 9
    assert payload["current_phase"] == "upscale"
    assert payload["last_activity_at"] == 1234.5
    # the computed age rides too (anti-freeze surface)
    assert isinstance(payload["activity_age_seconds"], float)


def test_m3_old_jobs_json_without_the_field_loads(tmp_path):
    """M3: a v0.4.8-era jobs.json (no progress_snapshot key) loads —
    from_dict tolerant, the field reads as None, nothing else changes."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    old_payload = {
        "jobs": [
            {
                "job_id": "vl-20261026-000000",
                "kind": "run",
                "paths": ["C:\\media\\movie.avi"],
                "options": {},
                "state": "succeeded",
                "submitted_at": 1.0,
                "started_at": 2.0,
                "finished_at": 3.0,
                "last_activity_at": 3.0,
                "output_path": None,
                "manifest_path": None,
                "log_path": None,
                "error": None,
                "ok": True,
                "engine_simulated": False,
                "engine_note": None,
                "result": None,
            }
        ]
    }
    (state_dir / "jobs.json").write_text(json.dumps(old_payload))
    registry = Registry(state_dir)
    registry.load()
    record = registry.require("vl-20261026-000000")
    assert record.progress_snapshot is None


def test_m4_mirror_survives_registry_reload_merge(tmp_path):
    """M4: a RELOADED registry (another writer's jobs.json) materializes
    records with their persisted progress_snapshot — the reinstalled
    row keeps the mirror data (the GUI's cross-process read)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    snapshot = {
        "progress": {"step": "chunk", "frames_done": 40, "frames_total": 52},
        "current_phase": "upscale",
        "last_activity_at": 42.0,
    }
    payload = {
        "jobs": [
            {
                "job_id": "vl-20261027-000001",
                "kind": "run",
                "paths": ["C:\\media\\movie.avi"],
                "options": {},
                "state": "running",
                "submitted_at": 1.0,
                "started_at": 2.0,
                "last_activity_at": 42.0,
                "progress_snapshot": snapshot,
            }
        ]
    }
    (state_dir / "jobs.json").write_text(json.dumps(payload))
    registry = Registry(state_dir)
    registry.load()
    record = registry.require("vl-20261027-000001")
    assert record.progress_snapshot == snapshot


def test_m5_activity_age_computed_not_persisted(tmp_path):
    """M5: activity_age_seconds is computed at payload time from
    last_activity_at and NEVER persisted to jobs.json."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    record = JobRecord(
        job_id="vl-test-age",
        kind="run",
        paths=["/tmp/x.avi"],
        options={},
    )
    record.state = "running"
    record.last_activity_at = time.time() - 120.0
    registry = Registry(state_dir)
    registry._jobs[record.job_id] = record

    services = SimpleNamespace(registry=registry)

    payload = _status_payload(services, record)
    age = payload["activity_age_seconds"]
    assert 119.0 <= age <= 121.0
    # nothing age-like in the persisted row
    registry.save()
    data = json.loads((state_dir / "jobs.json").read_text())
    row = [j for j in data["jobs"] if j["job_id"] == record.job_id][0]
    assert "activity_age_seconds" not in row


def test_m6_mirror_throttle_respects_cadence(tmp_path):
    """The touch-path gate: a second maybe_mirror inside the 10 s
    window is a no-op (no redundant registry save)."""
    registry = Registry(tmp_path / "state")
    record = _running_record(tmp_path)
    registry._jobs[record.job_id] = record
    record.set_live("state_box", {"state": FakeRunState({"step": "x"})})

    services = SimpleNamespace(registry=registry)

    executor = JobExecutor(services, executor_fn=lambda *_: None)
    executor.maybe_mirror()
    snapshot_after_first = record.progress_snapshot
    assert snapshot_after_first is not None
    # within the window: no fresh pass (the snapshot object identity
    # stays; the throttle clock didn't advance past 10 s)
    executor._last_mirror_at = time.monotonic()  # pretend it just ran
    executor.maybe_mirror()
    assert record.progress_snapshot is snapshot_after_first