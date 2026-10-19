"""Boundary tests for the real-engine adapter (b-series, clauses a-g).

Every test here runs headless: fake runways replay canned ffprobe JSON
and fake engine processes touch expected output PNGs — no GPU, no real
ffmpeg, no real engine binary. GPU-dependent validation is the flagged
gap u1-u3 (Eric's 3090 Ti box), out of this suite's scope.

The v0.2.32 rule (clause f): every boundary test provably executes.
Each test registers itself in the module execution set; the canary test
(test_b0, which pytest runs last in this file) fails unless every
expected b-series marker registered. No importorskip anywhere in this
file — a missing engine module fails collection, not silently.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

import vidlift.engine_ncnn as engine_module
from vidlift.engine_ncnn import (
    MODEL_TABLE,
    NcnnVulkanEngine,
    duration_gate,
    gate_tolerance,
    probe_frame_clock,
    tile_argument,
)
from vidlift.engine_ncnn import _positive_rate
from vidlift.plan import build_file_plan
from vidlift.probe import classify, parse_probe
from vidlift.run import (
    EngineCancelled,
    ToolBox,
    UpscaleError,
)
from vidlift.tools_realesrgan import verify_model_artifacts

from _bcanary import register_b

# ---- b0 execution canary state -------------------------------------------

_EXPECTED_B_TESTS = {"b0", "b1", "b2", "b3", "b4", "b5", "b9", "b10", "b11"}
_EXECUTED_B_TESTS: set[str] = set()


def marker(name: str) -> None:
    """Provable execution: the canary requires every b-test to land."""
    _EXECUTED_B_TESTS.add(name)
    register_b(name)  # suite-wide span (the canary-span fix)


# ---- fixtures and fakes -----------------------------------------------------


def make_png(path: Path, width: int, height: int) -> None:
    """A PNG whose first 24 bytes carry a valid IHDR (signature, chunk
    header, width, height) — exactly what the adapter's extent probe
    reads; no decoder dependency, a few bytes of data."""
    header = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
    )
    path.write_bytes(header + b"\x00" * 8)


def make_fake_binary(root: Path) -> Path:
    """An executable stand-in for realesrgan-ncnn-vulkan (resolution only;
    the engine process itself is faked at the process seam)."""
    root.mkdir(parents=True, exist_ok=True)
    exe = root / "realesrgan-ncnn-vulkan"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def make_fixture_models(root: Path) -> tuple[Path, dict]:
    """Tiny stand-in model artifacts plus their computed hash table
    (b9: real 33 MB artifacts are hashed in the slow test below)."""
    root.mkdir(parents=True, exist_ok=True)
    param_bytes = b"tiny-fixture-param-v1"
    bin_bytes = b"tiny-fixture-bin-0123456789abcdef"
    (root / "realesrgan-x2plus.param").write_bytes(param_bytes)
    (root / "realesrgan-x2plus.bin").write_bytes(bin_bytes)
    table = {
        "realesrgan-x2plus": {
            "param": (
                len(param_bytes),
                hashlib.sha256(param_bytes).hexdigest(),
            ),
            "bin": (
                len(bin_bytes),
                hashlib.sha256(bin_bytes).hexdigest(),
            ),
        }
    }
    return root, table


# Sentinel for run_engine: the source probe reported NO duration.
UNKNOWN_DURATION = object()

# Sentinel for run_engine's engine_process: run the LIVE engine process
# path (popen_factory) instead of a fake seam — used by the terminate/kill
# discipline test.
LIVE_ENGINE = object()


def make_plan(*, duration: float | None = 5400.0) -> object:
    """A real PlannedFile built from canned probe JSON (engine-only tests
    consume just report.duration_seconds for Gate P)."""
    fmt = {"format_name": "avi"}
    if duration is not None:
        fmt["duration"] = str(duration)
    payload = json.dumps(
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
                }
            ],
            "format": fmt,
        }
    )
    report = classify(parse_probe("/media/movie.avi", payload))
    return build_file_plan(report)


class EngineFakeRunner:
    """The ffmpeg/ffprobe seam: canned probes, real side effects.

    v0.4.0 decode-truth shapes served:
    - ffprobe ``-show_format``: duration keyed by path — the mezzanine
      (``.upscaled.mkv``), segment files (``segment.mkv``), the SOURCE
      (any path that is neither prepared nor mezzanine), else the
      prepared file.
    - ffprobe ``-show_streams``: canned frame-rate JSON (the container
      clock, diagnostic-only under B.2).
    - ffprobe ``stream=nb_frames`` (fast) / ``-count_frames`` +
      ``nb_read_frames`` (slow, authoritative): the decode-counted
      total keyed by path (prepared vs mezzanine).
    - ffprobe ``a:0`` + ``stream=duration``: the shipped-audio extent
      (the source's a:0 stream duration; ``audio_stream_duration``).
    - ffmpeg extraction (output pattern ``frame_%08d.png``): writes
      EXACTLY the frames the ``select='between(n,a,b)'`` filter asks
      for (the per-chunk count assertion is the point, B.3).
    - ffmpeg concat: writes the mezzanine file.
    - ffmpeg segment: writes the segment file.
    """

    def __init__(
        self,
        *,
        prepared_duration: float = 5400.0,
        mezzanine_duration: float = 5400.0,
        avg_rate: str = "25/1",
        r_rate: str = "25/1",
        frames_per_chunk: int = 10,
        png_geometry: tuple[int, int] = (640, 360),
        prepared_frames: int | None = None,
        mezzanine_frames: int | None = None,
        audio_stream_duration: float | None = None,
        source_format_duration: float | None = None,
        source_format_absent: bool = False,
        segment_duration: float | None = None,
        mezzanine_count_probe_fails: bool = False,
    ) -> None:
        self.prepared_duration = prepared_duration
        self.mezzanine_duration = mezzanine_duration
        self.avg_rate = avg_rate
        self.r_rate = r_rate
        self.frames_per_chunk = frames_per_chunk
        self.png_geometry = png_geometry
        # decode-truth knobs: prepared_frames None mirrors the canned
        # duration x container rate (coherent by default, like
        # run_engine); mezzanine_frames None mirrors the prepared count
        # (the mezzanine is the same frames re-encoded)
        self.prepared_frames = (
            prepared_frames
            if prepared_frames is not None
            else max(
                1, round(prepared_duration * (_positive_rate(avg_rate) or 25.0))
            )
        )
        self.mezzanine_frames = mezzanine_frames
        # coherent by default: an untagged a:0 stream duration mirrors
        # the prepared duration (the run_engine convention), so direct
        # constructions stay in-sync; tests wanting an ABSENT tag use
        # source_format_absent / explicit probes
        self.audio_stream_duration = (
            audio_stream_duration
            if audio_stream_duration is not None
            else prepared_duration
        )
        self.source_format_duration = (
            source_format_duration
            if source_format_duration is not None
            else prepared_duration
        )
        self.source_format_absent = source_format_absent
        self.segment_duration = segment_duration
        self.mezzanine_count_probe_fails = mezzanine_count_probe_fails
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    def _frame_count_for(self, path: str) -> int | None:
        text = str(path)
        if text.endswith(".upscaled.mkv"):
            if self.mezzanine_count_probe_fails:
                return None
            if self.mezzanine_frames is not None:
                return self.mezzanine_frames
            return self.prepared_frames
        return self.prepared_frames

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        self.commands.append((binary, tuple(args)))
        # -- decode-count probes (B.1/B.4) --
        if "-count_frames" in args:
            count = self._frame_count_for(args[-1])
            if count is None:
                return 1, "", "count probe boom"
            return 0, f"{count}\n", ""
        if "stream=nb_frames" in args:
            count = self._frame_count_for(args[-1])
            if count is None:
                return 0, json.dumps({"streams": [{}]}), ""
            return (
                0,
                json.dumps({"streams": [{"nb_frames": str(count)}]}),
                "",
            )
        # -- shipped-audio extent probe (B.2): a:0 on the SOURCE --
        if "a:0" in args and "stream=duration" in args:
            return 0, f"{self.audio_stream_duration}\n", ""
        if "-show_format" in args:
            path = args[-1]
            text = str(path)
            if text.endswith(".upscaled.mkv"):
                duration = self.mezzanine_duration
            elif "segment.mkv" in text:
                duration = (
                    self.segment_duration
                    if self.segment_duration is not None
                    else 120.0
                )
            elif not text.endswith(".prepared.mkv"):
                # the SOURCE file's format duration (plan.report.path);
                # source_format_absent means the tag does not exist
                if self.source_format_absent:
                    return (
                        0,
                        json.dumps(
                            {"streams": [], "format": {"format_name": "avi"}}
                        ),
                        "",
                    )
                duration = self.source_format_duration
            else:
                duration = self.prepared_duration
            return (
                0,
                json.dumps(
                    {
                        "streams": [],
                        "format": {
                            "format_name": "matroska",
                            "duration": str(duration),
                        },
                    }
                ),
                "",
            )
        if "-show_streams" in args:
            stream = {
                "index": 0,
                "codec_type": "video",
                "codec_name": "ffv1",
                "width": self.png_geometry[0],
                "height": self.png_geometry[1],
                "sample_aspect_ratio": "1:1",
                "field_order": "progressive",
                "r_frame_rate": self.r_rate,
                "avg_frame_rate": self.avg_rate,
            }
            return 0, json.dumps({"streams": [stream], "format": {}}), ""
        if "concat" in args:
            Path(args[-1]).write_bytes(b"mezzanine-by-concat")
            return 0, "", ""
        if args and str(args[-1]).endswith("frame_%08d.png"):
            # B.3: the fake honors the frame-index plan — write exactly
            # the frames select='between(n,first,last)' names.
            vf = args[args.index("-vf") + 1] if "-vf" in args else ""
            count = self.frames_per_chunk
            match = re.search(r"between\(n,(\d+),(\d+)\)", vf)
            if match:
                count = int(match.group(2)) - int(match.group(1)) + 1
            out_dir = Path(args[-1]).parent
            width, height = self.png_geometry
            for i in range(1, count + 1):
                make_png(out_dir / f"frame_{i:08d}.png", width, height)
            return 0, "", ""
        if args and str(args[-1]).endswith(".mkv"):
            Path(args[-1]).write_bytes(b"segment-data")
            return 0, "", ""
        return 1, "", f"unexpected command: {binary} {' '.join(args)}"

    def texts(self) -> list[str]:
        return [f"{b} {' '.join(a)}" for b, a in self.commands]

    def command_with(self, token: str) -> tuple[str, ...]:
        for _binary, args in self.commands:
            if token in args:
                return args
        raise AssertionError(f"no recorded command contains {token!r}")


class FakeEngineProcess:
    """The engine process seam: records argv, touches output PNGs at 2x.

    ``fail_on`` is a set of 0-based chunk indexes whose engine call
    exits 1 (failure tests); ``on_call`` observes each invocation
    mid-run (retention/cleanup assertions).
    """

    def __init__(
        self,
        *,
        output_geometry: tuple[int, int] = (1280, 720),
        fail_on: set[int] | None = None,
        on_call=None,
    ) -> None:
        self.output_geometry = output_geometry
        self.fail_on = fail_on or set()
        self.on_call = on_call
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, binary: str, args: list[str]) -> int:
        self.calls.append(tuple(args))
        index = len(self.calls) - 1
        if self.on_call is not None:
            self.on_call(index, args)
        if index in self.fail_on:
            return 1
        in_dir = Path(args[args.index("-i") + 1])
        out_dir = Path(args[args.index("-o") + 1])
        frames = sorted(in_dir.glob("*.png"))
        width, height = self.output_geometry
        for i in range(1, len(frames) + 1):
            make_png(out_dir / f"frame_{i:08d}.png", width, height)
        return 0


class CancelAfter:
    """Cancel flag that flips after ``n`` checks (drive mid-run cancels).

    Check count per single-chunk fake run: ffmpeg extraction pre(1)/
    post(2), engine pre-check(3), then the live poll loop (4, 5, ...).
    """

    def __init__(self, n: int) -> None:
        self.n = n
        self.count = 0

    def __call__(self) -> bool:
        self.count += 1
        return self.count > self.n


class StubbornPopen:
    """A child that ignores SIGTERM and only dies on SIGKILL — the shape
    the terminate/wait-10/kill/wait-5 discipline exists for."""

    instances: list["StubbornPopen"] = []

    def __init__(self, cmd=None, **kwargs) -> None:
        self.cmd = cmd
        self.stdout = None
        self.stderr = None
        self.terminated = 0
        self.killed = 0
        self.waits: list[int | None] = []
        self._alive = True
        StubbornPopen.instances.append(self)

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated += 1  # stays alive: ignores SIGTERM

    def kill(self):
        self.killed += 1
        self._alive = False

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self._alive and timeout is not None:
            raise subprocess.TimeoutExpired(cmd="stubborn-child", timeout=timeout)
        return 0


def run_engine(
    tmp_path,
    *,
    prepared_duration: float = 5400.0,
    source_duration: float | object | None = None,  # None mirrors prepared
    mezzanine_duration: float | None = None,  # None mirrors prepared
    avg_rate: str = "25/1",
    r_rate: str = "25/1",
    chunk_seconds: int = 120,
    png_geometry: tuple[int, int] = (640, 360),
    frames_per_chunk: int = 10,
    engine_process: FakeEngineProcess | None = None,
    tile_floor: int | None = None,
    cancel=None,
    on_progress=None,
    on_step=None,
    popen_factory=subprocess.Popen,
    prepared_frames: int | None = None,
    mezzanine_frames: int | None = None,
    audio_stream_duration: float | None = None,
    source_format_duration: float | None = None,
    segment_duration: float | None = None,
):
    """One full fake-engine adapter run: resolves a fake binary + fixture
    models, replays canned probes, and returns (engine, runner, result).

    Decode-truth coherence by default (v0.4.0): the decode-counted
    total defaults to ``round(prepared_duration x avg_rate)`` and the
    audio extent defaults to ``prepared_duration`` — the derivation
    then lands in the same rate family the container declares, so the
    old boundary tests' in-sync fixtures stay in-sync under the new
    authority. Explicit knobs override any piece for out-of-band
    fixtures (E1/E2/E5/E6 and the gate trips)."""
    if source_duration is None:
        source_duration = prepared_duration  # in-sync by default
    if mezzanine_duration is None:
        mezzanine_duration = prepared_duration  # in-sync by default
    plan_duration = (
        None if source_duration is UNKNOWN_DURATION else float(source_duration)
    )
    if prepared_frames is None:
        rate = _positive_rate(avg_rate) or 25.0
        prepared_frames = max(1, round(prepared_duration * rate))
    if audio_stream_duration is None:
        audio_stream_duration = float(prepared_duration)
    if source_format_duration is None:
        source_format_duration = float(prepared_duration)
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner = EngineFakeRunner(
        prepared_duration=prepared_duration,
        mezzanine_duration=float(mezzanine_duration),
        avg_rate=avg_rate,
        r_rate=r_rate,
        png_geometry=png_geometry,
        frames_per_chunk=frames_per_chunk,
        prepared_frames=prepared_frames,
        mezzanine_frames=mezzanine_frames,
        audio_stream_duration=audio_stream_duration,
        source_format_duration=source_format_duration,
        segment_duration=segment_duration,
    )
    models, table = make_fixture_models(tmp_path / "models")
    engine_runner = (
        None
        if engine_process is LIVE_ENGINE
        else (engine_process or FakeEngineProcess())
    )
    engine = NcnnVulkanEngine(
        binary=str(make_fake_binary(tmp_path / "bin")),
        models_dir=str(models),
        artifacts=table,
        runner=runner,
        engine_runner=engine_runner,
        chunk_seconds=chunk_seconds,
        tile_floor=tile_floor,
        on_progress=on_progress,
        on_step=on_step,
        popen_factory=popen_factory,
    )
    plan = make_plan(duration=plan_duration)
    result = engine.run(
        str(prepared), plan, ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        cancel=cancel,
        on_step=on_step,
    )
    return engine, runner, result


# ---- b1: clock flow (clause a; v0.4.0: the stamp authority) -------------


@pytest.mark.parametrize(
    "clock,token",
    [
        ("24000/1001", "24000/1001"),
        ("25/1", "25"),
        ("30000/1001", "30000/1001"),
    ],
)
def test_b1_prepared_clock_flows_byte_identical(tmp_path, clock, token):
    marker("b1")
    _engine, runner, result = run_engine(
        tmp_path, prepared_duration=100.0, source_duration=100.0,
        mezzanine_duration=100.0, avg_rate=clock, r_rate=clock,
    )
    # v0.4.0: the STAMP is the derived canonical token (decode-truth,
    # B.2); it lands in every assembled segment's -framerate argument,
    # and the frame-clock provenance names its derivation, not the
    # container string (the container fields ride as diagnostics).
    assert result.frame_clock["value"] == token
    assert result.frame_clock["source"] == "derived"
    assert result.frame_clock["container_avg_frame_rate"] == clock
    segment_cmds = [a for _b, a in runner.commands if "-framerate" in a]
    assert segment_cmds, "no reassembly command was recorded"
    for args in segment_cmds:
        assert args[args.index("-framerate") + 1] == token


# ---- b2: container-field demotion (v0.4.0 B.2) -------------------------------


def test_b2_container_fields_unusable_stamp_still_derives(tmp_path):
    """v0.4.0 replaces the v0.3.0 refusal: container r/avg fields are
    DIAGNOSTIC-ONLY. With both fields unparseable the run PROCEEDS on
    decode-truth (the derived token), records the unusable container
    fields as None, and never invents a default rate string."""
    marker("b2")
    _engine, runner, result = run_engine(
        tmp_path, prepared_duration=100.0, source_duration=100.0,
        mezzanine_duration=100.0, avg_rate="0/0", r_rate="N/A",
    )
    # decode-truth: 2500 frames / 100 s = 25.000 -> canonical "25"
    assert result.frame_clock["value"] == "25"
    assert result.frame_clock["source"] == "derived"
    assert result.frame_clock["container_avg_frame_rate"] is None
    assert result.frame_clock["container_r_frame_rate"] is None
    for _b, args in runner.commands:
        if "-framerate" in args:
            assert args[args.index("-framerate") + 1] == "25"
    # no DEFAULT rate string was invented: only the derived token
    # appears, and no other canonical token does
    stamped = {
        a[a.index("-framerate") + 1]
        for _b, a in runner.commands
        if "-framerate" in a
    }
    assert stamped == {"25"}


# ---- b3: duration gates (clause a) -------------------------------------------


def test_b3_gate_tolerance_is_half_second_floor_with_hundredth_percent():
    marker("b3")
    assert gate_tolerance(120.0) == 0.5  # floor wins on short references
    assert gate_tolerance(6838.0) == pytest.approx(0.6838)  # 0.01% wins long


def test_b3_gate_matrix_accept_and_reject_shapes():
    marker("b3")
    # accept: -0.3 s on a 120 s reference (within the 0.5 s floor)
    record = duration_gate(
        label="mezzanine_vs_prepared",
        reference_s=120.0,
        actual_s=119.7,
        actual_name="mezzanine",
        reference_name="prepared",
    )
    assert record["mezzanine_vs_prepared"]["ok"] is True
    assert record["mezzanine_vs_prepared"]["tolerance_s"] == pytest.approx(0.5)
    # reject: just past the floor
    with pytest.raises(UpscaleError, match="off by 0.600s"):
        duration_gate(
            label="mezzanine_vs_prepared",
            reference_s=120.0,
            actual_s=120.6,
            actual_name="mezzanine",
            reference_name="prepared",
        )
    # unknown reference refuses (v0.2.29: a zero reference accepted anything)
    for bad in (None, 0.0, -1.0):
        with pytest.raises(UpscaleError, match="duration unknown"):
            duration_gate(
                label="mezzanine_vs_prepared",
                reference_s=bad,
                actual_s=119.7,
                actual_name="mezzanine",
                reference_name="prepared",
            )


def test_b3_mamma_mia_drift_shape_rejects_at_gate_m(tmp_path):
    marker("b3")
    # the measured failure shape, scaled to a test-sized file: a 300 s
    # prepared file, +15.050 s of mezzanine drift. The old max(5 s, 2%)
    # gate called 0.22% drift "fine"; v0.3.0's max(0.5 s, 0.01%)
    # mezzanine-vs-prepared gate caught it; v0.4.0's clock gate vs the
    # SHIPPED-AUDIO extent (max(2.0 s, 0.1%)) still calls it what it
    # is: a desynced mezzanine — and retains the reject (B.5).
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner = EngineFakeRunner(
        prepared_duration=300.0, mezzanine_duration=315.05,
        prepared_frames=7500,
        audio_stream_duration=300.0, source_format_duration=300.0,
    )
    models, table = make_fixture_models(tmp_path / "models")
    engine = NcnnVulkanEngine(
        binary=str(make_fake_binary(tmp_path / "bin")),
        models_dir=str(models),
        artifacts=table,
        runner=runner,
        engine_runner=FakeEngineProcess(),
    )
    with pytest.raises(UpscaleError) as excinfo:
        engine.run(
            str(prepared), make_plan(duration=300.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    message = str(excinfo.value)
    # the refusal is the AUDIO-extent clock gate (B.4) — the axis that
    # actually ships; the drift and tolerance are named
    assert "mezzanine_vs_audio" in message
    assert "300.000" in message and "315.050" in message
    assert "15.050" in message
    assert "2.000" in message  # max(2.0 s, 0.1% of 300 s): floor wins
    # B.5: the rejected mezzanine was RETAINED, never deleted
    assert "rejected mezzanine retained at" in message
    rejects = list(tmp_path.glob("movie.prepared.rejected-*.mkv"))
    assert len(rejects) == 1
    diagnostics = list(
        tmp_path.glob("movie.prepared.rejected-*.diagnostic.json")
    )
    assert len(diagnostics) == 1


def test_b3_gate_p_rejects_prepare_drift_before_any_frame(tmp_path):
    marker("b3")
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner = EngineFakeRunner(prepared_duration=5600.0)
    models, table = make_fixture_models(tmp_path / "models")
    engine = NcnnVulkanEngine(
        binary=str(make_fake_binary(tmp_path / "bin")),
        models_dir=str(models),
        artifacts=table,
        runner=runner,
        engine_runner=FakeEngineProcess(),
    )
    with pytest.raises(UpscaleError) as excinfo:
        engine.run(
            str(prepared), make_plan(duration=5400.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    assert "prepare_vs_source" in str(excinfo.value)
    # hours were NOT spent: no extraction ever ran
    assert all("frame_%08d.png" not in a for _b, a in runner.commands)
    assert engine.invocations == []


def test_b3_unknown_source_duration_refuses_gate_p(tmp_path):
    marker("b3")
    with pytest.raises(UpscaleError, match="source duration unknown"):
        run_engine(
            tmp_path,
            prepared_duration=5400.0,
            source_duration=UNKNOWN_DURATION,
            mezzanine_duration=5400.0,
        )


def test_b3_accept_shape_minus_300ms_on_120s_passes_both_gates(tmp_path):
    marker("b3")
    _engine, runner, result = run_engine(
        tmp_path, prepared_duration=120.0, source_duration=120.0,
        mezzanine_duration=119.7, chunk_seconds=120,
    )
    gates = result.duration_gates
    assert gates["prepare_vs_source"]["ok"] is True
    assert gates["prepare_vs_source"]["reference_kind"] == "format_duration"
    # the audio-extent clock gate is the new authority (B.4)
    assert gates["mezzanine_vs_audio"]["ok"] is True
    assert gates["mezzanine_vs_audio"]["mezzanine_s"] == pytest.approx(119.7)
    assert gates["mezzanine_vs_audio"]["audio_s"] == pytest.approx(120.0)
    # gate M survives as a diagnostic record (manifest compat)
    assert gates["mezzanine_vs_prepared"]["ok"] is True
    assert gates["mezzanine_vs_prepared"]["diagnostic_only"] is True


# ---- b4: model/scale pairing (clause b) --------------------------------------


def test_b4_registry_drives_model_and_scale_flags(tmp_path):
    marker("b4")
    engine, _runner, result = run_engine(tmp_path, prepared_duration=100.0)
    assert MODEL_TABLE["live-action"].name == "realesrgan-x2plus"
    assert MODEL_TABLE["live-action"].native_scale == 2
    # x4plus is unreachable through the table: no key, no constructed argv
    assert not any("x4plus" in key for key in MODEL_TABLE)
    assert engine.invocations, "engine was never invoked"
    for invocation in engine.invocations:
        argv = invocation.argv
        assert argv[argv.index("-n") + 1] == "realesrgan-x2plus"
        scale = int(argv[argv.index("-s") + 1])
        assert scale == MODEL_TABLE[engine.profile].native_scale
        assert scale == 2
        assert all("x4plus" not in part for part in argv)
    assert result.engine_model == "realesrgan-x2plus"
    assert result.engine_native_scale == 2
    assert result.simulated is False


def test_b4_unknown_profile_is_impossible_to_construct():
    marker("b4")
    with pytest.raises(UpscaleError, match="unknown engine profile"):
        NcnnVulkanEngine(profile="anime-4x-corruption")


# ---- b5: tiling (clause b) ----------------------------------------------------


def test_b5_tile_derivation_rule():
    marker("b5")
    # the 640x360 class: extent 640 -> requested 704 -> tile 704
    assert tile_argument(640) == 704
    # explicit floor is a FLOOR only: never shrinks below the derived tile
    assert tile_argument(640, floor=512) == 704
    assert tile_argument(640, floor=1024) == 1024
    # never 0, never below the measured extent, always a 64 multiple
    for extent in range(0, 2049, 7):
        tile = tile_argument(extent)
        assert tile != 0, "-t 0 is forbidden (auto-tiling seam artifacts)"
        assert tile >= extent, "tile smaller than the frame extent"
        assert tile % 64 == 0, "tile is not a driver-friendly 64 multiple"


def test_b5_engine_argv_has_explicit_tile_never_zero(tmp_path):
    marker("b5")
    engine, _runner, _result = run_engine(
        tmp_path, prepared_duration=100.0, png_geometry=(640, 360),
    )
    assert engine.invocations
    for invocation in engine.invocations:
        argv = invocation.argv
        assert "-t" in argv, "engine command missing -t entirely"
        tile = int(argv[argv.index("-t") + 1])
        assert tile != 0
        assert tile >= 640
        assert tile == 704  # clause b's stated number for the 640x360 class


def test_b5_env_tile_floor_honored(tmp_path, monkeypatch):
    marker("b5")
    monkeypatch.setenv("VIDLIFT_ENGINE_TILE", "1024")
    engine, _runner, _result = run_engine(
        tmp_path, prepared_duration=100.0, png_geometry=(640, 360),
    )
    argv = engine.invocations[0].argv
    assert int(argv[argv.index("-t") + 1]) == 1024


# ---- b9: hash verification (clause g) ------------------------------------------


def test_b9_hash_gate_matrix(tmp_path):
    marker("b9")
    models, table = make_fixture_models(tmp_path / "models")
    # correct fixture hashes verify
    assert verify_model_artifacts(str(models), "realesrgan-x2plus", artifacts=table)
    # first-use-per-process cache: a later corruption does not re-verify
    (models / "realesrgan-x2plus.bin").write_bytes(b"corrupted-after-verify")
    assert verify_model_artifacts(str(models), "realesrgan-x2plus", artifacts=table)
    # a DIFFERENT dir with corrupted content is refused, not cached;
    # flip one byte (same size) so the sha256 check is what fires
    models2, table2 = make_fixture_models(tmp_path / "models2")
    data = bytearray((models2 / "realesrgan-x2plus.bin").read_bytes())
    data[0] ^= 0xFF
    (models2 / "realesrgan-x2plus.bin").write_bytes(bytes(data))
    with pytest.raises(UpscaleError, match="sha256 mismatch"):
        verify_model_artifacts(str(models2), "realesrgan-x2plus", artifacts=table2)
    # size mismatch is caught before hashing
    models3, table3 = make_fixture_models(tmp_path / "models3")
    (models3 / "realesrgan-x2plus.param").write_bytes(b"wrong-size")
    with pytest.raises(UpscaleError, match="does not match the pinned"):
        verify_model_artifacts(str(models3), "realesrgan-x2plus", artifacts=table3)
    # missing artifact file
    models4, table4 = make_fixture_models(tmp_path / "models4")
    (models4 / "realesrgan-x2plus.bin").unlink()
    with pytest.raises(UpscaleError, match="not found in"):
        verify_model_artifacts(str(models4), "realesrgan-x2plus", artifacts=table4)
    # unpinned model names are refused outright
    with pytest.raises(UpscaleError, match="no pinned artifacts"):
        verify_model_artifacts(str(models4), "realesrgan-x4plus", artifacts=table4)


def test_b9_engine_refuses_substituted_model_before_extraction(tmp_path):
    marker("b9")
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner = EngineFakeRunner(prepared_duration=100.0)
    models, table = make_fixture_models(tmp_path / "models")
    data = bytearray((models / "realesrgan-x2plus.bin").read_bytes())
    data[0] ^= 0xFF  # same size, different content: sha256 fires
    (models / "realesrgan-x2plus.bin").write_bytes(bytes(data))
    engine = NcnnVulkanEngine(
        binary=str(make_fake_binary(tmp_path / "bin")),
        models_dir=str(models),
        artifacts=table,
        runner=runner,
        engine_runner=FakeEngineProcess(),
    )
    with pytest.raises(UpscaleError, match="sha256 mismatch"):
        engine.run(
            str(prepared), make_plan(duration=100.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    # refused BEFORE any frame was extracted
    assert all("frame_%08d.png" not in a for _b, a in runner.commands)
    assert engine.invocations == []


REAL_X2PLUS_DIR = Path(
    "/home/yetisoldier/projects/x2plus-ncnn-verified"
)


def test_b9_real_artifacts_match_the_pinned_table():
    """The marked-slow real-artifact check (design Section 10, b9 note):
    hashes the actual 33 MB x2plus pair once against the pinned table.
    Runs wherever the verified archive exists (this box, Eric's); skips
    with a named reason elsewhere — it is the flagged-gap companion to
    the fixture tests above, not a boundary test itself.
    """
    if not REAL_X2PLUS_DIR.is_dir():
        pytest.skip(
            "verified x2plus artifact archive not present on this host "
            "(fixture-based hash tests above still ran)"
        )
    assert verify_model_artifacts(str(REAL_X2PLUS_DIR), "realesrgan-x2plus")


# ---- b10: chunk-flow shape ------------------------------------------------------


def test_b10_multi_chunk_flow_shape(tmp_path):
    marker("b10")
    seen_cleanup: list[bool] = []

    def on_call(index, _args):
        if index == 2:  # the LAST chunk's engine call
            workdir = tmp_path / "movie.engine-work"
            seen_cleanup.append(
                not (workdir / "chunk_0000" / "in").exists()
                and not (workdir / "chunk_0000" / "out").exists()
                and not (workdir / "chunk_0001" / "in").exists()
                and (workdir / "chunk_0001" / "segment.mkv").exists()
            )

    process = FakeEngineProcess(on_call=on_call)
    engine, runner, result = run_engine(
        tmp_path, prepared_duration=300.0, source_duration=300.0,
        mezzanine_duration=300.0, chunk_seconds=120, engine_process=process,
    )
    # v0.4.0 frame-index plan (B.3): 7500 decoded frames at 25 fps,
    # F = round(120 x 25) = 3000 -> 3 chunks of [3000, 3000, 1500]
    assert len(engine.invocations) == 3
    extraction_cmds = [
        a for _b, a in runner.commands
        if a and str(a[-1]).endswith("frame_%08d.png")
    ]
    # frame-index addressing: select='between(n,a,b)', -vsync 0,
    # NO -ss / -t (the retired wall-time windows)
    assert len(extraction_cmds) == 3
    for args in extraction_cmds:
        assert "-ss" not in args and "-t" not in args
        assert args[args.index("-vsync") + 1] == "0"
        assert args[args.index("-start_number") + 1] == "1"
    vf_args = [a[a.index("-vf") + 1] for a in extraction_cmds]
    assert vf_args == [
        "select='between(n,0,2999)'",
        "select='between(n,3000,5999)'",
        "select='between(n,6000,7499)'",
    ]
    # exactly one concat for the multi-segment mezzanine
    concat_cmds = [a for _b, a in runner.commands if "concat" in a]
    assert len(concat_cmds) == 1
    # completed chunks' frame dirs were deleted as the run progressed
    assert seen_cleanup == [True]
    # the clock gate probe ran against the mezzanine
    assert any(
        "-show_format" in a and str(a[-1]).endswith(".upscaled.mkv")
        for _b, a in runner.commands
    )
    assert result.duration_gates["mezzanine_vs_audio"]["mezzanine_s"] == (
        pytest.approx(300.0)
    )
    assert result.frames_done == 7500  # the decode-counted total, exact
    # success: the workdir is deleted (retention contract)
    assert not (tmp_path / "movie.engine-work").exists()
    assert Path(result.mezzanine_path).is_file()


def test_b10_single_chunk_moves_segment_without_concat(tmp_path):
    engine, runner, result = run_engine(
        tmp_path, prepared_duration=100.0, source_duration=100.0,
        mezzanine_duration=100.0, chunk_seconds=120,
    )
    assert len(engine.invocations) == 1
    assert all("concat" not in a for _b, a in runner.commands)
    assert Path(result.mezzanine_path).is_file()
    assert not (tmp_path / "movie.engine-work").exists()


def test_b10_workdir_retained_on_engine_failure(tmp_path):
    process = FakeEngineProcess(fail_on={2})  # third chunk's engine dies
    with pytest.raises(UpscaleError, match="failed with code 1"):
        run_engine(
            tmp_path, prepared_duration=300.0, source_duration=300.0,
            mezzanine_duration=300.0, chunk_seconds=120, engine_process=process,
        )
    workdir = tmp_path / "movie.engine-work"
    assert workdir.is_dir(), "failed runs keep the workdir for triage (clause e)"
    # completed segments stay; the failed partial chunk is cleaned
    assert (workdir / "chunk_0000" / "segment.mkv").is_file()
    assert (workdir / "chunk_0001" / "segment.mkv").is_file()
    assert not (workdir / "chunk_0002" / "in").exists()
    assert not (workdir / "chunk_0002" / "out").exists()
    # a partial mezzanine never survives a pre-reassembly failure
    assert not (tmp_path / "movie.prepared.upscaled.mkv").exists()
    # B.7: the checkpoint records exactly the completed chunks
    checkpoint = json.loads(
        (workdir / "chunk_state.json").read_text()
    )
    assert [entry["idx"] for entry in checkpoint["completed"]] == [0, 1]


# ---- b11: cancel -----------------------------------------------------------------


def test_b11_cancel_before_first_frame_raises_engine_cancelled(tmp_path):
    marker("b11")
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner = EngineFakeRunner(prepared_duration=100.0)
    models, table = make_fixture_models(tmp_path / "models")
    engine = NcnnVulkanEngine(
        binary=str(make_fake_binary(tmp_path / "bin")),
        models_dir=str(models),
        artifacts=table,
        runner=runner,
        engine_runner=FakeEngineProcess(),
    )
    with pytest.raises(EngineCancelled) as excinfo:
        engine.run(
            str(prepared), make_plan(duration=100.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            cancel=lambda: True,
        )
    # a cancel is NOT a failure: EngineCancelled, never UpscaleError
    assert not isinstance(excinfo.value, UpscaleError)
    assert "cancelled before" in str(excinfo.value)
    assert all("frame_%08d.png" not in a for _b, a in runner.commands)
    assert engine.invocations == []


def test_b11_mid_chunk_cancel_terminates_then_kills_stubborn_child(tmp_path):
    marker("b11")
    StubbornPopen.instances = []
    # cancel flips after the three pre-engine checks (extraction pre+post,
    # engine pre) so the live poll loop observes it while the child runs
    with pytest.raises(EngineCancelled):
        run_engine(
            tmp_path, prepared_duration=100.0, source_duration=100.0,
            mezzanine_duration=100.0, cancel=CancelAfter(3),
            engine_process=LIVE_ENGINE,  # the live Popen path runs
            popen_factory=StubbornPopen,  # a child that ignores SIGTERM
        )
    child = StubbornPopen.instances[-1]
    # the ported discipline, observed: terminate -> wait 10 -> kill -> wait 5
    assert child.terminated == 1
    assert child.killed == 1
    assert 10 in child.waits and 5 in child.waits
    # partial chunk dirs are cleaned on cancel; the workdir is kept
    workdir = tmp_path / "movie.engine-work"
    assert workdir.is_dir()
    assert not (workdir / "chunk_0000" / "in").exists()
    assert not (workdir / "chunk_0000" / "out").exists()
    assert not isinstance(None, UpscaleError)  # cancel reached, not failure


def test_b11_cancel_between_chunks_keeps_completed_segments(tmp_path):
    marker("b11")
    flags = {"cancel": False}

    def on_call(index, _args):
        if index == 1:  # flip at the SECOND chunk's engine call: chunk 1
            flags["cancel"] = True  # completed fully; chunk 2 never starts

    process = FakeEngineProcess(on_call=on_call)
    engine, _runner, _result = None, None, None
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner = EngineFakeRunner(
        prepared_duration=300.0, mezzanine_duration=300.0,
    )
    models, table = make_fixture_models(tmp_path / "models")
    engine = NcnnVulkanEngine(
        binary=str(make_fake_binary(tmp_path / "bin")),
        models_dir=str(models),
        artifacts=table,
        runner=runner,
        engine_runner=process,
        chunk_seconds=120,
    )
    with pytest.raises(EngineCancelled) as excinfo:
        engine.run(
            str(prepared), make_plan(duration=300.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            cancel=lambda: flags["cancel"],
        )
    assert "cancelled during Real-ESRGAN chunk 2/3" in str(excinfo.value)
    assert len(engine.invocations) == 1  # chunk 2's invocation never recorded
    workdir = tmp_path / "movie.engine-work"
    assert (workdir / "chunk_0000" / "segment.mkv").is_file()
    assert not (workdir / "chunk_0001" / "in").exists()
    assert not (workdir / "chunk_0001" / "out").exists()


def test_b11_pre_first_frame_cancel_not_a_failure_type_check():
    marker("b11")
    # EngineCancelled and UpscaleError are distinct failure classes at the
    # seam: the runner routes UpscaleError to fail-open and EngineCancelled
    # to the cancelled outcome (P2 wiring asserts the manifest side).
    assert issubclass(EngineCancelled, engine_module.EngineCancelled)
    assert not issubclass(EngineCancelled, UpscaleError)
    assert not issubclass(UpscaleError, EngineCancelled)


# ---- progress seam (Section 7) ----------------------------------------------------


def test_progress_seam_reports_per_chunk_frames(tmp_path):
    seen: list[tuple[int, int]] = []
    _engine, _runner, result = run_engine(
        tmp_path, prepared_duration=300.0, source_duration=300.0,
        mezzanine_duration=300.0, chunk_seconds=120,
        on_progress=lambda done, total: seen.append((done, total)),
    )
    # v0.4.0 (E15): frames_total is the DECODE-COUNTED total (7500),
    # never the old ceil(duration x container fps) estimate
    assert seen == [(3000, 7500), (6000, 7500), (7500, 7500)]
    assert result.frames_done == 7500


def test_run_progress_kwarg_is_the_runner_seam_shape(tmp_path):
    """Regression (found by the real-engine smoke run): run_file calls
    engine.run(..., on_progress=record_progress) — the adapter must
    accept the kwarg and chain it with any constructor callback. This
    is the EXACT call shape the runner uses, driven end-to-end through
    the fake seams."""
    constructor_seen: list[tuple[int, int]] = []
    kwarg_seen: list[tuple[int, int]] = []
    step_seen: list[str] = []
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner = EngineFakeRunner(prepared_duration=300.0, mezzanine_duration=300.0)
    models, table = make_fixture_models(tmp_path / "models")
    engine = NcnnVulkanEngine(
        binary=str(make_fake_binary(tmp_path / "bin")),
        models_dir=str(models),
        artifacts=table,
        runner=runner,
        engine_runner=FakeEngineProcess(),
        chunk_seconds=120,
        on_progress=lambda done, total: constructor_seen.append((done, total)),
    )
    plan = make_plan(duration=300.0)
    # the exact runner call: on_progress passed as a keyword argument
    result = engine.run(
        str(prepared),
        plan,
        ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        cancel=None,
        on_progress=lambda done, total: kwarg_seen.append((done, total)),
        on_step=lambda label: step_seen.append(label),
    )
    assert result.frames_done == 7500
    # BOTH callbacks observed the per-chunk progress, and the adapter
    # restored its constructor callback afterwards (no cross-run leak)
    assert len(constructor_seen) == 3 and len(kwarg_seen) == 3
    assert engine.on_progress is not None
    assert constructor_seen == kwarg_seen == [
        (3000, 7500), (6000, 7500), (7500, 7500),
    ]
    # B.6: the step labels landed through the on_step seam
    assert "prepared-count-probe" in step_seen
    assert "concat" in step_seen
    assert "gate-frames-probe" in step_seen
    assert "gate-clock-probe" in step_seen


def test_engine_stdout_progress_lines_parse_cheaply():
    calls: list[tuple[int, int]] = []
    engine = NcnnVulkanEngine(on_progress=lambda d, t: calls.append((d, t)))
    engine._parse_engine_progress("45.2% complete, 1234/7500 frames")
    assert calls == [(1234, 7500)]
    engine._parse_engine_progress("no numbers here")
    assert calls == [(1234, 7500)]  # garbage lines are ignored, never crash


def test_unknown_chunk_env_is_ignored_safely(monkeypatch):
    monkeypatch.setenv("VIDLIFT_CHUNK_SECONDS", "not-a-number")
    engine = NcnnVulkanEngine()
    assert engine._chunk_seconds == engine_module.CHUNK_SECONDS


# =========================================================================
# v0.4.0 — the E-series: decode-truth chunking (design B.9, E1-E15)
# Fixture numbers are the field-proven ones from the design doc:
# 163,509 frames / 6,818.4 s -> 24000/1001; 5,453.0 s -> 30000/1001
# control; the Karen band fixture (under-reported audio -> 24 vs
# 24000/1001 cross-provenance disagreement -> refuse).
# =========================================================================


def make_decode_truth_runner(
    tmp_path,
    *,
    prepared_frames: int,
    prepared_duration: float,
    avg_rate: str,
    r_rate: str,
    audio_stream_duration: float | None,
    source_format_duration: float | None = None,
    source_format_absent: bool = False,
    mezzanine_frames: int | None = None,
    mezzanine_duration: float | None = None,
) -> EngineFakeRunner:
    """A decode-truth fixture runner: explicit operands, no mirroring
    magic — each derivation input is named exactly as the design doc's
    fixtures name them. ``source_format_absent`` means the source's
    format duration tag does not exist (the single-provenance case)."""
    return EngineFakeRunner(
        prepared_duration=prepared_duration,
        mezzanine_duration=(
            mezzanine_duration
            if mezzanine_duration is not None
            else prepared_duration
        ),
        avg_rate=avg_rate,
        r_rate=r_rate,
        prepared_frames=prepared_frames,
        mezzanine_frames=mezzanine_frames,
        audio_stream_duration=audio_stream_duration,
        source_format_duration=source_format_duration,
        source_format_absent=source_format_absent,
    )


def derive_only(
    tmp_path,
    *,
    prepared_frames: int,
    prepared_duration: float,
    avg_rate: str = "24000/1001",
    r_rate: str = "24000/1001",
    audio_stream_duration: float | None,
    source_format_duration: float | None = None,
    source_format_absent: bool = False,
    mezzanine_frames: int | None = None,
    mezzanine_duration: float | None = None,
    engine_process: FakeEngineProcess | None = None,
    chunk_seconds: int = 120,
    plan_duration: float | None = None,
):
    """One adapter run over an explicitly-numbered decode-truth fixture
    (all operands named; nothing mirrored). ``plan_duration`` defaults
    to the prepared duration so gate P (a sanity gate, not a stamp
    input) passes; tests that want gate P to fire pass something else."""
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner = make_decode_truth_runner(
        tmp_path,
        prepared_frames=prepared_frames,
        prepared_duration=prepared_duration,
        avg_rate=avg_rate,
        r_rate=r_rate,
        audio_stream_duration=audio_stream_duration,
        source_format_duration=source_format_duration,
        source_format_absent=source_format_absent,
        mezzanine_frames=mezzanine_frames,
        mezzanine_duration=mezzanine_duration,
    )
    models, table = make_fixture_models(tmp_path / "models")
    engine = NcnnVulkanEngine(
        binary=str(make_fake_binary(tmp_path / "bin")),
        models_dir=str(models),
        artifacts=table,
        runner=runner,
        engine_runner=engine_process or FakeEngineProcess(),
        chunk_seconds=chunk_seconds,
    )
    plan = make_plan(
        duration=(
            plan_duration if plan_duration is not None else prepared_duration
        )
    )
    return engine, runner, plan


# ---- E1: the stamp comes from decode-truth, not the container ---------------


def test_stamp_derives_from_decode_truth_not_container(tmp_path):
    """E1: prepared declares 30000/1001 in its container (the soft-
    telecine lie), but decode-truth is 163,509 frames over a 6,818.4 s
    audio extent -> the stamp is 24000/1001, NOT the container token.
    The derivation is pinned at unit scale (the field-proven numbers);
    the full chunk flow is pinned by E7/E8/E15 at test-friendly scale."""
    runner = EngineFakeRunner(
        prepared_duration=6818.4,
        mezzanine_duration=6818.4,
        avg_rate="30000/1001",
        r_rate="30000/1001",
        prepared_frames=163509,
        audio_stream_duration=6818.4,
        source_format_duration=6818.4,
    )
    from vidlift.engine_ncnn import derive_stamp_clock, probe_frame_count
    from vidlift.engine_ncnn import probe_audio_extent_seconds

    tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
    frames, from_metadata = probe_frame_count(
        str(tmp_path / "movie.prepared.mkv"),
        ffprobe="ffprobe", runner=runner,
    )
    assert frames == 163509 and from_metadata is False
    audio = probe_audio_extent_seconds(
        "/media/movie.avi", ffprobe="ffprobe", runner=runner
    )
    stamp = derive_stamp_clock(
        source_path="/media/movie.avi",
        prepared_total_frames=frames,
        audio=audio,
        prepared_duration=6818.4,
        container_clock={
            "source": "avg_frame_rate",
            "value": "30000/1001",
            "avg_frame_rate_raw": "30000/1001",
            "r_frame_rate_raw": "30000/1001",
        },
    )
    assert stamp["value"] == "24000/1001"
    assert stamp["source"] == "derived"
    assert stamp["prepared_total_frames"] == 163509
    assert stamp["audio_extent_s"] == pytest.approx(6818.4)
    assert stamp["container_avg_frame_rate"] == "30000/1001"
    assert stamp["derived_raw"] == pytest.approx(
        163509 / 6818.4, rel=1e-9
    )
    # every provenance snapshot agreed on the true token
    tokens = {
        entry["token"]
        for entry in stamp["cross_provenance"]
        if entry["snapped"]
    }
    assert tokens == {"24000/1001"}


# ---- E2: the true-29.97 control snaps unchanged ------------------------------


def test_stamp_true_2997_unchanged(tmp_path):
    """E2: 163,509 frames over a 5,453.0 s audio extent derives
    ~29.978 fps -> snaps 30000/1001 (the conservative control: a
    true-29.97 file must pass cleanly, not just fail loudly)."""
    runner = EngineFakeRunner(
        prepared_duration=5453.0,
        mezzanine_duration=5453.0,
        avg_rate="30000/1001",
        r_rate="30000/1001",
        prepared_frames=163509,
        audio_stream_duration=5453.0,
        source_format_duration=5453.0,
    )
    from vidlift.engine_ncnn import derive_stamp_clock
    from vidlift.engine_ncnn import probe_audio_extent_seconds

    audio = probe_audio_extent_seconds(
        "/media/movie.avi", ffprobe="ffprobe", runner=runner
    )
    stamp = derive_stamp_clock(
        source_path="/media/movie.avi",
        prepared_total_frames=163509,
        audio=audio,
        prepared_duration=5453.0,
        container_clock=None,
    )
    assert stamp["value"] == "30000/1001"
    assert stamp["derived_raw"] == pytest.approx(
        163509 / 5453.0, rel=1e-9
    )


# ---- E3: the snap table, boundaries included -------------------------------


def test_snap_table_boundaries():
    """E3: nearest-neighbor snap with the 0.1% inclusive boundary."""
    from vidlift.engine_ncnn import snap_to_canonical_rate as snap

    assert snap(23.9775) == ("24000/1001", True)
    assert snap(25.02) == ("25", True)
    assert snap(50.04) == ("50", True)
    assert snap(59.97) == ("60000/1001", True)
    # the 23.976-vs-24 overlap resolves to NEAREST, deterministically
    assert snap(23.99) == ("24", True)  # nearer to 24 than 23.976
    assert snap(23.985) == ("24000/1001", True)  # nearer to 23.976
    # ±0.1% edge, inclusive (with the ulp guard on the right side)
    assert snap(25 * 1.001) == ("25", True)
    assert snap(25 * 0.999) == ("25", True)
    assert snap(26.32) == ("25", False)  # unsnappable; nearest named
    assert snap(123.0) == ("60000/1001", False)


# ---- E4: unsnappable refuses BEFORE any extraction ---------------------------


def test_unsnappable_refuses_before_any_extraction(tmp_path):
    """E4: a derived rate off the canonical map (163,509 frames over a
    123.4 s extent -> ~1325 fps) refuses pre-GPU: zero extraction calls,
    zero engine invocations, and the refusal names the derived rate,
    both operands, and the nearest candidate."""
    engine, runner, plan = derive_only(
        tmp_path,
        prepared_frames=163509,
        prepared_duration=6818.4,
        audio_stream_duration=123.4,
        source_format_absent=True,  # only the (broken) stream exists
    )
    with pytest.raises(UpscaleError) as excinfo:
        engine.run(
            str(tmp_path / "movie.prepared.mkv"), plan,
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    message = str(excinfo.value)
    assert "1325.032" in message  # the derived rate, named
    assert "163509" in message  # both operands, named
    assert "123.400" in message
    assert "60000/1001" in message  # the nearest candidate, named
    assert "snap" in message
    # NOTHING ran: no extraction, no engine call (fail-open in seconds)
    assert all(
        "frame_%08d.png" not in a for _b, a in runner.commands
    )
    assert engine.invocations == []


# ---- E5: the Karen snap-band cross-provenance refusal -----------------------


def test_cross_provenance_snap_band_refusal(tmp_path):
    """E5 (the Karen finding, first-class gate): a true-23.976 film whose
    a:0 stream duration tag under-reports (6814.0 s vs the true
    6818.4 s) derives 23.99 fps -> snaps '24' on the audio side while
    the prepared file's own duration (163,509 / 6818.4) stays in the
    24000/1001 family. Disagreement ACROSS provenance is the lie
    signature -> refuse, naming both provenances and both snaps."""
    engine, runner, plan = derive_only(
        tmp_path,
        prepared_frames=163509,
        prepared_duration=6818.4,  # honest prepared format duration
        avg_rate="24000/1001",
        r_rate="24000/1001",
        audio_stream_duration=6814.0,  # under-reported (the lie)
        source_format_absent=True,  # only one audio provenance exists
        plan_duration=6818.4,
    )
    with pytest.raises(UpscaleError) as excinfo:
        engine.run(
            str(tmp_path / "movie.prepared.mkv"), plan,
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    message = str(excinfo.value)
    assert "cross-provenance" in message
    assert "prepared_format_duration" in message  # both provenances named
    assert "'24'" in message and "'24000/1001'" in message  # both snaps
    # nothing ran (pre-GPU refusal)
    assert all(
        "frame_%08d.png" not in a for _b, a in runner.commands
    )
    assert engine.invocations == []


# ---- E6: audio-extent sanity minimums ---------------------------------------


def test_audio_extent_sanity_minimums(tmp_path):
    """E6: the denominator sanity gate (B.2, port of .34 §1.1(3)):
    missing / <= 0 / < 10 s refuse; a 26% stream-vs-format skew
    refuses; a 5% skew passes the SKEW gate (the snap cross-checks
    remain the fine-grain defense); no audio -> format fallback with
    the provenance flag recorded."""
    from vidlift.engine_ncnn import probe_audio_extent_seconds

    tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")

    class AudioRunner:
        def __init__(self, *, stream=None, fmt=None, stream_exit=0):
            self.stream = stream
            self.fmt = fmt
            self.stream_exit = stream_exit
            self.commands: list[tuple[str, tuple[str, ...]]] = []

        def __call__(self, binary, args):
            self.commands.append((binary, tuple(args)))
            if "a:0" in args and "stream=duration" in args:
                if self.stream_exit != 0 or self.stream is None:
                    return 1, "", "no audio stream"
                return 0, f"{self.stream}\n", ""
            if "-show_format" in args:
                if self.fmt is None:
                    return 0, json.dumps({"streams": [], "format": {}}), ""
                return (
                    0,
                    json.dumps(
                        {"streams": [], "format": {"duration": str(self.fmt)}}
                    ),
                    "",
                )
            return 1, "", "unexpected"

    # missing entirely (no stream, no format) -> refuse
    with pytest.raises(UpscaleError, match="no usable audio extent"):
        probe_audio_extent_seconds(
            "/media/movie.avi", ffprobe="ffprobe", runner=AudioRunner()
        )
    # no audio stream -> format fallback carries the provenance flag
    extent = probe_audio_extent_seconds(
        "/media/movie.avi",
        ffprobe="ffprobe",
        runner=AudioRunner(fmt=5400.0),
    )
    assert extent.seconds == pytest.approx(5400.0)
    assert extent.source == "format"
    # < 10 s refuses (a broken probe, not a stamp source)
    with pytest.raises(UpscaleError, match="sanity minimum"):
        probe_audio_extent_seconds(
            "/media/movie.avi",
            ffprobe="ffprobe",
            runner=AudioRunner(stream=9.9),
        )
    # <= 0 refuses
    with pytest.raises(UpscaleError, match="no usable audio extent"):
        probe_audio_extent_seconds(
            "/media/movie.avi",
            ffprobe="ffprobe",
            runner=AudioRunner(fmt=0.0),
        )
    # 26% stream-vs-format skew refuses
    with pytest.raises(UpscaleError, match="skew"):
        probe_audio_extent_seconds(
            "/media/movie.avi",
            ffprobe="ffprobe",
            runner=AudioRunner(stream=4000.0, fmt=5400.0),
        )
    # 5% skew passes the SKEW gate (proceeds to the snap cross-checks)
    extent = probe_audio_extent_seconds(
        "/media/movie.avi",
        ffprobe="ffprobe",
        runner=AudioRunner(stream=5400.0, fmt=5130.0),
    )
    assert extent.source == "stream"
    assert extent.stream_seconds == pytest.approx(5400.0)
    assert extent.format_seconds == pytest.approx(5130.0)


# ---- E7: extraction is frame-index addressed ---------------------------------


def test_extraction_uses_frame_index_select(tmp_path):
    """E7: at 23.976 fps with 120 s chunks, F = 2877; chunk 2 (idx 1)
    extracts frames [2877, 5753] with select=between(n,...), -vsync 0,
    and NO -ss/-t."""
    engine, runner, plan = derive_only(
        tmp_path,
        prepared_frames=10000,
        prepared_duration=10000 / (24000 / 1001),
        audio_stream_duration=10000 / (24000 / 1001),
        chunk_seconds=120,
    )
    result = engine.run(
        str(tmp_path / "movie.prepared.mkv"), plan,
        ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    extraction_cmds = [
        a for _b, a in runner.commands
        if a and str(a[-1]).endswith("frame_%08d.png")
    ]
    assert len(extraction_cmds) == 4  # E8's chunk count
    second = extraction_cmds[1]
    assert second[second.index("-vf") + 1] == "select='between(n,2877,5753)'"
    assert "-ss" not in second and "-t" not in second
    assert second[second.index("-vsync") + 1] == "0"


# ---- E8: the chunk math, remainder included ----------------------------------


def test_chunk_math_and_remainder(tmp_path):
    """E8: N=10000 at 24000/1001 with 120 s chunks -> F=2877,
    chunks [2877, 2877, 2877, 1369]."""
    engine, runner, plan = derive_only(
        tmp_path,
        prepared_frames=10000,
        prepared_duration=10000 / (24000 / 1001),
        audio_stream_duration=10000 / (24000 / 1001),
        chunk_seconds=120,
    )
    result = engine.run(
        str(tmp_path / "movie.prepared.mkv"), plan,
        ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    extraction_cmds = [
        a for _b, a in runner.commands
        if a and str(a[-1]).endswith("frame_%08d.png")
    ]
    counts = []
    for args in extraction_cmds:
        vf = args[args.index("-vf") + 1]
        match = re.search(r"between\(n,(\d+),(\d+)\)", vf)
        counts.append(int(match.group(2)) - int(match.group(1)) + 1)
    assert counts == [2877, 2877, 2877, 1369]
    assert result.frames_done == 10000


# ---- E9: per-chunk count assertions, in and out ------------------------------


def test_per_chunk_count_assertion_in_and_out(tmp_path):
    """E9: an extraction under-count refuses naming idx/first/count/
    actual; an engine under-count refuses the same way."""
    # in-dir under-count: the fake writes one frame short when asked
    class ShortExtractionRunner(EngineFakeRunner):
        def __call__(self, binary, args):
            if (
                args
                and str(args[-1]).endswith("frame_%08d.png")
                and "-vf" in args
            ):
                self.commands.append((binary, tuple(args)))
                vf = args[args.index("-vf") + 1]
                match = re.search(r"between\(n,(\d+),(\d+)\)", vf)
                count = (
                    int(match.group(2)) - int(match.group(1)) + 1
                    if match
                    else self.frames_per_chunk
                )
                out_dir = Path(args[-1]).parent
                width, height = self.png_geometry
                for i in range(1, count):  # one short
                    make_png(out_dir / f"frame_{i:08d}.png", width, height)
                return 0, "", ""
            return super().__call__(binary, args)

    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    # 25 fps x 12 s chunks -> F = 300; N = 900 -> 3 chunks (fast fixture)
    runner = ShortExtractionRunner(
        prepared_duration=36.0, prepared_frames=900,
        audio_stream_duration=36.0, source_format_duration=36.0,
    )
    models, table = make_fixture_models(tmp_path / "models")
    engine = NcnnVulkanEngine(
        binary=str(make_fake_binary(tmp_path / "bin")),
        models_dir=str(models),
        artifacts=table,
        runner=runner,
        engine_runner=FakeEngineProcess(),
        chunk_seconds=12,
    )
    with pytest.raises(UpscaleError) as excinfo:
        engine.run(
            str(prepared), make_plan(duration=36.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    message = str(excinfo.value)
    assert "extraction count mismatch" in message
    assert "expected 300 frames" in message
    assert "extracted 299" in message

    # out-dir under-count: the engine drops a frame
    class DroppingEngineProcess(FakeEngineProcess):
        def __call__(self, binary, args):
            self.calls.append(tuple(args))
            in_dir = Path(args[args.index("-i") + 1])
            out_dir = Path(args[args.index("-o") + 1])
            frames = sorted(in_dir.glob("*.png"))
            width, height = self.output_geometry
            for i in range(1, len(frames)):  # one short
                make_png(out_dir / f"frame_{i:08d}.png", width, height)
            return 0

    prepared2 = tmp_path / "movie2.prepared.mkv"
    prepared2.write_bytes(b"prepared-stub")
    runner2 = EngineFakeRunner(
        prepared_duration=36.0, prepared_frames=900,
        audio_stream_duration=36.0, source_format_duration=36.0,
    )
    engine2 = NcnnVulkanEngine(
        binary=str(make_fake_binary(tmp_path / "bin")),
        models_dir=str(models),
        artifacts=table,
        runner=runner2,
        engine_runner=DroppingEngineProcess(),
        chunk_seconds=12,
    )
    with pytest.raises(UpscaleError) as excinfo2:
        engine2.run(
            str(prepared2), make_plan(duration=36.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    message2 = str(excinfo2.value)
    assert "engine output count mismatch" in message2
    assert "expected 300 frames" in message2
    assert "engine produced 299" in message2


# ---- E10: the exact frame gate ------------------------------------------------


def test_frames_gate_exact_equality(tmp_path):
    """E10: +1/-1 mezzanine frame mismatch refuses with both counts;
    a matching count passes; an unprobeable mezzanine refuses.
    (N=900 at 25 fps keeps the fixture fast; the gate is the point.)"""
    for mezzanine_frames in (901, 899):
        engine, runner, plan = derive_only(
            tmp_path,
            prepared_frames=900,
            prepared_duration=36.0,
            audio_stream_duration=36.0,
            mezzanine_frames=mezzanine_frames,
            chunk_seconds=12,
        )
        prepared = tmp_path / f"movie{mezzanine_frames}.prepared.mkv"
        prepared.write_bytes(b"prepared-stub")
        with pytest.raises(UpscaleError) as excinfo:
            engine.run(
                str(prepared), plan,
                ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            )
        message = str(excinfo.value)
        assert "frame gate" in message
        assert str(mezzanine_frames) in message
        assert "900" in message
        # B.5: the miscounted mezzanine was retained
        rejects = list(
            prepared.parent.glob(f"{prepared.stem}.rejected-*.mkv")
        )
        assert len(rejects) == 1

    # matching passes
    engine, runner, plan = derive_only(
        tmp_path,
        prepared_frames=900,
        prepared_duration=36.0,
        audio_stream_duration=36.0,
        chunk_seconds=12,
    )
    prepared = tmp_path / "movie-ok.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    result = engine.run(
        str(prepared), plan, ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
    )
    assert result.frame_gates["mezzanine_vs_prepared"]["ok"] is True
    assert result.frame_gates["mezzanine_vs_prepared"]["expected"] == 900
    assert result.frame_gates["mezzanine_vs_prepared"]["actual"] == 900

    # unprobeable mezzanine refuses (never a silent pass)
    engine, runner, plan = derive_only(
        tmp_path,
        prepared_frames=900,
        prepared_duration=36.0,
        audio_stream_duration=36.0,
        chunk_seconds=12,
    )
    prepared = tmp_path / "movie-blind.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner.mezzanine_count_probe_fails = True
    with pytest.raises(UpscaleError, match="could not be probed"):
        engine.run(
            str(prepared), plan, ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        )


# ---- E11: the clock-gate tolerance vs the audio extent -----------------------


def test_clock_gate_audio_extent_tolerance(tmp_path):
    """E11: the tolerance rule max(2.0 s, 0.1%) pinned at unit scale
    with the field-proven 6818.4 s figure, plus end-to-end pass/fail
    shapes at test-friendly scale (a rate-class lie the old gate M
    could not see)."""
    from vidlift.engine_ncnn import clock_gate_tolerance

    # unit: the tolerance rule itself, with the design's numbers
    assert clock_gate_tolerance(6818.4) == pytest.approx(6.8184)
    assert clock_gate_tolerance(5400.0) == pytest.approx(5.4)
    assert clock_gate_tolerance(90.0) == pytest.approx(2.0)
    assert clock_gate_tolerance(60.0) == pytest.approx(2.0)

    # pass shape: mezzanine 36.9 vs audio 36.0 (0.9 s, tol 2.0 s)
    engine, runner, plan = derive_only(
        tmp_path,
        prepared_frames=900,
        prepared_duration=36.0,
        audio_stream_duration=36.0,
        chunk_seconds=12,
    )
    prepared = tmp_path / "movie-pass.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner.mezzanine_duration = 36.9
    result = engine.run(
        str(prepared), plan, ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
    )
    gate = result.duration_gates["mezzanine_vs_audio"]
    assert gate["ok"] is True
    assert gate["tolerance_s"] == pytest.approx(2.0)

    # fail shape: a 30%-off mezzanine (the rate-class lie)
    engine, runner, plan = derive_only(
        tmp_path,
        prepared_frames=900,
        prepared_duration=36.0,
        audio_stream_duration=36.0,
        chunk_seconds=12,
    )
    prepared = tmp_path / "movie-fail.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner.mezzanine_duration = 27.0
    with pytest.raises(UpscaleError) as excinfo:
        engine.run(
            str(prepared), plan, ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        )
    assert "mezzanine_vs_audio" in str(excinfo.value)
    assert "27.000" in str(excinfo.value)
    assert "36.000" in str(excinfo.value)


# ---- E12: rejected-mezzanine retention + diagnostic ---------------------------


def test_gate_trip_retains_rejected_mezzanine_and_diagnostic(tmp_path):
    """E12: a clock-gate trip renames the mezzanine to
    .rejected-<ts>.mkv, writes the diagnostic JSON with the design's
    keys, and the UpscaleError message names both paths."""
    engine, runner, plan = derive_only(
        tmp_path,
        prepared_frames=900,
        prepared_duration=36.0,
        audio_stream_duration=36.0,
        chunk_seconds=12,
    )
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner.mezzanine_duration = 60.0  # 24 s off: way past any tolerance
    with pytest.raises(UpscaleError) as excinfo:
        engine.run(
            str(prepared), plan, ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        )
    message = str(excinfo.value)
    assert "rejected mezzanine retained at" in message
    rejects = list(tmp_path.glob("movie.prepared.rejected-*.mkv"))
    diagnostics = list(
        tmp_path.glob("movie.prepared.rejected-*.diagnostic.json")
    )
    assert len(rejects) == 1 and len(diagnostics) == 1
    assert str(rejects[0]) in message and str(diagnostics[0]) in message
    # the original mezzanine path no longer exists (renamed, kept)
    assert not (tmp_path / "movie.prepared.upscaled.mkv").exists()
    diag = json.loads(diagnostics[0].read_text())
    for key in (
        "source_path", "prepared_path", "prepared_total_frames",
        "frame_count_from_metadata", "audio_extent_s", "audio_source",
        "derived_rate_raw", "derived_rate_snapped", "stamp_fps",
        "container_r_frame_rate", "container_avg_frame_rate",
        "cross_provenance", "chunk_frames", "chunks",
        "expected_per_chunk", "mezzanine_frame_count",
        "mezzanine_duration_s", "clock_tolerance_s", "error",
    ):
        assert key in diag, f"diagnostic missing {key}"
    assert diag["prepared_total_frames"] == 900
    assert diag["stamp_fps"] == "25"
    assert diag["mezzanine_duration_s"] == pytest.approx(60.0)
    assert diag["chunks"] == 3
    assert diag["expected_per_chunk"] == [300, 300, 300]
    assert "mezzanine_vs_audio" in diag["error"]
    # the exception carries the retention paths for the manifest (B.8)
    assert excinfo.value.rejected_mezzanine == {
        "path": str(rejects[0]),
        "diagnostic_path": str(diagnostics[0]),
    }


# ---- E13: steps + heartbeat through the state -------------------------------


def test_long_steps_emit_steps_and_heartbeat(tmp_path, monkeypatch):
    """E13: engine step labels land in state.progress['step'] through
    the on_step seam, and the runner's heartbeat pump advances
    last_activity_at during a simulated long encode."""
    import vidlift.run as run_module

    steps: list[str] = []
    engine, runner, plan = derive_only(
        tmp_path,
        prepared_frames=900,
        prepared_duration=36.0,
        audio_stream_duration=36.0,
        chunk_seconds=12,
    )
    engine.on_step = steps.append
    result = engine.run(
        str(tmp_path / "movie.prepared.mkv"), plan,
        ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        on_step=steps.append,
    )
    for label in (
        "prepared-count-probe", "concat", "gate-frames-probe",
        "gate-clock-probe",
    ):
        assert label in steps

    # the runner-side pump: a slow encode command through run_file
    monkeypatch.setattr(run_module, "HEARTBEAT_TOUCH_SECONDS", 0.05)
    monkeypatch.setattr(run_module, "HEARTBEAT_LOG_SECONDS", 0.2)
    from vidlift.run import run_file

    payload = json.dumps(
        {
            "streams": [
                {
                    "index": 0, "codec_type": "video", "codec_name": "mpeg4",
                    "width": 640, "height": 360,
                    "sample_aspect_ratio": "1:1", "field_order": "progressive",
                    "r_frame_rate": "25/1", "avg_frame_rate": "25/1",
                },
                {"index": 1, "codec_type": "audio", "codec_name": "mp3",
                 "channels": 2},
            ],
            "format": {"format_name": "avi", "duration": "5400.0"},
        }
    )
    seen_activity: list[float] = []
    seen_steps: list[str] = []
    encode_entered = threading.Event()

    class SlowEncodeRunner:
        """A command seam whose encode call blocks 0.4 s — long enough
        for the shrunk pump to fire multiple touches."""

        def __init__(self):
            self.commands: list[tuple[str, tuple[str, ...]]] = []

        def __call__(self, binary, args):
            self.commands.append((binary, tuple(args)))
            if "libx265" in args:
                encode_entered.set()
                time.sleep(0.4)
            if "-show_format" in args:
                return 0, payload, ""
            if "-show_streams" in args:
                return (
                    0,
                    json.dumps(
                        {"streams": [
                            {"codec_type": "video", "width": 1280,
                             "height": 720}
                        ]}
                    ),
                    "",
                )
            if args and args[-1] == "-" and "null" in args:
                return 0, "", ""
            if args and str(args[-1]).endswith(".prepared.mkv"):
                Path(args[-1]).write_bytes(b"prepared-stub")
                return 0, "", ""
            if args and str(args[-1]).endswith(".hevc.mp4"):
                Path(args[-1]).write_bytes(b"output-stub")
                return 0, "", ""
            return 1, "", f"unexpected: {binary} {' '.join(args)}"

    media = tmp_path / "movie.avi"
    media.write_text("")
    slow = SlowEncodeRunner()
    state_box: dict = {}

    # drive the run on a thread so this test can observe the live state
    result_box: dict = {}

    def _run():
        result_box["value"] = run_file(
            str(media),
            tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            probe_runner=slow,
            command_runner=slow,
            log_dir=str(tmp_path / "runs"),
            state_box=state_box,
        )

    run_thread = threading.Thread(target=_run, daemon=True)
    run_thread.start()
    # wait for the encode to begin, then sample the live state
    assert encode_entered.wait(timeout=5)
    for _ in range(20):
        state = state_box.get("state")
        if state is not None:
            if state.progress:
                seen_steps.append(state.progress.get("step"))
                seen_activity.append(state.last_activity_at)
        time.sleep(0.03)
        if not run_thread.is_alive():
            break
    run_thread.join(timeout=10)
    assert result_box["value"].ok is True
    assert "encode" in seen_steps
    assert seen_activity == sorted(seen_activity)  # activity advanced
    log_text = Path(result_box["value"].state.log.path).read_text()
    assert "heartbeat" in log_text  # the log tail disproves "stuck"


# ---- E14: per-chunk resume ----------------------------------------------------


def test_resume_from_checkpoint_skips_completed_chunks(tmp_path):
    """E14: a failed run's checkpoint lets the retry skip completed
    chunks (engine invoked only for the missing one); a params
    mismatch or a truncated segment deletes the workdir wholesale.
    (12 s chunks x 25 fps -> F = 300; N = 900 -> 3 chunks, fast.)"""
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")

    def make_engine(runner, process):
        models, table = make_fixture_models(tmp_path / "models")
        return NcnnVulkanEngine(
            binary=str(make_fake_binary(tmp_path / "bin")),
            models_dir=str(models),
            artifacts=table,
            runner=runner,
            engine_runner=process,
            chunk_seconds=12,
        )

    def fixture_runner():
        return EngineFakeRunner(
            prepared_duration=36.0, mezzanine_duration=36.0,
            prepared_frames=900, segment_duration=12.0,
            audio_stream_duration=36.0, source_format_duration=36.0,
        )

    # run 1: the third chunk's engine dies; chunks 0-1 complete and are
    # checkpointed
    runner1 = fixture_runner()
    process1 = FakeEngineProcess(fail_on={2})
    engine1 = make_engine(runner1, process1)
    with pytest.raises(UpscaleError, match="failed with code 1"):
        engine1.run(
            str(prepared), make_plan(duration=36.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    workdir = tmp_path / "movie.engine-work"
    assert (workdir / "chunk_state.json").is_file()

    # run 2: same prepared file, same params -> resume; the engine is
    # invoked ONLY for the missing chunk, concat covers all three
    runner2 = fixture_runner()
    process2 = FakeEngineProcess()
    engine2 = make_engine(runner2, process2)
    result = engine2.run(
        str(prepared), make_plan(duration=36.0),
        ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    assert len(engine2.invocations) == 1  # chunk 2 only
    assert engine2.invocations[0].chunk_index == 2
    assert result.frames_done == 900
    assert result.resume == {"checkpoint_used": True, "chunks_reused": 2}
    assert result.frame_gates["mezzanine_vs_prepared"]["ok"] is True
    # success deletes the workdir (and its checkpoint)
    assert not workdir.exists()

    # params mismatch -> wholesale delete, clean restart: a checkpoint
    # from a DIFFERENT frame total must never be trusted
    runner3 = fixture_runner()
    process_fail = FakeEngineProcess(fail_on={2})
    engine3 = make_engine(runner3, process_fail)
    with pytest.raises(UpscaleError):
        engine3.run(
            str(prepared), make_plan(duration=36.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    checkpoint = json.loads((workdir / "chunk_state.json").read_text())
    checkpoint["prepared_total_frames"] = 999999  # poisoned
    (workdir / "chunk_state.json").write_text(json.dumps(checkpoint))
    runner4 = fixture_runner()
    process4 = FakeEngineProcess()
    engine4 = make_engine(runner4, process4)
    result4 = engine4.run(
        str(prepared), make_plan(duration=36.0),
        ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    assert len(engine4.invocations) == 3  # every chunk re-ran
    assert result4.resume == {"checkpoint_used": False, "chunks_reused": 0}

    # truncated segment -> wholesale delete, clean restart
    runner5 = fixture_runner()
    process_fail2 = FakeEngineProcess(fail_on={2})
    engine5 = make_engine(runner5, process_fail2)
    with pytest.raises(UpscaleError):
        engine5.run(
            str(prepared), make_plan(duration=36.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    # truncate the recorded size: the segment file shrank (the
    # kill-mid-segment shape) — the checkpoint's recorded size no longer
    # matches, so the whole workdir is refused
    (workdir / "chunk_0000" / "segment.mkv").write_bytes(b"truncated")
    runner6 = fixture_runner()
    process6 = FakeEngineProcess()
    engine6 = make_engine(runner6, process6)
    result6 = engine6.run(
        str(prepared), make_plan(duration=36.0),
        ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    assert len(engine6.invocations) == 3  # the truncated cache was refused
    assert result6.resume == {"checkpoint_used": False, "chunks_reused": 0}


def test_resume_truncated_segment_duration_restarts_clean(tmp_path):
    """E14 companion: a segment whose DURATION probe sits more than one
    frame-time from count/stamp (the truncated-write shape) is refused
    with a wholesale workdir delete, even when the file size is right."""
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")

    def make_engine(runner, process):
        models, table = make_fixture_models(tmp_path / "models")
        return NcnnVulkanEngine(
            binary=str(make_fake_binary(tmp_path / "bin")),
            models_dir=str(models),
            artifacts=table,
            runner=runner,
            engine_runner=process,
            chunk_seconds=12,  # F = 300 at 25 fps; N = 900 -> 3 chunks
        )

    runner1 = EngineFakeRunner(
        prepared_duration=36.0, mezzanine_duration=36.0,
        prepared_frames=900, segment_duration=12.0,
        audio_stream_duration=36.0, source_format_duration=36.0,
    )
    with pytest.raises(UpscaleError):
        make_engine(runner1, FakeEngineProcess(fail_on={2})).run(
            str(prepared), make_plan(duration=36.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    workdir = tmp_path / "movie.engine-work"
    # the recorded size is correct but the duration probe lies short
    # (a truncated write that kept its placeholder bytes)
    runner2 = EngineFakeRunner(
        prepared_duration=36.0, mezzanine_duration=36.0,
        prepared_frames=900, segment_duration=6.0,  # half a chunk long
        audio_stream_duration=36.0, source_format_duration=36.0,
    )
    result = make_engine(runner2, FakeEngineProcess()).run(
        str(prepared), make_plan(duration=36.0),
        ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    assert result.resume == {"checkpoint_used": False, "chunks_reused": 0}


# ---- E15: progress totals are decode-truth, not estimates ---------------------


def test_progress_reporting_uses_exact_total(tmp_path):
    """E15: frames_total == prepared_total_frames (the decode-counted
    total), never the ceil(duration x container fps) estimate — the
    container declares 30000/1001 while decode-truth is 25 fps, so the
    old estimate would have said 8991."""
    seen: list[tuple[int, int]] = []
    engine, runner, plan = derive_only(
        tmp_path,
        prepared_frames=900,
        prepared_duration=36.0,
        avg_rate="30000/1001",  # the container lies; decode-truth is 25
        r_rate="30000/1001",
        audio_stream_duration=36.0,
        chunk_seconds=12,
    )
    result = engine.run(
        str(tmp_path / "movie.prepared.mkv"), plan,
        ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        on_progress=lambda done, total: seen.append((done, total)),
    )
    assert seen
    for _done, total in seen:
        assert total == 900  # the decode-counted total, not ceil(36x29.97)=1079
    assert seen[-1] == (900, 900)
    assert result.frames_done == 900


# ---- b0: execution canary (runs LAST; pytest executes in file order) ------------


def test_b0_execution_canary_all_boundary_tests_ran():
    marker("b0")
    # the engine module imports (collection would have failed otherwise);
    # no importorskip exists in this file
    assert engine_module is not None
    assert engine_module.MODEL_TABLE["live-action"].native_scale == 2
    # every expected b-series boundary test provably executed this session
    missing = _EXPECTED_B_TESTS - _EXECUTED_B_TESTS
    assert not missing, f"boundary tests did not execute: {sorted(missing)}"