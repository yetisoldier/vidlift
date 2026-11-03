"""v0.4.16 regression: cancel reflected end-to-end (the §4.6 observe-fix).

Eric's repro (2026-11-03): "cancel current" / "cancel batch" left the job
rendered `running` forever — `/status` corroborated `state: "running"`
long after the click, and `/history` held a mix of `cancelled` AND
`failed` rows. Root cause: ``run_file``'s cancel flag was observed only
inside the engine adapter (upscale) and between transcription segments;
prepare/encode/verify/mux ran the flag-flipped child to completion, the
run CONTINUED into the next phases, and the job closed `succeeded` (or
`failed`, when a terminated child died with a nonzero exit that
``VideoToolError`` masked). The registry transition to `cancelled` never
fired, so the GUI tick and the monitor faithfully rendered `running`.

This file pins the fixed journey with deterministic fakes (no real
subprocess except the two tiny interpreter children that prove the
cancel-aware runner discipline):

- run-level: a flag flipped during ANY phase/command closes the run
  cancelled — never succeeded, never failed (the terminated-child mask).
- subprocess-level: the production runner terminates a live child on the
  flag (terminate -> wait -> kill escalation) and raises
  :class:`EngineCancelled`; the no-flag call keeps the (rc, out, err)
  contract byte-identical.
- bridge-level: one click stops the running job's underlying work and
  the SAME ``_status_payload`` the GUI's 1 Hz tick and the monitor
  serve reads `cancelled`; Cancel Batch closes every queued/running id;
  a cancelled BENCHMARK job closes `cancelled` (not `failed`).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from vidlift.mcp_bridge import (
    BridgeServices,
    JobExecutor,
    McpServer,
    Registry,
)
from vidlift.mcp_bridge.jobs import CancelFlag
from vidlift.run import (
    EngineAdapter,
    EngineCancelled,
    EngineResult,
    PHASE_PREPARE,
    ToolBox,
    _subprocess_runner,
    run_file,
)

# ---- deterministic fakes -----------------------------------------------------


def make_media_file(tmp_path, name: str = "movie.avi") -> str:
    path = tmp_path / name
    path.write_text("")
    return str(path)


def probe_payload() -> str:
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
                {"index": 1, "codec_type": "audio", "codec_name": "mp3", "channels": 2},
            ],
            "format": {"format_name": "avi", "duration": "5400.0"},
        }
    )


class FlipRunner:
    """The command seam that flips the cancel flag at a chosen command
    marker, then finishes that command like the real tool would (an
    encode completing despite the flag is exactly the window the old
    code ignored). Everything beyond flips still runs — the runner
    itself never observes the flag, so ONLY run_file's boundary
    observations can convert this run to cancelled."""

    def __init__(self, payload: str, *, flip_marker: str) -> None:
        self.payload = payload
        self.flip_marker = flip_marker
        self.flag = CancelFlag()
        self.commands: list[str] = []
        self.encode_exit = 0
        self.encode_writes_output = True

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        name = " ".join(str(part) for part in args)
        self.commands.append(name)
        if self.flip_marker in name:
            self.flag.set()
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
        if args and args[-1] == "-" and "null" in args:
            return 0, "", ""
        if args and str(args[-1]).endswith(".prepared.mkv"):
            Path(args[-1]).write_bytes(b"prepared-stub")
            return 0, "", ""
        if args and str(args[-1]).endswith(".720p.mp4"):
            if self.encode_writes_output:
                Path(args[-1]).write_bytes(b"output-stub")
            return self.encode_exit, "", ""
        return 1, "", f"unexpected: {binary} {name}"


class IgnorantEngine(EngineAdapter):
    """A foreign engine that ignores the cancel flag entirely (its
    run() completes) — the shape only run_file's post-engine boundary
    can catch."""

    def __init__(self, *, flip_before_complete=False, flag=None) -> None:
        self.runs = 0
        self._flip = flip_before_complete
        self._flag = flag

    def run(self, prepared_path, plan, tools, *, cancel=None, on_progress=None, on_step=None):
        self.runs += 1
        if self._flip and self._flag is not None:
            self._flag.set()  # the flip lands mid-engine; run() completes anyway
        mezzanine = Path(prepared_path).with_suffix(".upscaled.mkv")
        mezzanine.write_bytes(b"mezzanine-stub")
        return EngineResult(mezzanine_path=str(mezzanine), frames_done=2, simulated=True)


def run_kwargs(tmp_path, runner, *, engine=None):
    return dict(
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        engine=engine,
        log_dir=str(tmp_path / "runs"),
    )


# ---- run-level: the cancel lands at every boundary ---------------------------


class TestRunLevelBoundaries:
    def test_cancel_flip_during_encode_closes_cancelled_not_succeeded(
        self, tmp_path
    ):
        """The heart of Eric's bug: the flag flips during the encode
        (a completed fake child, output written, exit 0) — the run used
        to continue through verify and close `succeeded`. The
        post-command boundary closes it cancelled; verify/manifest never
        produce a success manifest."""
        path = make_media_file(tmp_path)
        runner = FlipRunner(probe_payload(), flip_marker=".720p.mp4")
        with pytest.raises(EngineCancelled) as excinfo:
            run_file(path, cancel=runner.flag, **run_kwargs(tmp_path, runner))
        state = excinfo.value.state  # type: ignore[attr-defined]
        assert state.cancelled is True
        assert excinfo.value.state.current_phase == "encode"  # type: ignore[attr-defined]
        # the post-command boundary fired BEFORE verify/manifest: the
        # encode command is the LAST command the run ever issued (the
        # probe itself contains -show_format/-show_streams, so the
        # "nothing followed the encode" check is the precise one)
        encode_index = next(
            i for i, name in enumerate(runner.commands)
            if name.split(" ")[-1].endswith(".720p.mp4")
        )
        assert encode_index == len(runner.commands) - 1
        # the cancel manifest was written (outcome cancelled, not success)
        manifest = json.loads(Path(state.manifest_path).read_text())
        assert manifest["run"]["outcome"] == "cancelled"
        assert manifest["run"]["cancelled"] is True

    def test_terminated_child_exits_nonzero_without_masking_failed(
        self, tmp_path
    ):
        """A cancel-terminated child (exit nonzero, no output) must read
        cancelled — the old flow surfaced VideoToolError and the job
        closed `failed` (the `/history` mix Eric's monitor showed)."""
        path = make_media_file(tmp_path)
        runner = FlipRunner(probe_payload(), flip_marker=".720p.mp4")
        runner.encode_exit = -15  # killed by SIGTERM, wrote nothing
        runner.encode_writes_output = False
        with pytest.raises(EngineCancelled) as excinfo:
            run_file(path, cancel=runner.flag, **run_kwargs(tmp_path, runner))
        state = excinfo.value.state  # type: ignore[attr-defined]
        assert state.cancelled is True
        assert state.ok is False
        assert "cancel" in (state.error or "")
        # the cancel manifest carries the cancelled outcome — never the
        # failed vocabulary a VideoToolError would have stamped
        manifest = json.loads(Path(state.manifest_path).read_text())
        assert manifest["run"]["outcome"] == "cancelled"
        assert manifest["run"]["cancelled"] is True

    def test_cancel_flip_mid_prepare_never_leaves_prepare_and_never_runs_engine(
        self, tmp_path
    ):
        """The post-command boundary for the PREPARE pass: prepare
        returns (rc 0, mezzanine written) with the flag flipped and the
        engine must never be invoked."""
        path = make_media_file(tmp_path)
        runner = FlipRunner(probe_payload(), flip_marker=".prepared.mkv")
        engine = IgnorantEngine()
        with pytest.raises(EngineCancelled):
            run_file(
                path, cancel=runner.flag, **run_kwargs(tmp_path, runner, engine=engine)
            )
        assert engine.runs == 0

    def test_cancel_flip_during_probe_never_issues_prepare_or_encode(
        self, tmp_path
    ):
        """The pre-command boundary: the flag flips during the probe
        (before any run_command) — the prepare command itself is never
        issued."""
        path = make_media_file(tmp_path)
        runner = FlipRunner(probe_payload(), flip_marker="-show_format")
        with pytest.raises(EngineCancelled) as excinfo:
            run_file(path, cancel=runner.flag, **run_kwargs(tmp_path, runner))
        commands = " ".join(runner.commands)
        assert ".prepared.mkv" not in commands
        assert ".720p.mp4" not in commands
        state = excinfo.value.state  # type: ignore[attr-defined]
        assert state.current_phase == PHASE_PREPARE

    def test_engine_completed_despite_mid_engine_flip_is_caught(
        self, tmp_path
    ):
        """The engine→encode boundary: an engine that observes nothing
        and completes WITH the flag flipping mid-run must not smuggle
        the cancel into encode — the post-engine observe closes the run
        cancelled before the encode command is ever issued."""
        path = make_media_file(tmp_path)
        runner = FlipRunner(probe_payload(), flip_marker="never-matches")  # no flip at commands
        engine = IgnorantEngine(flip_before_complete=True, flag=runner.flag)
        with pytest.raises(EngineCancelled):
            run_file(
                path, cancel=runner.flag, **run_kwargs(tmp_path, runner, engine=engine)
            )
        assert engine.runs == 1  # the engine ran; its completed frames are discarded
        commands = " ".join(runner.commands)
        assert ".720p.mp4" not in commands  # encode never issued

    def test_cancel_flip_in_verify_decode_never_yields_a_success_manifest(
        self, tmp_path
    ):
        """The verify→manifest boundary: the flip arrives during the
        full decode (a minutes-long pass); the decode completes anyway;
        the run closes cancelled before the run manifest is built."""
        path = make_media_file(tmp_path)
        runner = FlipRunner(probe_payload(), flip_marker="-f null")
        with pytest.raises(EngineCancelled):
            run_file(path, cancel=runner.flag, **run_kwargs(tmp_path, runner))
        # the cancel manifest was written; the run manifest never built
        manifests = sorted((tmp_path / "runs").glob("*.manifest.json"))
        assert manifests, "a cancel manifest must still be written"
        payload = json.loads(manifests[-1].read_text())
        assert payload["run"]["outcome"] == "cancelled"
        assert payload["run"]["cancelled"] is True


# ---- subprocess-level: the runner's terminate escalation ---------------------


class TestCancelAwareSubprocessRunner:
    def test_no_cancel_keeps_the_capture_contract(self, tmp_path):
        """The default (no flag) call is byte-identical to the old
        communicate() contract — (rc, stdout, stderr)."""
        script = "print('out-ok'); import sys; print('err-ok', file=sys.stderr)"
        code, out, err = _subprocess_runner(sys.executable, ["-c", script])
        assert code == 0
        assert "out-ok" in out
        assert "err-ok" in err

    def test_live_child_terminated_on_the_flag_raises_engine_cancelled(self):
        """A running child + a flipped flag: the runner enforces the
        engine adapter's binding discipline (terminate -> wait 10 s ->
        kill) and reports the cancel itself. A `sleep` child dies on the
        terminate within the wait window, so the seam raises within
        seconds instead of the old never-returning communicate()."""
        flag = CancelFlag()
        box: dict = {}

        def _run() -> None:
            try:
                _subprocess_runner(
                    sys.executable,
                    ["-c", "import time; time.sleep(30)"],
                    cancel=flag,
                )
                box["ok"] = True
            except BaseException as exc:
                box["error"] = exc

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        # give the child time to actually start (the poll loop is in
        # its 0.2 s slices): the worker is still blocked in the seam and
        # nothing has been reported yet, then flip, and bound the
        # wind-down — the escalation must complete well inside the
        # terminate-wait ceiling (a sleep child exits on the terminate)
        time.sleep(0.5)
        assert box == {} and thread.is_alive()
        flag.set()
        thread.join(timeout=20)
        assert not thread.is_alive(), "the runner never stopped"
        assert isinstance(box.get("error"), EngineCancelled)
        assert "cancelled during subprocess" in str(box["error"])

    def test_flag_set_before_the_call_reports_cancel_before_work(self):
        """A cancel already in flight when the runner is entered: the
        pre-work check raises without ever letting the child do work."""
        flag = CancelFlag()
        flag.set()
        with pytest.raises(EngineCancelled) as excinfo:
            _subprocess_runner(sys.executable, ["-c", "print('nope')"], cancel=flag)
        assert "cancelled during subprocess" in str(excinfo.value)


# ---- bridge-level: the registry → executor → status journey ------------------


class BridgeRunner:
    """The bridge command seam (test_mcp_bridge's shape): canned probe
    JSON, prepared/slice writes, encode/verify effects, plus a gate that
    holds the worker INSIDE the encode command until released — the
    real cancel window (a minutes-to-hours child)."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.commands: list[tuple[str, tuple[str, ...]]] = []
        self._gate = threading.Event()
        self.blocked_count = 0

    def release(self) -> None:
        self._gate.set()

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        self.commands.append((binary, tuple(args)))
        name = " ".join(str(part) for part in args)
        if ".720p.mp4" in str(args[-1] if args else "") and "-i" in args:
            self.blocked_count += 1
            self._gate.wait(timeout=15)
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
            Path(args[-1]).write_bytes(b"prepared-stub")
            return 0, "", ""
        if args and str(args[-1]).endswith(".slice.prepared.mkv"):
            Path(args[-1]).write_bytes(b"slice-stub")
            return 0, "", ""
        if args and str(args[-1]).endswith(".720p.mp4"):
            Path(args[-1]).write_bytes(b"output-stub")
            return 0, "", ""
        if args and args[-1] == "-" and "null" in args:
            return 0, "", ""
        return 1, "", f"unexpected: {binary} {name}"


def build_server(tmp_path, runner: BridgeRunner) -> McpServer:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    services = BridgeServices(
        registry=None,
        state_dir=state,
        probe_runner=runner,
        command_runner=runner,
    )
    return McpServer(state_dir=state, services=services, start_workers=True)


def call(server: McpServer, name: str, arguments: dict, request_id: int = 1) -> dict:
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
    message = json.loads(response)
    return json.loads(message["result"]["content"][0]["text"])


def wait_for_state(server: McpServer, job_id: str, states: set[str], timeout: float = 15.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = call(server, "status", {"job_id": job_id})
        if last["state"] in states:
            return last
        time.sleep(0.02)
    raise AssertionError(f"job never reached {states}; last: {last}")


class TestBridgeCancelJourney:
    def test_cancel_current_mid_encode_lands_cancelled_and_reflects(
        self, tmp_path
    ):
        """The end-to-end Eric journey for 'cancel current': the worker
        blocked inside a long encode; the click flips the claim-time
        flag ('cancelling'); the worker's wind-down now closes
        `cancelled` — and the ONE payload the GUI tick and the monitor
        `/status` serve reads cancelled. The run manifest never records
        a success for the cancelled job."""
        runner = BridgeRunner(probe_payload())
        server = build_server(tmp_path, runner)
        path = make_media_file(tmp_path)
        run_payload = call(server, "run", {"paths": [path]})
        job_id = run_payload["jobs"][0]["job_id"]
        deadline = time.time() + 10
        while runner.blocked_count == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert runner.blocked_count, "worker never reached the encode"
        verdict = call(server, "cancel", {"job_id": job_id})
        assert verdict["state"] == "cancelling"
        flag = server.registry.get_cancel_flag(job_id)
        assert flag is not None and flag.is_set()
        # the wind-down (fake child exits on the release like a
        # terminate-aware child): the boundary observe converts the run
        runner.release()
        final = wait_for_state(server, job_id, {"cancelled", "succeeded", "failed"})
        assert final["state"] == "cancelled", final["error"]
        assert final["error"] is None or "cancel" in final["error"]
        # the /status / GUI-tick read is the SAME payload and it moved
        status = call(server, "status", {"job_id": job_id})
        assert status["state"] == "cancelled"
        history = call(server, "list", {"include_history": True})["history"]
        assert any(row["job_id"] == job_id and row["state"] == "cancelled" for row in history)
        # no success slipped into history for the cancelled job
        assert not any(row["job_id"] == job_id and row["state"] == "succeeded" for row in history)
        server.executor.stop()

    def test_cancel_batch_mid_run_closes_every_job_cancelled(self, tmp_path):
        """'cancel batch': the running job's flag flips AND every queued
        job marks cancelled in the same lock hold; after the wind-down
        the batch is fully terminal — the UI's active set drains."""
        runner = BridgeRunner(probe_payload())
        server = build_server(tmp_path, runner)
        ids: list[str] = []
        for index in range(3):
            path = make_media_file(tmp_path, f"part{index}.avi")
            ids.append(call(server, "run", {"paths": [path]})["jobs"][0]["job_id"])
        deadline = time.time() + 10
        while runner.blocked_count == 0 and time.time() < deadline:
            time.sleep(0.02)
        assert runner.blocked_count, "worker never reached the encode"
        # tool_cancel has no batch form; the GUI's cancel batch rides the
        # registry's atomic batch op through the in-process client —
        # drive it exactly the way LocalBridgeClient.cancel_batch does:
        results = server.registry.cancel_batch(ids)
        assert results == {ids[0]: "cancelling", ids[1]: "cancelled", ids[2]: "cancelled"}
        runner.release()
        for job_id in ids:
            final = wait_for_state(server, job_id, {"cancelled"})
            assert final["state"] == "cancelled"
        # the batch view is fully terminal: nothing queued, nothing running
        listing = call(server, "list", {})
        assert listing["queued"] == [] and listing["running"] == []
        assert {row["state"] for row in listing["history"]} == {"cancelled"}
        server.executor.stop()

    def test_tracked_runner_forwards_the_flag_to_the_production_runner(
        self, tmp_path, monkeypatch
    ):
        """The executor's tracked runner must hand the job's claim-time
        CancelFlag to the production subprocess runner (command/probe
        seams None ⇒ the real runner path), so run.py's own children
        terminate on the flip. The patched fake records the kwarg."""
        seen: dict = {}

        def patched_runner(binary, args, cancel=None):
            if "-show_format" in args:
                seen["probe_cancel"] = cancel
                return 0, probe_payload(), ""
            if ".prepared.mkv" in str(args[-1] if args else ""):
                seen["prepare_cancel"] = cancel
                Path(args[-1]).write_bytes(b"prepared-stub")
                return 0, "", ""
            if "idet" in args:
                return 0, "", (
                    "[Parsed_idet] Single frame detection: TFF: 0 BFF: 0 "
                    "Progressive: 0 Undetermined: 0\n"
                )
            if "-show_streams" in args and "-show_format" not in args:
                return 0, json.dumps(
                    {"streams": [
                        {"codec_type": "video", "width": 1280, "height": 720}]}
                ), ""
            if ".720p.mp4" in str(args[-1] if args else ""):
                seen["encode_cancel"] = cancel
                Path(args[-1]).write_bytes(b"output-stub")
                return 0, "", ""
            if args and args[-1] == "-" and "null" in args:
                return 0, "", ""
            return 1, "", f"unexpected: {binary} {' '.join(map(str, args))}"

        monkeypatch.setattr("vidlift.run._subprocess_runner", patched_runner)
        state = tmp_path / "state"
        state.mkdir(parents=True, exist_ok=True)
        registry = Registry(state)
        services = BridgeServices(registry=registry, state_dir=state)
        server = McpServer(
            state_dir=state, services=services, registry=registry, start_workers=False
        )
        executor = JobExecutor(services)
        path = make_media_file(tmp_path)
        record = registry.enqueue(kind="run", paths=[path], options={"log_dir": str(tmp_path / "runs")})
        registry.claim_next()  # the claim registers the CancelFlag
        flag = registry.get_cancel_flag(record.job_id)
        assert flag is not None
        executor._run_job(record)
        final = registry.require(record.job_id)
        assert final.state == "succeeded", final.error
        # every production-runner call saw THE job's claim-time flag
        assert seen["probe_cancel"] is flag
        assert seen["prepare_cancel"] is flag
        assert seen["encode_cancel"] is flag

    def test_cancelled_benchmark_closes_cancelled_not_failed(self, tmp_path):
        """A cancel observed by the benchmark engine used to fall through
        the run-error mapping and close `failed` (/history mix). The
        exact-type mapping lands `cancelled` — deterministically, an
        engine that raises EngineCancelled mid-benchmark."""
        registry_state = tmp_path / "state"
        registry_state.mkdir(parents=True, exist_ok=True)
        registry = Registry(registry_state)

        class CancellingBenchmarkEngine:
            def run(self, *_a, **_kw):
                raise EngineCancelled("cancelled during benchmark engine")

        services = BridgeServices(
            registry=registry,
            state_dir=registry_state,
            probe_runner=BridgeRunner(probe_payload()),
            command_runner=BridgeRunner(probe_payload()),
        )
        services.benchmark_engine = CancellingBenchmarkEngine()
        events: list[str] = []
        services.log_event = events.append
        executor = JobExecutor(services)
        executor.start()
        path = make_media_file(tmp_path)
        record = registry.enqueue(
            kind="benchmark", paths=[path], options={"sample_path": path}
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            state = registry.require(record.job_id).state
            if state in ("cancelled", "succeeded", "failed"):
                break
            time.sleep(0.02)
        executor.stop()
        final = registry.require(record.job_id)
        # a cancel is not a failure: the cancelled outcome, always
        assert final.state == "cancelled", (
            f"benchmark cancel closed {final.state}: {final.error}"
        )
        assert "cancel" in (final.error or "")
        assert final.ok is False
        # the close transition popped the flag (a closed job owns none)
        assert registry.get_cancel_flag(record.job_id) is None