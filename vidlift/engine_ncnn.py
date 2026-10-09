"""VidLift's Real-ESRGAN ncnn/Vulkan engine adapter (file-first).

A disciplined port of bluray-to-mp4's validated v0.2.20-v0.2.29 upscale
path (``bluray_to_mp4/upscale.py``, read-only reference; the import is
deliberately NOT taken — its module graph pulls disc-shaped
``makemkv``/``binaries`` vocabulary across the engine seam, which
architecture.md forbids). Where this module deviates from or narrows
the sibling, the deviation is stated in a comment citing the design
section. Binding design: ``docs/designs/real-engine-adapter.md``.

Implements roadmap "Next" item 1 acceptance clauses verbatim:
  a. frame-clock discipline (strict avg -> r -> FAIL resolution, never
     a default) + duration sanity gates P and M with the
     max(0.5 s, 0.01%) tolerance and the unknown-reference refusal;
  b. explicit tiling derived from the MEASURED frame extent
     (extent+64, ceil to 64, floor 512); ``-t 0`` is unreachable;
  d. ``UpscaleError`` (fail open) and ``EngineCancelled`` (a cancel,
     not a failure) raised at the runner seam;
  g. model artifacts consumed with sha256 verified at first use per
     process, BEFORE any frame is extracted.

Chunk flow (design Section 7): extract wall-time-addressed PNG frames
per chunk (``-ss/-t`` on the PREPARED file — VidLift's prepare phase
already normalized frames, so extraction is a decode+copy; the sibling
applied its prepare filter here because ITS input was the raw rip),
run the engine once per chunk, reassemble each chunk's output frames
into an ffv1 segment stamped with the prepared file's probed clock
(the exact rational string, never a default), concat the segments,
gate the mezzanine duration against the prepared duration, and hand
the runner the mezzanine path. The workdir is kept on failure/cancel
for clause-e triage and deleted on success.
"""

from __future__ import annotations

import math
import os
import shlex
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .plan import PlannedFile
from .probe import parse_probe
from .run import (
    EngineAdapter,
    EngineCancelled,
    EngineResult,
    MissingToolError,
    ToolBox,
    UpscaleError,
)
from .tools_realesrgan import (
    resolve_realesrgan,
    resolve_realesrgan_models,
    verify_model_artifacts,
)

__all__ = [
    "CHUNK_SECONDS",
    "MIN_CHUNK_SECONDS",
    "EngineInvocation",
    "MODEL_TABLE",
    "ModelSpec",
    "NcnnVulkanEngine",
    "PNGExtent",
    "duration_gate",
    "gate_tolerance",
    "gpu_probe_frame",
    "probe_duration_seconds",
    "probe_frame_clock",
    "select_engine",
    "tile_argument",
]

# Runner seam for external commands (same shape as run.Runner /
# probe.Runner): run(binary, args) -> (returncode, stdout, stderr).
# It carries ffmpeg extraction/assembly and ffprobe clock/duration
# probes; the engine process itself needs the live drain/cancel
# discipline and runs through ``_run_process`` (Popen), except in
# tests where the same injected runner fakes the engine call too.
Runner = Callable[[str, list[str]], "tuple[int, str, str]"]

CHUNK_SECONDS = 120  # default; >= 10; env override: VIDLIFT_CHUNK_SECONDS
MIN_CHUNK_SECONDS = 10

# Duration gate tolerance (clause a; port of v0.2.27's measured values):
# frame-scale container rounding plus a tiny proportional allowance,
# rejecting any viewer-visible accumulation. The old max(5 s, 2%) gate
# caught gross mismatch but allowed Mamma Mia's 15.050 s feature-length
# drift (0.22% — under the old 2% gate).
GATE_TOLERANCE_SECONDS = 0.5
GATE_TOLERANCE_FRACTION = 0.0001


@dataclass(frozen=True)
class ModelSpec:
    """One pinned engine profile: the ONLY source of ``-n``/``-s`` (clause b)."""

    name: str
    native_scale: int


# The model registry (design Section 3). x2plus is a TRUE native-2x
# RRDBNet (pixel_unshuffle head, verified in the x2plus
# VERIFICATION-REPORT.md): ``-s 2`` IS its native mode — this is NOT the
# v0.2.25 corruption case (x4plus, a native-4x net, at -s 2). x4plus
# never appears in any VidLift code path; it cannot be constructed
# through this table. The anime profile row is added when its mode
# option ships (out of scope this milestone; hash pinning identical).
MODEL_TABLE: dict[str, ModelSpec] = {
    "live-action": ModelSpec(name="realesrgan-x2plus", native_scale=2),
}


def gate_tolerance(reference_s: float) -> float:
    """max(0.5 s, 0.01%) — the gate tolerance for a known reference."""
    return max(GATE_TOLERANCE_SECONDS, GATE_TOLERANCE_FRACTION * float(reference_s))


def duration_gate(
    *,
    label: str,
    reference_s: float | None,
    actual_s: float | None,
    actual_name: str,
    reference_name: str,
) -> dict:
    """One duration sanity gate (clause a; design Section 4.2).

    Returns the manifest record ``{reference_s, actual_s, tolerance_s,
    ok}`` keyed by ``label`` (field names per manifest schema v2), and
    raises :class:`UpscaleError` naming both durations and the
    tolerance when the deviation exceeds it. An unknown reference
    (``None`` or <= 0) FAILS the gate — ported from v0.2.29: a gate
    with a zero reference accepted anything. Callers route the failure
    through fail-open, never a dead job.
    """
    if reference_s is None or float(reference_s) <= 0:
        raise UpscaleError(
            f"{label} duration gate: {reference_name} duration unknown; "
            "refusing to trust a gate with no reference"
        )
    reference = float(reference_s)
    actual = float(actual_s) if actual_s is not None else 0.0
    tolerance = gate_tolerance(reference)
    deviation = abs(actual - reference)
    record = {
        label: {
            f"{actual_name}_s": actual,
            f"{reference_name}_s": reference,
            "tolerance_s": tolerance,
            "ok": True,
        }
    }
    if deviation > tolerance:
        record[label]["ok"] = False
        raise UpscaleError(
            f"{label} duration gate: {actual_name} {actual:.3f}s vs "
            f"{reference_name} {reference:.3f}s (off by {deviation:.3f}s, "
            f"tolerance {tolerance:.3f}s); refusing a desynced output"
        )
    return record


def _positive_rate(raw: str) -> float | None:
    """Parse an ffprobe rational like '24000/1001' into a positive fps."""
    if not isinstance(raw, str) or not raw:
        return None
    num, sep, den = raw.partition("/")
    try:
        if sep:
            n, d = float(num), float(den)
        else:
            return float(raw)
    except ValueError:
        return None
    if d == 0 or n <= 0:
        return None
    return n / d


def probe_frame_clock(
    prepared_path: str,
    *,
    ffprobe: str,
    runner: Runner,
) -> dict:
    """Resolve the prepared file's frame clock (clause a; design Section 4.1).

    Strict order: ``avg_frame_rate_raw`` (if it parses to a positive
    rate) -> ``r_frame_rate_raw`` (same check) -> a named
    :class:`UpscaleError`. NEVER a default — no 25, no 30000/1001, no
    "assume source": the prepare phase may legitimately change the rate
    (bwdif at field rate today; IVTC later), so the adapter probes the
    file it will actually re-stamp. The clock is carried as the exact
    rational STRING ffprobe reported — float re-rendering is how drift
    gets invented — plus a parsed ``fps`` used only for frame-count
    math (never for re-stamping).
    """
    code, stdout, stderr = runner(
        ffprobe,
        ["-v", "error", "-show_streams", "-of", "json", prepared_path],
    )
    if code != 0:
        raise UpscaleError(
            f"no usable frame clock on prepared file {prepared_path}: "
            f"ffprobe failed: {(stderr or 'no diagnostic').strip()[:200]}"
        )
    try:
        probe = parse_probe(prepared_path, stdout)
    except Exception as exc:  # ProbeError; an unparseable clock is unusable
        raise UpscaleError(
            f"no usable frame clock on prepared file {prepared_path}: {exc}"
        ) from exc
    video = probe.video
    if video is None:
        raise UpscaleError(
            f"no usable frame clock on prepared file {prepared_path}: "
            "no video stream"
        )
    for source, raw in (
        ("avg_frame_rate", video.avg_frame_rate_raw),
        ("r_frame_rate", video.r_frame_rate_raw),
    ):
        rate = _positive_rate(raw)
        if rate is not None:
            return {
                "value": raw,  # the exact rational string, byte-identical
                "source": source,
                "fps": rate,
            }
    raise UpscaleError(
        f"no usable frame clock on prepared file {prepared_path}: neither "
        "avg_frame_rate nor r_frame_rate parses to a positive rate"
    )


def probe_duration_seconds(
    path: str, *, ffprobe: str, runner: Runner
) -> float | None:
    """The file's format duration via the seam; None when unusable."""
    code, stdout, _stderr = runner(
        ffprobe, ["-v", "error", "-show_format", "-of", "json", path]
    )
    if code != 0:
        return None
    try:
        return parse_probe(path, stdout).duration_seconds
    except Exception:
        return None


@dataclass(frozen=True)
class PNGExtent:
    """Measured PNG geometry from the 24-byte IHDR header (no decoder)."""

    width: int
    height: int

    @property
    def extent(self) -> int:
        return max(self.width, self.height)


def _png_size(path: Path) -> PNGExtent | None:
    """PNG header read: signature check plus IHDR width/height at bytes
    16..24. Port of the sibling's ``_png_size`` verbatim in rule."""
    try:
        with path.open("rb") as fh:
            header = fh.read(24)
    except OSError:
        return None
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    width, height = struct.unpack(">II", header[16:24])
    return PNGExtent(width, height)


def tile_argument(frame_extent: int, *, floor: int | None = None) -> int:
    """Explicit tile size from the MEASURED extent (clause b; design 6).

    ``requested = max(512, extent + 64)`` (one tile, frame plus margin),
    ``tile = ceil(requested / 64) * 64`` (driver-friendly multiple). An
    operator floor (env ``VIDLIFT_ENGINE_TILE``) is honored exactly as
    the sibling does — as a FLOOR only. ``-t 0`` is unreachable by
    construction, and the result is never smaller than the extent.
    """
    requested = max(512, int(frame_extent) + 64)
    if floor is not None and floor > 0:
        requested = max(requested, int(floor))
    tile = int(math.ceil(requested / 64) * 64)
    return max(tile, int(frame_extent))


@dataclass(frozen=True)
class EngineInvocation:
    """One recorded engine process command line (for tests and logs)."""

    argv: tuple[str, ...]
    chunk_index: int
    input_frames: int
    tile: int


# Lazy "can this machine even run it" probe cache (design Section 5):
# one probe frame per (binary, models_dir) per process — auto selection
# must not pay the probe cost for every run in a long-lived bridge.
_PROBE_CACHE: dict[tuple[str, str], tuple[bool, str]] = {}


def _tiny_png(size: int = 64) -> bytes:
    """A minimal valid solid-gray RGB PNG the real engine can decode.

    Written byte-by-byte (signature, IHDR, one zlib IDAT of filter-0
    scanlines, IEND) because PIL is deliberately not a dependency and
    the probe must run inside any packaged environment.
    """
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            len(data).to_bytes(4, "big")
            + tag
            + data
            + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes([120]) * (size * 3) for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def gpu_probe_frame(
    binary: str,
    models_dir: str,
    *,
    spec: ModelSpec | None = None,
    timeout_s: float = 180.0,
    run_fn=subprocess.run,
    workspace: Path | None = None,
) -> tuple[bool, str]:
    """Run one REAL tiny frame through the engine and verify its output.

    The auto-selection gate (design Section 5, the sibling's "can this
    machine even run it" pattern): the binary must decode the probe
    PNG, run the model, and emit the EXACT native-scale output geometry
    (64x64 -> 128x128 at scale 2). Cached per process per
    (binary, models_dir). Never raises — every failure is
    ``(False, reason)`` so auto selection can fall to the stub with
    the reason recorded (design r1: a bad binary fails here, not
    mid-run). """
    spec = spec or MODEL_TABLE["live-action"]
    key = (str(binary), str(models_dir))
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    tmp: Path | None = None
    ok, reason = False, ""
    try:
        if workspace is None:
            import tempfile

            tmp = Path(tempfile.mkdtemp(prefix="vidlift-engine-probe-"))
        else:
            tmp = workspace
        # The engine requires input/output of the SAME type (file+file or
        # dir+dir); the real chunk flow is dir+dir, so the probe mirrors it.
        in_dir = tmp / "in"
        out_dir = tmp / "out"
        in_dir.mkdir(exist_ok=True)
        out_dir.mkdir(exist_ok=True)
        (in_dir / "probe.png").write_bytes(_tiny_png())
        cmd = [
            binary,
            "-i", str(in_dir),
            "-o", str(out_dir),
            "-s", str(spec.native_scale),
            "-t", str(tile_argument(64)),
            "-m", str(models_dir),
            "-n", spec.name,
            "-f", "png",
        ]
        try:
            proc = run_fn(cmd, capture_output=True, text=True, timeout=timeout_s)
        except Exception as exc:  # OSError, TimeoutExpired, driver hangs
            ok, reason = False, f"probe frame could not run: {type(exc).__name__}: {exc}"
        else:
            if proc.returncode != 0:
                tail = (getattr(proc, "stderr", "") or "").strip()[-200:]
                ok, reason = (
                    False,
                    f"probe frame exited {proc.returncode}: {tail or 'no diagnostic'}",
                )
            else:
                out_png = out_dir / "probe.png"
                size = _png_size(out_png) if out_png.is_file() else None
                expected = 64 * spec.native_scale
                if size is None:
                    ok, reason = False, "probe frame produced no output PNG"
                elif (size.width, size.height) != (expected, expected):
                    ok, reason = (
                        False,
                        f"probe frame geometry {size.width}x{size.height} "
                        f"is not the exact {expected}x{expected} native-scale result",
                    )
                else:
                    ok, reason = True, (
                        f"probe frame ok (64x64 -> {expected}x{expected})"
                    )
    finally:
        if tmp is not None and workspace is None:
            shutil.rmtree(tmp, ignore_errors=True)
    _PROBE_CACHE[key] = (ok, reason)
    return _PROBE_CACHE[key]


def select_engine(
    request: str = "auto",
    *,
    binary: str | None = None,
    models_dir: str | None = None,
    tools: ToolBox | None = None,
) -> tuple[EngineAdapter, str | None]:
    """Resolve the ``--engine {auto,stub,ncnn}`` request (design Section 5).

    - ``stub``: the passthrough stub, forced.
    - ``ncnn``: forces the real engine; a resolution failure raises the
      named :class:`MissingToolError` (a setup problem), and a hash
      mismatch raises :class:`UpscaleError` (the run then fails open
      honestly per Section 9).
    - ``auto`` (the default): ncnn when the binary + models resolve AND
      the lazy GPU probe frame succeeds; anything else falls to the
      stub with a note saying exactly why (design r1: a bad binary
      fails the probe, never the run).

    Returns ``(engine, note)`` — ``note`` is the auto-fallback reason
    for the manifest's ``engine_note`` when auto lands on the stub.
    """
    from .run import StubEngine

    if request == "stub":
        return StubEngine(), None
    if request == "ncnn":
        binary_path = resolve_realesrgan(binary)
        models = resolve_realesrgan_models(binary_path, models_dir)
        verify_model_artifacts(models, MODEL_TABLE["live-action"].name)
        return (
            NcnnVulkanEngine(binary=binary_path, models_dir=models),
            None,
        )
    if request == "auto":
        try:
            binary_path = resolve_realesrgan(binary)
            models = resolve_realesrgan_models(binary_path, models_dir)
            verify_model_artifacts(models, MODEL_TABLE["live-action"].name)
            ok, reason = gpu_probe_frame(binary_path, models)
            if not ok:
                note = f"auto: stub engine ({reason})"
                return StubEngine(note=note), note
            return (
                NcnnVulkanEngine(binary=binary_path, models_dir=models),
                None,
            )
        except (MissingToolError, UpscaleError) as exc:
            note = f"auto: stub engine ({exc})"
            return StubEngine(note=note), note
    raise UpscaleError(f"unknown engine request: {request!r}")


class NcnnVulkanEngine(EngineAdapter):
    """Real Real-ESRGAN pass through chunked PNG frame directories.

    File-first contract (``EngineAdapter.run``): prepared progressive
    square-pixel file path + plan in; mezzanine path + frame count out.
    No disc/rip/drive/title vocabulary crosses this seam.
    """

    def __init__(
        self,
        *,
        profile: str = "live-action",
        binary: str | None = None,
        models_dir: str | None = None,
        artifacts: dict | None = None,
        chunk_seconds: int | None = None,
        tile_floor: int | None = None,
        runner: Runner | None = None,
        engine_runner: Callable[[str, list[str]], int] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        popen_factory=subprocess.Popen,
    ) -> None:
        spec = MODEL_TABLE.get(profile)
        if spec is None:
            raise UpscaleError(f"unknown engine profile: {profile}")
        self.profile = profile
        self.spec = spec
        self._binary = binary
        self._models_dir = models_dir
        self._artifacts = artifacts  # fixture table for tests; None = pinned
        self._chunk_seconds = int(
            chunk_seconds
            if chunk_seconds is not None
            else (_env_int("VIDLIFT_CHUNK_SECONDS") or CHUNK_SECONDS)
        )
        self.tile_floor = (
            tile_floor
            if tile_floor is not None
            else _env_int("VIDLIFT_ENGINE_TILE")
        )
        # The runner seam carries ffmpeg/ffprobe (canned JSON in tests);
        # engine_runner, when injected, stands in for the engine process
        # (returning its exit code) so tests can observe command shape.
        # With neither, the engine runs live through popen_factory.
        self.runner = runner
        self.engine_runner = engine_runner
        self.on_progress = on_progress
        self.popen_factory = popen_factory
        self.invocations: list[EngineInvocation] = []
        self.log_lines: list[str] = []

    # -- public seam -----------------------------------------------------

    def run(
        self,
        prepared_path: str,
        plan: PlannedFile,
        tools: ToolBox,
        *,
        cancel: Callable[[], bool] | None = None,
    ) -> EngineResult:
        """Run the real engine over the prepared file (design Sections 4-7)."""
        binary = resolve_realesrgan(self._binary)
        models_dir = resolve_realesrgan_models(binary, self._models_dir)
        verify_model_artifacts(models_dir, self.spec.name, artifacts=self._artifacts)

        runner = self._seam_runner()
        prepared_duration = probe_duration_seconds(
            prepared_path, ffprobe=tools.ffprobe, runner=runner
        )
        if prepared_duration is None:
            raise UpscaleError(
                f"prepared file duration could not be probed "
                f"({prepared_path}); refusing to upscale blind"
            )
        clock = probe_frame_clock(
            prepared_path, ffprobe=tools.ffprobe, runner=runner
        )

        # Gate P: prepared must match the SOURCE duration within
        # tolerance — catches a prepare-phase bug (a future IVTC that
        # forgot decimate, a bwdif/fps mismatch) before hours are spent.
        # An unknown source duration refuses (v0.2.29 rule).
        gate_p = duration_gate(
            label="prepare_vs_source",
            reference_s=plan.report.duration_seconds,
            actual_s=prepared_duration,
            actual_name="prepared",
            reference_name="source",
        )

        output = Path(prepared_path).with_suffix(".upscaled.mkv")
        workdir = _workdir_for(prepared_path)
        frames_done = self._run_chunks(
            prepared_path=str(prepared_path),
            prepared_duration=prepared_duration,
            clock=clock,
            tools=tools,
            binary=binary,
            models_dir=models_dir,
            workdir=workdir,
            output=output,
            cancel=cancel,
        )

        # Gate M: mezzanine must match the PREPARED duration — the Mamma
        # Mia gate (the 15.050 s drift sailed under the old 2% gate).
        mezzanine_duration = probe_duration_seconds(
            str(output), ffprobe=tools.ffprobe, runner=runner
        )
        if mezzanine_duration is None:
            raise UpscaleError(
                f"upscale mezzanine duration could not be probed ({output}); "
                "refusing to encode a possibly desynced upscale"
            )
        gate_m = duration_gate(
            label="mezzanine_vs_prepared",
            reference_s=prepared_duration,
            actual_s=mezzanine_duration,
            actual_name="mezzanine",
            reference_name="prepared",
        )

        # Success: workdir deleted (retention contract); the sibling's
        # frame-dir peak stays ~1-2 GB because in/out died per chunk.
        shutil.rmtree(workdir, ignore_errors=True)
        return EngineResult(
            mezzanine_path=str(output),
            frames_done=frames_done,
            note=None,
            simulated=False,
            engine_model=self.spec.name,
            engine_native_scale=self.spec.native_scale,
            frame_clock={"value": clock["value"], "source": clock["source"]},
            duration_gates={**gate_p, **gate_m},
            model_hashes_verified=True,
        )

    # -- internals ---------------------------------------------------------

    def _seam_runner(self) -> Runner:
        """ffmpeg/ffprobe through the injected runner when present
        (tests replay canned JSON), else real subprocesses."""
        if self.runner is not None:
            return self.runner
        from .run import _subprocess_runner

        return _subprocess_runner

    @staticmethod
    def _expected_frames(duration: float | None, clock: dict) -> int:
        """ceil(prepared_duration x rate) — the progress/ETA denominator."""
        if duration is None:
            return 0
        return max(1, math.ceil(float(duration) * float(clock["fps"])))

    def _log(self, text: str) -> None:
        self.log_lines.append(text)

    @staticmethod
    def _cancelled(cancel) -> bool:
        return bool(cancel is not None and cancel())

    def _run_process(
        self, cmd: list[str], *, label: str, cancel=None
    ) -> None:
        """Process discipline, port of the sibling's ``_run_process``:
        drain threads on stdout/stderr (no pipe deadlock), 0.2 s cancel
        poll, terminate -> wait 10 s -> kill -> wait 5 s. When an
        ``engine_runner`` seam is injected it replaces the live process
        while the cancel contract (raise :class:`EngineCancelled`) still
        holds; ``popen_factory`` lets tests observe the terminate/kill
        escalation without a real stubborn child.
        """
        if self._cancelled(cancel):
            raise EngineCancelled(f"cancelled before {label}")
        if self.engine_runner is not None:
            code = self.engine_runner(cmd[0], cmd[1:])
            if self._cancelled(cancel):
                raise EngineCancelled(f"cancelled during {label}")
            if code != 0:
                raise UpscaleError(f"{label} failed with code {code}")
            return
        try:
            proc = self.popen_factory(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise UpscaleError(f"{label} could not start: {exc}") from exc

        output_tail: list[str] = []

        def drain(pipe) -> None:
            try:
                for line in pipe:
                    output_tail.append(line.rstrip())
                    self._parse_engine_progress(line)
            finally:
                try:
                    pipe.close()
                except OSError:
                    pass

        threads = []
        for pipe in (proc.stdout, proc.stderr):
            if pipe is None:
                continue
            thread = threading.Thread(target=drain, args=(pipe,), daemon=True)
            thread.start()
            threads.append(thread)

        while proc.poll() is None:
            if self._cancelled(cancel):
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                raise EngineCancelled(f"cancelled during {label}")
            time.sleep(0.2)

        proc.wait()
        for thread in threads:
            thread.join(timeout=1)
        if proc.returncode != 0:
            detail = " | ".join(output_tail[-6:])[:600]
            raise UpscaleError(
                f"{label} failed with code {proc.returncode}: {detail}"
            )

    def _parse_engine_progress(self, line: str) -> None:
        """Cheap best-effort frame counter from engine stdout lines like
        '16.67%' or '1234/7392' — a progress hint through
        ``on_progress`` only, never a promise."""
        if self.on_progress is None:
            return
        text = line.strip()
        for token in text.replace(",", " ").split():
            if "/" in token and token.replace("/", "").isdigit():
                done, _, total = token.partition("/")
                if total.isdigit() and int(total) > 0:
                    self.on_progress(int(done), int(total))
                return

    def _ffmpeg(
        self,
        cmd: list[str],
        *,
        label: str,
        tools: ToolBox,
        cancel=None,
    ) -> None:
        """ffmpeg through the runner seam (subprocess when live);
        a nonzero exit is a named UpscaleError (fail-open destination)."""
        if self._cancelled(cancel):
            raise EngineCancelled(f"cancelled before {label}")
        code, _out, err = self._seam_runner()(cmd[0], cmd[1:])
        if self._cancelled(cancel):
            raise EngineCancelled(f"cancelled during {label}")
        if code != 0:
            raise UpscaleError(
                f"{label} failed (exit {code}): {(err or '').strip()[:300]}"
            )

    def _run_chunks(
        self,
        *,
        prepared_path: str,
        prepared_duration: float,
        clock: dict,
        tools: ToolBox,
        binary: str,
        models_dir: str,
        workdir: Path,
        output: Path,
        cancel=None,
    ) -> int:
        """Extract -> engine -> reassemble per chunk; concat; returns
        total output frames (design Section 7)."""
        total_seconds = max(1.0, float(prepared_duration))
        chunk_seconds = max(MIN_CHUNK_SECONDS, int(self._chunk_seconds))
        chunks = max(1, math.ceil(total_seconds / chunk_seconds))
        frames_total = self._expected_frames(prepared_duration, clock)
        workdir.mkdir(parents=True, exist_ok=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()

        segments: list[Path] = []
        frames_done = 0
        try:
            for idx in range(chunks):
                start = idx * chunk_seconds
                length = min(chunk_seconds, total_seconds - start)
                chunk = workdir / f"chunk_{idx:04d}"
                in_dir = chunk / "in"
                out_dir = chunk / "out"
                shutil.rmtree(in_dir, ignore_errors=True)
                shutil.rmtree(out_dir, ignore_errors=True)
                in_dir.mkdir(parents=True, exist_ok=True)
                out_dir.mkdir(parents=True, exist_ok=True)
                segment = chunk / "segment.mkv"
                if segment.exists():
                    segment.unlink()

                try:
                    # Extraction (design Section 4.1): wall-time addressing
                    # on the PREPARED file, identity filter — frames are
                    # already normalized by the runner's prepare phase;
                    # extraction never re-stamps. (The sibling applied its
                    # prepare filter here because its input was raw.)
                    self._ffmpeg(
                        [
                            tools.ffmpeg,
                            "-y", "-hide_banner", "-nostdin",
                            "-ss", f"{start:g}",
                            "-t", f"{length:g}",
                            "-i", prepared_path,
                            "-an", "-sn",
                            str(in_dir / "frame_%08d.png"),
                        ],
                        label=f"frame extraction chunk {idx + 1}/{chunks}",
                        tools=tools,
                        cancel=cancel,
                    )
                    input_frames = _count_pngs(in_dir)
                    if not input_frames:
                        raise UpscaleError(
                            f"no frames extracted for chunk {idx + 1}/{chunks}"
                        )
                    extent_png = _png_size(in_dir / "frame_00000001.png")
                    frame_extent = extent_png.extent if extent_png else 0
                    tile = tile_argument(
                        frame_extent, floor=self.tile_floor
                    )

                    # The engine command (clause b): -n/-s pairing comes
                    # from MODEL_TABLE alone; the tile from the measured
                    # extent. -t 0 is unreachable by construction.
                    esrgan_cmd = [
                        binary,
                        "-i", str(in_dir),
                        "-o", str(out_dir),
                        "-s", str(self.spec.native_scale),
                        "-t", str(tile),
                        "-m", models_dir,
                        "-n", self.spec.name,
                        "-f", "png",
                    ]
                    self._log(
                        f"chunk {idx + 1}/{chunks}: frames={input_frames} "
                        f"extent={frame_extent} tile={tile} "
                        f"cmd={shlex.join(esrgan_cmd)}"
                    )
                    self._run_process(
                        esrgan_cmd,
                        label=f"Real-ESRGAN chunk {idx + 1}/{chunks}",
                        cancel=cancel,
                    )
                    self.invocations.append(
                        EngineInvocation(
                            argv=tuple(esrgan_cmd),
                            chunk_index=idx,
                            input_frames=input_frames,
                            tile=tile,
                        )
                    )
                    output_frames = _count_pngs(out_dir)
                    if not output_frames:
                        raise UpscaleError(
                            f"no upscaled frames produced for chunk "
                            f"{idx + 1}/{chunks}"
                        )

                    # Reassembly (clause a): the probed prepared clock
                    # stamps the segment — the exact rational string,
                    # never a default. The segment filter degenerates to
                    # setsar=1 at scale == 2 (design Section 3 geometry
                    # note; the sibling's rule kept verbatim).
                    self._ffmpeg(
                        [
                            tools.ffmpeg,
                            "-y", "-hide_banner", "-nostdin",
                            "-framerate", clock["value"],
                            "-i", str(out_dir / "frame_%08d.png"),
                            "-an",
                            "-vf", "setsar=1",
                            "-c:v", "ffv1",
                            "-level", "3",
                            str(segment),
                        ],
                        label=f"mezzanine segment {idx + 1}/{chunks}",
                        tools=tools,
                        cancel=cancel,
                    )
                    segments.append(segment)
                    frames_done += output_frames
                    if self.on_progress is not None:
                        self.on_progress(
                            min(frames_done, frames_total), frames_total
                        )
                except EngineCancelled:
                    # Partial chunk dirs are cleaned on cancel; completed
                    # segments stay in the kept workdir (design Section 7).
                    shutil.rmtree(in_dir, ignore_errors=True)
                    shutil.rmtree(out_dir, ignore_errors=True)
                    raise
                finally:
                    shutil.rmtree(in_dir, ignore_errors=True)
                    shutil.rmtree(out_dir, ignore_errors=True)

            if len(segments) == 1:
                shutil.move(str(segments[0]), str(output))
            else:
                concat = _write_concat_list(segments)
                try:
                    self._ffmpeg(
                        [
                            tools.ffmpeg,
                            "-y", "-hide_banner", "-nostdin",
                            "-f", "concat", "-safe", "0",
                            "-i", concat,
                            "-c", "copy",
                            str(output),
                        ],
                        label="mezzanine concat",
                        tools=tools,
                        cancel=cancel,
                    )
                finally:
                    try:
                        Path(concat).unlink()
                    except OSError:
                        pass
            if not output.is_file() or output.stat().st_size == 0:
                raise UpscaleError("upscale mezzanine was not created")
        except EngineCancelled:
            try:
                if output.exists():
                    output.unlink()
            except OSError:
                pass
            raise
        except Exception as exc:
            try:
                if output.exists():
                    output.unlink()
            except OSError:
                pass
            if isinstance(exc, UpscaleError):
                raise
            raise UpscaleError(str(exc)) from exc
        return frames_done


def _workdir_for(prepared_path: str) -> Path:
    """``<log_root>/<run_id>.engine-work`` (design Section 7): the
    prepared file lives at ``<log_root>/<run_id>.prepared.mkv``, so the
    workdir sits beside it under the same log root."""
    prepared = Path(prepared_path)
    name = prepared.name
    if name.endswith(".prepared.mkv"):
        name = name[: -len(".prepared.mkv")]
    return prepared.parent / f"{name}.engine-work"


def _count_pngs(directory: Path) -> int:
    return sum(1 for _ in directory.glob("*.png"))


def _write_concat_list(segments: list[Path]) -> str:
    """concat demuxer list with single-quote escaping (sibling port)."""
    fh = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, suffix=".txt"
    )
    with fh:
        for segment in segments:
            path = str(segment).replace("'", "'\\''")
            fh.write(f"file '{path}'\n")
    return fh.name


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None