"""Measured benchmark step (roadmap item 4, clause c).

The plan surfaces quote wall-clock ETAs from a REAL measurement on the
current machine, never from theory tables (the sibling's Section-1.1
throughput numbers were explicitly "to be pinned by a probe run"; that
culture carries). :func:`run_benchmark` drives one short slice of a
real file through the SAME engine seam a real run uses — probe, plan,
prepare a slice, run the engine phase — timing the engine phase, and
returns a :class:`BenchResult` that every ETA surface (planner via
``attach_estimate``, CLI, GUI, MCP bridge) reads from one cache:
``$VIDLIFT_STATE_DIR/benchmarks.json``.

Honesty rules (binding, design Section 8):
- Under the stub engine the benchmark measures pipeline machinery, not
  an AI pass: the result carries ``engine_simulated: true`` plus the
  note "stub numbers; not an AI-pass measurement." A stub-derived ETA
  may only ever be displayed labeled ``simulated``.
- ``estimate_eta`` returns None when no measured constant exists for
  the geometry bucket — no ETA rather than a projected one.
- The cache is append-only (new measurements append rows; readers use
  the latest row per key) and written atomically (write-to-temp +
  ``os.replace``), guarded by one lock at the write boundary.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .plan import PlannedFile, build_file_plan
from .probe import probe_and_classify
from .run import (
    Runner,
    ToolBox,
    build_prepare_command,
)

__all__ = [
    "BENCHMARKS_FILENAME",
    "BenchResult",
    "BenchmarkCacheError",
    "DEFAULT_SLICE_SECONDS",
    "SLICE_MAX",
    "SLICE_MIN",
    "STUB_NOTE",
    "build_slice_prepare_command",
    "clamp_slice_seconds",
    "estimate_eta",
    "estimate_key",
    "geometry_bucket",
    "load_estimates",
    "record_estimate",
    "run_benchmark",
    "state_dir",
]

DEFAULT_SLICE_SECONDS = 30
SLICE_MIN = 10
SLICE_MAX = 120

# Binding wording (design Section 8): a stub benchmark is machinery
# timing and MUST carry exactly this note.
STUB_NOTE = "stub numbers; not an AI-pass measurement."

BENCHMARKS_FILENAME = "benchmarks.json"

# One lock guards the atomic cache write (the registry pattern from the
# bridge spec; the cache is the benchmark module's only mutable shared
# state).
_CACHE_LOCK = threading.Lock()


class BenchmarkCacheError(RuntimeError):
    """The benchmark cache exists but cannot be parsed (delete it to
    reset; media outputs are never affected — worst case is lost
    measurement history)."""


def state_dir(explicit: str | None = None) -> Path:
    """``$VIDLIFT_STATE_DIR`` (default ``~/.vidlift``); the same state
    root the MCP bridge's job registry uses, so one machine has one
    benchmark truth."""
    if explicit:
        return Path(explicit)
    env = os.environ.get("VIDLIFT_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".vidlift"


def clamp_slice_seconds(requested: float | None) -> float:
    """Clamp the requested slice length to the binding [10, 120] window."""
    value = DEFAULT_SLICE_SECONDS if requested is None else float(requested)
    return max(float(SLICE_MIN), min(float(SLICE_MAX), value))


def geometry_bucket(report) -> str:
    """Group by the prepared-frame extent class (e.g. "640x360").

    The bucket is the square-pixel displayed geometry the engine will
    actually see (the prepare chain normalizes to exactly this), which
    is the honest unit of throughput variation — not the container.
    """
    return f"{report.displayed_width}x{report.displayed_height}"


@dataclass(frozen=True)
class BenchResult:
    """One measured benchmark constant (design Section 8, verbatim)."""

    engine: str  # adapter class name
    engine_simulated: bool
    model: str | None  # "realesrgan-x2plus" under the real engine
    native_scale: int | None
    geometry_bucket: str  # e.g. "640x360", grouped by extent class
    duration_sliced_s: float
    frames: int
    wall_seconds: float
    seconds_per_frame: float
    measured_at: float  # epoch
    host_note: str  # e.g. "RTX 3090 Ti" / "stub"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "engine": self.engine,
            "engine_simulated": self.engine_simulated,
            "model": self.model,
            "native_scale": self.native_scale,
            "geometry_bucket": self.geometry_bucket,
            "duration_sliced_s": self.duration_sliced_s,
            "frames": self.frames,
            "wall_seconds": self.wall_seconds,
            "seconds_per_frame": self.seconds_per_frame,
            "measured_at": self.measured_at,
            "host_note": self.host_note,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BenchResult":
        return cls(
            engine=str(data["engine"]),
            engine_simulated=bool(data["engine_simulated"]),
            model=data.get("model"),
            native_scale=data.get("native_scale"),
            geometry_bucket=str(data["geometry_bucket"]),
            duration_sliced_s=float(data["duration_sliced_s"]),
            frames=int(data["frames"]),
            wall_seconds=float(data["wall_seconds"]),
            seconds_per_frame=float(data["seconds_per_frame"]),
            measured_at=float(data["measured_at"]),
            host_note=str(data.get("host_note", "")),
            notes=list(data.get("notes", [])),
        )


def estimate_key(engine: str, model: str | None, bucket: str) -> str:
    """Cache key: ``(engine, model, geometry bucket)`` (design 8)."""
    return f"{engine}|{model or '-'}|{bucket}"


def _cache_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / BENCHMARKS_FILENAME


def load_estimates(
    root: Path | None = None,
) -> dict[str, BenchResult]:
    """Read the benchmark cache; the LATEST row per key wins.

    Returns {} when no cache exists. A corrupt cache raises
    :class:`BenchmarkCacheError` (recovery: delete the file; media
    outputs are unaffected).
    """
    path = _cache_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkCacheError(
            f"benchmark cache at {path} is unreadable: {exc}; delete the "
            "file to reset (measurement history only, media outputs are "
            "unaffected)"
        ) from exc
    rows = data.get("benchmarks") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise BenchmarkCacheError(
            f"benchmark cache at {path} has no benchmarks list; delete the "
            "file to reset"
        )
    latest: dict[str, BenchResult] = {}
    for row in rows:
        try:
            result = BenchResult.from_dict(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise BenchmarkCacheError(
                f"benchmark cache at {path} has a malformed row: {exc}"
            ) from exc
        latest[estimate_key(result.engine, result.model, result.geometry_bucket)] = (
            result  # append-only file, later rows are newer
        )
    return latest


def record_estimate(
    result: BenchResult,
    *,
    root: Path | None = None,
) -> Path:
    """Append one measurement to the cache atomically and return its path.

    The file is small and append-only: write-to-temp in the same
    directory, then ``os.replace`` (atomic on POSIX and Windows). One
    lock guards concurrent writers within this process (the bridge's
    benchmark jobs run on one worker, but the CLI may race a live
    server).
    """
    path = _cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CACHE_LOCK:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {"benchmarks": []}
        rows = current.get("benchmarks")
        if not isinstance(rows, list):
            rows = []
        rows.append(result.to_dict())
        payload = json.dumps({"benchmarks": rows}, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=".benchmarks-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    return path


def build_slice_prepare_command(
    tools: ToolBox,
    plan: PlannedFile,
    prepared_path: str,
    *,
    slice_seconds: float,
) -> list[str]:
    """Prepare command for the benchmark SLICE: identical chain to a
    real run's prepare, plus ``-t <slice>`` wall-time addressing so
    only a short excerpt is normalized (the same extraction semantics
    the engine adapter uses per chunk)."""
    cmd = build_prepare_command(tools, plan, prepared_path)
    # insert -t before -i: ffmpeg wall-time addressing on the input
    i = cmd.index("-i")
    return cmd[:i] + ["-t", f"{slice_seconds:g}"] + cmd[i:]


def run_benchmark(
    sample_path: str,
    *,
    tools: ToolBox,
    engine,  # EngineAdapter
    slice_seconds: float | None = DEFAULT_SLICE_SECONDS,
    workdir: str | None = None,
    probe_runner: Runner | None = None,
    command_runner: Runner | None = None,
    cancel=None,
    state_root: Path | None = None,
    host_note: str = "",
) -> BenchResult:
    """Measure one slice through the REAL engine seam (design Section 8).

    Flow: probe + plan the sample (real classification), prepare ONE
    ``[10, 120]``-second slice through the exact prepare chain a real
    run uses, run the active engine over it timing the ENGINE phase
    wall-clock, and derive ``seconds_per_frame = wall / frames``. The
    result is recorded to the cache keyed
    ``(engine, model, geometry bucket)``.

    Under the stub the result is stamped ``engine_simulated: true``
    with :data:`STUB_NOTE` — machinery numbers, never an AI-pass
    measurement, and every derived ETA is labeled simulated.
    """
    from .run import _subprocess_runner

    run_cmd = command_runner or _subprocess_runner
    probe_cmd = probe_runner or run_cmd

    report = probe_and_classify(
        sample_path, binary=tools.ffprobe, runner=probe_cmd
    )
    plan = build_file_plan(report)
    if plan.process_verdict != "ready":
        raise BenchmarkCacheError(
            f"benchmark sample is not runnable: verdict "
            f"'{plan.process_verdict}' (need an SD candidate)"
        )
    slice_s = clamp_slice_seconds(slice_seconds)

    work_root = Path(workdir) if workdir else Path(tempfile.mkdtemp(
        prefix="vidlift-benchmark-"
    ))
    work_root.mkdir(parents=True, exist_ok=True)
    prepared_path = work_root / "slice.prepared.mkv"

    # Phase 1: prepare the slice (timed only for the record; the
    # benchmark constant is the ENGINE phase, so machinery does not
    # pollute the AI number).
    prepare_cmd = build_slice_prepare_command(
        tools, plan, str(prepared_path), slice_seconds=slice_s
    )
    code, _out, err = run_cmd(prepare_cmd[0], prepare_cmd[1:])
    if code != 0 or not prepared_path.is_file():
        raise BenchmarkCacheError(
            f"benchmark slice prepare failed (exit {code}): "
            f"{(err or '').strip()[:200]}"
        )

    # Phase 2: the engine, timed. Cancel is honored cooperatively.
    engine_started = time.time()
    result = engine.run(
        str(prepared_path), plan, tools, cancel=cancel, on_progress=None
    )
    wall_seconds = time.time() - engine_started

    simulated = bool(result.simulated)
    notes: list[str] = list(result.notes) if getattr(result, "notes", None) else []
    if result.note:
        notes.append(result.note)
    if simulated:
        notes.append(STUB_NOTE)

    frames = result.frames_done
    if frames <= 0:
        # The default stub reports no frame count (it is a copy, not a
        # frame processor). Under the stub the honest machinery constant
        # counts the slice's REAL frames - prepare preserves cadence (no
        # -r), so the slice holds ceil(slice x fps) frames - and the
        # STUB_NOTE already forbids reading it as an AI measurement. A
        # real engine reporting zero frames is broken: refuse.
        if simulated and report.video is not None:
            fps = report.video.avg_frame_rate or report.video.r_frame_rate
            if fps and fps > 0:
                frames = math.ceil(slice_s * fps)
                notes.append(
                    "stub machinery constant: frames counted from the "
                    "slice cadence (no AI pass ran)"
                )
        if frames <= 0:
            raise BenchmarkCacheError(
                "engine reported no frames for the benchmark slice; refusing "
                "to derive a seconds-per-frame constant from nothing"
            )
    seconds_per_frame = wall_seconds / frames

    bench = BenchResult(
        engine=type(engine).__name__,
        engine_simulated=simulated,
        model=result.engine_model,
        native_scale=result.engine_native_scale,
        geometry_bucket=geometry_bucket(report),
        duration_sliced_s=slice_s,
        frames=frames,
        wall_seconds=wall_seconds,
        seconds_per_frame=seconds_per_frame,
        measured_at=time.time(),
        host_note=host_note or ("stub" if simulated else ""),
        notes=notes,
    )
    record_estimate(bench, root=state_root)
    # benchmark workdirs are one-shot scratch; the caller may pass a
    # persistent workdir, so only default-created scratch is cleaned
    if workdir is None:
        import shutil

        shutil.rmtree(work_root, ignore_errors=True)
    return bench


def estimate_eta(
    plan: PlannedFile,
    *,
    estimates: dict[str, BenchResult],
    engine: str | None = None,
    model: str | None = None,
) -> dict | None:
    """Measured ETA for one planned file, or None (clause c).

    ``frames_total x seconds_per_frame`` and nothing else — computed
    as ceil(duration x fps) x seconds_per_frame. No constant for this
    geometry bucket (optionally narrowed to ``engine``/``model`` when
    the caller knows the active engine) -> None, never a projection.
    The returned mapping carries the constant's provenance so every
    surface can label a stub-sourced ETA ``simulated`` (bridge honesty
    invariant 2).
    """
    report = plan.report
    if report.video is None or report.duration_seconds is None:
        return None
    fps = report.video.avg_frame_rate or report.video.r_frame_rate
    if fps is None or fps <= 0:
        return None
    frames_total = math.ceil(report.duration_seconds * fps)
    if frames_total <= 0:
        return None
    bucket = geometry_bucket(report)
    for bench in estimates.values():
        if bench.geometry_bucket != bucket:
            continue
        if engine is not None and bench.engine != engine:
            continue
        if model is not None and (bench.model or None) != model:
            continue
        return {
            "seconds_per_frame": bench.seconds_per_frame,
            "engine_simulated": bench.engine_simulated,
            "measured_at": bench.measured_at,
            "frames_total": frames_total,
            "eta_seconds": frames_total * bench.seconds_per_frame,
            "geometry_bucket": bucket,
            "engine": bench.engine,
            "model": bench.model,
        }
    return None