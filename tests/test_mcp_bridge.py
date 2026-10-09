"""MCP bridge acceptance tests (t-series, mcp-bridge.md Section 8).

All headless against the stub engine with fake runner seams; none
requires a GPU or real video files beyond the synthetic fixtures.
Every test drives the REAL server dispatch (``handle_line`` runs the
same JSON-RPC decode -> schema validation -> tool adapter -> payload
serialization the stdio loop runs; t9 additionally exercises real
stdio pipe pairs).

The v0.2.32 rule (clause f): the boundary suite fails if the MCP
server module is absent — no importorskip anywhere in this file, and
the t9 canary requires every t-marker to have registered.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from vidlift.mcp_bridge import (
    RESTART_ERROR,
    BridgeServices,
    InvalidInput,
    JobExecutor,
    McpServer,
    Registry,
    validate_tool_args,
)
from vidlift.mcp_bridge.jobs import JobOutcome
from vidlift.mcp_bridge.schemas import TOOL_SCHEMAS
from vidlift.run import (
    EngineCancelled,
    StubEngine,
    ToolBox,
    UpscaleError,
)

# ---- t9 execution-canary state ----------------------------------------------

_EXPECTED_T_TESTS = {f"t{i}" for i in range(1, 10)}
_EXECUTED_T_TESTS: set[str] = set()


def marker(name: str) -> None:
    _EXECUTED_T_TESTS.add(name)


# ---- shared fakes -------------------------------------------------------------


def make_media_file(tmp_path, name: str = "movie.avi") -> str:
    path = tmp_path / name
    path.write_text("")
    return str(path)


def probe_payload(*, duration: str = "5400.0") -> str:
    return json.dumps(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "mpeg4",
                    "width": 640,
                    "height": 360,
                    "sample_aspect_ratio": "1:1",
                    "field_order": "progressive",
                    "r_frame_rate": "25/1",
                    "avg_frame_rate": "25/1",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "mp3",
                    "channels": 2,
                },
            ],
            "format": {"format_name": "avi", "duration": duration},
        }
    )


class BridgeRunner:
    """The command/probe seam for bridge tests: canned probe JSON,
    prepare writes the slice/prepared file, encode writes the output,
    verify reports the planned geometry, decode passes.

    ``block_on_command`` lets a test hold the worker inside one command
    (drives cancel-mid-run through the real executor).
    """

    def __init__(
        self,
        payload: str,
        *,
        prepare_exit: int = 0,
        block_on_command: str | None = None,
    ) -> None:
        self.payload = payload
        self.prepare_exit = prepare_exit
        self.block_on_command = block_on_command
        self.commands: list[tuple[str, tuple[str, ...]]] = []
        self._gate = threading.Event()
        self.blocked_count = 0

    def release(self) -> None:
        self._gate.set()

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        self.commands.append((binary, tuple(args)))
        if (
            self.block_on_command is not None
            and any(
                self.block_on_command in str(part) for part in args
            )
        ):
            self.blocked_count += 1
            self._gate.wait(timeout=10)  # hold the worker mid-run
        if "-show_format" in args:
            return 0, self.payload, ""
        if "idet" in args:
            return 0, "", (
                "[Parsed_idet] Single frame detection: TFF: 0 BFF: 0 "
                "Progressive: 0 Undetermined: 0\n"
            )
        if "-show_streams" in args and "-show_format" not in args:
            return 0, json.dumps(
                {"streams": [{"codec_type": "video", "width": 1280, "height": 720}]}
            ), ""
        if args and str(args[-1]).endswith(".prepared.mkv"):
            if self.prepare_exit == 0:
                Path(args[-1]).write_bytes(b"prepared-stub")
            return self.prepare_exit, "", "prepare boom" if self.prepare_exit else ""
        if args and str(args[-1]).endswith(".slice.prepared.mkv"):
            Path(args[-1]).write_bytes(b"slice-stub")
            return 0, "", ""
        if args and str(args[-1]).endswith(".vidlift.720p.hevc.mp4"):
            Path(args[-1]).write_bytes(b"output-stub")
            return 0, "", ""
        if args and args[-1] == "-" and "null" in args:
            return 0, "", ""
        return 1, "", f"unexpected: {binary} {' '.join(args)}"


def make_server(
    tmp_path,
    *,
    payload: str | None = None,
    probe_runner=None,
    command_runner=None,
    engine=None,
    benchmark_engine=None,
    start_workers: bool = True,
    registry: Registry | None = None,
):
    """A real McpServer over a temp state dir, with fake seams."""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    runner = BridgeRunner(payload or probe_payload())
    services = BridgeServices(
        registry=None,  # wired by McpServer
        state_dir=state,
        probe_runner=probe_runner if probe_runner is not None else runner,
        command_runner=command_runner if command_runner is not None else runner,
        engine=engine,
        benchmark_engine=benchmark_engine,
    )
    server = McpServer(
        state_dir=state,
        services=services,
        registry=registry,
        start_workers=start_workers,
    )
    return server, runner


def call(server: McpServer, name: str, arguments: dict, *, request_id: int = 1):
    """One tools/call through the REAL dispatch; returns (result, error,
    decoded_text_payload_or_none)."""
    response = server.handle_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
    )
    assert response is not None
    message = json.loads(response)
    assert message["id"] == request_id
    result = message.get("result")
    error = message.get("error")
    payload = None
    if result is not None and "content" in result:
        text = result["content"][0]["text"]
        payload = json.loads(text)
    return result, error, payload


def wait_for_state(server: McpServer, job_id: str, states: set[str], timeout: float = 10.0):
    """Poll status until the job reaches one of ``states`` (the async
    lifecycle is real; tests never sleep blindly)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        _result, _error, payload = call(server, "status", {"job_id": job_id})
        last = payload
        if payload["state"] in states:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {states}; last: {last}")


# ---- t1: tool surface completeness ---------------------------------------------


def test_t1_tool_surface_matches_the_binding_shapes():
    marker("t1")
    # the eight tools, exactly, and nothing else
    assert set(TOOL_SCHEMAS) == {
        "probe", "plan", "benchmark", "run",
        "status", "cancel", "list", "manifest",
    }
    # field names, types, required-ness per Section 4
    assert TOOL_SCHEMAS["probe"]["required"] == ["paths"]
    assert TOOL_SCHEMAS["plan"]["properties"]["options"]["properties"].keys() >= {
        "target", "gb_per_hour", "output_dir"
    }
    assert TOOL_SCHEMAS["benchmark"]["required"] == ["sample_path"]
    assert TOOL_SCHEMAS["run"]["required"] == ["paths"]
    assert TOOL_SCHEMAS["status"]["required"] == ["job_id"]
    assert TOOL_SCHEMAS["cancel"]["required"] == ["job_id"]
    assert set(TOOL_SCHEMAS["list"]["properties"]) == {"limit", "include_history"}
    assert TOOL_SCHEMAS["manifest"]["required"] == ["job_id"]
def test_t1_dispatch_executes_over_pipe_pair(tmp_path):
    marker("t1")
    server, _runner = make_server(tmp_path)
    _result, error, _payload = call(server, "list", {})
    assert error is None  # the real dispatch answered


# ---- t2: 1:1 mapping proof --------------------------------------------------------


def test_t2_probe_and_plan_equal_pipeline_output_field_by_field(tmp_path):
    marker("t2")
    from vidlift.probe import probe_and_classify
    from vidlift.plan import build_file_plan
    from vidlift.benchmark import geometry_bucket

    server, runner = make_server(tmp_path)
    path = make_media_file(tmp_path)
    _result, error, payload = call(server, "probe", {"paths": [path]})
    assert error is None
    # the bridge's report equals what vidlift.probe produces, field by field
    report = probe_and_classify(
        path, binary="ffprobe", runner=runner
    )
    bridge_report = payload["reports"][0]["report"]
    assert bridge_report["source_class"] == report.source_class
    assert bridge_report["displayed_width"] == report.displayed_width
    assert bridge_report["displayed_height"] == report.displayed_height
    assert bridge_report["upscale_verdict"] == report.upscale_verdict
    assert bridge_report["duration_seconds"] == report.duration_seconds
    assert bridge_report["video"]["codec_name"] == report.video.codec_name
    assert bridge_report["video"]["width"] == report.video.width
    assert bridge_report["audio"][0]["codec_name"] == report.audio[0].codec_name

    _result, error, plan_payload = call(
        server, "plan", {"paths": [path]}
    )
    assert error is None
    plan = build_file_plan(report)
    entry = plan_payload["files"][0]
    assert entry["ok"] is True
    bridge_plan = entry["plan"]
    assert bridge_plan["verdict"] == plan.process_verdict
    assert bridge_plan["output_path"] == plan.output_path
    assert bridge_plan["output_geometry"] == [plan.output_width, plan.output_height]
    assert bridge_plan["video_bitrate_kbps"] == plan.video_bitrate_kbps
    assert bridge_plan["audio"]["bitrate_kbps"] == plan.audio.bitrate_kbps
    assert bridge_plan["estimated_output_bytes"] == plan.estimated_output_bytes
    # the summary's honesty list is present and non-empty (clause c)
    assert plan_payload["summary"]["cannot_know"], "cannot_know must be non-empty"


# ---- t3: async run lifecycle ------------------------------------------------------


def test_t3_run_lifecycle_reaches_succeeded_with_live_status(tmp_path):
    marker("t3")
    server, runner = make_server(tmp_path)
    path = make_media_file(tmp_path)
    _result, error, payload = call(server, "run", {"paths": [path]})
    assert error is None
    jobs = payload["jobs"]
    assert len(jobs) == 1 and jobs[0]["ok"] is True
    job_id = jobs[0]["job_id"]

    # status during/after the run shows the observability contract
    final = wait_for_state(server, job_id, {"succeeded", "failed"})
    assert final["state"] == "succeeded"
    assert final["current_phase"] == "done"
    # last_activity_at is present and sane
    assert final["last_activity_at"] >= final["started_at"]
    # the manifest after completion contains engine_simulated: true
    # (the stub engine) and equals the on-disk file byte-for-byte
    _r, _e, manifest_payload = call(server, "manifest", {"job_id": job_id})
    assert manifest_payload["manifest"] is not None
    run = manifest_payload["manifest"]["run"]
    assert run["engine_simulated"] is True
    on_disk = Path(manifest_payload["manifest_path"]).read_text()
    assert json.loads(on_disk) == manifest_payload["manifest"]
    assert run["engine"] == "StubEngine"


def test_t3_run_returns_immediately_with_job_id(tmp_path):
    marker("t3")
    server, runner = make_server(tmp_path)
    path = make_media_file(tmp_path)
    slow_runner = BridgeRunner(probe_payload(), block_on_command=".prepared.mkv")
    server.services.command_runner = slow_runner
    server.services.probe_runner = slow_runner
    started = time.time()
    _result, _error, payload = call(server, "run", {"paths": [path]})
    elapsed = time.time() - started
    # the anti-babysitter contract: enqueue is instant, never the run
    assert elapsed < 1.0
    assert payload["jobs"][0]["job_id"]
    slow_runner.release()
    job_id = payload["jobs"][0]["job_id"]
    wait_for_state(server, job_id, {"succeeded", "failed", "cancelled"})


# ---- t4: both cancel paths ----------------------------------------------------------


def test_t4_cancel_queued_job_never_runs(tmp_path):
    marker("t4")
    server, runner = make_server(tmp_path, start_workers=False)
    first = make_media_file(tmp_path, "hold.avi")
    second = make_media_file(tmp_path, "target.avi")
    slow_runner = BridgeRunner(probe_payload(), block_on_command=".prepared.mkv")
    server.services.command_runner = slow_runner
    server.services.probe_runner = slow_runner
    server.executor.start()
    _r, _e, p1 = call(server, "run", {"paths": [first]})
    _r, _e, p2 = call(server, "run", {"paths": [second]})
    blocked_id = p1["jobs"][0]["job_id"]
    queued_id = p2["jobs"][0]["job_id"]
    # worker is blocked inside job 1; job 2 is queued behind it
    _deadline = time.time() + 5
    while slow_runner.blocked_count == 0 and time.time() < _deadline:
        time.sleep(0.02)
    _r, _e, cancel_payload = call(server, "cancel", {"job_id": queued_id})
    assert cancel_payload["state"] == "cancelled"
    slow_runner.release()
    wait_for_state(server, blocked_id, {"succeeded", "failed"})
    # the cancelled job NEVER ran: no command ever touched it
    _r, _e, status = call(server, "status", {"job_id": queued_id})
    assert status["state"] == "cancelled"
    assert status["commands"] == []
    target_commands = [
        c for _b, a in slow_runner.commands
        if any(second in str(part) for part in a)
    ]
    assert target_commands == []


def test_t4_cancel_running_job_is_cooperative_cancelled(tmp_path):
    marker("t4")
    server, runner = make_server(tmp_path)
    # the stub engine's pre-frame cancel check raises EngineCancelled;
    # block inside prepare so cancel lands mid-run
    blocking = BridgeRunner(probe_payload(), block_on_command=".prepared.mkv")
    server.services.command_runner = blocking
    server.services.probe_runner = blocking
    path = make_media_file(tmp_path)
    _r, _e, payload = call(server, "run", {"paths": [path]})
    job_id = payload["jobs"][0]["job_id"]
    deadline = time.time() + 5
    while blocking.blocked_count == 0 and time.time() < deadline:
        time.sleep(0.02)
    _r, _e, cancel_payload = call(server, "cancel", {"job_id": job_id})
    assert cancel_payload["state"] == "cancelling"
    blocking.release()
    final = wait_for_state(server, job_id, {"cancelled", "failed", "succeeded"})
    assert final["state"] == "cancelled", (
        f"cooperative cancel must close cancelled, got {final['state']} "
        f"({final['error']})"
    )
    # a cancel is not a failure: no error posing as one
    assert final["error"] is None or "cancel" in final["error"]


# ---- t4 (D1 regression): the claim-window cancel race, both halves ---------


def test_t4_claim_window_cancel_is_cooperative_never_swept(tmp_path):
    marker("t4")
    # The D1 shape, deterministically at the registry + executor seam.
    # The claim transition itself registers the CancelFlag, so a
    # cancel arriving in the old claim window now flips the LIVE flag
    # ("cancelling") instead of sweeping the job closed behind the
    # running pipeline's back ("cancelled"). The sweep path from the
    # running state is unreachable; the executor closes the job.
    state = tmp_path / "state"
    state.mkdir(parents=True)
    registry = Registry(state)
    record = registry.enqueue(
        kind="run", paths=["/x/movie.avi"], options={}
    )
    registry.transition(record, "running")  # what _claim_next does
    # OLD DEFECT: this returned "cancelled" with no flag ever set
    assert registry.cancel(record.job_id) == "cancelling"
    flag = registry.get_cancel_flag(record.job_id)
    assert flag is not None and flag.is_set()
    # the executor observes the flag and closes the job itself; the
    # terminal close pops the flag (dead flag = closed job)
    registry.transition(record, "cancelled")  # legal: running -> cancelled
    assert registry.get_cancel_flag(record.job_id) is None
    # honesty: registry and record agree, no contradiction is possible
    assert record.state == "cancelled"


def test_t4_raced_terminal_close_never_kills_the_worker(tmp_path):
    marker("t4")
    # Layers b + c proof: when a job's terminal state races the
    # executor (closed by one path while the executor finishes ok),
    # the worker LOGS the contradiction instead of raising RegistryError
    # out of _run_job — and the next job still completes.
    state = tmp_path / "state"
    state.mkdir(parents=True)
    registry = Registry(state)
    events: list[str] = []
    served: list[str] = []

    def racing_executor(record, services, cancel):
        served.append(record.job_id)
        if len(served) == 1:
            # first job: simulate the race — the record is closed by
            # another path while this executor is mid-run
            registry.transition(record, "cancelled")  # running -> cancelled
        return JobOutcome(ok=True)  # both executors finish ok

    services = BridgeServices(
        registry=registry, state_dir=state, log_event=events.append
    )
    executor = JobExecutor(services, executor_fn=racing_executor)
    r1 = registry.enqueue(kind="run", paths=["/x/a.avi"], options={})
    r2 = registry.enqueue(kind="run", paths=["/x/b.avi"], options={})
    executor.start()

    deadline = time.time() + 10
    while time.time() < deadline and (
        r1.state not in ("succeeded", "failed", "cancelled")
        or r2.state not in ("succeeded", "failed", "cancelled")
    ):
        time.sleep(0.02)
    executor.stop()

    # the race close won for r1 (registry is the source of truth) and
    # the success transition was LOGGED, never raised
    assert r1.state == "cancelled", f"r1={r1.state}"
    assert any("terminal state raced" in e for e in events), events
    # THE point of D1: the worker survived and served the next job
    assert r2.state == "succeeded", f"r2={r2.state}"



# ---- t4 (N1 regression): unlocked queued() iteration under concurrent enqueue


def test_t4_concurrent_enqueue_during_claim_iteration_never_raises(tmp_path):
    marker("t4")
    # N1: queued() iterated self._jobs.values() unlocked while enqueue
    # inserted under the lock from the message-loop thread; a landing
    # enqueue raised RuntimeError and killed the single worker. The
    # readers now snapshot under the lock, so this hammer must be clean.
    state = tmp_path / "state"
    state.mkdir(parents=True)
    registry = Registry(state)
    for i in range(200):
        registry.enqueue(kind="run", paths=[f"/x/{i}.avi"], options={})

    errors: list[str] = []
    done = threading.Event()

    def enqueuer():
        for n in range(400):
            registry.enqueue(kind="run", paths=[f"/y/{n}.avi"], options={})
        done.set()

    thread = threading.Thread(target=enqueuer)
    thread.start()
    # on the unfixed code this reproduced within ~0.02s of hammering
    while not done.is_set():
        try:
            registry.queued()
        except RuntimeError as exc:
            errors.append(str(exc))
            break
    thread.join(timeout=5)
    assert not errors, f"unlocked iteration raised: {errors}"
    # the queue survived intact: 200 seeded + 400 enqueued, all present
    assert len(registry.queued()) == 600



# ---- t5: registry persistence + restart sweep ----------------------------------------


def test_t5_registry_persists_across_restart_queued_job_survives(tmp_path):
    marker("t5")
    state = tmp_path / "state"
    server, _runner = make_server(tmp_path, start_workers=False)
    path = make_media_file(tmp_path)
    _r, _e, payload = call(server, "run", {"paths": [path]})
    job_id = payload["jobs"][0]["job_id"]
    # stop the server WITHOUT running the job (workers never started)
    # and restart on the same state dir: the queued job is still listed
    server2, _r2 = make_server(
        tmp_path, registry=Registry(state), start_workers=False
    )
    _r, _e, status = call(server2, "status", {"job_id": job_id})
    assert status["state"] == "queued"
    _r, _e, listing = call(server2, "list", {})
    assert any(q["job_id"] == job_id for q in listing["queued"])


def test_t5_running_job_swept_failed_on_restart(tmp_path):
    marker("t5")
    state = tmp_path / "state"
    # simulate the previous server dying mid-run: a registry fixture
    # with a job stuck in running state
    registry = Registry(state)
    record = registry.enqueue(
        kind="run", paths=["/media/gone.avi"], options={}
    )
    registry.transition(record, "running")
    server, _runner = make_server(tmp_path, registry=registry, start_workers=False)
    # the sweep ran at construction: the corpse is failed, never
    # silently succeeded, with the binding restart error
    _r, _e, status = call(server, "status", {"job_id": record.job_id})
    assert status["state"] == "failed"
    assert RESTART_ERROR in status["error"]
    _r, _e, listing = call(server, "list", {})
    history = listing["history"]
    assert history and history[0]["state"] == "failed"


def test_t5_registry_file_is_json_and_atomic(tmp_path):
    marker("t5")
    server, _runner = make_server(tmp_path, start_workers=False)
    path = make_media_file(tmp_path)
    _r, _e, payload = call(server, "run", {"paths": [path]})
    jobs_file = server.state_dir / "jobs.json"
    data = json.loads(jobs_file.read_text())
    assert isinstance(data["jobs"], list) and data["jobs"]
    # no temp leftovers from the atomic write
    leftovers = [p for p in server.state_dir.iterdir() if p.name.startswith(".jobs-")]
    assert leftovers == []


# ---- t6: honesty flow-through -----------------------------------------------------------


def test_t6_sentinel_note_and_flag_flow_byte_identical(tmp_path):
    marker("t6")
    server, runner = make_server(tmp_path)
    path = make_media_file(tmp_path)

    class SentinelEngine(StubEngine):
        """The fake engine whose note is the sentinel: the bridge must
        carry it through status and manifest unchanged (clause d)."""

        def __init__(self):
            super().__init__(note="sentinel note")
            self.simulated = True

    server.services.engine = SentinelEngine()
    _r, _e, payload = call(server, "run", {"paths": [path]})
    job_id = payload["jobs"][0]["job_id"]
    final = wait_for_state(server, job_id, {"succeeded", "failed"})
    assert final["state"] == "succeeded"
    assert final["engine_note"] == "sentinel note"
    assert final["engine_simulated"] is True
    # byte-identical in the manifest payload (clause d at the boundary)
    _r, _e, manifest_payload = call(server, "manifest", {"job_id": job_id})
    manifest = manifest_payload["manifest"]
    assert manifest["run"]["engine_note"] == "sentinel note"
    assert manifest["run"]["engine_simulated"] is True
    assert manifest["run"]["engine_note"] == final["engine_note"]
    # eta_seconds is null with no benchmark cached (clause c)
    assert final["eta_seconds"] is None


def test_t6_eta_null_without_measured_constant_even_with_progress(tmp_path):
    marker("t6")
    server, runner = make_server(tmp_path)
    path = make_media_file(tmp_path)

    class ProgressEngine(StubEngine):
        def run(self, prepared_path, plan, tools, *, cancel=None, on_progress=None):
            if on_progress is not None:
                on_progress(100, 7500)
            return super().run(
                prepared_path, plan, tools, cancel=cancel, on_progress=on_progress
            )

    server.services.engine = ProgressEngine()
    _r, _e, payload = call(server, "run", {"paths": [path]})
    job_id = payload["jobs"][0]["job_id"]
    final = wait_for_state(server, job_id, {"succeeded", "failed"})
    assert final["eta_seconds"] is None  # no cache -> no ETA, ever


# ---- t7: benchmark honesty ----------------------------------------------------------------


def test_t7_stub_benchmark_labeled_simulated_with_notes(tmp_path):
    marker("t7")
    server, runner = make_server(tmp_path)
    path = make_media_file(tmp_path)
    server.services.benchmark_engine = StubEngine()
    _r, _e, payload = call(server, "benchmark", {"sample_path": path})
    job_id = payload["job_id"]
    assert payload["kind"] == "benchmark"
    final = wait_for_state(server, job_id, {"succeeded", "failed"})
    assert final["state"] == "succeeded", final["error"]
    result = final["result"]
    assert result["engine_simulated"] is True
    # the note says exactly that the numbers are stub numbers
    assert any(
        "not an AI-pass measurement" in note for note in result["notes"]
    )
    # the Section-4.3 completion shape, field names verbatim
    for field in (
        "sample_path", "duration_seconds", "engine", "engine_simulated",
        "frames_processed", "wall_seconds", "frames_per_second",
        "per_plan_constant", "notes",
    ):
        assert field in result, f"missing Section-4.3 field {field}"
    # a plan-adjacent ETA from the stub cache is labeled simulated
    _r, _e, plan_payload = call(server, "plan", {"paths": [path]})
    entry = plan_payload["files"][0]
    assert "eta_estimate" in entry["plan"]
    assert entry["plan"]["eta_estimate"]["engine_simulated"] is True
    cannot_know = plan_payload["summary"]["cannot_know"]
    assert any("simulated" in text for text in cannot_know)


# ---- t8: error codes -----------------------------------------------------------------------


def test_t8_invalid_input_rejected_with_code(tmp_path):
    marker("t8")
    server, _runner = make_server(tmp_path)
    result, _error, payload = call(server, "status", {})
    assert result["isError"] is True
    assert payload["code"] == "invalid_input"
    assert "job_id" in payload["message"]

    result, _error, payload = call(server, "run", {"paths": [], "bogus": 1})
    assert result["isError"] is True
    assert payload["code"] == "invalid_input"
    assert "bogus" in payload["message"]


def test_t8_probe_failed_when_every_file_fails(tmp_path):
    marker("t8")
    server, _runner = make_server(tmp_path)
    result, _error, payload = call(
        server, "probe", {"paths": ["/no/such/file.avi"]}
    )
    assert result["isError"] is True
    assert payload["code"] == "probe_failed"
    # the per-file entries still ride along
    assert payload["payload"]["reports"][0]["ok"] is False


def test_t8_plan_failed_on_unsupported_target(tmp_path):
    marker("t8")
    server, _runner = make_server(tmp_path)
    path = make_media_file(tmp_path)
    result, _error, payload = call(
        server,
        "plan",
        {"paths": [path], "options": {"target": "1080p"}},
    )
    assert result["isError"] is True
    assert payload["code"] == "plan_failed"


def test_t8_missing_tool_named(tmp_path):
    marker("t8")
    server, _runner = make_server(tmp_path)
    # break tool discovery for this call: ffprobe is gone
    import vidlift.mcp_bridge.tools as tools_module
    from vidlift.run import MissingToolError

    def boom(**kwargs):
        raise MissingToolError("ffprobe", "ffprobe not found at 'x'")

    original = tools_module.BridgeServices.resolve_tools
    tools_module.BridgeServices.resolve_tools = lambda self: boom()
    try:
        result, _error, payload = call(server, "probe", {"paths": ["/x.avi"]})
    finally:
        tools_module.BridgeServices.resolve_tools = original
    assert result["isError"] is True
    assert payload["code"] == "missing_tool"
    assert "ffprobe" in payload["message"]


def test_t8_not_found_for_unknown_job(tmp_path):
    marker("t8")
    server, _runner = make_server(tmp_path)
    # status and manifest are errors with the not_found code; cancel
    # on an unknown job is a REPORTED NON-EVENT (Section 4.6): its
    # payload carries state "not-found", not isError
    for tool in ("status", "manifest"):
        result, _error, payload = call(
            server, tool, {"job_id": "vl-19990101-deadbeef"}
        )
        assert result["isError"] is True
        assert payload["code"] == "not_found"
    result, _error, payload = call(
        server, "cancel", {"job_id": "vl-19990101-deadbeef"}
    )
    assert result is not None and "isError" not in result
    assert payload == {
        "job_id": "vl-19990101-deadbeef", "state": "not-found"
    }


def test_t8_state_conflict_only_on_double_cancel(tmp_path):
    marker("t8")
    server, runner = make_server(tmp_path)
    blocking = BridgeRunner(probe_payload(), block_on_command=".prepared.mkv")
    server.services.command_runner = blocking
    server.services.probe_runner = blocking
    path = make_media_file(tmp_path)
    _r, _e, payload = call(server, "run", {"paths": [path]})
    job_id = payload["jobs"][0]["job_id"]
    deadline = time.time() + 5
    while blocking.blocked_count == 0 and time.time() < deadline:
        time.sleep(0.02)
    _r, _e, first = call(server, "cancel", {"job_id": job_id})
    assert first["state"] == "cancelling"
    result, _error, conflict = call(server, "cancel", {"job_id": job_id})
    assert result["isError"] is True
    assert conflict["code"] == "state_conflict"
    # a cancel on a FINISHED job is a non-event, not a conflict
    blocking.release()
    wait_for_state(server, job_id, {"cancelled", "succeeded", "failed"})
    _r, _e, after = call(server, "cancel", {"job_id": job_id})
    assert after["state"] == "already-finished"


# ---- t9: boundary-rule execution proof -------------------------------------------------------


def test_t9_server_serves_over_real_stdio_pipes(tmp_path):
    marker("t9")
    # drive the REAL serve loop over a real os.pipe pair: no mock of the
    # server is ever exercised by this suite
    server, _runner = make_server(tmp_path, start_workers=False)
    read_fd, write_fd = os.pipe()
    frames_in = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode() + b"\n",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}).encode() + b"\n",
    ]
    os.write(write_fd, b"".join(frames_in))
    os.close(write_fd)
    received: list[bytes] = []
    import io

    class ReadStream:
        def __init__(self, fd):
            self.buffer = io.BufferedReader(io.FileIO(fd, "r"))

        def readline(self) -> bytes:
            return self.buffer.readline()

    class WriteStream:
        def __init__(self):
            self.buffer = io.BytesIO()

        def write(self, frame: bytes) -> None:
            self.buffer.write(frame)

    writer = WriteStream()
    server.serve(read=ReadStream(read_fd).readline, write=writer.write)
    text = writer.buffer.getvalue().decode()
    lines = [json.loads(line) for line in text.splitlines() if line]
    assert lines[0]["id"] == 1
    assert [t["name"] for t in lines[0]["result"]["tools"]] == [
        "probe", "plan", "benchmark", "run",
        "status", "cancel", "list", "manifest",
    ]
    assert lines[1]["id"] == 2 and lines[1]["result"] == {}


def test_t9_mcp_server_module_imports_and_entry_answers(tmp_path):
    marker("t9")
    # the server module is present and the entry point answers a real
    # initialize handshake through a subprocess (packaged-shape drive)
    env = dict(os.environ)
    env["VIDLIFT_STATE_DIR"] = str(tmp_path / "state")
    proc = subprocess.run(
        [sys.executable, "-m", "vidlift", "mcp-server"],
        input=(
            json.dumps(
                {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "initialize",
                    "params": {},
                }
            )
            + "\n"
        ).encode(),
        capture_output=True,
        timeout=30,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
    )
    assert proc.returncode == 0
    lines = [
        json.loads(line)
        for line in proc.stdout.decode().splitlines()
        if line.strip()
    ]
    assert lines and lines[0]["id"] == 1
    assert lines[0]["result"]["serverInfo"]["name"] == "vidlift"
    # stdout carried ONLY protocol frames; diagnostics went to stderr
    assert proc.stderr, "bridge diagnostics must go to stderr"


def test_t9_execution_canary_all_t_tests_ran():
    marker("t9")
    # every t1-t9 boundary test provably executed this session; the
    # module imports (collection would have failed otherwise)
    from vidlift import mcp_bridge  # noqa: F401

    missing = _EXPECTED_T_TESTS - _EXECUTED_T_TESTS
    assert not missing, f"t-tests did not execute: {sorted(missing)}"