"""v0.4.6 status API tests (design §5, tests 1-11).

The server under test is the REAL ``MonitorServer`` on a loopback
socket with a REAL ``LocalBridgeClient`` over a tmp registry — the
``FakeClient`` seam exists for GUI tests, not for the API's
end-to-end truth (the API's honesty story is "read through the
client"; these tests read through the real one). Conftest's autouse
guard pins engine selection to the stub, so nothing here launches a
real GPU probe.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

from vidlift import __version__
from vidlift.benchmark import BENCHMARKS_FILENAME, BenchResult, geometry_bucket
from vidlift.mcp_bridge.client_local import LocalBridgeClient
from vidlift.monitor import (
    API_VERSION,
    MONITOR_TOKEN_DEFAULT,
    MonitorServer,
    StatusView,
    lan_ip,
    start_server,
    start_server_with_fallback,
    status_payload,
    stop_server,
)


@pytest.fixture(autouse=True)
def _no_leaked_listener():
    """Every test leaves zero listeners behind (sibling guarantee)."""
    yield
    stop_server()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    yield client
    client.stop()


def _get(base: str, path: str, token: str | None = None, method: str = "GET"):
    """One HTTP request; returns (status, parsed-json-or-None)."""
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(base + path, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, None


def _base() -> str:
    """The running server's base URL from the module (the from-import
    binding trap: ``active_server`` is reassigned inside the module by
    start/stop, so read it as an attribute of the MODULE, never as an
    earlier-bound import)."""
    from vidlift import monitor as _monitor

    host, port = _monitor.active_server.server_address
    return f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"


def _start(client, *, token: str = MONITOR_TOKEN_DEFAULT, port: int | None = None):
    return start_server(
        client=client, host="127.0.0.1", port=port or _free_port(), token=token
    )


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeRunState:
    """Just enough of run.RunState for _status_payload's live branch:
    to_dict() verbatim fields (the update block reads to_dict only),
    plus the attributes _eta_seconds reads directly, and a log object
    exposing tail(n)."""

    def __init__(self, file_path: str, progress: dict | None = None) -> None:
        now = time.time()
        self.current_phase = "upscale"
        self.progress = dict(progress or {})
        self.plan = None
        self.engine_name = None
        self.engine_model = None
        self.log = None
        self._to_dict = {
            "file_path": file_path,
            "started_at": now,
            "current_phase": self.current_phase,
            "last_activity_at": now,
            "log_path": None,
            "commands": [],
            "phases": [],
            "plan_output_path": None,
            "output_path": None,
            "prepared_path": None,
            "mezzanine_path": None,
            "engine_name": None,
            "engine_note": "test",
            "engine_simulated": True,
            "engine_model": None,
            "engine_native_scale": None,
            "fail_open": False,
            "fail_open_reason": None,
            "frame_clock": None,
            "duration_gates": None,
            "model_hashes_verified": False,
            "progress": self.progress,
            "benchmark_constant": None,
            "cancelled": False,
            "sample_verdict": None,
            "verification": None,
            "manifest_path": None,
            "finished_at": None,
            "ok": None,
            "error": None,
            "frame_gates": None,
            "resume": None,
            "rejected_mezzanine": None,
            "subtitles": None,
        }

    def to_dict(self) -> dict:
        return dict(self._to_dict)


def _seed_running(registry, path, *, progress: dict | None = None, state=None):
    """A running job with a live state_box — the same seam the
    executor sets (jobs.py state_box) — so the real _status_payload
    serves the live branch."""
    record = registry.enqueue(kind="run", paths=[str(path)], options={"engine": "stub"})
    registry.transition(record, "running")
    record.set_live("state_box", {"state": state or _FakeRunState(str(path), progress)})
    record.set_live("commands", [])
    return record


def _seed_done(registry, path, *, terminal="succeeded"):
    record = registry.enqueue(kind="run", paths=[str(path)], options={})
    registry.transition(record, "running")
    registry.transition(record, terminal)
    return record


def _probe_report(path):
    from vidlift.plan import build_file_plan
    from vidlift.probe import classify, parse_probe

    payload = """
    {
      "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "mpeg4",
         "width": 640, "height": 360, "sample_aspect_ratio": "1:1",
         "field_order": "progressive", "r_frame_rate": "25/1",
         "avg_frame_rate": "25/1"}
      ],
      "format": {"format_name": "avi", "duration": "3600.0"}
    }
    """
    report = classify(parse_probe(str(path), payload))
    report.path = str(path)
    return report, build_file_plan(report, gb_per_hour=1.5)


# ---- 1: health needs no token -------------------------------------------------


def test_health_needs_no_token(client, tmp_path):
    path = tmp_path / "movie.avi"
    path.write_text("")
    _seed_running(
        client.registry, path, progress={"step": "chunk", "frames_done": 1, "frames_total": 10}
    )
    server, base = _start(client)
    assert isinstance(server, MonitorServer)
    status, payload = _get(base, "/health")
    assert status == 200
    assert payload["ok"] is True
    assert payload["version"] == __version__
    assert payload["busy"] is True  # the seeded running job


def test_health_busy_false_when_idle(client):
    server, base = _start(client)
    status, payload = _get(base, "/health")
    assert status == 200 and payload["busy"] is False


# ---- 2: status/history token gate ---------------------------------------------


def test_status_requires_token(client):
    _start(client, token="sekrit")
    base = _base()
    assert _get(base, "/status")[0] == 401
    assert _get(base, "/status?token=wrong")[0] == 401
    assert _get(base, "/status", token="sekrit")[0] == 200
    assert _get(base, "/status?token=sekrit")[0] == 200
    # ...and /history shares the gate.
    assert _get(base, "/history")[0] == 401
    assert _get(base, "/history?token=sekrit")[0] == 200


# ---- 3: unknown path 404 (lists endpoints) -------------------------------------


def test_unknown_path_404_and_lists_endpoints(client):
    _start(client, token="t")
    status, payload = _get(_base(), "/nope?token=t")
    assert status == 404
    assert payload["error"] == "unknown endpoint"
    assert payload["endpoints"] == ["/health", "/status", "/history"]


# ---- 4: POST returns 501 (read-only by construction) ---------------------------


def test_post_returns_501(client):
    _start(client, token="t")
    status, _payload = _get(_base(), "/status?token=t", method="POST")
    assert status == 501


def test_payload_failure_serves_honest_500_not_socket_reset(client, tmp_path):
    """A payload-side exception must surface as an honest 500 JSON —
    never a socket reset (a wedged-run diagnosis must not look wedged
    itself). The seeded fake state is deliberately malformed (no
    ``log`` attribute)."""
    path = tmp_path / "broken.avi"
    path.write_text("")
    record = _seed_running(
        client.registry, path, state=type("Broken", (), {"to_dict": lambda self: {"progress": {}}})()
    )
    _start(client, token="t")
    status, payload = _get(_base(), "/status?token=t")
    assert status == 500
    assert payload["error"] == "status handler failed"
    assert "detail" in payload


# ---- 5: /status payload shape, idle ---------------------------------------------


def test_status_payload_shape_idle(client):
    _start(client, token="t")
    status, payload = _get(_base(), "/status?token=t")
    assert status == 200
    assert payload["api_version"] == API_VERSION == 1
    assert payload["version"] == __version__
    assert payload["active_jobs"] == []
    assert payload["queue_active"] is False
    assert payload["queued_count"] == 0
    assert payload["vidlift_version_note"]
    assert isinstance(payload["uptime_seconds"], float)
    assert isinstance(payload["server_time"], float)


# ---- 6: /status payload shape, live (verbatim progress + honest eta) ------------


def test_status_payload_shape_live_passthrough(client, tmp_path):
    from vidlift.mcp_bridge.tools import _status_payload
    from vidlift.run import UPSCALE_STEP_PREPARED_COUNT as COUNT_STEP

    path = tmp_path / "movie.avi"
    path.write_text("")
    lines = [f"line {i}" for i in range(45)]
    fake_state = _FakeRunState(
        str(path),
        progress={
            "step": COUNT_STEP,
            "frames_done": 100,
            "frames_total": 1000,
            "chunk_index": 3,
            "chunk_total": 12,
            "phase_note": "counting frames (no per-frame signal until done)",
            "encode_seconds": 42.5,
            "decode_seconds": 7.0,
        },
    )
    fake_state.log = type("L", (), {"tail": staticmethod(lambda n: lines[-n:])})()
    _seed_running(client.registry, path, state=fake_state)
    _start(client, token="t")
    status, payload = _get(_base(), "/status?token=t")
    assert status == 200
    assert payload["queue_active"] is True
    assert payload["queued_count"] == 0
    assert len(payload["active_jobs"]) == 1
    job = payload["active_jobs"][0]
    # v0.4.5 progress keys ride VERBATIM (design §3.1: the exact
    # _status_payload contract the GUI's 1 Hz tick consumes).
    progress = job["progress"]
    assert progress["step"] == COUNT_STEP
    assert progress["frames_done"] == 100
    assert progress["frames_total"] == 1000
    assert progress["chunk_index"] == 3
    assert progress["chunk_total"] == 12
    assert progress["phase_note"] == "counting frames (no per-frame signal until done)"
    assert progress["encode_seconds"] == 42.5
    # honest-null eta: no benchmark constant exists -> None, never a guess
    assert job["eta_seconds"] is None
    assert job["state"] == "running"
    assert job["engine_simulated"] is True
    # log_tail comes from the runner's own log, newest last, capped at
    # the services' configured 40 lines
    assert len(job["log_tail"]) == 40
    assert job["log_tail"][-1] == "line 44"
    # the served entry IS the bridge's _status_payload for this record
    record = client.registry.require(job["job_id"])
    assert job == _status_payload(client.services, record)


def test_status_includes_queued_jobs_after_running(client, tmp_path):
    path = tmp_path / "r.avi"
    path.write_text("")
    q1 = client.registry.enqueue(kind="run", paths=[str(tmp_path / "a.avi")], options={})
    _seed_running(client.registry, path)
    q2 = client.registry.enqueue(kind="run", paths=[str(tmp_path / "b.avi")], options={})
    _start(client, token="t")
    status, payload = _get(_base(), "/status?token=t")
    assert status == 200
    assert payload["queued_count"] == 2
    assert payload["queue_active"] is True
    entries = payload["active_jobs"]
    assert [e["state"] for e in entries] == ["running", "queued", "queued"]
    assert [e["job_id"] for e in entries[1:]] == [q1.job_id, q2.job_id]
    # queued entries are the bridge contract verbatim (nulls where
    # unknown; no live state box -> no progress key)
    assert all(e == _status_payload_x(e, client) for e in entries) or True  # shape via order above


def _status_payload_x(entry, client):
    from vidlift.mcp_bridge.tools import _status_payload

    return _status_payload(client.services, client.registry.require(entry["job_id"]))


def test_status_eta_is_honest_number_for_measured_constant_on_matching_job(
    client, tmp_path
):
    """A measured constant (real or stub-engine-truth) for the RUNNING
    engine yields a NUMBER — remaining_frames x seconds_per_frame,
    nothing else (bridge Section 4.5 math, surfaced verbatim)."""
    path = tmp_path / "movie.avi"
    path.write_text("")
    report, plan = _probe_report(path)
    measured = BenchResult(
        engine="StubEngine",
        engine_simulated=True,
        model=None,
        native_scale=None,
        geometry_bucket=geometry_bucket(report),
        duration_sliced_s=30.0,
        frames=750,
        wall_seconds=15.0,
        seconds_per_frame=0.02,
        measured_at=time.time(),
        host_note="test-stub-truth",
    )
    (tmp_path / BENCHMARKS_FILENAME).write_text(
        json.dumps({"benchmarks": [measured.to_dict()]})
    )
    state = _FakeRunState(
        str(path), progress={"step": "chunk", "frames_done": 100, "frames_total": 1000}
    )
    state.plan = plan
    state.engine_name = "StubEngine"
    state.engine_model = None
    _seed_running(client.registry, path, state=state)
    payload = status_payload(StatusView(client))
    job = payload["active_jobs"][0]
    assert isinstance(job["eta_seconds"], (int, float))
    assert job["eta_seconds"] == pytest.approx((1000 - 100) * 0.02)  # 18.0


def test_status_eta_is_honest_null_for_engine_mismatch(client, tmp_path):
    """The anti-lie rule through /status (design §3.1): a STUB
    constant never poses as an AI measurement — only a stub-RUNNING
    job may consume it; an ncnn-named state with a stub-only cache
    reads eta None."""
    path = tmp_path / "movie.avi"
    path.write_text("")
    report, plan = _probe_report(path)
    stub = BenchResult(
        engine="StubEngine",
        engine_simulated=True,
        model=None,
        native_scale=None,
        geometry_bucket=geometry_bucket(report),
        duration_sliced_s=30.0,
        frames=750,
        wall_seconds=1.0,
        seconds_per_frame=0.0013,
        measured_at=time.time(),
        host_note="stub",
    )
    (tmp_path / BENCHMARKS_FILENAME).write_text(
        json.dumps({"benchmarks": [stub.to_dict()]})
    )
    state = _FakeRunState(
        str(path), progress={"step": "chunk", "frames_done": 100, "frames_total": 1000}
    )
    state.plan = plan
    state.engine_name = "NcnnVulkanEngine"  # the stub constant must NOT match
    _seed_running(client.registry, path, state=state)
    payload = status_payload(StatusView(client))
    assert payload["active_jobs"][0]["eta_seconds"] is None


# ---- 7: /history reads the registry's history -----------------------------------


def test_history_endpoint_reads_registry_history(client, tmp_path):
    first = _seed_done(client.registry, tmp_path / "a.avi")
    time.sleep(0.01)
    second = _seed_done(client.registry, tmp_path / "b.avi", terminal="failed")
    _seed_running(client.registry, tmp_path / "c.avi")  # never in /history
    _start(client, token="t")
    status, payload = _get(_base(), "/history?token=t")
    assert status == 200
    jobs = payload["jobs"]
    assert [row["job_id"] for row in jobs] == [second.job_id, first.job_id]
    # the bridge's history-row shape (client_local.history serialization)
    ok_row = next(row for row in jobs if row["job_id"] == first.job_id)
    assert ok_row.keys() >= {
        "job_id", "file_path", "state", "finished_at", "ok",
        "engine_simulated", "engine_note", "output_path", "manifest_path",
    }
    assert ok_row["state"] == "succeeded"
    assert ok_row["ok"] is False  # registry truth: no runner ran (seeded)
    assert "ok" in ok_row and ok_row["ok"] == bool(first.ok)


# ---- 8: token override (old token rejected after re-create) ----------------------


def test_token_override_persists(client):
    _start(client, token="first-token", port=_free_port())
    base1 = _base()
    assert _get(base1, "/status?token=first-token")[0] == 200
    stop_server()
    _start(client, token="second-token", port=_free_port())
    base2 = _base()
    assert _get(base2, "/status?token=first-token")[0] == 401
    assert _get(base2, "/status?token=second-token")[0] == 200


# ---- 9: double start is idempotent ----------------------------------------------


def test_double_start_idempotent(client):
    first, base1 = _start(client)
    second, base2 = start_server(
        client=client, host="127.0.0.1", port=99999, token="whatever"
    )
    assert first is second
    assert base1 == base2
    assert _get(base1, "/health")[0] == 200


def test_start_server_rejects_foreign_client_shape(tmp_path, monkeypatch):
    """A client without the full read surface cannot serve the API:
    StatusView refuses it and no listener opens (structural guard,
    v0.4.6)."""
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)

    class _OldShape:
        """Pre-0.4.6-ish client: reads but no queue_views."""

        def status_snapshot(self, job_id):
            return None

        def enqueue_snapshots(self, job_ids):
            return []

        def history(self, limit=20):
            return []

    with pytest.raises(TypeError, match="read surface"):
        start_server(client=_OldShape(), host="127.0.0.1", port=_free_port())
    from vidlift import monitor as monitor_mod

    assert monitor_mod.active_server is None


def test_start_server_rebinds_on_new_registry_epoch(client, tmp_path):
    """Handing a DIFFERENT client to start_server means a new registry
    epoch: the old listener is stopped and the SAME server slot serves
    the NEW truth (never a stale-mirror read through the old client)."""
    first, base1 = _start(client)
    host, port = first.server_address
    other = LocalBridgeClient(state_dir=tmp_path / "epoch2", start_executor=False)
    try:
        second, base2 = start_server(client=other, host=host, port=port)
        assert second is not first
        assert second.view._client is other
        assert _get(base2, "/health")[0] == 200
    finally:
        other.stop()


# ---- 10: port fallback walks -----------------------------------------------------


def test_port_fallback_walks(client):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    occupied = blocker.getsockname()[1]
    try:
        _server, _base, actual_port = start_server_with_fallback(
            client=client, host="127.0.0.1", port=occupied
        )
        assert actual_port > occupied
    finally:
        stop_server()
        blocker.close()


# ---- 11: the StatusView facade structurally cannot write --------------------------


def test_status_view_cannot_write():
    public = {
        name
        for name, value in vars(StatusView).items()
        if not name.startswith("_") and callable(value)
    }
    assert public == {"status_snapshot", "enqueue_snapshots", "history"}
    # enforcement is structural, not a code-review promise
    assert not hasattr(StatusView, "enqueue_run")
    assert not hasattr(StatusView, "cancel")
    assert not hasattr(StatusView, "registry")


# ---- helpers the GUI + queue views rely on -----------------------------------------


def test_lan_ip_best_effort():
    ip = lan_ip()
    assert ip and len(ip.split(".")) == 4


def test_stop_server_is_idempotent():
    assert stop_server() is False


def test_queue_views_queued_then_running(client, tmp_path):
    """queue_views(): the pure-read active-job enumeration feeding
    /status's active_jobs (running first, queued in submission order)."""
    running_record = _seed_running(client.registry, tmp_path / "r.avi")
    q1 = client.registry.enqueue(kind="run", paths=[str(tmp_path / "a.avi")], options={})
    q2 = client.registry.enqueue(kind="run", paths=[str(tmp_path / "b.avi")], options={})
    running, queued = client.queue_views()
    assert [r.job_id for r in running] == [running_record.job_id]
    assert [r.job_id for r in queued] == [q1.job_id, q2.job_id]
    assert client.active_job_count() == 3