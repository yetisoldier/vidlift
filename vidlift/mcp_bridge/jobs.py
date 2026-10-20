"""Job execution for the MCP bridge (mcp-bridge.md Sections 3, 4.4, 5.4).

The concurrency model (binding): a single-threaded MCP message loop;
``run`` returns immediately (async job model); actual pipeline work
happens on bounded worker threads owned by the executor — default
max 1 concurrent run, because the runner is hours-per-file and
GPU-bound: a second concurrent run is a misconfiguration, not
parallelism. Benchmark jobs share the same machine (the same worker),
so ``status``/``cancel`` apply uniformly.

The executor holds the :class:`BridgeServices` dependency bundle and
contains NO pipeline logic (Section 2): each job is one call through
the same seams the CLI uses (``run_file`` / ``run_benchmark``), with
the services' test seams (probe/command runners, engine override)
passing straight through. The command-runner wrapper records exact
argv and bumps ``last_activity_at`` for live status; it delegates
behavior-identically to whatever runner is underneath.

Cancel plumbing: each running job owns a :class:`CancelFlag` the
registry's ``cancel()`` flips; ``run_file``'s ``cancel`` callable
observes it; the engine adapter's child-process discipline
(terminate, wait 10 s, kill) is the engine's responsibility, ported
in P1. A cancelled job closes ``cancelled``, never ``failed``.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from .registry import JobRecord, Registry, RegistryError

__all__ = [
    "CancelFlag",
    "JobExecutor",
    "JobOutcome",
    "infer_phase",
]


class CancelFlag:
    """Thread-safe cooperative cancel signal; call-shaped for the
    runner seam (``cancel() -> bool``)."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def __call__(self) -> bool:
        return self._event.is_set()


class JobOutcome:
    """What a finished job produced (carried into the registry record)."""

    def __init__(
        self,
        *,
        ok: bool,
        cancelled: bool = False,
        output_path: str | None = None,
        manifest_path: str | None = None,
        log_path: str | None = None,
        error: str | None = None,
        engine_simulated: bool | None = None,
        engine_note: str | None = None,
        result: dict | None = None,
    ) -> None:
        self.ok = ok
        self.cancelled = cancelled
        self.output_path = output_path
        self.manifest_path = manifest_path
        self.log_path = log_path
        self.error = error
        self.engine_simulated = engine_simulated
        self.engine_note = engine_note
        self.result = result


def infer_phase(binary: str, args: list[str]) -> str | None:
    """Map one executed command to the runner phase that owns it —
    honest inference from seam facts, never a claim the runner did
    not make. The phase enum is the runner's own (probe | prepare |
    upscale | encode | verify).

    v0.4.0 (design B.6): the decode-count probe shapes (they carry
    ``-count_frames``) map to ``upscale`` — the engine adapter's B.1
    prepared-count probe and B.4 mezzanine gates run inside the
    upscale phase."""
    joined = " ".join(args)
    if "-count_frames" in args:
        return "upscale"  # B.1/B.4 decode-count probes (v0.4.0)
    if "-show_format" in args and "-show_streams" in args:
        return "probe"
    if "idet" in args:
        return "prepare"  # the sample pass runs in the prepare phase
    if "libx265" in args:
        return "encode"
    if args and str(args[-1]) == "-" and "null" in args:
        return "verify"
    if "-show_streams" in args and "-show_format" not in args:
        return "verify"
    if "realesrgan" in binary or "realesrgan" in joined:
        return "upscale"
    if args and str(args[-1]).endswith(".prepared.mkv"):
        return "prepare"
    return None


def _execute_run_job(record: JobRecord, services, cancel: CancelFlag) -> JobOutcome:
    """One queued run job, one file, through the REAL runner seam."""
    from ..plan import PlanError
    from ..probe import ProbeError
    from ..run import EngineCancelled, RunError, find_tools, run_file

    options = record.options
    path = record.paths[0]

    # The tracked runner seam: records exact argv into the job's live
    # state and bumps last_activity_at on every external command — the
    # live-status contract (Section 4.5). Delegates behavior-identically
    # to the services' runner (tests) or the real subprocess runner.
    from ..run import _subprocess_runner

    base_command_runner = services.command_runner
    base_probe_runner = services.probe_runner

    # The live-status seam (run_file's state_box): the runner hands the
    # executor the very RunState object it mutates, so the bridge's
    # status tool reports the runner's own state verbatim (Section 4.5)
    # while the worker thread is blocked inside run_file.
    state_box: dict = {}
    record.set_live("state_box", state_box)

    log_root = Path(
        options.get("log_dir") or (Path(path).parent / ".vidlift-runs")
    )
    live_commands: list[list[str]] = []
    record.set_live("commands", live_commands)

    def discover_log_path() -> None:
        """Best-effort discovery of the runner's own log file (the
        newest .log under the run's log root, modified since this job
        started): status reads the runner's log, it never duplicates
        it (Section 5.3)."""
        if record.log_path or not log_root.is_dir():
            return
        try:
            candidates = [
                p for p in log_root.glob("*.log")
                if p.stat().st_mtime >= record.started_at - 1
            ]
            if candidates:
                record.log_path = str(max(candidates, key=lambda p: p.stat().st_mtime))
        except OSError:
            pass

    def tracked(binary: str, args: list[str]):
        record.touch()
        live_commands.append([binary, *args])
        phase = infer_phase(binary, args)
        if phase is not None:
            record.set_live("current_phase", phase)
        discover_log_path()
        runner = base_command_runner or _subprocess_runner
        return runner(binary, args)

    def tracked_probe(binary: str, args: list[str]):
        record.touch()
        live_commands.append([binary, *args])
        phase = infer_phase(binary, args)
        if phase is not None:
            record.set_live("current_phase", phase)
        discover_log_path()
        runner = base_probe_runner or base_command_runner or _subprocess_runner
        return runner(binary, args)

    try:
        tools = find_tools(
            ffmpeg=options.get("ffmpeg"), ffprobe=options.get("ffprobe")
        )
    except RunError as exc:
        return JobOutcome(ok=False, error=f"missing_tool: {exc}")
    try:
        result = run_file(
            path,
            target=options.get("target", "720p"),
            gb_per_hour=float(options.get("gb_per_hour", 1.5)),
            output_dir=options.get("output_dir"),
            tools=tools,
            engine=services.engine,  # None = run_file's own auto selection
            engine_request=options.get("engine", "auto"),
            probe_runner=tracked_probe,
            command_runner=tracked,
            log_dir=options.get("log_dir"),
            cancel=cancel,
            state_box=state_box,
            subtitle_ocr=bool(options.get("subtitle_ocr", True)),
        )
    except EngineCancelled as exc:
        state = getattr(exc, "state", None)
        return JobOutcome(
            ok=False,
            cancelled=True,
            error=str(exc),
            manifest_path=_state_field(state, "manifest_path"),
            log_path=_state_log_path(state),
            engine_simulated=_state_field(state, "engine_simulated"),
            engine_note=_state_field(state, "engine_note"),
        )
    except (ProbeError, PlanError, RunError) as exc:
        state = getattr(exc, "state", None)
        return JobOutcome(
            ok=False,
            error=str(exc),
            manifest_path=_state_field(state, "manifest_path"),
            log_path=_state_log_path(state),
            engine_simulated=_state_field(state, "engine_simulated"),
            engine_note=_state_field(state, "engine_note"),
        )
    state = result.state
    return JobOutcome(
        ok=bool(result.ok),
        output_path=result.plan.output_path,
        manifest_path=result.manifest_path,
        log_path=state.log.path if state.log else None,
        error=state.error,
        engine_simulated=state.engine_simulated,
        engine_note=state.engine_note,
    )


def _state_field(state, name):
    return getattr(state, name, None) if state is not None else None


def _state_log_path(state):
    log = getattr(state, "log", None) if state is not None else None
    return log.path if log is not None else None


def _execute_benchmark_job(
    record: JobRecord, services, cancel: CancelFlag
) -> JobOutcome:
    """One benchmark job through the REAL benchmark seam (P3)."""
    from ..benchmark import BenchmarkCacheError, run_benchmark
    from ..engine_ncnn import select_engine
    from ..plan import PlanError
    from ..probe import ProbeError
    from ..run import RunError, find_tools

    options = record.options
    sample_path = options["sample_path"]
    try:
        tools = find_tools()
    except RunError as exc:
        return JobOutcome(ok=False, error=f"missing_tool: {exc}")
    engine = services.benchmark_engine
    note = None
    if engine is None:
        try:
            engine, note = select_engine(options.get("engine_request", "auto"))
        except RunError as exc:
            return JobOutcome(ok=False, error=f"missing_tool: {exc}")
    try:
        bench = run_benchmark(
            sample_path,
            tools=tools,
            engine=engine,
            slice_seconds=options.get("duration_seconds"),
            workdir=str(services.state_dir / "benchmark-work")
            if services.state_dir else None,
            probe_runner=services.probe_runner,
            command_runner=services.command_runner,
            cancel=cancel,
            state_root=services.state_dir,
        )
    except (BenchmarkCacheError, ProbeError, PlanError, RunError) as exc:
        return JobOutcome(ok=False, error=str(exc))
    # The Section-4.3 completion result, verbatim field names: the
    # benchmark cache row plus the per-plan constant surfaces quote.
    frames_per_second = (
        bench.frames / bench.wall_seconds if bench.wall_seconds > 0 else 0.0
    )
    seconds_per_output_second = bench.seconds_per_frame * max(
        bench.frames / bench.duration_sliced_s, 1.0
    ) if bench.duration_sliced_s > 0 else 0.0
    result = {
        "sample_path": sample_path,
        "duration_seconds": bench.duration_sliced_s,
        "engine": bench.engine,
        "engine_simulated": bench.engine_simulated,
        "frames_processed": bench.frames,
        "wall_seconds": bench.wall_seconds,
        "frames_per_second": frames_per_second,
        "per_plan_constant": {
            "seconds_per_output_second": seconds_per_output_second
        },
        "notes": list(bench.notes),
    }
    return JobOutcome(
        ok=True,
        engine_simulated=bench.engine_simulated,
        engine_note=note,
        result=result,
    )


_EXECUTORS = {
    "run": _execute_run_job,
    "benchmark": _execute_benchmark_job,
}


class JobExecutor:
    """Single-worker job execution with cooperative cancel (Section 3).

    ``max_workers`` defaults to 1 (the binding misconfiguration rule).
    The worker claims jobs in submission order, one at a time. Each
    running job registers its :class:`CancelFlag` with the registry so
    ``cancel`` flips it; the flag is cleared when the job finishes.
    """

    def __init__(
        self,
        services,
        *,
        max_workers: int = 1,
        executor_fn: Callable | None = None,
    ) -> None:
        self.services = services
        self.registry = services.registry
        self.max_workers = max_workers
        self._executor_fn = executor_fn  # tests substitute job execution
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._wake = threading.Event()
        self._stopping = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            needed = self.max_workers - len(self._threads)
            for _ in range(needed):
                thread = threading.Thread(
                    target=self._worker_loop,
                    daemon=True,
                    name="vidlift-mcp-worker",
                )
                thread.start()
                self._threads.append(thread)

    def stop(self, *, join: bool = True) -> None:
        """Signal workers to finish their current job and exit."""
        self._stopping = True
        self._wake.set()
        if join:
            for thread in self._threads:
                thread.join(timeout=5)

    # -- queue ---------------------------------------------------------------

    def _claim_next(self) -> JobRecord | None:
        """Claim the oldest queued job (submission order)."""
        queued = sorted(
            self.registry.queued(), key=lambda r: r.submitted_at
        )
        for job in queued:
            try:
                self.registry.transition(job, "running")
                return job
            except RegistryError:
                continue  # cancelled between view and claim
        return None

    def _worker_loop(self) -> None:
        # The N1 amendment to layer c: the guard must cover the CLAIM
        # path too, not just _run_job — any bug anywhere in the loop
        # body kills the single worker and silently starves the queue.
        while not self._stopping:
            try:
                job = self._claim_next()
                if job is None:
                    # bounded idle poll; a new enqueue shortens latency
                    self._wake.wait(timeout=0.2)
                    self._wake.clear()
                    continue
                self._run_job(job)
            except Exception as exc:
                # The D1/N1 lesson: the single worker thread must NEVER
                # die on any per-iteration bug — log and keep serving,
                # or every later job starves queued with no diagnostic.
                log_event = getattr(self.services, "log_event", None)
                if log_event is not None:
                    log_event(
                        f"worker survived loop failure: "
                        f"{type(exc).__name__}: {exc}"
                    )

    def _run_job(self, record: JobRecord) -> None:
        # The D1 fix (layer a): the registry created and registered
        # this job's CancelFlag at the claim transition, so a running
        # job ALWAYS has a live flag — a cancel can never land in the
        # old claim window against "no live owner". The defensive
        # create covers direct-API callers that bypass the executor.
        cancel_flag = self.registry.get_cancel_flag(record.job_id)
        if cancel_flag is None:
            cancel_flag = CancelFlag()
            self.registry.set_cancel_flag(record.job_id, cancel_flag)
        log_event = getattr(self.services, "log_event", None)
        if log_event is not None:
            log_event(f"job {record.job_id} ({record.kind}) running")
        executor = self._executor_fn or _EXECUTORS.get(record.kind)
        try:
            if executor is None:
                outcome = JobOutcome(
                    ok=False, error=f"unknown job kind: {record.kind}"
                )
            else:
                outcome = executor(record, self.services, cancel_flag)
        except Exception as exc:  # a worker must never die on a job bug
            outcome = JobOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")
        finally:
            record.clear_live()
        record.ok = outcome.ok
        record.output_path = outcome.output_path
        record.manifest_path = outcome.manifest_path
        record.log_path = outcome.log_path
        record.error = outcome.error
        record.engine_simulated = outcome.engine_simulated
        record.engine_note = outcome.engine_note
        record.result = outcome.result
        record.touch()
        # The D1 fix (layer b): a raced terminal state must never
        # raise out of the worker. The registry stays the source of
        # truth; the contradiction is LOGGED, never silent.
        try:
            if outcome.cancelled:
                self.registry.transition(record, "cancelled")
            elif outcome.ok:
                self.registry.transition(record, "succeeded")
            else:
                self.registry.transition(record, "failed")
        except RegistryError as exc:
            if log_event is not None:
                log_event(
                    f"job {record.job_id} terminal state raced "
                    f"(registry says {record.state}, executor outcome "
                    f"{'cancelled' if outcome.cancelled else ('ok' if outcome.ok else 'failed')}): {exc}"
                )
        if log_event is not None:
            log_event(
                f"job {record.job_id} ({record.kind}) {record.state}"
                + (f": {record.error}" if record.error else "")
            )

    def notify_new_work(self) -> None:
        """Wake an idle worker (called after enqueue)."""
        self._wake.set()


def _now() -> float:
    return time.time()