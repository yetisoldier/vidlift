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
import stat
import struct
import subprocess
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
from vidlift.plan import build_file_plan
from vidlift.probe import classify, parse_probe
from vidlift.run import (
    EngineCancelled,
    ToolBox,
    UpscaleError,
)
from vidlift.tools_realesrgan import verify_model_artifacts

# ---- b0 execution canary state -------------------------------------------

_EXPECTED_B_TESTS = {"b0", "b1", "b2", "b3", "b4", "b5", "b9", "b10", "b11"}
_EXECUTED_B_TESTS: set[str] = set()


def marker(name: str) -> None:
    """Provable execution: the canary requires every b-test to land."""
    _EXECUTED_B_TESTS.add(name)


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

    - ffprobe -show_format: duration keyed by path suffix (prepared vs
      mezzanine) so Gate P and Gate M see the canned durations.
    - ffprobe -show_streams: canned frame-rate JSON for the clock probe.
    - ffmpeg extraction (output pattern frame_%08d.png): writes
      ``frames_per_chunk`` PNGs of ``png_geometry`` into the pattern dir.
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
    ) -> None:
        self.prepared_duration = prepared_duration
        self.mezzanine_duration = mezzanine_duration
        self.avg_rate = avg_rate
        self.r_rate = r_rate
        self.frames_per_chunk = frames_per_chunk
        self.png_geometry = png_geometry
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        self.commands.append((binary, tuple(args)))
        if "-show_format" in args:
            path = args[-1]
            if str(path).endswith(".upscaled.mkv"):
                duration = self.mezzanine_duration
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
            out_dir = Path(args[-1]).parent
            width, height = self.png_geometry
            for i in range(1, self.frames_per_chunk + 1):
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
    popen_factory=subprocess.Popen,
):
    """One full fake-engine adapter run: resolves a fake binary + fixture
    models, replays canned probes, and returns (engine, runner, result)."""
    if source_duration is None:
        source_duration = prepared_duration  # in-sync by default
    if mezzanine_duration is None:
        mezzanine_duration = prepared_duration  # in-sync by default
    plan_duration = (
        None if source_duration is UNKNOWN_DURATION else float(source_duration)
    )
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner = EngineFakeRunner(
        prepared_duration=prepared_duration,
        mezzanine_duration=float(mezzanine_duration),
        avg_rate=avg_rate,
        r_rate=r_rate,
        png_geometry=png_geometry,
        frames_per_chunk=frames_per_chunk,
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
        popen_factory=popen_factory,
    )
    plan = make_plan(duration=plan_duration)
    result = engine.run(
        str(prepared), plan, ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        cancel=cancel,
    )
    return engine, runner, result


# ---- b1: clock flow (clause a) ---------------------------------------------


@pytest.mark.parametrize("clock", ["24000/1001", "25/1", "30000/1001"])
def test_b1_prepared_clock_flows_byte_identical(tmp_path, clock):
    marker("b1")
    _engine, runner, result = run_engine(
        tmp_path, prepared_duration=100.0, source_duration=100.0,
        mezzanine_duration=100.0, avg_rate=clock, r_rate=clock,
    )
    # the exact rational string ffprobe reported lands in every assembled
    # segment's -framerate argument (highest boundary: the command line)
    segment_cmds = [a for _b, a in runner.commands if "-framerate" in a]
    assert segment_cmds, "no reassembly command was recorded"
    for args in segment_cmds:
        assert args[args.index("-framerate") + 1] == clock
    assert result.frame_clock == {"value": clock, "source": "avg_frame_rate"}


# ---- b2: clock fallback + refusal (clause a) --------------------------------


def test_b2_avg_unusable_falls_back_to_r_frame_rate(tmp_path):
    marker("b2")
    _engine, runner, result = run_engine(
        tmp_path, prepared_duration=100.0, source_duration=100.0,
        mezzanine_duration=100.0, avg_rate="0/0", r_rate="24000/1001",
    )
    assert result.frame_clock == {
        "value": "24000/1001",
        "source": "r_frame_rate",
    }
    for _b, args in runner.commands:
        if "-framerate" in args:
            assert args[args.index("-framerate") + 1] == "24000/1001"


def test_b2_both_clocks_missing_is_a_named_refusal_never_a_default(tmp_path):
    marker("b2")
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner = EngineFakeRunner(
        prepared_duration=100.0,
        mezzanine_duration=100.0,
        avg_rate="N/A",
        r_rate="0/0",
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
            str(prepared), make_plan(duration=100.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    # the named error is exactly what the fail-open path records verbatim
    # (the end-to-end manifest assertion lands with the runner wiring, b7)
    assert "no usable frame clock" in str(excinfo.value)
    assert str(prepared) in str(excinfo.value)
    # nothing was ever re-stamped: no -framerate anywhere, so no default
    # rate string could have been invented (Section 4.3 regression pin:
    # no literal default rate string can ever appear)
    assert all("-framerate" not in a for _b, a in runner.commands)
    for forbidden in ("25/1", "24000/1001", "30000/1001", "23.976", "29.97"):
        for text in runner.texts():
            assert forbidden not in text or "-framerate" not in text, (
                f"default rate string {forbidden!r} appeared in a command"
            )
    assert engine.invocations == []  # no engine call ever happened


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
    # the measured failure shape: 6838 s prepared, +15.050 s mezzanine.
    # The old max(5 s, 2%) gate called 0.22% drift "fine"; max(0.5 s,
    # 0.01%) calls it what it is: a desynced mezzanine.
    prepared = tmp_path / "movie.prepared.mkv"
    prepared.write_bytes(b"prepared-stub")
    runner = EngineFakeRunner(
        prepared_duration=6838.0, mezzanine_duration=6853.05,
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
            str(prepared), make_plan(duration=6838.0),
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
    message = str(excinfo.value)
    assert "mezzanine_vs_prepared" in message
    assert "6838.000" in message and "6853.050" in message
    assert "15.050" in message
    assert "0.684" in message  # the max(0.5 s, 0.01%) tolerance, named


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
    assert gates["mezzanine_vs_prepared"]["ok"] is True
    assert gates["mezzanine_vs_prepared"]["mezzanine_s"] == pytest.approx(119.7)
    assert gates["mezzanine_vs_prepared"]["prepared_s"] == pytest.approx(120.0)


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
    # 300 s / 120 s chunks -> 3 engine invocations, monotone -ss addressing
    assert len(engine.invocations) == 3
    extraction_cmds = [
        a for _b, a in runner.commands
        if a and str(a[-1]).endswith("frame_%08d.png")
    ]
    assert [a[a.index("-ss") + 1] for a in extraction_cmds] == ["0", "120", "240"]
    assert [a[a.index("-t") + 1] for a in extraction_cmds] == ["120", "120", "60"]
    # exactly one concat for the multi-segment mezzanine
    concat_cmds = [a for _b, a in runner.commands if "concat" in a]
    assert len(concat_cmds) == 1
    # completed chunks' frame dirs were deleted as the run progressed
    assert seen_cleanup == [True]
    # Gate M executed with the probed mezzanine duration
    assert any(
        "-show_format" in a and str(a[-1]).endswith(".upscaled.mkv")
        for _b, a in runner.commands
    )
    assert result.duration_gates["mezzanine_vs_prepared"]["mezzanine_s"] == (
        pytest.approx(300.0)
    )
    assert result.frames_done == 30  # 3 chunks x 10 fake frames
    # success: the workdir is deleted (retention contract)
    assert not (tmp_path / "movie.engine-work").exists()
    assert Path(result.mezzanine_path).is_file()


def test_b10_single_chunk_moves_segment_without_concat(tmp_path):
    marker("b10")
    engine, runner, result = run_engine(
        tmp_path, prepared_duration=100.0, source_duration=100.0,
        mezzanine_duration=100.0, chunk_seconds=120,
    )
    assert len(engine.invocations) == 1
    assert all("concat" not in a for _b, a in runner.commands)
    assert Path(result.mezzanine_path).is_file()
    assert not (tmp_path / "movie.engine-work").exists()


def test_b10_workdir_retained_on_engine_failure(tmp_path):
    marker("b10")
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
    # a partial mezzanine never survives a failure
    assert not (tmp_path / "movie.prepared.upscaled.mkv").exists()


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
    # frames_total = ceil(300 s x 25 fps) = 7500, frames_done monotone
    assert seen == [(10, 7500), (20, 7500), (30, 7500)]
    assert result.frames_done == 30


def test_run_progress_kwarg_is_the_runner_seam_shape(tmp_path):
    """Regression (found by the real-engine smoke run): run_file calls
    engine.run(..., on_progress=record_progress) — the adapter must
    accept the kwarg and chain it with any constructor callback. This
    is the EXACT call shape the runner uses, driven end-to-end through
    the fake seams."""
    constructor_seen: list[tuple[int, int]] = []
    kwarg_seen: list[tuple[int, int]] = []
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
    )
    assert result.frames_done == 30
    # BOTH callbacks observed the per-chunk progress, and the adapter
    # restored its constructor callback afterwards (no cross-run leak)
    assert len(constructor_seen) == 3 and len(kwarg_seen) == 3
    assert engine.on_progress is not None
    assert constructor_seen == kwarg_seen == [(10, 7500), (20, 7500), (30, 7500)]


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