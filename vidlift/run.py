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
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path

from .plan import PlanError, PlannedFile, build_file_plan
from .probe import (
    FileReport,
    MediaProbe,
    ProbeError,
    probe_and_classify,
    probe_args,
)
from . import _proc
from .subtitles import (
    SubtitleOcrError,
    SubtitlePlan,
    mux_language,
    mux_srt_into_mp4,
    resolve_seconv,
    resolve_tesseract,
    run_subtitle_ocr,
    sidecar_srt_path,
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
    "PHASE_SUBTITLES",
    "PHASE_UPSCALE",
    "PHASE_VERIFY",
    "RunError",
    "RunLog",
    "RunPhase",
    "RunResult",
    "RunState",
    "StubEngine",
    "ToolBox",
    "UPSCALE_STEP_PREPARED_COUNT",
    "UpscaleError",
    "VideoToolError",
    "VerificationResult",
    "build_encode_command",
    "build_decode_command",
    "build_prepare_command",
    "build_verification_command",
    "ensure_output_dir",
    "find_tools",
    "run_file",
    "run_with_heartbeat",
    "cleanup_run_artifacts",
    "replace_source_with_output",
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
# v0.4.1 (design §B.4): the subtitles phase between verify and done.
# OCR operates on the SOURCE file but muxes into the FINISHED MP4, so
# it must run after encode; after verify keeps verify's scope purely
# A/V structural (§B.5 adds the cheap subtitle assertion there
# instead of reordering). Optional by presence: no selected track ->
# the phase records 'skipped' and ends in milliseconds.
PHASE_SUBTITLES = "subtitles"
PHASE_DONE = "done"

# v0.4.5 (progress-honesty design §3b, D-count/D-eta): the runner's
# step token for the decode-count pass (the engine's B.1 label). The
# GUI's run_stage_label discriminates on this exact token to render
# the honest "counting frames" label instead of a bare step name.
UPSCALE_STEP_PREPARED_COUNT = "prepared-count-probe"

PHASE_ORDER = [
    PHASE_PROBE,
    PHASE_PLAN,
    PHASE_PREPARE,
    PHASE_UPSCALE,
    PHASE_ENCODE,
    PHASE_VERIFY,
    PHASE_SUBTITLES,
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
        binary = given or os.environ.get(f"VIDLIFT_{name.upper()}")
        if not binary and getattr(sys, "frozen", False):
            suffix = ".exe" if os.name == "nt" else ""
            candidates = []
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidates.append(Path(meipass) / "tools" / f"{name}{suffix}")
            candidates.append(
                Path(sys.executable).resolve().parent / "tools" / f"{name}{suffix}"
            )
            binary = next((str(path) for path in candidates if path.is_file()), None)
        binary = binary or name
        if not shutil.which(binary):
            raise MissingToolError(
                name,
                f"{name} not found at '{binary}': install FFmpeg, pass "
                f"--{name}, or set VIDLIFT_{name.upper()}",
            )
        resolved[name] = binary
    return ToolBox(ffmpeg=resolved["ffmpeg"], ffprobe=resolved["ffprobe"])


def _subprocess_runner(binary: str, args: list[str]) -> tuple[int, str, str]:
    # v0.4.4 (design §6): Popen+communicate instead of subprocess.run so
    # the Windows-only child-console suppression kwargs (startupinfo/
    # CREATE_NO_WINDOW — an empty dict elsewhere) reach the call. The
    # capture shape (returncode, stdout, stderr) is unchanged.
    proc = subprocess.Popen(
        [binary, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **_proc.popen_kwargs(),
    )
    stdout, stderr = proc.communicate()
    return proc.returncode, stdout, stderr


# v0.4.0 (design B.6): heartbeat cadence for long phases — the pump
# polls/touches at HEARTBEAT_TOUCH_SECONDS and writes a log line at
# most every HEARTBEAT_LOG_SECONDS, so the log tail alone disproves
# "stuck" during a 65-minute encode or a 10-minute full decode.
# Module-level constants so tests can shrink the cadence without
# touching any call site.
HEARTBEAT_TOUCH_SECONDS = 5.0
HEARTBEAT_LOG_SECONDS = 30.0


def run_with_heartbeat(
    run_cmd: Runner,
    binary: str,
    args: list[str],
    *,
    touch: Callable[[], None],
    log_line: Callable[[str], None] | None = None,
    phase_name: str = "",
    on_touch: Callable[[float], None] | None = None,
    touch_seconds: float | None = None,
    log_seconds: float | None = None,
) -> tuple[int, str, str]:
    """B.6: one blocking seam call pumped on a worker thread.

    A poll loop (not signal/callback machinery — the B.10(4) risk
    discipline): the seam call runs on a daemon thread while the pump
    joins in short slices, touching the state every
    ``HEARTBEAT_TOUCH_SECONDS`` and writing one throttled
    ``phase <name>: heartbeat t=<n>s`` log line per
    ``HEARTBEAT_LOG_SECONDS`` so a long encode or full decode never
    reads as a frozen ``last_activity_at``. ``on_touch(elapsed)`` lets
    the caller record an elapsed-time progress hint. Short commands
    never enter this path (the callers opt in per long command)."""
    touch_s = (
        HEARTBEAT_TOUCH_SECONDS if touch_seconds is None else touch_seconds
    )
    log_s = HEARTBEAT_LOG_SECONDS if log_seconds is None else log_seconds
    started = time.time()
    box: dict = {}

    def _work() -> None:
        try:
            box["result"] = run_cmd(binary, args)
        except BaseException as exc:  # surfaced after the join
            box["error"] = exc

    thread = threading.Thread(
        target=_work, daemon=True, name="vidlift-heartbeat"
    )
    thread.start()
    next_log = started + log_s
    while thread.is_alive():
        thread.join(timeout=touch_s)
        if thread.is_alive():
            touch()
            elapsed = time.time() - started
            if on_touch is not None:
                on_touch(elapsed)
            if log_line is not None and time.time() >= next_log:
                prefix = f"phase {phase_name}: " if phase_name else ""
                log_line(f"{prefix}heartbeat t={elapsed:.0f}s")
                next_log = time.time() + log_s
    if "error" in box:
        raise box["error"]
    return box["result"]


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
    # v0.4.0 additive fields (design B.8, manifest schema v2):
    # exact frame-gate record, per-chunk resume facts, and the
    # retained rejected-mezzanine paths when a gate trip fired after
    # reassembly. All optional/None so stub results and older
    # consumers are unaffected.
    frame_gates: dict | None = None
    resume: dict | None = None
    rejected_mezzanine: dict | None = None


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
        on_progress: Callable[[int, int], None] | None = None,
        on_step: Callable[[str], None] | None = None,
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
        on_progress: Callable[[int, int], None] | None = None,
        on_step: Callable[[str], None] | None = None,
    ) -> EngineResult:
        self.calls.append(prepared_path)
        if cancel is not None and cancel():
            raise EngineCancelled(
                "upscale cancelled before any frame was processed"
            )
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

    def run(self, prepared_path, plan, tools, *, cancel=None, on_progress=None,
            on_step=None):
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
    """Post-encode output verification: exists, decodes, geometry.

    ``decodes`` is the ffprobe container check; ``full_decode_ok`` is
    the ffmpeg decode-every-frame pass (roadmap item 1 / b12) that
    runs on otherwise-good outputs before the run may be called
    verified.

    v0.4.1 additive (design §B.5): ``subtitle_ok``/``subtitle_detail``
    carry the cheap subtitle assertion — when the plan selected a
    track the output must contain a mov_text stream; when none was
    selected it asserts none. A mismatch is REPORTED, never a
    verification hard-fail: subtitle loss must not fail an
    otherwise-good 5-hour upscale (fail-open asymmetry, Eric-approved:
    A/V integrity is gating, subtitle presence is reporting).
    """

    output_path: str
    ok: bool
    exists: bool = False
    decodes: bool = False
    full_decode_ok: bool = False
    geometry_ok: bool = False
    actual_geometry: tuple[int, int] | None = None
    expected_geometry: tuple[int, int] | None = None
    error: str | None = None
    subtitle_expected: bool = False
    subtitle_ok: bool | None = None
    subtitle_detail: str | None = None

    def to_dict(self) -> dict:
        return {
            "output_path": self.output_path,
            "ok": self.ok,
            "exists": self.exists,
            "decodes": self.decodes,
            "full_decode_ok": self.full_decode_ok,
            "geometry_ok": self.geometry_ok,
            "actual_geometry": list(self.actual_geometry) if self.actual_geometry else None,
            "expected_geometry": list(self.expected_geometry) if self.expected_geometry else None,
            "error": self.error,
            "subtitle_expected": self.subtitle_expected,
            "subtitle_ok": self.subtitle_ok,
            "subtitle_detail": self.subtitle_detail,
        }


def build_prepare_command(
    tools: ToolBox,
    plan: PlannedFile,
    prepared_path: str,
    *,
    sample_chain=None,
) -> list[str]:
    """ffmpeg command writing the prepared source the engine contract needs.

    v0.2.19 filter chain, file-first: normalize to the square-pixel
    displayed size with setsar=1 (anamorphic safety; unknown SAR was
    already defaulted to square at probe time, never guessed), and
    deinterlace by the P4 three-way classification
    (vidlift/classify_sample.py) when the stream flagged interlace:
    - progressive -> NO deinterlace at all, even when the flag lied;
    - hard 3:2 telecine -> the reference IVTC chain
      (fieldmatch+decimate) with output rate 24000/1001;
    - genuine interlace -> bwdif at field rate (parity=auto + fps at
      the doubled base rate, exact rational string);
    - undecidable sample -> the flag-driven bwdif=mode=send_field
      fallback (the pre-P4 behavior; the classification refines, it
      never kills a run).
    Unflagged sources never get any deinterlace filter.

    ``sample_chain`` is the :class:`vidlift.classify_sample.PrepareChain`
    built from the idet verdict; None means the flag heuristic.
    """
    report = plan.report
    filters = [
        f"scale={report.displayed_width}:{report.displayed_height}",
        "setsar=1",
    ]
    output_rate = None
    if sample_chain is not None:
        filters.extend(sample_chain.filters)
        if sample_chain.fps_filter:
            filters.append(sample_chain.fps_filter)
        output_rate = sample_chain.output_rate
    elif report.interlaced_flagged:
        filters.append("bwdif=mode=send_field")
    cmd = [
        tools.ffmpeg,
        "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", report.path,
        "-vf", ",".join(filters),
        "-c:v", "ffv1",
        "-an",
    ]
    if output_rate:
        # the telecine arm pins the container rate (v0.2.19 reference
        # chain pins -r 24000/1001); an output option, so before -y
        cmd += ["-r", output_rate]
    cmd += ["-y", prepared_path]
    return cmd


def build_encode_command(
    tools: ToolBox, plan: PlannedFile, mezzanine_path: str
) -> list[str]:
    """ffmpeg command encoding the mezzanine to the planned MP4.

    The encode planner is media-agnostic (docs/design.md): HEVC at the
    planned geometry and bitrate (the final lanczos downscale to the
    720p frame happens here, after the engine's 2x), AAC at the
    planned bitrate, chapters dropped per the design. Audio and
    subtitles come from the ORIGINAL file -- the mezzanine is
    video-only by contract.

    Subtitle carry rule (v0.4.1, design §B.3): the PLANNED text track
    maps by explicit stream index (``-map 1:<idx> -c:s mov_text``),
    replacing v0.4.0's all-or-nothing gate -- a mixed text+bitmap
    file now ships its text track instead of silently dropping
    everything. Bitmap tracks are skipped here (mov_text cannot hold
    them); the selected bitmap track rides the OCR stage and muxes
    after verify. A sidecar-selected text plan also skips this map
    (there is nothing embedded to map) and muxes post-encode.
    """
    report = plan.report
    sub_plan = plan.subtitle_plan
    embed_text_in_encode = bool(
        sub_plan is not None
        and sub_plan.source == "embedded"
        and sub_plan.kind == "text"
    )
    cmd = [
        tools.ffmpeg,
        "-hide_banner", "-loglevel", "error", "-nostdin",
        "-i", mezzanine_path,
    ]
    if report.audio or embed_text_in_encode:
        cmd += ["-i", report.path]
    cmd += ["-map", "0:v:0"]
    if report.audio:
        # Audio from the SOURCE's first stream. MUST follow the engine's
        # stamp derivation (engine_ncnn.probe_audio_extent_seconds): if
        # audio selection ever becomes configurable, both seams move
        # together or the derived clock is silently wrong.
        cmd += ["-map", "1:a:0"]
    if embed_text_in_encode:
        # The selected text track by its probe index (plan-time
        # decision, run-time execution).
        cmd += ["-map", f"1:{sub_plan.stream_index}"]
    cmd += [
        "-c:v", "libx265",
        "-b:v", f"{plan.video_bitrate_kbps}k",
        "-vf", f"scale={plan.output_width}:{plan.output_height}:flags=lanczos",
        "-pix_fmt", "yuv420p",
    ]
    if report.audio:
        cmd += ["-c:a", "aac", "-b:a", f"{plan.audio.bitrate_kbps}k"]
    if embed_text_in_encode:
        cmd += [
            "-c:s", "mov_text",
            "-metadata:s:s:0",
            f"language={mux_language(sub_plan.lang)}",
        ]
    cmd += ["-map_chapters", "-1", "-movflags", "+faststart", "-y", plan.output_path]
    return cmd


def build_decode_command(tools: ToolBox, output_path: str) -> list[str]:
    """ffmpeg full-decode pass for the verify stage (roadmap item 1, b12).

    ``ffmpeg -v error -i out -f null -`` decodes every frame of the
    encode output; any decode error exits nonzero and fails
    verification. This is the structural-integrity complement to the
    ffprobe geometry check (cheap checks first, the full decode only
    for otherwise-good outputs, because a 2-hour file costs minutes
    here). ``-nostdin`` follows the codebase's uniform anti-TTY
    discipline; the pinned command shape is otherwise exact.
    """
    return [
        tools.ffmpeg,
        "-v", "error", "-nostdin",
        "-i", output_path,
        "-f", "null", "-",
    ]


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
    decode_command: list[str] | None = None,
    full_decode: bool = True,
    decode_runner: Runner | None = None,
) -> VerificationResult:
    """Verify the encode output exists, decodes, and matches plan geometry.

    "Decodes" here means ffprobe reads the output container and finds
    a video stream; the encode itself runs ffmpeg with ``-loglevel
    error``, so a nonzero encode exit is already a hard failure upstream.
    The full decode pass (``ffmpeg -v error -i out -f null -``, b12)
    then decodes every frame of an otherwise-good output; its failure
    fails verification. Geometry must match the plan exactly; a
    mismatch is a verification failure, not a warning.
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
    # v0.4.1 (design §B.5): the cheap subtitle assertion — REPORTED,
    # never gating. When the plan selected a track the output must
    # hold a mov_text stream; when none was selected a stray map must
    # not have smuggled one in. A mismatch lands as a run warning via
    # subtitle_detail; verification's ok is untouched (the fail-open
    # asymmetry: A/V integrity gates, subtitle presence reports).
    sub_plan = getattr(plan, "subtitle_plan", None)
    expected_sub = bool(
        sub_plan is not None
        and sub_plan.source == "embedded"
        and sub_plan.kind == "text"
    )
    result.subtitle_expected = expected_sub
    mov_text = [
        s
        for s in streams
        if s.get("codec_type") == "subtitle"
        and s.get("codec_name") == "mov_text"
    ]
    if expected_sub and not mov_text:
        result.subtitle_ok = False
        result.subtitle_detail = (
            "planned subtitle track missing from the encode output "
            "(no mov_text stream); reported, not gated"
        )
    elif not expected_sub and mov_text:
        result.subtitle_ok = False
        result.subtitle_detail = (
            f"unexpected mov_text stream(s) in the output ({len(mov_text)}); "
            "reported, not gated"
        )
    else:
        result.subtitle_ok = True
    if not result.geometry_ok:
        result.ok = False
        result.error = (
            f"geometry mismatch: planned {expected[0]}x{expected[1]}, "
            f"got {actual[0]}x{actual[1]}"
        )
        return result
    if full_decode:
        decode_cmd = decode_command if decode_command is not None else (
            build_decode_command(tools, plan.output_path)
        )
        # v0.4.0 (B.6): the runner may route the minutes-long full
        # decode through its heartbeat pump; the default stays the
        # plain blocking runner for every other caller.
        decode_run = decode_runner if decode_runner is not None else runner
        code, _out, err = decode_run(decode_cmd[0], decode_cmd[1:])
        if code != 0:
            result.ok = False
            result.error = (
                "full decode failed on the encode output: "
                f"{(err or '').strip()[:200] or 'no diagnostic'}"
            )
            return result
        result.full_decode_ok = True
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
    engine_name: str | None = None
    engine_note: str | None = None
    engine_simulated: bool = False
    engine_model: str | None = None
    engine_native_scale: int | None = None
    fail_open: bool = False
    fail_open_reason: str | None = None
    frame_clock: dict | None = None
    duration_gates: dict | None = None
    model_hashes_verified: bool | None = None
    progress: dict | None = None
    benchmark_constant: dict | None = None
    cancelled: bool = False
    sample_verdict: dict | None = None
    verification: VerificationResult | None = None
    manifest_path: str | None = None
    finished_at: float | None = None
    ok: bool | None = None
    error: str | None = None
    # v0.4.0 additive (design B.8): frame gates, resume facts, and the
    # retained rejected-mezzanine paths (set when a gate trip fired
    # after reassembly; carried from the engine either through a
    # successful EngineResult or through the UpscaleError's
    # ``rejected_mezzanine`` attribute on the fail-open path).
    frame_gates: dict | None = None
    resume: dict | None = None
    rejected_mezzanine: dict | None = None
    # v0.4.1 additive (design §B.7): the subtitles phase outcome —
    # status ('carried' | 'ocr' | 'none' | 'failed-open'), the selected
    # plan echo, OCR facts, sidecar/embed paths, and the honest
    # warnings. None until the phase runs; absent = older consumer
    # unaffected (schema v2 additive convention).
    subtitles: dict | None = None

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
            "engine_name": self.engine_name,
            "engine_note": self.engine_note,
            "engine_simulated": self.engine_simulated,
            "engine_model": self.engine_model,
            "engine_native_scale": self.engine_native_scale,
            "fail_open": self.fail_open,
            "fail_open_reason": self.fail_open_reason,
            "frame_clock": self.frame_clock,
            "duration_gates": self.duration_gates,
            "model_hashes_verified": self.model_hashes_verified,
            "progress": self.progress,
            "benchmark_constant": self.benchmark_constant,
            "cancelled": self.cancelled,
            "sample_verdict": self.sample_verdict,
            "verification": self.verification.to_dict() if self.verification else None,
            "manifest_path": self.manifest_path,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "error": self.error,
            "frame_gates": self.frame_gates,
            "resume": self.resume,
            "rejected_mezzanine": self.rejected_mezzanine,
            "subtitles": self.subtitles,
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


# v0.4.7 (success cleanup): the log-root artifacts a SUCCESSFUL run
# leaves behind that are pure scratch. The log and manifest are the
# run's durable record ("what happened" evidence) and never cleanup
# candidates; the engine workdir is engine_ncnn's own retention
# contract; the OCR workdir holds subtitle intermediates already
# copied into the retained .ocr/ diagnostic kit. Cleanup is
# namespaced per run-id (no globbing), so a concurrent run under the
# same log root can never lose its own artifacts.
_SUCCESS_SCRATCH_EXTENSIONS = (
    ".prepared.mkv",
    ".engine-work",
    ".ocr-work",
)


def _iter_run_scratch_artifacts(run_id: str, log_root: Path):
    for ext in _SUCCESS_SCRATCH_EXTENSIONS:
        candidate = log_root / f"{run_id}{ext}"
        if candidate.is_dir():
            yield candidate
        elif candidate.is_file():
            yield candidate


def cleanup_run_artifacts(
    run_id: str, log_root: str, *, log_line=None
) -> list[str]:
    """Delete one successful run's scratch artifacts; return what was removed.

    v0.4.7 (Eric: "when a run is completed successfully we need to ensure
    the files used for the conversion are properly removed"): after the
    run completes verified, the scratch (the prepared mezzanine, the
    engine's frame/chunk workdir, the OCR intermediates) is removed so a
    finished job does not leave gigabytes of hidden disk usage behind.

    Retention stays exactly as designed:
    - the run log and manifest are KEPT (the durable record);
    - nothing is deleted for a failed/cancelled run (clause-e retention
      contract) -- the caller only invokes this on the success path;
    - caches are NOT temp (benchmark caches under ``~/.vidlift`` stay);
    - sidecar .srt files and the .ocr/ diagnosis kit live beside the
      OUTPUT and are never touched here.

    Every delete failure is logged as a WARNING (via ``log_line``) and
    swallowed: cleanup must never convert a successful run into a
    failure; best effort only.
    """
    removed: list[str] = []
    root = Path(log_root)
    for candidate in _iter_run_scratch_artifacts(run_id, root):
        try:
            if candidate.is_dir():
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
            removed.append(str(candidate))
            if log_line is not None:
                log_line(f"cleanup run artifact: {candidate}")
        except OSError as exc:
            if log_line is not None:
                log_line(
                    f"WARNING run artifact cleanup could not remove "
                    f"{candidate}: {exc}"
                )
    return removed


def _sha256_file(path: str | Path) -> str:
    digest = sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def replace_source_with_output(
    source_path: str,
    output_path: str,
    *,
    log_line=None,
) -> str:
    """Delete the source and move the verified output into its place.

    v0.4.7 (Eric: "I want an option to have it delete and replace the
    source file when the upscale is complete"): strictly opt-in, called
    only AFTER ``verify_output`` passed, never automatically.

    Guardrails:
    - the output must already exist on disk (verify checked it);
    - never when the output IS the source path (nothing to replace);
    - same filesystem -> one atomic ``os.replace`` (rename(2): either
      the old file is replaced or the call errs);
    - cross filesystem -> copy first, verify the copy by SHA-256, and
      only then delete the original output copy -- a verified copy
      never destroys the only good data;
    - failures raise :class:`RunError` -- the runner catches it and
      converts to a logged warning so a replace failure can never fail
      a successful upscale.

    Returns the path the output now occupies (the source's original
    path). The output's former path may then no longer hold a file
    when the source and output directories differ.
    """
    source = Path(source_path)
    output = Path(output_path)
    if not source.is_file():
        raise RunError(
            f"replace-source refused: source is gone: {source}"
        )
    if not output.is_file():
        raise RunError(
            f"replace-source refused: verified output is missing: {output}"
        )
    if os.path.normcase(os.path.abspath(str(source))) == os.path.normcase(
        os.path.abspath(str(output))
    ):
        if log_line is not None:
            log_line(
                "replace-source: skipped (source path == output path)"
            )
        return str(source)
    if os.stat(str(source)).st_dev == os.stat(str(output)).st_dev:
        try:
            os.replace(str(output), str(source))
        except OSError as exc:
            raise RunError(
                f"replace-source failed to move the output over the "
                f"source ({source}): {exc}"
            ) from exc
    else:
        try:
            shutil.copy2(str(output), str(source))
            if _sha256_file(output) != _sha256_file(source):
                raise RunError(
                    "replace-source: the copied file at "
                    f"{source} does not hash-match the verified output; "
                    "keeping the verified original and the source"
                )
        except OSError as exc:
            raise RunError(
                f"replace-source cross-filesystem copy into {source} "
                f"failed: {exc}"
            ) from exc
        # The copy hash-verified: the file at the source path is now the
        # good data, so only the former output copy gets removed.
        try:
            output.unlink()
        except OSError as exc:
            if log_line is not None:
                log_line(
                    f"WARNING replace-source: the verified output copy at "
                    f"{output} could not be removed: {exc}"
                )
    if log_line is not None:
        log_line(f"replace-source: output now at {source}")
    return str(source)


def _apply_subtitle_ocr_preference(
    plan: PlannedFile, *, enabled: bool
) -> PlannedFile:
    """Remove only bitmap OCR work when the user disables that feature."""
    if (
        not enabled
        and plan.subtitle_plan is not None
        and plan.subtitle_plan.ocr_required
    ):
        return replace(
            plan,
            subtitle_plan=None,
            subtitle_note="bitmap subtitle OCR disabled; no subtitles carried",
        )
    return plan


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
    engine_request: str = "auto",
    realesrgan: str | None = None,
    realesrgan_models: str | None = None,
    log_dir: str | None = None,
    cancel: Callable[[], bool] | None = None,
    sample_classify: bool = True,
    state_box: dict | None = None,
    subtitle_ocr: bool = True,
    replace_source: bool = False,
) -> RunResult:
    """Run one file through the full pipeline and return its state.

    Phase order is fixed: probe, plan, prepare, upscale, encode,
    verify. Every external command goes through ``command_runner``
    (subprocess by default; fakes in tests record command lines and
    simulate tool effects). ``tools`` bypasses discovery for callers
    that already resolved binaries (the CLI does, honoring
    --ffmpeg/--ffprobe); leaving it None resolves via find_tools.
    ``engine`` bypasses selection for callers that already built an
    adapter (tests, the bridge); otherwise ``engine_request`` selects:
    ``auto`` (the default) uses the real ncnn engine when its binary,
    models, and a lazy GPU probe frame succeed, else falls to the stub
    with a note saying exactly why; ``stub`` forces the stub; ``ncnn``
    forces the real engine and turns resolution failure into a named
    missing-tool error. An :class:`UpscaleError` from the engine fails
    OPEN (clause d): the run completes with a synchronized
    original-resolution encode, the manifest stating what shipped and
    why. :class:`EngineCancelled` closes the run as cancelled, never
    as a failure.

    v0.4.7: on a VERIFIED run (never on failure/cancel), the run's
    scratch artifacts under ``log_dir``/``.vidlift-runs`` (prepared
    mezzanine, engine workdir, OCR workdir) are cleaned up
    best-effort; cleanup failures log a warning and keep the success.
    ``replace_source`` (strictly opt-in) moves the verified output over
    the source path afterwards; a replace failure logs a warning and
    keeps the success.
    """
    if tools is None:
        tools = find_tools()
    run_cmd = command_runner or _subprocess_runner
    state = RunState(file_path=file_path)
    if state_box is not None:
        # The bridge's live-status seam: the state object IS the
        # observability contract (its to_dict() is what status
        # reports), so callers that watch a run while it executes
        # receive the very object the runner mutates. Additive and
        # optional; nothing else changes.
        state_box["state"] = state

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

    def run_command(
        phase_name: str, command: list[str], *, heartbeat: bool = False,
        on_touch: Callable[[float], None] | None = None,
    ) -> None:
        state.commands.append(list(command))
        log(f"{phase_name} command: {' '.join(command)}")
        if heartbeat:
            # B.6: long commands opt in — encode and the full-decode
            # verify can run tens of minutes; the pump keeps the
            # state's last_activity true and the log tail alive.
            code, stdout, stderr = run_with_heartbeat(
                run_cmd,
                command[0],
                command[1:],
                touch=state.touch,
                log_line=log,
                phase_name=phase_name,
                on_touch=on_touch,
            )
        else:
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
        original_plan = plan
        plan = _apply_subtitle_ocr_preference(plan, enabled=subtitle_ocr)
        if plan is not original_plan:
            log("subtitle OCR disabled by user; bitmap subtitles will not be carried")
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
        sample_chain = None
        sample_verdict = None
        if plan.report.interlaced_flagged and sample_classify:
            # P4: confirm the flag with a 60-second idet sample pass
            # BEFORE filtering (v0.2.19 Section 3.3: classify first,
            # deinterlace correctly; no blind deinterlace, ever). The
            # command goes through the same seam (recorded, tail-able);
            # an undecidable sample falls back to the flag heuristic.
            from .classify_sample import (
                chain_for,
                classify_interlace_sample,
                build_idet_command,
            )

            idet_cmd = build_idet_command(
                tools.ffmpeg, file_path
            )
            state.commands.append(idet_cmd)
            log(f"sample classify command: {' '.join(idet_cmd)}")
            idet_result = run_cmd(idet_cmd[0], idet_cmd[1:])
            state.touch()
            sample_verdict = classify_interlace_sample(
                file_path,
                ffmpeg=tools.ffmpeg,
                result=idet_result,
            )
            if sample_verdict.kind is not None:
                sample_chain = chain_for(sample_verdict, report)
                log(
                    f"sample classification: {sample_verdict.kind} "
                    f"({sample_verdict.detail})"
                )
            else:
                log(
                    "sample classification undecidable; using the "
                    f"interlace flag heuristic: {sample_verdict.detail}"
                )
        elif plan.report.interlaced_flagged:
            log("sample classification disabled; using the interlace flag heuristic")
        state.sample_verdict = (
            {
                "kind": sample_verdict.kind,
                "detail": sample_verdict.detail,
                "single": sample_verdict.single,
                "sample_seconds": sample_verdict.sample_seconds,
            }
            if sample_verdict is not None
            else None
        )
        prepared_path = str(log_root / f"{run_id}.prepared.mkv")
        state.prepared_path = prepared_path
        run_command(
            PHASE_PREPARE,
            build_prepare_command(
                tools, plan, prepared_path, sample_chain=sample_chain
            ),
        )
        if not os.path.isfile(prepared_path):
            raise RunError(f"prepare phase produced no file at {prepared_path}")
        end_phase(phase, note=f"prepared={prepared_path}")

        # ---- upscale ----
        phase = begin_phase(PHASE_UPSCALE)

        def record_progress(frames_done: int, frames_total: int) -> None:
            # B.6: merge, never replace — the "step" label set by
            # record_step must survive frame-progress updates.
            progress = dict(state.progress or {})
            progress.update(
                {"frames_done": frames_done, "frames_total": frames_total}
            )
            state.progress = progress
            state.touch()

        def record_step(label: str) -> None:
            # B.6: the step-label seam — names WHAT is running during
            # the previously-blind windows (concat, gate probes,
            # encode, verify) and touches the state so a long phase
            # never reads as stuck.
            #
            # v0.4.5 (progress-honesty §3b, D-count/D-eta): the seam
            # parses ADDITIVE structured facts the adapter embeds in
            # the label after a " — " separator (its contract):
            #   chunk_index/chunk_total — the promoted chunk counters
            #   phase_note — a short honest note for sub-stages that
            #     have no measurable denominator (schema-v2 additive,
            #     same convention as v0.4.1's subtitles block)
            # Plain labels (encode/verify/concat/gate probes) parse to
            # no facts and behave exactly as before.
            progress = dict(state.progress or {})
            progress["step"] = label
            if " — " in label:
                head, sep, rest = label.partition(" — ")
                progress["step"] = head
                progress["phase_note"] = rest.strip()
                idx: int | None = None
                total: int | None = None
                match = re.match(r"^chunk (\d+)/(\d+)$", head.strip())
                if match:
                    idx = int(match.group(1))
                    total = int(match.group(2))
                else:
                    note_match = re.match(
                        r"^upscaling chunk (\d+)/(\d+) \(", rest.strip()
                    )
                    if note_match:
                        idx = int(note_match.group(1))
                        total = int(note_match.group(2))
                if idx is not None and total is not None:
                    progress["chunk_index"] = idx
                    progress["chunk_total"] = total
                if re.match(
                    r"^reassembly chunk \d+/\d+ \(\d+/\d+ frames", rest.strip()
                ):
                    progress["phase_note"] = rest.strip()
            state.progress = progress
            state.touch()

        mezzanine_for_encode: str | None = None
        frames_done = 0
        try:
            if engine is not None:
                active_engine = engine
                selection_note: str | None = None
            else:
                from .engine_ncnn import select_engine

                active_engine, selection_note = select_engine(
                    engine_request,
                    binary=realesrgan,
                    models_dir=realesrgan_models,
                )
            state.engine_name = type(active_engine).__name__
            if selection_note:
                state.engine_note = selection_note
            log(
                f"engine selected: {state.engine_name}"
                + (f" ({selection_note})" if selection_note else "")
            )
            engine_result = active_engine.run(
                prepared_path, plan, tools,
                cancel=cancel, on_progress=record_progress,
                on_step=record_step,
            )
        except UpscaleError as exc:
            # FAIL OPEN (clause d): the job ships the synchronized
            # original-resolution re-encode of the prepared file at
            # planned bitrate/geometry -- a real, verified 720p HEVC
            # file with NO AI pass -- and the manifest states exactly
            # what shipped and why. Never a desynced upscale, never
            # no output, never a silent AI-pass claim. (Selection-time
            # UpscaleErrors fail open too: a substituted model is
            # named as the reason, never run silently.)
            log(f"WARNING: engine failed, failing open: {exc}")
            if state.engine_name is None:
                state.engine_name = "NcnnVulkanEngine"
            state.fail_open = True
            state.fail_open_reason = str(exc)
            state.engine_note = f"fail open: {exc}"
            state.engine_simulated = True  # shipped pixels are NOT AI
            state.engine_model = None
            state.engine_native_scale = None
            state.mezzanine_path = prepared_path
            mezzanine_for_encode = prepared_path
            # v0.4.0 (B.5/B.8): when the adapter retained a rejected
            # mezzanine + diagnostic, those paths ride into the state
            # (and manifest) through the exception — no signature change.
            state.rejected_mezzanine = getattr(
                exc, "rejected_mezzanine", None
            )
        else:
            state.mezzanine_path = engine_result.mezzanine_path
            state.engine_note = selection_note or engine_result.note
            state.engine_simulated = engine_result.simulated
            state.engine_model = engine_result.engine_model
            state.engine_native_scale = engine_result.engine_native_scale
            state.frame_clock = engine_result.frame_clock
            state.duration_gates = engine_result.duration_gates
            state.model_hashes_verified = engine_result.model_hashes_verified
            state.frame_gates = engine_result.frame_gates
            state.resume = engine_result.resume
            state.rejected_mezzanine = engine_result.rejected_mezzanine
            mezzanine_for_encode = engine_result.mezzanine_path
            frames_done = engine_result.frames_done
        log(
            f"engine: {state.engine_name}"
            f"{' (simulated, no Real-ESRGAN pass)' if state.engine_simulated else ''}"
        )
        if state.fail_open:
            log(f"fail open reason: {state.fail_open_reason}")
        end_phase(
            phase,
            note=f"frames={frames_done} mezzanine={mezzanine_for_encode}",
        )

        # ---- encode ----
        phase = begin_phase(PHASE_ENCODE)
        record_step("encode")

        def _encode_hint(elapsed: float) -> None:
            # B.6: an elapsed-time hint, honestly labeled — the pump is
            # touch+log only (B.10(4) discipline), no stdout parsing.
            # v0.4.5 (§3b): the encode heartbeat touches last_activity
            # too, so a 65-minute encode never reads as stuck.
            state.touch()
            progress = dict(state.progress or {})
            progress["step"] = "encode"
            progress["encode_seconds"] = elapsed
            state.progress = progress

        run_command(
            PHASE_ENCODE,
            build_encode_command(tools, plan, mezzanine_for_encode),
            heartbeat=True,
            on_touch=_encode_hint,
        )
        if not os.path.isfile(plan.output_path):
            raise RunError(f"encode phase produced no file at {plan.output_path}")
        end_phase(phase, note=f"output={plan.output_path}")

        # ---- verify ----
        phase = begin_phase(PHASE_VERIFY)
        record_step("verify")
        verify_cmd = build_verification_command(tools, plan.output_path)
        decode_cmd = build_decode_command(tools, plan.output_path)
        state.commands.append(verify_cmd)
        state.commands.append(decode_cmd)
        log(f"verify command: {' '.join(verify_cmd)}")
        log(f"verify decode command: {' '.join(decode_cmd)}")

        def _decode_runner(binary: str, args: list[str]) -> tuple[int, str, str]:
            # B.6: the full decode (minutes on a feature file) runs
            # through the heartbeat pump; the cheap ffprobe geometry
            # check keeps the plain blocking path.
            def _decode_hint(elapsed: float) -> None:
                # v0.4.5 (§3b): the decode heartbeat touches
                # last_activity for the same stickiness reason.
                state.touch()
                progress = dict(state.progress or {})
                progress["step"] = "verify"
                progress["decode_seconds"] = elapsed
                state.progress = progress

            return run_with_heartbeat(
                run_cmd,
                binary,
                args,
                touch=state.touch,
                log_line=log,
                phase_name=PHASE_VERIFY,
                on_touch=_decode_hint,
            )

        verification = verify_output(
            tools,
            plan,
            runner=run_cmd,
            command=verify_cmd,
            decode_command=decode_cmd,
            decode_runner=_decode_runner,
        )
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

        # ---- subtitles (v0.4.1, design §B.4/B.8) ----
        # The phase is optional by presence: no selected track -> the
        # phase records 'skipped' and ends in milliseconds. Every
        # failure inside is fail-open (§B.8): a subtitle problem NEVER
        # fails the run, blocks the upscale, or converts to a RunError
        # — the honest warning names what happened and what to install.
        phase = begin_phase(PHASE_SUBTITLES)
        subtitles_block: dict = {"status": "none"}
        sub_plan = getattr(plan, "subtitle_plan", None)
        if sub_plan is None:
            subtitles_block["status"] = "none"
            end_phase(phase, note="skipped (no subtitle track selected)")
        else:
            # Plan-time decision, run-time execution: the selection was
            # made in build_file_plan; here we only execute it.
            subtitles_block["selected"] = {
                "source": sub_plan.source,
                "stream_index": sub_plan.stream_index,
                "sidecar_path": sub_plan.sidecar_path,
                "kind": sub_plan.kind,
                "codec_name": sub_plan.codec_name,
                "lang": sub_plan.lang,
                "ocr_required": sub_plan.ocr_required,
            }
            warnings: list[str] = []
            sidecar_target = sidecar_srt_path(plan.output_path)
            try:
                if sub_plan.kind == "text" and sub_plan.source == "embedded":
                    # Embedded text was carried IN the encode (mov_text,
                    # selected-index map). Extract the same stream as an
                    # .srt sidecar post-encode (the KEEP disposition,
                    # §B.3): ffmpeg transcodes the stream to SRT.
                    record_step("subtitle-sidecar-extract")
                    extract_cmd = [
                        tools.ffmpeg,
                        "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                        "-i", report.path,
                        "-map", f"0:{sub_plan.stream_index}",
                        "-c:s", "srt",
                        sidecar_target,
                    ]
                    run_command(PHASE_SUBTITLES, extract_cmd)
                    if os.path.isfile(sidecar_target):
                        subtitles_block["status"] = "carried"
                        subtitles_block["sidecar_path"] = sidecar_target
                        subtitles_block["embedded"] = {
                            "codec": "mov_text",
                            "language_tag": mux_language(sub_plan.lang),
                        }
                    else:
                        raise SubtitleOcrError(
                            "text sidecar extraction produced no file"
                        )
                elif sub_plan.kind == "text" and sub_plan.source == "sidecar":
                    # Adopted .srt sidecar (§B.3): copy to the output
                    # sidecar, mux mov_text into the finished MP4 with
                    # the temp-then-replace atomicity.
                    shutil.copyfile(sub_plan.sidecar_path, sidecar_target)
                    record_step("subtitle-mux")
                    mux_srt_into_mp4(
                        plan.output_path,
                        sidecar_target,
                        language=sub_plan.lang,
                        ffmpeg=tools.ffmpeg,
                        runner=run_cmd,
                    )
                    subtitles_block["status"] = "carried"
                    subtitles_block["sidecar_path"] = sidecar_target
                    subtitles_block["embedded"] = {
                        "codec": "mov_text",
                        "language_tag": mux_language(sub_plan.lang),
                    }
                else:
                    # The bitmap OCR path (embedded or adopted sidecar).
                    # Engine resolution is explicit per platform (§A.3):
                    # Windows = seconv payload; Linux = direct tesseract;
                    # missing tool = fail-open warning, never a refusal.
                    engine = "seconv" if os.name == "nt" else "tesseract"
                    try:
                        if engine == "seconv":
                            seconv_binary = resolve_seconv()
                        else:
                            tesseract_binary = resolve_tesseract()
                    except SubtitleOcrError as exc:
                        raise SubtitleOcrError(
                            f"no usable OCR engine on this platform: {exc}"
                        ) from exc
                    ocr_kwargs: dict = {}
                    if engine == "seconv":
                        ocr_kwargs["seconv_binary"] = seconv_binary
                    else:
                        ocr_kwargs["tesseract_binary"] = tesseract_binary
                    ocr_source = (
                        report.path if sub_plan.source == "embedded"
                        else sub_plan.sidecar_path
                    )
                    ocr_workdir = str(log_root / f"{run_id}.ocr-work")
                    ocr_result = run_subtitle_ocr(
                        sub_plan,
                        ocr_source,
                        plan.output_path,
                        ffmpeg=tools.ffmpeg,
                        runner=run_cmd,
                        workdir=ocr_workdir,
                        on_step=record_step,
                        on_progress=lambda done, total: record_progress(
                            done, total
                        ),
                        **ocr_kwargs,
                    )
                    subtitles_block["status"] = "ocr"
                    subtitles_block["sidecar_path"] = ocr_result.srt_path
                    subtitles_block["ocr"] = {
                        "engine": ocr_result.engine,
                        "language": ocr_result.language,
                        "cue_count": ocr_result.cue_count,
                        "auto_resolved": ocr_result.auto_resolved_note,
                        "notes": list(ocr_result.notes),
                    }
                    record_step("subtitle-mux")
                    mux_srt_into_mp4(
                        plan.output_path,
                        ocr_result.srt_path,
                        language=sub_plan.lang,
                        ffmpeg=tools.ffmpeg,
                        runner=run_cmd,
                    )
                    subtitles_block["embedded"] = {
                        "codec": "mov_text",
                        "language_tag": mux_language(sub_plan.lang),
                    }
                    for note in ocr_result.notes:
                        log(f"subtitles: {note}")
                        warnings.append(note)
            except SubtitleOcrError as exc:
                # FAIL OPEN (§B.8): the run ships video+audio; the
                # warning names the failure; the manifest records
                # failed-open. The sidecar, when one was written, is
                # kept (mux-failure wording distinguishes 'embed
                # failed; sidecar kept' from 'no subtitles').
                reason = str(exc)
                log(f"WARNING: subtitles failed open: {reason}")
                warnings.append(reason)
                subtitles_block["status"] = "failed-open"
                subtitles_block["reason"] = reason
                if os.path.isfile(sidecar_target):
                    subtitles_block["sidecar_path"] = sidecar_target
                    subtitles_block["sidecar_kept"] = True
                    warnings.append(
                        "embed failed; sidecar kept next to the output"
                    )
            subtitles_block["warnings"] = warnings
            state.subtitles = subtitles_block
            end_phase(
                phase,
                note=subtitles_block["status"],
            )
        state.subtitles = subtitles_block

        # ---- manifest + finish ----
        state.ok = verification.ok
        state.finished_at = time.time()
        if not verification.ok:
            state.error = f"verification failed: {verification.error}"
        manifest = _build_manifest(state, tools, report, plan, verification)
        manifest_path = log_root / f"{run_id}.manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        state.manifest_path = str(manifest_path)
        log(f"manifest: {manifest_path}")
        if not verification.ok:
            raise RunError(state.error)
        # v0.4.7 (success cleanup, Eric): the run is verified; remove its
        # scratch. Retention on failure/cancel is untouched (the except
        # blocks below never reach this code). Best-effort: a cleanup
        # failure logs a WARNING inside cleanup_run_artifacts and keeps
        # the success; the log and manifest are never candidates.
        cleanup_run_artifacts(run_id, str(log_root), log_line=log)
        if replace_source:
            try:
                replaced_path = replace_source_with_output(
                    file_path, plan.output_path, log_line=log
                )
                state.output_path = replaced_path
            except (RunError, OSError) as exc:
                # Guardrail (d): a replace failure must never crash the
                # caller or fail the verified run; the upscale output
                # stays exactly where the plan put it.
                log(f"WARNING: replace-source failed; keeping both files: {exc}")
        state.current_phase = PHASE_DONE
        log("run complete")
        return RunResult(
            state=state,
            plan=plan,
            verification=verification,
            manifest_path=str(manifest_path),
        )
    except EngineCancelled as exc:
        # A cancel is NOT a failure (clause d): the state records the
        # cancelled outcome, the manifest is written with
        # outcome "cancelled", and EngineCancelled propagates so the
        # caller (CLI/bridge) reports a cancel.
        state.finished_at = time.time()
        state.ok = False
        state.cancelled = True
        state.error = str(exc)
        if state.log is not None:
            log(f"cancelled in phase {state.current_phase}: {exc}")
        exc.state = state  # type: ignore[attr-defined]
        _write_failure_manifest(state, tools)
        raise
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
) -> dict:
    """Per-run artifact manifest (roadmap item 2): inputs, plan,
    command lines, outputs, timings, verification.

    Schema v2 (design real-engine-adapter.md Section 11) is ADDITIVE:
    every v1 field is unchanged; the new engine-honesty fields state
    exactly what shipped and why (clause d). ``engine`` names the
    ATTEMPTED engine even under fail-open; ``engine_simulated``
    describes the SHIPPED PIXELS (stub run: true; real engine clean
    pass: false; real engine fail-open: true + ``fail_open_reason``).
    """
    return {
        "vidlift_version": _vidlift_version(),
        "run": {
            "input_file": state.file_path,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "ok": state.ok,
            "error": state.error,
            "outcome": "succeeded" if state.ok else "failed",
            "engine": state.engine_name,
            "engine_simulated": state.engine_simulated,
            "engine_note": state.engine_note,
            "engine_model": state.engine_model,
            "engine_native_scale": state.engine_native_scale,
            "fail_open": state.fail_open,
            "fail_open_reason": state.fail_open_reason,
            "frame_clock": state.frame_clock,
            "model_hashes_verified": state.model_hashes_verified,
            "duration_gates": state.duration_gates,
            "benchmark_constant": state.benchmark_constant,
            "cancelled": state.cancelled,
            "sample_verdict": state.sample_verdict,
            "frame_gates": state.frame_gates,
            "resume": state.resume,
            "rejected_mezzanine": state.rejected_mezzanine,
            # v0.4.1 additive (design §B.7): the subtitles phase outcome —
            # status/carried/ocr/none/failed-open, selected echo, OCR
            # facts, sidecar/embed paths, warnings. Absent = pre-0.4.1
            # consumer; additive schema v2, no bump (the doc pins this:
            # v3 is only warranted for breaking changes; there are none).
            "subtitles": state.subtitles,
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
            # v0.4.1 additive (design §B.7): the structured subtitle plan
            # echo and the ASS style-loss fact. Every key optional;
            # v1/v2-unextended consumers unaffected.
            "subtitle_plan": (
                {
                    "source": plan.subtitle_plan.source,
                    "stream_index": plan.subtitle_plan.stream_index,
                    "sidecar_path": plan.subtitle_plan.sidecar_path or None,
                    "kind": plan.subtitle_plan.kind,
                    "codec_name": plan.subtitle_plan.codec_name,
                    "lang": plan.subtitle_plan.lang,
                    "ocr_required": plan.subtitle_plan.ocr_required,
                }
                if plan.subtitle_plan is not None
                else None
            ),
            "subtitle_style_loss": plan.subtitle_style_loss,
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
                "outcome": "cancelled" if state.cancelled else "failed",
                "error_phase": state.current_phase,
                "engine": state.engine_name,
                "engine_simulated": state.engine_simulated,
                "engine_note": state.engine_note,
                "engine_model": state.engine_model,
                "engine_native_scale": state.engine_native_scale,
                "fail_open": state.fail_open,
                "fail_open_reason": state.fail_open_reason,
                "frame_clock": state.frame_clock,
                "model_hashes_verified": state.model_hashes_verified,
                "duration_gates": state.duration_gates,
                "cancelled": state.cancelled,
                "frame_gates": state.frame_gates,
                "resume": state.resume,
                "rejected_mezzanine": state.rejected_mezzanine,
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