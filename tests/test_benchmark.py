"""Boundary tests for the measured benchmark step (b6, clause c).

All headless: fake probe JSON, fake prepare/engine effects, real cache
files under a temp VIDLIFT_STATE_DIR. The benchmark's honesty contract
is the point — no theory ETAs, stub numbers always labeled — so every
test asserts the label as hard as the number.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vidlift import cli
from vidlift.benchmark import (
    BENCHMARKS_FILENAME,
    STUB_NOTE,
    BenchResult,
    BenchmarkCacheError,
    build_slice_prepare_command,
    clamp_slice_seconds,
    estimate_eta,
    estimate_key,
    geometry_bucket,
    load_estimates,
    record_estimate,
    run_benchmark,
)
from vidlift.plan import attach_estimate, build_file_plan
from vidlift.probe import classify, parse_probe
from vidlift.run import (
    EngineResult,
    StubEngine,
    ToolBox,
    UpscaleError,
)


def make_media_file(tmp_path, name: str = "movie.avi") -> str:
    path = tmp_path / name
    path.write_text("")
    return str(path)


def probe_payload(*, duration: str = "5400.0", fps: str = "25/1") -> str:
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
                    "r_frame_rate": fps,
                    "avg_frame_rate": fps,
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


class BenchRunner:
    """Fake seam: canned probe JSON, prepare writes the slice file.

    Every command is recorded; the benchmark only needs probe (json),
    prepare (writes .prepared.mkv). ``clock_scale`` lets tests make the
    wall-clock deterministic for exact-arithmetic assertions.
    """

    def __init__(self, payload: str, *, prepare_exit: int = 0) -> None:
        self.payload = payload
        self.prepare_exit = prepare_exit
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        self.commands.append((binary, tuple(args)))
        if "-show_format" in args:
            return 0, self.payload, ""
        if "-show_streams" in args and "-show_format" not in args:
            # probe_and_classify uses one command with both flags; a
            # streams-only command (the adapter's clock probe) never
            # happens under the stub benchmark
            return 0, self.payload, ""
        if args and str(args[-1]).endswith(".prepared.mkv"):
            if self.prepare_exit == 0:
                Path(args[-1]).write_bytes(b"slice-stub")
            return self.prepare_exit, "", "prepare boom" if self.prepare_exit else ""
        return 1, "", f"unexpected: {binary} {' '.join(args)}"


class TimedStubEngine(StubEngine):
    """A stub whose engine phase takes an exact wall-clock time and
    reports an exact frame count — the two numbers b6's exact
    arithmetic asserts."""

    def __init__(self, *, frames: int, seconds: float) -> None:
        super().__init__()
        self.frames = frames
        self.seconds = seconds

    def run(self, prepared_path, plan, tools, *, cancel=None, on_progress=None):
        import time as _time

        _time.sleep(self.seconds)
        return EngineResult(
            mezzanine_path=prepared_path,
            frames_done=self.frames,
            note=self.note,
            simulated=True,
        )


class FailingEngine(StubEngine):
    def run(self, prepared_path, plan, tools, *, cancel=None, on_progress=None):
        raise UpscaleError("engine exploded during benchmark")


def bench_state(tmp_path, monkeypatch):
    """A temp state dir so cache tests never touch ~/.vidlift."""
    state = tmp_path / "state"
    monkeypatch.setenv("VIDLIFT_STATE_DIR", str(state))
    return state


def make_plan(duration: str = "5400.0") -> object:
    report = classify(parse_probe("/media/movie.avi", probe_payload(duration=duration)))
    return build_file_plan(report)


def make_result(
    *,
    engine: str = "StubEngine",
    simulated: bool = True,
    model: str | None = None,
    bucket: str = "640x360",
    seconds_per_frame: float = 1.83,
    measured_at: float = 1_760_000_000.0,
) -> BenchResult:
    frames = 750
    return BenchResult(
        engine=engine,
        engine_simulated=simulated,
        model=model,
        native_scale=2 if model else None,
        geometry_bucket=bucket,
        duration_sliced_s=30.0,
        frames=frames,
        wall_seconds=frames * seconds_per_frame,
        seconds_per_frame=seconds_per_frame,
        measured_at=measured_at,
        host_note="stub" if simulated else "RTX 3090 Ti",
        notes=[STUB_NOTE] if simulated else [],
    )


class TestSliceClamping:
    def test_slice_clamps_to_the_binding_window(self):
        assert clamp_slice_seconds(30) == 30
        assert clamp_slice_seconds(None) == 30
        assert clamp_slice_seconds(3) == 10
        assert clamp_slice_seconds(5_000) == 120
        assert clamp_slice_seconds(10) == 10 and clamp_slice_seconds(120) == 120


class TestSlicePrepareCommand:
    def test_slice_prepare_is_the_real_chain_plus_wall_time(self):
        plan = make_plan()
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        cmd = build_slice_prepare_command(
            tools, plan, "/w/slice.prepared.mkv", slice_seconds=30
        )
        # the exact prepare chain a real run uses, with -t addressing
        assert cmd[0] == "ffmpeg"
        assert "setsar=1" in " ".join(cmd)
        assert cmd[cmd.index("-t") + 1] == "30"
        assert cmd[cmd.index("-t") + 2] == "-i"
        assert cmd[-1] == "/w/slice.prepared.mkv"


class TestRunBenchmark:
    def test_stub_benchmark_measures_and_labels_itself(self, tmp_path, monkeypatch):
        state = bench_state(tmp_path, monkeypatch)
        path = make_media_file(tmp_path)
        runner = BenchRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_benchmark(
            path,
            tools=tools,
            engine=TimedStubEngine(frames=750, seconds=0.05),
            slice_seconds=30,
            workdir=str(tmp_path / "bench"),
            probe_runner=runner,
            command_runner=runner,
        )
        # machinery numbers, honestly stamped
        assert result.engine == "TimedStubEngine"
        assert result.engine_simulated is True
        assert STUB_NOTE in result.notes
        assert result.geometry_bucket == "640x360"
        assert result.frames == 750
        assert result.seconds_per_frame > 0
        assert result.seconds_per_frame == pytest.approx(
            result.wall_seconds / 750
        )
        # the constant was recorded to the ONE cache
        cache_file = state / BENCHMARKS_FILENAME
        assert cache_file.is_file()
        data = json.loads(cache_file.read_text())
        assert len(data["benchmarks"]) == 1
        assert data["benchmarks"][0]["geometry_bucket"] == "640x360"

    def test_stub_frame_count_falls_back_to_slice_cadence(self, tmp_path, monkeypatch):
        # the default StubEngine reports frames_done=0 (it is a copy):
        # the machinery constant counts the slice's real frames and
        # says so in its notes
        bench_state(tmp_path, monkeypatch)
        path = make_media_file(tmp_path)
        runner = BenchRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_benchmark(
            path,
            tools=tools,
            engine=StubEngine(),
            slice_seconds=30,
            workdir=str(tmp_path / "bench"),
            probe_runner=runner,
            command_runner=runner,
        )
        # 30 s at 25 fps -> 750 frames counted from cadence
        assert result.frames == 750
        assert any("no AI pass ran" in note for note in result.notes)
        assert result.engine_simulated is True

    def test_real_engine_result_carries_model_and_not_simulated(
        self, tmp_path, monkeypatch
    ):
        bench_state(tmp_path, monkeypatch)
        path = make_media_file(tmp_path)
        runner = BenchRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")

        class RealishEngine(StubEngine):
            def run(self, prepared_path, plan, tools, *, cancel=None,
                    on_progress=None):
                return EngineResult(
                    mezzanine_path=prepared_path,
                    frames_done=3000,
                    note=None,
                    simulated=False,
                    engine_model="realesrgan-x2plus",
                    engine_native_scale=2,
                )

        result = run_benchmark(
            path,
            tools=tools,
            engine=RealishEngine(),
            slice_seconds=30,
            workdir=str(tmp_path / "bench"),
            probe_runner=runner,
            command_runner=runner,
        )
        assert result.engine_simulated is False
        assert result.model == "realesrgan-x2plus"
        assert STUB_NOTE not in result.notes

    def test_prepare_failure_is_a_named_benchmark_error(self, tmp_path, monkeypatch):
        bench_state(tmp_path, monkeypatch)
        path = make_media_file(tmp_path)
        runner = BenchRunner(probe_payload(), prepare_exit=1)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(BenchmarkCacheError, match="slice prepare failed"):
            run_benchmark(
                path,
                tools=tools,
                engine=StubEngine(),
                slice_seconds=30,
                workdir=str(tmp_path / "bench"),
                probe_runner=runner,
                command_runner=runner,
            )

    def test_engine_failure_propagates_not_cached(self, tmp_path, monkeypatch):
        state = bench_state(tmp_path, monkeypatch)
        path = make_media_file(tmp_path)
        runner = BenchRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(UpscaleError, match="engine exploded"):
            run_benchmark(
                path,
                tools=tools,
                engine=FailingEngine(),
                slice_seconds=30,
                workdir=str(tmp_path / "bench"),
                probe_runner=runner,
                command_runner=runner,
            )
        assert not (state / BENCHMARKS_FILENAME).exists()

    def test_non_candidate_sample_is_refused(self, tmp_path, monkeypatch):
        bench_state(tmp_path, monkeypatch)
        path = make_media_file(tmp_path)
        payload = probe_payload()
        payload = json.dumps(
            json.loads(payload) | {
                "streams": [
                    s | {"width": 1920, "height": 1080}
                    if s.get("codec_type") == "video"
                    else s
                    for s in json.loads(payload)["streams"]
                ]
            }
        )
        runner = BenchRunner(payload)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(BenchmarkCacheError, match="not runnable"):
            run_benchmark(
                path,
                tools=tools,
                engine=StubEngine(),
                slice_seconds=30,
                workdir=str(tmp_path / "bench"),
                probe_runner=runner,
                command_runner=runner,
            )


class TestCacheSemantics:
    def test_cache_is_append_only_and_latest_wins(self, tmp_path, monkeypatch):
        state = bench_state(tmp_path, monkeypatch)
        first = make_result(seconds_per_frame=1.0, measured_at=100.0)
        record_estimate(first)
        record_estimate(make_result(seconds_per_frame=2.0, measured_at=200.0))
        estimates = load_estimates()
        assert len(estimates) == 1
        bench = next(iter(estimates.values()))
        assert bench.seconds_per_frame == 2.0  # latest row wins
        # both rows are still on disk (append-only, never rewritten)
        rows = json.loads((state / BENCHMARKS_FILENAME).read_text())["benchmarks"]
        assert len(rows) == 2

    def test_cache_key_groups_engine_model_bucket(self):
        assert estimate_key("NcnnVulkanEngine", "realesrgan-x2plus", "640x360") == (
            "NcnnVulkanEngine|realesrgan-x2plus|640x360"
        )
        assert estimate_key("StubEngine", None, "640x360") == (
            "StubEngine|-|640x360"
        )

    def test_missing_cache_reads_empty_never_errors(self, tmp_path, monkeypatch):
        bench_state(tmp_path, monkeypatch)
        assert load_estimates() == {}

    def test_corrupt_cache_is_a_named_error_with_recovery_story(
        self, tmp_path, monkeypatch
    ):
        state = bench_state(tmp_path, monkeypatch)
        state.mkdir(parents=True)
        (state / BENCHMARKS_FILENAME).write_text("{not json")
        with pytest.raises(BenchmarkCacheError, match="delete the"):
            load_estimates()

    def test_geometry_bucket_is_displayed_geometry(self):
        report = classify(
            parse_probe("/x/v.mkv", probe_payload())
        )
        assert geometry_bucket(report) == "640x360"


class TestB6EtaHonesty:
    """b6 (clause c): none-without-cache, simulated-labeled under stub,
    exact arithmetic under a cached bucket."""

    def test_no_cache_means_no_eta_ever(self):
        plan = make_plan()
        assert estimate_eta(plan, estimates={}) is None

    def test_wrong_bucket_means_no_eta(self):
        plan = make_plan()  # bucket 640x360
        other = make_result(bucket="854x480")
        estimates = {
            estimate_key(other.engine, other.model, other.geometry_bucket): other
        }
        assert estimate_eta(plan, estimates=estimates) is None

    def test_cached_bucket_computes_exact_frames_times_constant(self):
        plan = make_plan()  # 5400 s at 25 fps -> 135000 frames
        bench = make_result(seconds_per_frame=1.83)
        estimates = {estimate_key(bench.engine, bench.model, "640x360"): bench}
        eta = estimate_eta(plan, estimates=estimates)
        assert eta is not None
        assert eta["frames_total"] == 135000
        assert eta["eta_seconds"] == pytest.approx(135000 * 1.83)
        assert eta["eta_seconds"] == pytest.approx(247050.0)
        assert eta["engine_simulated"] is bench.engine_simulated
        assert eta["seconds_per_frame"] == 1.83

    def test_stub_sourced_eta_is_labeled_simulated(self):
        plan = make_plan()
        stub_bench = make_result(simulated=True)  # StubEngine
        estimates = {
            estimate_key("StubEngine", None, "640x360"): stub_bench
        }
        eta = estimate_eta(plan, estimates=estimates)
        assert eta is not None
        assert eta["engine_simulated"] is True  # the label surfaces carry

    def test_engine_narrowing_skips_other_engines(self):
        plan = make_plan()
        stub_bench = make_result(engine="StubEngine", simulated=True)
        estimates = {estimate_key("StubEngine", None, "640x360"): stub_bench}
        assert estimate_eta(plan, estimates=estimates, engine="NcnnVulkanEngine") is None
        assert estimate_eta(plan, estimates=estimates, engine="StubEngine") is not None

    def test_unknown_duration_cannot_know(self):
        plan = make_plan(duration="N/A")
        bench = make_result()
        estimates = {estimate_key(bench.engine, bench.model, "640x360"): bench}
        assert estimate_eta(plan, estimates=estimates) is None

    def test_attach_estimate_is_additive_on_the_plan(self):
        plan = make_plan()
        attached = attach_estimate(plan, {})
        assert attached.eta_estimate is None  # no cache -> cannot know
        bench = make_result()
        estimates = {estimate_key(bench.engine, bench.model, "640x360"): bench}
        attached = attach_estimate(plan, estimates)
        assert attached.eta_estimate is not None
        assert attached.eta_estimate["eta_seconds"] == pytest.approx(247050.0)
        # the original plan object is untouched (frozen dataclass)
        assert plan.eta_estimate is None
        # every v1 field still equals the original
        assert attached.output_path == plan.output_path
        assert attached.video_bitrate_kbps == plan.video_bitrate_kbps


class TestCliBenchmark:
    """t7-adjacent: the CLI surface prints provenance."""

    def test_cli_parses_benchmark_flags(self):
        args = cli.build_parser().parse_args(
            ["benchmark", "movie.avi", "--duration", "60", "--engine", "stub"]
        )
        assert args.command == "benchmark"
        assert args.file == "movie.avi"
        assert args.duration == 60
        assert args.engine == "stub"

    def test_cli_benchmark_defaults(self):
        args = cli.build_parser().parse_args(["benchmark", "movie.avi"])
        assert args.duration == 30
        assert args.engine == "auto"

    def test_cli_benchmark_prints_provenance_and_stub_label(
        self, tmp_path, monkeypatch, capsys
    ):
        bench_state(tmp_path, monkeypatch)
        path = make_media_file(tmp_path)
        runner = BenchRunner(probe_payload())
        monkeypatch.setattr(
            cli, "find_tools", lambda **k: ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        )
        import vidlift.benchmark as benchmark_module

        real_run_benchmark = benchmark_module.run_benchmark

        def fake_run_benchmark(sample_path, **kwargs):
            # keep the real measurement flow, inject the fake seams the
            # CLI cannot pass itself (its boundary test is the printing)
            kwargs.setdefault("probe_runner", runner)
            kwargs["command_runner"] = runner
            return real_run_benchmark(sample_path, **kwargs)

        monkeypatch.setattr(benchmark_module, "run_benchmark", fake_run_benchmark)
        rc = cli.main(
            ["benchmark", "--engine", "stub", "--duration", "10", path]
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "engine: StubEngine" in out
        assert "engine_simulated: true" in out
        assert "not an AI-pass measurement" in out
        assert "geometry bucket: 640x360" in out
        assert "seconds per frame:" in out
        assert "cache: benchmark constant recorded" in out

    def test_cli_benchmark_missing_tool_is_exit_2(self, monkeypatch, capsys):
        def boom(**kwargs):
            from vidlift.run import MissingToolError

            raise MissingToolError("ffmpeg", "ffmpeg not found at 'x'")

        monkeypatch.setattr(cli, "find_tools", boom)
        rc = cli.main(["benchmark", "movie.avi"])
        assert rc == 2
        assert "ffmpeg not found" in capsys.readouterr().err

    def test_cli_benchmark_failure_is_exit_1(self, tmp_path, monkeypatch, capsys):
        bench_state(tmp_path, monkeypatch)
        path = tmp_path / "missing-file.avi"
        rc = cli.main(["benchmark", "--engine", "stub", str(path)])
        assert rc == 1
        assert "error:" in capsys.readouterr().err