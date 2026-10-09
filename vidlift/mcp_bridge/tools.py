"""Thin tool adapters: MCP args -> vidlift.* calls -> MCP payloads.

mcp-bridge.md Section 5.2: every adapter is one screenful of mapping
with no logic beyond mapping; any policy that sneaks in here violates
the Section-2 non-goals. Decisions (verdicts, geometry, bitrates,
fail-open, honesty fields) are computed in vidlift.* and passed through
verbatim. Errors raise :class:`BridgeError` carrying the Section-7
machine-readable code.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from ..benchmark import STUB_NOTE, estimate_eta, load_estimates
from ..plan import PlanError, attach_estimate, build_file_plan
from ..probe import ProbeError, probe_and_classify
from ..run import MissingToolError
from .schemas import InvalidInput

__all__ = [
    "BridgeError",
    "BridgeServices",
    "TOOL_HANDLERS",
    "tool_benchmark",
    "tool_cancel",
    "tool_list",
    "tool_manifest",
    "tool_plan",
    "tool_probe",
    "tool_run",
    "tool_status",
]


class BridgeError(Exception):
    """A tool-level failure with its Section-7 machine-readable code.

    ``payload`` (optional) rides along in the error content so no data
    is lost (e.g. per-file probe errors when every file fails).
    """

    def __init__(self, code: str, message: str, payload: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload


class BridgeServices:
    """The bridge's dependency bundle: registry + executor + test seams.

    Production wires the real registry/executor and leaves the runner
    seams None (real subprocesses). Tests inject canned probe JSON /
    command effects and engine overrides through the same object —
    the bridge never grows a second code path for testing.
    """

    def __init__(
        self,
        *,
        registry,
        state_dir: Path,
        executor=None,
        probe_runner=None,
        command_runner=None,
        engine=None,
        benchmark_engine=None,
        log_tail_lines: int = 40,
        log_event=None,
    ) -> None:
        self.registry = registry
        self.state_dir = Path(state_dir)
        self.executor = executor
        self.probe_runner = probe_runner
        self.command_runner = command_runner
        self.engine = engine  # None = run_file's own selection (auto)
        self.benchmark_engine = benchmark_engine
        self.log_tail_lines = log_tail_lines
        self.log_event = log_event  # server-level lifecycle log hook

    def resolve_tools(self):
        """Per-call tool discovery (Section 5.1): server startup
        resolves nothing; a misconfiguration surfaces as a job/tool
        error naming the tool, never a silent hang."""
        from ..run import find_tools

        return find_tools()

    def probe_kwargs(self, tools):
        kwargs = {"binary": tools.ffprobe}
        if self.probe_runner is not None:
            kwargs["runner"] = self.probe_runner
        return kwargs


# ---- payload serializers (Section 4 shapes, verbatim) ------------------------


def _video_payload(video) -> dict | None:
    if video is None:
        return None
    return {
        "codec_name": video.codec_name,
        "width": video.width,
        "height": video.height,
        "sample_aspect_ratio": video.sample_aspect_ratio,
        "display_aspect_ratio": video.display_aspect_ratio,
        "field_order": video.field_order,
        "r_frame_rate": video.r_frame_rate,
        "avg_frame_rate": video.avg_frame_rate,
        "interlaced_flagged": video.interlaced_flagged,
    }


def _report_payload(report) -> dict:
    return {
        "container": report.container,
        "duration_seconds": report.duration_seconds,
        "video": _video_payload(report.video),
        "audio": [
            {"codec_name": a.codec_name, "channels": a.channels, "lang": a.lang}
            for a in report.audio
        ],
        "subtitles": [
            {"codec_name": s.codec_name, "kind": s.kind, "lang": s.lang}
            for s in report.subtitles
        ],
        "source_class": report.source_class,
        "displayed_width": report.displayed_width,
        "displayed_height": report.displayed_height,
        "upscale_verdict": report.upscale_verdict,
        "pal_speedup_suspected": report.pal_speedup_suspected,
    }


def _plan_payload(plan) -> dict:
    report = plan.report
    warnings: list[str] = []
    if report.pal_speedup_suspected:
        warnings.append("PAL speedup suspected; VidLift will process as-is")
    if report.interlaced_flagged:
        warnings.append(
            "interlace flagged; the prepare stage classifies the source "
            "and deinterlaces correctly"
        )
    payload = {
        "verdict": plan.process_verdict,
        "output_path": plan.output_path,
        "output_geometry": [plan.output_width, plan.output_height],
        "video_bitrate_kbps": plan.video_bitrate_kbps,
        "audio": {
            "channels": plan.audio.channels,
            "bitrate_kbps": plan.audio.bitrate_kbps,
            "label": plan.audio.label,
        },
        "estimated_output_bytes": plan.estimated_output_bytes,
        "expectation": plan.expectation,
        "subtitle_note": plan.subtitle_note,
        "warnings": warnings,
    }
    if plan.eta_estimate is not None:
        # additive field; carries its own provenance (engine_simulated)
        # so a stub-sourced ETA can never pose as an AI measurement
        payload["eta_estimate"] = plan.eta_estimate
    return payload


def _load_estimates_safe(services) -> dict:
    from ..benchmark import BenchmarkCacheError

    try:
        return load_estimates(services.state_dir)
    except BenchmarkCacheError:
        return {}  # a corrupt cache never blocks planning; ETA -> cannot_know


def _cannot_know(planned: list) -> list[str]:
    """The mandatory honesty list (Section 4.2): what the plan cannot
    state. Always non-empty; the entry says exactly which wall-clock
    truth applies — none, stub-simulated, or bucket-scoped."""
    etas = [p.eta_estimate for p in planned if p.eta_estimate is not None]
    if not etas:
        return [
            "wall-clock time: no measured benchmark constant on this "
            "machine; run the benchmark tool first"
        ]
    if any(e["engine_simulated"] for e in etas):
        return [
            "wall-clock time: measured on the stub engine (simulated); "
            + STUB_NOTE
        ]
    return [
        "wall-clock time: the measured constant covers this geometry "
        "bucket on this machine; per-title variation is not modeled"
    ]


# ---- the eight tools ----------------------------------------------------------


def tool_probe(services: BridgeServices, args: dict) -> dict:
    """probe (Section 4.1): per-file isolation; one unreadable file
    never fails the batch. Maps 1:1 onto probe_and_classify."""
    try:
        tools = services.resolve_tools()
    except MissingToolError as exc:
        raise BridgeError("missing_tool", str(exc)) from exc
    kwargs = services.probe_kwargs(tools)
    reports: list[dict] = []
    failures = 0
    for path in args["paths"]:
        try:
            report = probe_and_classify(path, **kwargs)
        except ProbeError as exc:
            failures += 1
            reports.append({"path": path, "ok": False, "error": str(exc)})
            continue
        reports.append({"path": path, "ok": True, "report": _report_payload(report)})
    if args["paths"] and failures == len(args["paths"]):
        # tool-level probe_failed only when every file failed (the
        # per-file entries ride along so nothing is lost)
        raise BridgeError(
            "probe_failed",
            f"all {failures} file(s) failed to probe",
            payload={"reports": reports},
        )
    return {"reports": reports}


def tool_plan(services: BridgeServices, args: dict) -> dict:
    """plan (Section 4.2): build_batch_plan over per-path
    probe_and_classify, with the mandatory cannot_know list."""
    options = args.get("options") or {}
    target = options.get("target", "720p")
    gbph = float(options.get("gb_per_hour", 1.5))
    output_dir = options.get("output_dir")
    # A caller configuration error is a tool-level plan_failed (the
    # Section-7 code table's example), never a per-file entry: an
    # unsupported target fails every file identically.
    if target != "720p":
        raise BridgeError("plan_failed", f"unsupported target: {target}")
    try:
        tools = services.resolve_tools()
    except MissingToolError as exc:
        raise BridgeError("missing_tool", str(exc)) from exc
    kwargs = services.probe_kwargs(tools)

    files: list[dict] = []
    planned: list = []
    for path in args["paths"]:
        try:
            report = probe_and_classify(path, **kwargs)
        except ProbeError as exc:
            files.append({"path": path, "ok": False, "error": str(exc)})
            continue
        try:
            plan = build_file_plan(
                report, target=target, gb_per_hour=gbph, output_dir=output_dir
            )
        except PlanError as exc:
            files.append({"path": path, "ok": False, "error": str(exc)})
            continue
        plan = attach_estimate(plan, _load_estimates_safe(services))
        planned.append(plan)
        files.append({"path": path, "ok": True, "plan": _plan_payload(plan)})

    runnable = sum(1 for p in planned if p.process_verdict == "ready")
    total_bytes: int | None = 0
    for p in planned:
        if p.estimated_output_bytes is None:
            total_bytes = None
            break
        total_bytes += p.estimated_output_bytes
    if not args["paths"]:
        raise BridgeError("plan_failed", "no paths given")
    return {
        "files": files,
        "summary": {
            "runnable_count": runnable,
            "total_estimated_bytes": total_bytes,
            "cannot_know": _cannot_know(planned),
        },
    }


def tool_benchmark(services: BridgeServices, args: dict) -> dict:
    """benchmark (Section 4.3): a tracked job, returns immediately."""
    sample_path = args["sample_path"]
    if not os.path.isfile(sample_path):
        raise BridgeError(
            "invalid_input",
            f"sample_path: no such file on this machine: {sample_path}",
        )
    options: dict = {"sample_path": sample_path}
    if "duration_seconds" in args:
        options["duration_seconds"] = args["duration_seconds"]
    record = services.registry.enqueue(
        kind="benchmark", paths=[sample_path], options=options
    )
    services.executor.notify_new_work()
    return {"job_id": record.job_id, "kind": "benchmark"}


def tool_run(services: BridgeServices, args: dict) -> dict:
    """run (Section 4.4): enqueue, return immediately, never block."""
    options = args.get("options") or {}
    jobs: list[dict] = []
    for path in args["paths"]:
        if not os.path.isfile(path):
            jobs.append(
                {"path": path, "job_id": None, "ok": False,
                 "error": f"no such file: {path}"}
            )
            continue
        record = services.registry.enqueue(
            kind="run", paths=[path], options=dict(options)
        )
        services.executor.notify_new_work()
        jobs.append({"path": path, "job_id": record.job_id, "ok": True})
    return {"jobs": jobs}


def _read_manifest(manifest_path: str | None) -> dict | None:
    """The manifest file parsed verbatim; None when absent/unreadable
    (Section 4.8: not an error)."""
    if not manifest_path:
        return None
    path = Path(manifest_path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _log_tail(services: BridgeServices, record, state) -> list[str]:
    """Up to ``log_tail_lines`` lines, newest last, read from the
    runner's own in-memory log when live (else the on-disk log file —
    the bridge never duplicates the run log)."""
    if state is not None and state.log is not None:
        return state.log.tail(services.log_tail_lines)
    log_path = record.log_path
    if not log_path or not Path(log_path).is_file():
        return []
    try:
        lines = Path(log_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return lines[-services.log_tail_lines:]


def _eta_seconds(services: BridgeServices, record, state) -> float | None:
    """eta_seconds (Section 4.5): ONLY from a measured benchmark
    constant AND only mid-run (upscale/encode); otherwise null, never
    projected. remaining_frames x seconds_per_frame, nothing else."""
    if state is None or state.plan is None or state.progress is None:
        return None
    if state.current_phase not in ("upscale", "encode"):
        return None
    frames_total = state.progress.get("frames_total") or 0
    frames_done = state.progress.get("frames_done") or 0
    if frames_total <= 0 or frames_done > frames_total:
        return None
    estimates = _load_estimates_safe(services)
    eta_map = estimate_eta(
        state.plan,
        estimates=estimates,
        engine=state.engine_name,
        model=state.engine_model,
    )
    if eta_map is None:
        return None
    remaining = frames_total - frames_done
    return max(0.0, remaining * eta_map["seconds_per_frame"])


def _live_state(record):
    """The runner's live RunState for a job mid-execution (the
    state_box seam), or None once the job has finished."""
    box = record.get_live("state_box")
    if box and "state" in box:
        return box["state"]
    return None


def _status_payload(services: BridgeServices, record) -> dict:
    """The full runner observability contract (Section 4.5): verbatim
    RunState.to_dict() fields when the job is live; honest
    reconstruction from the persisted record + on-disk manifest
    otherwise; nulls where unknown."""
    state = _live_state(record)
    manifest = _read_manifest(record.manifest_path) if state is None else None

    payload = {
        "job_id": record.job_id,
        "state": record.state,
        "queue_position": services.registry.queue_position(record.job_id),
        "file_path": record.paths[0] if record.paths else None,
        "current_phase": record.get_live("current_phase"),
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "last_activity_at": record.last_activity_at,
        "log_path": record.log_path,
        "log_tail": _log_tail(services, record, state),
        "commands": record.get_live("commands") or [],
        "phases": [],
        "engine_simulated": bool(record.engine_simulated)
        if record.engine_simulated is not None
        else False,
        "engine_note": record.engine_note,
        "verification": None,
        "manifest_path": record.manifest_path,
        "error": record.error,
        "eta_seconds": None,
    }
    if state is not None:
        data = state.to_dict()
        payload.update(
            {
                "current_phase": data["current_phase"],
                "started_at": data["started_at"],
                "finished_at": data["finished_at"],
                "last_activity_at": data["last_activity_at"],
                "log_path": data["log_path"],
                "commands": data["commands"],
                "phases": data["phases"],
                "engine_simulated": data["engine_simulated"],
                "engine_note": data["engine_note"],
                "verification": data["verification"],
                "manifest_path": data["manifest_path"],
                "error": data["error"] or record.error,
            }
        )
        payload["eta_seconds"] = _eta_seconds(services, record, state)
    elif manifest is not None:
        # a previous server's finished/failed job (or this server's
        # completed job): reconstruct from the runner's own artifacts,
        # never invented
        run = manifest.get("run") or {}
        payload["engine_simulated"] = bool(run.get("engine_simulated", False))
        payload["engine_note"] = run.get("engine_note")
        payload["verification"] = manifest.get("verification")
        payload["phases"] = manifest.get("phases") or []
        payload["commands"] = manifest.get("commands") or []
        artifacts = manifest.get("artifacts") or {}
        if not payload["log_path"]:
            payload["log_path"] = artifacts.get("log_path")
        if record.state == "succeeded":
            payload["current_phase"] = "done"
        elif run.get("error_phase"):
            payload["current_phase"] = run["error_phase"]
    if record.kind == "benchmark" and record.result is not None:
        payload["result"] = record.result
    return payload


def tool_status(services: BridgeServices, args: dict) -> dict:
    """status (Section 4.5): the full contract, one job."""
    record = services.registry.get(args["job_id"])
    if record is None:
        raise BridgeError("not_found", f"unknown job_id: {args['job_id']}")
    return _status_payload(services, record)


def tool_cancel(services: BridgeServices, args: dict) -> dict:
    """cancel (Section 4.6): cooperative; non-events stay non-events."""
    job_id = args["job_id"]
    result = services.registry.cancel(job_id)
    if result == "not-found":
        return {"job_id": job_id, "state": "not-found"}
    if result == "already-finished":
        return {"job_id": job_id, "state": "already-finished"}
    if result == "conflict":
        # a cancel already in flight: the only reachable state_conflict
        # (Section 7); the FIRST cancel proceeds normally
        raise BridgeError(
            "state_conflict", f"cancel already in flight for job {job_id}"
        )
    return {"job_id": job_id, "state": result}


def tool_list(services: BridgeServices, args: dict) -> dict:
    """list (Section 4.7): queue depth, running, history."""
    limit = args.get("limit", 20)
    limit = max(1, min(200, int(limit)))
    include_history = args.get("include_history", True)
    queued = sorted(
        services.registry.queued(), key=lambda r: r.submitted_at
    )
    running = services.registry.running()
    history = services.registry.history() if include_history else []
    return {
        "queued": [
            {
                "job_id": r.job_id,
                "file_path": r.paths[0] if r.paths else None,
                "submitted_at": r.submitted_at,
            }
            for r in queued
        ],
        "running": [
            {
                "job_id": r.job_id,
                "file_path": r.paths[0] if r.paths else None,
                "current_phase": _live_state(r).current_phase
                if _live_state(r) is not None
                else r.get_live("current_phase"),
            }
            for r in running
        ],
        "history": [
            {
                "job_id": r.job_id,
                "file_path": r.paths[0] if r.paths else None,
                "state": r.state,
                "finished_at": r.finished_at,
                "ok": bool(r.ok),
                "engine_simulated": bool(r.engine_simulated)
                if r.engine_simulated is not None
                else False,
                "output_path": r.output_path,
                "manifest_path": r.manifest_path,
            }
            for r in history[:limit]
        ],
    }


def tool_manifest(services: BridgeServices, args: dict) -> dict:
    """manifest (Section 4.8): the parsed manifest verbatim — never
    filtered, renamed, or redacted. Absent manifest: null, not an error."""
    record = services.registry.get(args["job_id"])
    if record is None:
        raise BridgeError("not_found", f"unknown job_id: {args['job_id']}")
    data = _read_manifest(record.manifest_path)
    return {
        "job_id": record.job_id,
        "manifest_path": record.manifest_path if data is not None else None,
        "manifest": data,
    }


TOOL_HANDLERS = {
    "probe": tool_probe,
    "plan": tool_plan,
    "benchmark": tool_benchmark,
    "run": tool_run,
    "status": tool_status,
    "cancel": tool_cancel,
    "list": tool_list,
    "manifest": tool_manifest,
}