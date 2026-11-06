"""Heartbeat truth survives authoritative row refresh and mirror publication."""
import json
from types import SimpleNamespace

import pytest

from vidlift.mcp_bridge.jobs import JobExecutor
from vidlift.mcp_bridge import BridgeServices
from vidlift.mcp_bridge.registry import Registry
from vidlift.mcp_bridge.tools import _status_payload
from vidlift.run import RunState


def running(tmp_path):
    registry = Registry(tmp_path / "state")
    record = registry.enqueue(kind="run", paths=[str(tmp_path / "synthetic.avi")], options={})
    registry.transition(record, "running")
    token = record._extra["owner"]["claim_token"]
    registry.update_job(record.job_id, owner_token=token, last_activity_at=100.0)
    return registry, record, token


@pytest.mark.parametrize("surface", ["live", "mirror"])
def test_age_matches_displayed_heartbeat_not_old_record(tmp_path, monkeypatch, surface):
    registry, record, token = running(tmp_path)
    monkeypatch.setattr("vidlift.mcp_bridge.tools.time.time", lambda: 200.0)
    if surface == "live":
        state = RunState(file_path=record.paths[0])
        state.last_activity_at = 199.0
        state.current_phase = "transcribe"
        state.progress = {"segments_done": 3}
        record.set_live("state_box", {"state": state})
    else:
        registry.update_job(record.job_id, owner_token=token, progress_snapshot={
            "last_activity_at": 199.0, "current_phase": "transcribe",
            "progress": {"segments_done": 3},
        })
        registry = Registry(tmp_path / "state")
        record = registry.require(record.job_id)
    before = registry.path.read_bytes()
    payload = _status_payload(BridgeServices(registry=registry, state_dir=tmp_path / "state"), record)
    assert payload["last_activity_at"] == 199.0
    assert payload["activity_age_seconds"] == 1.0
    assert payload["progress"]["segments_done"] == 3
    assert registry.path.read_bytes() == before  # a read is never a write


def test_mirror_persists_heartbeat_for_an_independent_reader(tmp_path):
    registry, record, _token = running(tmp_path)
    state = RunState(file_path=record.paths[0])
    state.touch()
    state.current_phase = "transcribe"
    state.progress = {"segments_done": 7}
    record.set_live("state_box", {"state": state})
    executor = JobExecutor(SimpleNamespace(registry=registry), executor_fn=lambda *_: None)
    assert executor._mirror_running_jobs() == 1
    reader = Registry(tmp_path / "state")
    observed = reader.require(record.job_id)
    assert observed.last_activity_at == state.last_activity_at
    assert observed.progress_snapshot["last_activity_at"] == state.last_activity_at
    assert observed.progress_snapshot["progress"] == {"segments_done": 7}
    assert "activity_age_seconds" not in json.loads(reader.path.read_text())["jobs"][0]


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_terminal_age_remains_null_with_retained_snapshot(tmp_path, terminal):
    registry, record, token = running(tmp_path)
    registry.update_job(record.job_id, owner_token=token, progress_snapshot={
        "last_activity_at": 199.0, "progress": {"segments_done": 3},
    })
    registry.finish_job(record.job_id, terminal, owner_token=token)
    payload = _status_payload(BridgeServices(registry=registry, state_dir=tmp_path / "state"), record)
    assert payload["activity_age_seconds"] is None
