"""Single-file GUI-free run pipeline for VidLift.

This is the first execution milestone from docs/roadmap.md and
docs/design.md section 4: `vidlift run FILE` proves ONE file end-to-end
from the CLI (probe -> plan -> prepare -> upscale -> encode -> verify)
before any batch queue or UI attaches to it.

Boundary (docs/architecture.md): VidLift is file-first only. There is
no disc/rip/drive/title vocabulary here, and the bluray-to-mp4 engine
is consumed (later, when its file-first seam exists) through
:class:`EngineAdapter` alone. Today the pipeline defaults to
:class:`StubEngine`, a passthrough stub: the prepare/encode/verify
machinery is real, the AI upscale is not. That default is explicit in
every manifest (``engine_simulated``) so no run can claim a
Real-ESRGAN pass it never did.

Explicit tool errors, not PATH guessing (roadmap packaging
discipline): ffmpeg/ffprobe are located via :func:`find_tools`, and a
missing tool aborts with a named :class:`MissingToolError` before any
phase starts. No silent fallback mid-run.

Observability contract (docs/design.md section 4): runner state
exposes the current phase, the run log path and tail, the
last-activity timestamp, command lines, output paths, phase timings,
the verification result, and a per-run artifact manifest, so health
is judged by activity, not by progress percent alone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .plan import PlanError, PlannedFile, build_file_plan
from .probe import (
    FileReport,
    MediaProbe,
    ProbeError,
    probe_and_classify,
    probe_args,
)

__all__ = [
    "EngineAdapter",
    "EngineCancelled",
    "EngineResult",
    "FakeEngine",
    "MissingToolError",
    "PHASE_DONE",
    "PHASE_ENCODE",
    "PHASE_ORDER",
    "PHASE_PLAN",
    "PHASE_PREPARE",
    "PHASE_PROBE",
    "PHASE_UPSCALE",
    "PHASE_VERIFY",
    "RunError",
    "RunLog",
    "RunPhase",
    "RunResult",
    "RunState",
    "StubEngine",
    "ToolBox",
    "UpscaleError",
    "VideoToolError",
    "VerificationResult",
    "build_encode_command",
    "build_prepare_command",
    "build_verification_command",
    "ensure_output_dir",
    "find_tools",
    "run_file",
    "verify_output",
]

# Runner seam for external commands, same shape as probe.Runner:
# run(binary, args) -> (returncode, stdout, stderr).
Runner = Callable[[str, list[str]], "tuple[int, str, str]"]

PHASE_PROBE = "probe"
PHASE_PLAN = "plan"
PHASE_PREPARE = "prepare"
PHASE_UPSCALE = "upscale"
PHASE_ENCODE = "encode"
PHASE_VERIFY = "verify"
PHASE_DONE = "done"

PHASE_ORDER = [
    PHASE_PROBE,
    PHASE_PLAN,
    PHASE_PREPARE,
    PHASE_UPSCALE,
    PHASE_ENCODE,
    PHASE_VERIFY,
    PHASE_DONE,
]


class RunError(RuntimeError):
    """A run failed; ``phase`` on the attached state says where."""


class UpscaleError(RunError):
    """The engine's upscale stage failed; the runner fails OPEN (clause d).

    Defined at the runner seam (design real-engine-adapter.md Section 5)
    so ``run_file`` can catch it without importing a concrete engine
    module, and re-exported by :mod:`vidlift.engine_ncnn`. Raising it
    means: ship the synchronized original-resolution re-encode with the
    reason stated verbatim in the manifest — never a desynced upscale,
    never no output, never a silent AI-pass claim.
    """


class EngineCancelled(RunError):
    """Cooperative cancel inside the engine; NOT a failure (clause d).

    The runner records the run as cancelled (manifest outcome
    ``cancelled``), never as a failed job.
    """


class MissingToolError(RunError):
    """An external tool VidLift needs is not installed (or not executable).

    Raised before any phase starts: the packaged app must honor
    bundled-tool paths, and a missing tool is a setup problem to fix,
    never something to guess around mid-run.
    """

    def __init__(self, tool: str, message: str) -> None:
        super().__init__(message)
        self.tool = tool


class VideoToolError(RunError):
    """A phase's external command exited nonzero."""

    def __init__(
        self, phase: str, command: list[str], exit_code: int, detail: str
    ) -> None:
        last = detail.strip().splitlines()[-1] if detail.strip() else "no output"
        super().__init__(
            f"{phase} command failed (exit {exit_code}: {last}): "
            + " ".join(command)
        )
        self.phase = phase
        self.command = list(command)
        self.exit_code = exit_code
        self.detail = detail


@dataclass
class RunPhase:
    """One named pipeline phase with its wall-clock duration."""

    name: str
    started_at: float
    finished_at: float | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return self.finished_at - self.started_at


@dataclass
class ToolBox:
    """Explicit external-tool paths for one run.

    ``ffmpeg`` and ``ffprobe`` are the only external tools the runner
    itself needs; any Real-ESRGAN binary belongs to the engine adapter.
    """

    ffmpeg: str
    ffprobe: str

    def to_dict(self) -> dict:
        return {"ffmpeg": self.ffmpeg, "ffprobe": self.ffprobe}


def find_tools(
    *, ffmpeg: str | None = None, ffprobe: str | None = None
) -> ToolBox:
    """Locate ffmpeg/ffprobe explicitly, with named missing-tool errors.

    Resolution order per tool: caller-supplied path (packaged apps pass
    bundled paths here), then the ``VIDLIFT_FFMPEG`` / ``VIDLIFT_FFPROBE``
    environment overrides, then a plain PATH lookup. Anything that
    cannot be found or executed raises :class:`MissingToolError`
    naming the tool, so failures surface as setup errors before any
    phase runs -- never a silent guess mid-run.
    """
    resolved: dict[str, str] = {}
    for name, given in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        binary = given or os.environ.get(f"VIDLIFT_{name.upper()}") or name
        if not shutil.which(binary):
            raise MissingToolError(
                name,
                f"{name} not found at '{binary}': install FFmpeg, pass "
                f"--{name}, or set VIDLIFT_{name.upper()}",
            )
        resolved[name] = binary
    return ToolBox(ffmpeg=resolved["ffmpeg"], ffprobe=resolved["ffprobe"])


def _subprocess_runner(binary: str, args: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout, result.stderr


@dataclass
class EngineResult:
    """What the upscale engine reports back for the upscale phase.

    The v2 fields (``engine_model`` onward) carry the adapter's honesty
    surface into manifest schema v2 (design real-engine-adapter.md
    Section 11); they are additive and default to None/absent so stub
    results and v1 consumers are unaffected.
    """

    mezzanine_path: str
    frames_done: int = 0
    note: str | None = None
    simulated: bool = False
    engine_model: str | None = None
    engine_native_scale: int | None = None
    frame_clock: dict | None = None
    duration_gates: dict | None = None
    model_hashes_verified: bool | None = None


class EngineAdapter:
    """The bluray-to-mp4 file-first engine seam.

    Contract per docs/design.md section 1: the runner hands the engine
    a *prepared progressive square-pixel source file path* plus the
    plan; the engine returns the written mezzanine path and a frame
    count. The engine's public surface never mentions discs, rips,
    drives, or titles, and VidLift imports nothing disc-shaped.

    The validated bluray-to-mp4 v0.2.20 path is chunk-directory based
    (disc-shaped); its file-first entry point does not exist yet. When
    it ships, this class gets one implementation and the runner changes
    zero lines.
    """

    def run(
        self,
        prepared_path: str,
        plan: PlannedFile,
        tools: ToolBox,
        *,
        cancel: Callable[[], bool] | None = None,
    ) -> EngineResult:
        raise NotImplementedError(
            "no real engine adapter exists yet; the runner uses StubEngine "
            "until bluray-to-mp4 ships its file-first seam (docs/roadmap.md)"
        )


class StubEngine(EngineAdapter):
    """Passthrough stub: no Real-ESRGAN, no GPU, copy prepared -> mezzanine.

    Honors the adapter contract exactly (prepared file in, mezzanine
    path out), so pipeline code is identical under a real engine later.
    The output of a stub run is a genuine re-encode of the prepared
    source (real ffmpeg, real HEVC), *not* an AI upscale; every
    manifest records ``engine_simulated: true`` for that reason.
    """

    def __init__(self, *, frames: int = 0, note: str = "stub engine (no Real-ESRGAN)") -> None:
        self.frames = frames
        self.note = note
        self.calls: list[str] = []

    def run(
        self,
        prepared_path: str,
        plan: PlannedFile,
        tools: ToolBox,
        *,
        cancel: Callable[[], bool] | None = None,
    ) -> EngineResult:
        self.calls.append(prepared_path)
        if cancel is not None and cancel():
            raise RunError("upscale cancelled before any frame was processed")
        mezzanine = Path(prepared_path).with_suffix(".upscaled.mkv")
        shutil.copyfile(prepared_path, mezzanine)
        return EngineResult(
            mezzanine_path=str(mezzanine),
            frames_done=self.frames,
            note=self.note,
            simulated=True,
        )


class FakeEngine(StubEngine):
    """A StubEngine that raises instead of running (for failure tests)."""

    def __init__(self, error: Exception, *, note: str = "failing engine") -> None:
        super().__init__(note=note)
        self.error = error

    def run(self, prepared_path, plan, tools, *, cancel=None):
        self.calls.append(prepared_path)
        raise self.error


@dataclass
class RunLog:
    """Per-run log file: the tail source for live observability."""

    path: str
    lines: list[str] = field(default_factory=list)
    max_tail_lines: int = 40

    def write(self, text: str) -> None:
        line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + text
        self.lines.append(line)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def tail(self, n: int | None = None) -> list[str]:
        n = self.max_tail_lines if n is None else n
        return self.lines[-n:]


@dataclass
class VerificationResult:
    """Post-encode output verification: exists, decodes, geometry."""

    output_path: str
    ok: bool
    exists: bool = False
    decodes: bool = False
    geometry_ok: bool = False
    actual_geometry: tuple[int, int] | None = None
    expected_geometry: tuple[int, int] | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "output_path": self.output_path,
            "ok": self.ok,
            "exists": self.exists,
            "decodes": self.decodes,
            "geometry_ok": self.geometry_ok,
            "actual_geometry": list(self.actual_geometry) if self.actual_geometry else None,
            "expected_geometry": list(self.expected_geometry) if self.expected_geometry else None,
            "error": self.error,
        }


def build_prepare_command(
    tools: ToolBox, plan: PlannedFile, prepared_path: str
) -> list[str]:
    """ffmpeg command writing the prepared source the engine contract needs.

    v0.2.19 filter chain, file-first: normalize to the square-pixel
    displayed size with setsar=1 (anamorphic safety; unknown SAR was
    already defaulted to square at probe time, never guessed), and
    deinterlace only when the stream itself flagged interlace -- no
    blind deinterlace, ever.

    Deliberately deferred (docs/design.md v0.2 session B): the
    60-second sample inspection that classifies progressive / hard
    3:2 telecine / genuine interlace. Until the real engine adapter
    lands, the probe's ``interlaced_flagged`` drives the bwdif decision
    and the sample-based classification is not implemented.
    """
    report = plan.report
    filters = [
        f"scale={report.displayed_width}:{report.displayed_height}",
        "setsar=1",
    ]
    if report.interlaced_flagged:
        filters.append("bwdif=mode=send_field")
    return [
        tools.ffmpeg,
        "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", report.path,
        "-vf", ",".join(filters),
        "-c:v", "ffv1",
        "-an",
        "-y", prepared_path,
    ]


def build_encode_command(
    tools: ToolBox, plan: PlannedFile, mezzanine_path: str
) -> list[str]:
    """ffmpeg command encoding the mezzanine to the planned MP4.

    The encode planner is already media-agnostic (docs/design.md):
    HEVC at the planned geometry and bitrate (the final lanczos
    downscale to the 720p frame happens here, after the engine's 2x),
    AAC at the planned bitrate, chapters dropped per the design, and
    text subtitles carried as mov_text. Audio and subtitles come from
    the ORIGINAL file -- the mezzanine is video-only by contract.

    Subtitle carry rule for this slice: all subtitle streams map only
    when every one is text; mixed text/image files carry none (image
    subs are never OCR'd and mov_text cannot hold them), matching the
    planner's "listed only" note.
    """
    report = plan.report
    text_subs = [s for s in report.subtitles if s.kind == "text"]
    carry_subs = bool(text_subs) and all(s.kind == "text" for s in report.subtitles)
    cmd = [
        tools.ffmpeg,
        "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", mezzanine_path,
    ]
    if report.audio or carry_subs:
        cmd += ["-i", report.path]
    cmd += ["-map", "0:v:0"]
    if report.audio:
        cmd += ["-map", "1:a:0"]
    if carry_subs:
        cmd += ["-map", "1:s?"]
    cmd += [
        "-c:v", "libx265",
        "-b:v", f"{plan.video_bitrate_kbps}k",
        "-vf", f"scale={plan.output_width}:{plan.output_height}:flags=lanczos",
        "-pix_fmt", "yuv420p",
    ]
    if report.audio:
        cmd += ["-c:a", "aac", "-b:a", f"{plan.audio.bitrate_kbps}k"]
    if carry_subs:
        cmd += ["-c:s", "mov_text"]
    cmd += ["-map_chapters", "-1", "-movflags", "+faststart", "-y", plan.output_path]
    return cmd


def build_verification_command(tools: ToolBox, output_path: str) -> list[str]:
    """ffprobe command run against the encode output to verify it."""
    return [tools.ffprobe, "-v", "error", "-show_streams", "-of", "json", output_path]


def ensure_output_dir(output_path: str) -> None:
    """Create the planned output file's directory before any encode runs.

    ffmpeg never creates a missing output directory -- it dies with a
    bare ``Error opening output files: No such file or directory`` --
    and ``--output-dir`` commonly names a folder that does not exist
    yet, so the runner makes it (with any missing parents) as soon as
    a plan is accepted. A directory that cannot be created is a named
    :class:`RunError` from the plan phase, never a raw OSError
    escaping mid-pipeline.
    """
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RunError(
            f"cannot create output directory for '{output_path}': {exc}"
        ) from exc


def verify_output(
    tools: ToolBox,
    plan: PlannedFile,
    *,
    runner: Runner,
    command: list[str] | None = None,
) -> VerificationResult:
    """Verify the encode output exists, decodes, and matches plan geometry.

    "Decodes" here means ffprobe reads the output container and finds
    a video stream; the encode itself runs ffmpeg with ``-loglevel
    error``, so a nonzero encode exit is already a hard failure upstream.
    A full decode pass (``ffmpeg -v error -i out -f null -``) is
    deliberately deferred until the real engine adapter lands, because
    the stub's outputs only need container/geometry honesty. Geometry
    must match the plan exactly; a mismatch is a verification failure,
    not a warning.
    """
    cmd = command if command is not None else build_verification_command(
        tools, plan.output_path
    )
    expected = (plan.output_width, plan.output_height)
    result = VerificationResult(
        output_path=plan.output_path, ok=False, expected_geometry=expected
    )
    if not os.path.isfile(plan.output_path):
        result.error = "output file not written"
        return result
    result.exists = True
    code, stdout, stderr = runner(cmd[0], cmd[1:])
    if code != 0:
        result.error = (
            f"ffprobe failed on the encode output: {stderr.strip()[:200] or 'no diagnostic'}"
        )
        return result
    result.decodes = True
    try:
        streams = json.loads(stdout).get("streams") or []
    except json.JSONDecodeError:
        result.error = "unparsable ffprobe output for the encode output"
        return result
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        result.error = "no video stream found in the encode output"
        return result
    actual = (int(video.get("width") or 0), int(video.get("height") or 0))
    result.actual_geometry = actual
    result.geometry_ok = actual == expected
    result.ok = True
    if not result.geometry_ok:
        result.ok = False
        result.error = (
            f"geometry mismatch: planned {expected[0]}x{expected[1]}, "
            f"got {actual[0]}x{actual[1]}"
        )
    return result


@dataclass
class RunState:
    """Live observability for one file run.

    The state contract from docs/design.md section 4: current phase,
    log path and tail, last-activity timestamp, command lines, output
    path, phase timings, verification, and the finished manifest. The
    batch queue and the UI become clients of exactly this object;
    none of it lives in the CLI or GUI.
    """

    file_path: str
    started_at: float = field(default_factory=time.time)
    current_phase: str = PHASE_PROBE
    last_activity_at: float = field(default_factory=time.time)
    log: RunLog | None = None
    commands: list[list[str]] = field(default_factory=list)
    phases: list[RunPhase] = field(default_factory=list)
    probe: MediaProbe | None = None
    plan: PlannedFile | None = None
    output_path: str | None = None
    prepared_path: str | None = None
    mezzanine_path: str | None = None
    engine_note: str | None = None
    engine_simulated: bool = False
    verification: VerificationResult | None = None
    manifest_path: str | None = None
    finished_at: float | None = None
    ok: bool | None = None
    error: str | None = None

    def touch(self) -> None:
        self.last_activity_at = time.time()

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "started_at": self.started_at,
            "current_phase": self.current_phase,
            "last_activity_at": self.last_activity_at,
            "log_path": self.log.path if self.log else None,
            "commands": [list(c) for c in self.commands],
            "phases": [
                {
                    "name": p.name,
                    "started_at": p.started_at,
                    "finished_at": p.finished_at,
                    "duration_seconds": p.duration_seconds,
                }
                for p in self.phases
            ],
            "plan_output_path": self.plan.output_path if self.plan else None,
            "output_path": self.output_path,
            "prepared_path": self.prepared_path,
            "mezzanine_path": self.mezzanine_path,
            "engine_note": self.engine_note,
            "engine_simulated": self.engine_simulated,
            "verification": self.verification.to_dict() if self.verification else None,
            "manifest_path": self.manifest_path,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "error": self.error,
        }


@dataclass
class RunResult:
    """Finished-run summary the CLI renders."""

    state: RunState
    plan: PlannedFile
    verification: VerificationResult
    manifest_path: str

    @property
    def ok(self) -> bool:
        return bool(self.state.ok)


def run_file(
    file_path: str,
    *,
    target: str = "720p",
    gb_per_hour: float = 1.5,
    output_dir: str | None = None,
    tools: ToolBox | None = None,
    probe_runner: Runner | None = None,
    command_runner: Runner | None = None,
    engine: EngineAdapter | None = None,
    log_dir: str | None = None,
    cancel: Callable[[], bool] | None = None,
) -> RunResult:
    """Run one file through the full pipeline and return its state.

    Phase order is fixed: probe, plan, prepare, upscale, encode,
    verify. Every external command goes through ``command_runner``
    (subprocess by default; fakes in tests record command lines and
    simulate tool effects). ``tools`` bypasses discovery for callers
    that already resolved binaries (the CLI does, honoring
    --ffmpeg/--ffprobe); leaving it None resolves via find_tools.
    The engine defaults to :class:`StubEngine` because no real
    file-first adapter exists yet.
    """
    if tools is None:
        tools = find_tools()
    run_cmd = command_runner or _subprocess_runner
    state = RunState(file_path=file_path)

    base = Path(file_path)
    run_id = f"{base.stem}.{time.strftime('%Y%m%d-%H%M%S')}"
    log_root = Path(log_dir) if log_dir else base.parent / ".vidlift-runs"
    log_root.mkdir(parents=True, exist_ok=True)
    log_path = log_root / f"{run_id}.log"
    if log_path.exists():
        log_path.unlink()  # a same-second rerun starts a fresh log
    state.log = RunLog(path=str(log_path))

    def log(text: str) -> None:
        state.log.write(text)
        state.touch()

    def begin_phase(name: str) -> RunPhase:
        phase = RunPhase(name=name, started_at=time.time())
        state.phases.append(phase)
        state.current_phase = name
        log(f"phase {name}: started")
        return phase

    def end_phase(phase: RunPhase, note: str = "") -> None:
        phase.finished_at = time.time()
        log(f"phase {phase.name}: done{(' ' + note) if note else ''}")

    def run_command(phase_name: str, command: list[str]) -> None:
        state.commands.append(list(command))
        log(f"{phase_name} command: {' '.join(command)}")
        code, stdout, stderr = run_cmd(command[0], command[1:])
        state.touch()
        if code != 0:
            raise VideoToolError(phase_name, command, code, stderr or stdout)

    try:
        # ---- probe ----
        phase = begin_phase(PHASE_PROBE)
        probe_cmd = [tools.ffprobe, *probe_args(file_path)]
        state.commands.append(probe_cmd)
        log(f"probe command: {' '.join(probe_cmd)}")
        report = probe_and_classify(
            file_path, binary=tools.ffprobe, runner=probe_runner or run_cmd
        )
        state.probe = report
        state.touch()
        end_phase(
            phase,
            note=f"{report.source_class}, displayed "
            f"{report.displayed_width}x{report.displayed_height}",
        )

        # ---- plan ----
        phase = begin_phase(PHASE_PLAN)
        plan = build_file_plan(
            report, target=target, gb_per_hour=gb_per_hour, output_dir=output_dir
        )
        state.plan = plan
        state.output_path = plan.output_path
        log(f"planned output: {plan.output_path}")
        log(
            f"plan: {plan.output_width}x{plan.output_height} HEVC at "
            f"{plan.video_bitrate_kbps}k video / {plan.audio.bitrate_kbps}k audio"
        )
        if plan.process_verdict != "ready":
            raise RunError(
                f"file is not runnable: verdict is '{plan.process_verdict}' "
                "(VidLift upscales SD candidates only)"
            )
        ensure_output_dir(plan.output_path)
        log(f"output dir ready: {Path(plan.output_path).parent}")
        end_phase(phase, note=f"verdict={plan.process_verdict}")

        # ---- prepare ----
        phase = begin_phase(PHASE_PREPARE)
        prepared_path = str(log_root / f"{run_id}.prepared.mkv")
        state.prepared_path = prepared_path
        run_command(PHASE_PREPARE, build_prepare_command(tools, plan, prepared_path))
        if not os.path.isfile(prepared_path):
            raise RunError(f"prepare phase produced no file at {prepared_path}")
        end_phase(phase, note=f"prepared={prepared_path}")

        # ---- upscale ----
        phase = begin_phase(PHASE_UPSCALE)
        active_engine = engine or StubEngine()
        engine_result = active_engine.run(prepared_path, plan, tools, cancel=cancel)
        state.mezzanine_path = engine_result.mezzanine_path
        state.engine_note = engine_result.note
        state.engine_simulated = engine_result.simulated
        log(
            f"engine: {type(active_engine).__name__}"
            f"{' (simulated, no Real-ESRGAN pass)' if engine_result.simulated else ''}"
        )
        end_phase(
            phase,
            note=f"frames={engine_result.frames_done} "
            f"mezzanine={engine_result.mezzanine_path}",
        )

        # ---- encode ----
        phase = begin_phase(PHASE_ENCODE)
        run_command(
            PHASE_ENCODE, build_encode_command(tools, plan, engine_result.mezzanine_path)
        )
        if not os.path.isfile(plan.output_path):
            raise RunError(f"encode phase produced no file at {plan.output_path}")
        end_phase(phase, note=f"output={plan.output_path}")

        # ---- verify ----
        phase = begin_phase(PHASE_VERIFY)
        verify_cmd = build_verification_command(tools, plan.output_path)
        state.commands.append(verify_cmd)
        log(f"verify command: {' '.join(verify_cmd)}")
        verification = verify_output(tools, plan, runner=run_cmd, command=verify_cmd)
        state.verification = verification
        state.touch()
        if verification.ok:
            log("verification: ok (exists, decodes, geometry matches plan)")
        else:
            log(f"verification FAILED: {verification.error}")
        end_phase(
            phase,
            note="ok" if verification.ok else f"not ok ({verification.error})",
        )

        # ---- manifest + finish ----
        state.ok = verification.ok
        state.finished_at = time.time()
        if not verification.ok:
            state.error = f"verification failed: {verification.error}"
        manifest = _build_manifest(state, tools, report, plan, verification, active_engine)
        manifest_path = log_root / f"{run_id}.manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        state.manifest_path = str(manifest_path)
        log(f"manifest: {manifest_path}")
        if not verification.ok:
            raise RunError(state.error)
        state.current_phase = PHASE_DONE
        log("run complete")
        return RunResult(
            state=state,
            plan=plan,
            verification=verification,
            manifest_path=str(manifest_path),
        )
    except (ProbeError, PlanError, VideoToolError, RunError) as exc:
        state.finished_at = time.time()
        state.ok = False
        state.error = str(exc)
        if state.log is not None:
            log(f"error in phase {state.current_phase}: {exc}")
        exc.state = state  # type: ignore[attr-defined]
        # A verification failure already wrote its full manifest above
        # (it carries the verification detail); only write the lean
        # failure manifest when the run died before manifest time.
        if state.manifest_path is None:
            _write_failure_manifest(state, tools)
        raise


def _build_manifest(
    state: RunState,
    tools: ToolBox,
    report: FileReport,
    plan: PlannedFile,
    verification: VerificationResult,
    engine: EngineAdapter,
) -> dict:
    """Per-run artifact manifest (roadmap item 2): inputs, plan,
    command lines, outputs, timings, verification."""
    return {
        "vidlift_version": _vidlift_version(),
        "run": {
            "input_file": state.file_path,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "ok": state.ok,
            "error": state.error,
            "engine": type(engine).__name__,
            "engine_simulated": state.engine_simulated,
            "engine_note": state.engine_note,
        },
        "tools": tools.to_dict(),
        "probe": {
            "source_class": report.source_class,
            "container": report.container,
            "displayed_width": report.displayed_width,
            "displayed_height": report.displayed_height,
            "upscale_verdict": report.upscale_verdict,
        },
        "plan": {
            "target": plan.target,
            "output_path": plan.output_path,
            "output_geometry": [plan.output_width, plan.output_height],
            "video_bitrate_kbps": plan.video_bitrate_kbps,
            "audio_bitrate_kbps": plan.audio.bitrate_kbps,
            "estimated_output_bytes": plan.estimated_output_bytes,
            "subtitle_note": plan.subtitle_note,
        },
        "artifacts": {
            "prepared_path": state.prepared_path,
            "mezzanine_path": state.mezzanine_path,
            "output_path": state.output_path,
            "log_path": state.log.path if state.log else None,
        },
        "phases": [
            {
                "name": p.name,
                "started_at": p.started_at,
                "finished_at": p.finished_at,
                "duration_seconds": p.duration_seconds,
            }
            for p in state.phases
        ],
        "commands": [list(c) for c in state.commands],
        "verification": verification.to_dict(),
    }


def _write_failure_manifest(state: RunState, tools: ToolBox) -> None:
    """Best-effort manifest for a failed run, so failures are
    debuggable from artifacts instead of guesswork."""
    if state.log is None:
        return
    manifest_path = Path(state.log.path).with_suffix(".manifest.json")
    try:
        manifest = {
            "vidlift_version": _vidlift_version(),
            "run": {
                "input_file": state.file_path,
                "started_at": state.started_at,
                "finished_at": state.finished_at,
                "ok": False,
                "error": state.error,
                "error_phase": state.current_phase,
            },
            "tools": tools.to_dict() if tools else None,
            "artifacts": {
                "prepared_path": state.prepared_path,
                "mezzanine_path": state.mezzanine_path,
                "output_path": state.output_path,
                "log_path": state.log.path,
            },
            "phases": [
                {
                    "name": p.name,
                    "started_at": p.started_at,
                    "finished_at": p.finished_at,
                }
                for p in state.phases
            ],
            "commands": [list(c) for c in state.commands],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        state.manifest_path = str(manifest_path)
    except OSError:
        pass


def _vidlift_version() -> str:
    from . import __version__

    return __version__