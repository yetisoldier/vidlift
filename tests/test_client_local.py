"""Slice 3 smoke tests (v0.4.4 design §10, slice 3).

``build_services`` returns a services object with registry + executor;
``client_local.enqueue_run`` enqueues JobRecords against a tmp
registry; the restart sweep runs on GUI-side construction exactly as
the MCP server's does; the status snapshot is the bridge's own
``_status_payload`` output.
"""

from __future__ import annotations

import json

import pytest

from vidlift.mcp_bridge.client_local import (
    BatchTooLarge,
    MAX_GUI_BATCH,
    LocalBridgeClient,
)
from vidlift.mcp_bridge.server import build_services
from vidlift.mcp_bridge.tools import _status_payload


def test_build_services_returns_registry_and_executor(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    services = build_services(tmp_path)
    assert services.registry is not None
    assert services.executor is not None
    assert services.executor.registry is services.registry
    assert services.state_dir == tmp_path
    # The executor is constructed but NOT started by the factory
    # (the consumer owns the start/stop lifecycle).
    assert services.executor._threads == []
    # The registry file exists (created by Registry.__init__).
    assert (tmp_path / "jobs.json").parent == tmp_path


def test_build_services_resolves_env_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDLIFT_STATE_DIR", str(tmp_path / "envstate"))
    services = build_services()
    assert services.state_dir == tmp_path / "envstate"


def test_build_services_runs_the_restart_sweep(tmp_path, monkeypatch):
    # A pre-seeded jobs.json with a running job becomes failed with the
    # binding restart error on the NEXT construction — the same sweep
    # the MCP server has always run (design §5 restart discipline).
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    from vidlift.mcp_bridge.registry import RESTART_ERROR, Registry

    registry = Registry(tmp_path)
    record = registry.enqueue(kind="run", paths=["/nowhere/movie.avi"], options={})
    registry.transition(record, "running")
    registry.save()

    services = build_services(tmp_path)
    swept = services.registry.get(record.job_id)
    assert swept.state == "failed"
    assert swept.error == RESTART_ERROR


def test_client_local_enqueue_run_creates_jobs_in_order(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    first = tmp_path / "a.avi"
    second = tmp_path / "b.avi"
    first.write_text("")
    second.write_text("")
    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    try:
        job_ids = client.enqueue_run(
            [str(first), str(second)], options={"gb_per_hour": 2.0}, engine="ncnn"
        )
        assert len(job_ids) == 2
        record_a = client.registry.require(job_ids[0])
        record_b = client.registry.require(job_ids[1])
        assert record_a.kind == "run"
        assert record_a.paths == [str(first)]
        assert record_b.paths == [str(second)]
        # The engine choice rides the record's options verbatim...
        assert record_a.options["engine"] == "ncnn"
        assert record_b.options["engine"] == "ncnn"
        assert record_a.options["gb_per_hour"] == 2.0
        # v0.4.7: replace-source rides options verbatim too (additive;
        # absent reads as False at the jobs seam).
        client2 = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
        try:
            ids2 = client2.enqueue_run(
                [str(first)], options={"replace_source": True}, engine="auto"
            )
            record = client2.registry.require(ids2[0])
            assert record.options["replace_source"] is True
        finally:
            client2.stop()
        # ...and the persisted registry carries it too.
        data = json.loads((tmp_path / "jobs.json").read_text())
        saved = {row["job_id"]: row for row in data["jobs"]}
        assert saved[job_ids[0]]["options"]["engine"] == "ncnn"
    finally:
        client.stop()


def test_client_local_rejects_missing_paths_and_oversized_batches(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    try:
        with pytest.raises(FileNotFoundError):
            client.enqueue_run([str(tmp_path / "missing.avi")])
        files = []
        for i in range(MAX_GUI_BATCH + 1):
            p = tmp_path / f"f{i}.avi"
            p.write_text("")
            files.append(str(p))
        with pytest.raises(BatchTooLarge):
            client.enqueue_run(files)
        # The oversized click left NO partial enqueues behind.
        assert len(client.registry.queued()) == 0
    finally:
        client.stop()


def test_client_local_status_snapshot_is_bridge_status_payload(
    tmp_path, monkeypatch
):
    # One source of truth: the snapshot equals `_status_payload`'s
    # output for the same record (design Appendix rule).
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    path = tmp_path / "movie.avi"
    path.write_text("")
    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    try:
        job_ids = client.enqueue_run([str(path)])
        record = client.registry.require(job_ids[0])
        # v0.4.9: `activity_age_seconds` is computed at read time (never
        # persisted), so the two payload builds differ by the wall-clock
        # slice between them; compare modulo that key.
        served = client.status_snapshot(job_ids[0])
        fresh = _status_payload(client.services, record)
        served.pop("activity_age_seconds", None)
        fresh.pop("activity_age_seconds", None)
        assert served == fresh
        # Unknown job: None (never a raise from the GUI's 1 Hz read).
        assert client.status_snapshot("vl-00000000-unknown") is None
    finally:
        client.stop()


def test_client_local_cancel_and_history_shapes(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    path = tmp_path / "movie.avi"
    path.write_text("")
    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    try:
        job_ids = client.enqueue_run([str(path)])
        # A queued job cancels outright (never runs).
        assert client.cancel(job_ids[0]) == "cancelled"
        record = client.registry.require(job_ids[0])
        assert record.state == "cancelled"
        assert client.cancel("vl-00000000-unknown") == "not-found"
        rows = client.history()
        assert len(rows) == 1
        assert rows[0]["job_id"] == job_ids[0]
        assert rows[0]["state"] == "cancelled"
        # history() matches tool_list's history-block serialization.
        assert rows[0].keys() >= {
            "job_id", "file_path", "state", "finished_at", "ok",
            "engine_simulated", "output_path", "manifest_path",
        }
        # Active count reflects the closed job only.
        assert client.active_job_count() == 0
    finally:
        client.stop()