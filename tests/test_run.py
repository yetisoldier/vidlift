"""Tests for the single-file run pipeline (vidlift run FILE).

All external commands go through fake runner seams: no real ffmpeg,
ffprobe, or GPU is needed. The fakes simulate tool effects (prepare
writes the prepared file, encode writes the output, ffprobe reports
canned geometry) so the phase sequencing, state, manifests, and
verification behavior are tested end-to-end.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from vidlift import cli
from vidlift.plan import PlanError
from vidlift.probe import probe_args
from vidlift.run import (
    PHASE_DONE,
    PHASE_ENCODE,
    PHASE_PLAN,
    PHASE_PREPARE,
    PHASE_PROBE,
    PHASE_UPSCALE,
    PHASE_VERIFY,
    EngineAdapter,
    FakeEngine,
    MissingToolError,
    RunError,
    RunLog,
    RunResult,
    RunState,
    StubEngine,
    ToolBox,
    VideoToolError,
    build_encode_command,
    build_prepare_command,
    build_verification_command,
    ensure_output_dir,
    find_tools,
    run_file,
    verify_output,
)


def make_media_file(tmp_path, name: str = "movie.avi") -> str:
    path = tmp_path / name
    path.write_text("")
    return str(path)


def probe_payload(
    *,
    codec: str = "mpeg4",
    width: int = 640,
    height: int = 272,
    sar: str = "1:1",
    field_order: str = "progressive",
    fps: str = "25/1",
    audio: list[dict] | None = None,
    subtitles: list[dict] | None = None,
    container: str = "avi",
) -> str:
    """Canned ffprobe JSON for one SD file (the common XviD case)."""
    if audio is None:
        audio = [{"codec_name": "mp3", "channels": 2, "tags": {"language": "und"}}]
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": codec,
            "width": width,
            "height": height,
            "sample_aspect_ratio": sar,
            "field_order": field_order,
            "r_frame_rate": fps,
            "avg_frame_rate": fps,
        }
    ]
    for i, a in enumerate(audio, start=1):
        streams.append({"index": i, "codec_type": "audio", **a})
    for i, s in enumerate(subtitles or [], start=1 + len(audio)):
        streams.append({"index": i, "codec_type": "subtitle", **s})
    return json.dumps(
        {
            "streams": streams,
            "format": {"format_name": container, "duration": "5400.0"},
        }
    )


class FakeRunner:
    """Command seam that simulates ffmpeg/ffprobe effects.

    - ffprobe input probe ("-show_format"): returns the canned payload.
    - prepare (last arg *.prepared.mkv): writes a placeholder file.
    - encode (last arg *.hevc.mp4): writes a placeholder output.
    - verify ("-show_streams" on the output): reports canned geometry.
    """

    def __init__(
        self,
        payload: str,
        *,
        output_geometry: tuple[int, int] = (1280, 720),
        prepare_exit: int = 0,
        encode_exit: int = 0,
        encode_writes_output: bool = True,
        verify_exit: int = 0,
    ) -> None:
        self.payload = payload
        self.output_geometry = output_geometry
        self.prepare_exit = prepare_exit
        self.encode_exit = encode_exit
        self.encode_writes_output = encode_writes_output
        self.verify_exit = verify_exit
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        self.commands.append((binary, tuple(args)))
        # Order matters: the probe command contains BOTH -show_format and
        # -show_streams; the verify command only -show_streams; and the
        # verify/prepared/encode commands all end in a file path suffix.
        if "-show_format" in args:
            return 0, self.payload, ""
        if "-show_streams" in args:
            if self.verify_exit != 0:
                return self.verify_exit, "", "verify boom"
            w, h = self.output_geometry
            return 0, json.dumps(
                {"streams": [{"codec_type": "video", "width": w, "height": h}]}
            ), ""
        if args and args[-1].endswith(".prepared.mkv"):
            if self.prepare_exit == 0:
                Path(args[-1]).write_bytes(b"prepared-stub")
            return self.prepare_exit, "", "prepare boom" if self.prepare_exit else ""
        if args and args[-1].endswith(".vidlift.720p.hevc.mp4"):
            if self.encode_exit == 0 and self.encode_writes_output:
                Path(args[-1]).write_bytes(b"output-stub")
            return self.encode_exit, "", "encode boom" if self.encode_exit else ""
        return 1, "", f"unexpected command: {binary} {' '.join(args)}"

    def command_texts(self) -> list[str]:
        return [f"{b} {' '.join(a)}" for b, a in self.commands]


def find_command(commands: list[list[str]], marker: str) -> list[str]:
    """The first recorded command with ``marker`` in any argument."""
    for command in commands:
        if any(marker in part for part in command):
            return command
    raise AssertionError(f"no command contains {marker!r}: {commands}")


def run_happy(
    tmp_path,
    *,
    payload: str | None = None,
    geometry: tuple[int, int] = (1280, 720),
    engine: EngineAdapter | None = None,
):
    """One full fake run with all tool effects succeeding."""
    path = make_media_file(tmp_path)
    runner = FakeRunner(payload or probe_payload())
    tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
    result = run_file(
        path,
        tools=tools,
        probe_runner=runner,
        command_runner=runner,
        engine=engine,
        log_dir=str(tmp_path / "runs"),
    )
    return result, runner


class TestFindTools:
    def test_missing_explicit_path_names_the_tool(self):
        with pytest.raises(MissingToolError) as excinfo:
            find_tools(ffmpeg="/no/such/ffmpeg")
        assert excinfo.value.tool == "ffmpeg"
        assert "ffmpeg not found" in str(excinfo.value)

    def test_missing_env_override_names_the_tool(self, monkeypatch):
        monkeypatch.setenv("VIDLIFT_FFPROBE", "no-such-ffprobe-xyz")
        with pytest.raises(MissingToolError) as excinfo:
            find_tools()
        assert excinfo.value.tool == "ffprobe"

    def test_explicit_and_env_paths_are_honored(self, tmp_path, monkeypatch):
        exe = tmp_path / "tools" / "ffmpeg"
        exe.parent.mkdir()
        exe.write_text("#!/bin/sh\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        probe = tmp_path / "tools" / "ffprobe"
        probe.write_text("#!/bin/sh\n")
        probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("VIDLIFT_FFPROBE", str(probe))
        tools = find_tools(ffmpeg=str(exe))
        assert tools.ffmpeg == str(exe)
        assert tools.ffprobe == str(probe)

    def test_cli_missing_tool_is_exit_2(self, monkeypatch, capsys):
        def boom(**kwargs):
            raise MissingToolError("ffmpeg", "ffmpeg not found at 'x'")

        monkeypatch.setattr(cli, "find_tools", boom)
        rc = cli.main(["run", "movie.avi"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "ffmpeg not found" in err
        assert "error:" in err


class TestCLIRunParsing:
    def test_run_defaults(self):
        args = cli.build_parser().parse_args(["run", "movie.avi"])
        assert args.command == "run"
        assert args.file == "movie.avi"
        assert args.target == "720p"
        assert args.gbph == 1.5
        assert args.output_dir is None
        assert args.ffmpeg is None
        assert args.ffprobe is None
        assert args.log_dir is None

    def test_run_options(self):
        args = cli.build_parser().parse_args(
            [
                "run", "--gbph", "2.5", "--output-dir", "/out",
                "--ffmpeg", "/f/ffmpeg", "--ffprobe", "/f/ffprobe",
                "--log-dir", "/logs", "movie.avi",
            ]
        )
        assert args.gbph == 2.5
        assert args.output_dir == "/out"
        assert args.ffmpeg == "/f/ffmpeg"
        assert args.ffprobe == "/f/ffprobe"
        assert args.log_dir == "/logs"
        assert args.file == "movie.avi"

    def test_cli_run_success_exit_0_and_report(self, monkeypatch, capsys, tmp_path):
        result, _ = run_happy(tmp_path)
        monkeypatch.setattr(
            cli, "find_tools",
            lambda **kwargs: ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
        monkeypatch.setattr(
            cli, "run_file", lambda *a, **kwargs: result
        )
        rc = cli.main(["run", result.state.file_path])
        assert rc == 0
        out = capsys.readouterr().out
        assert "output:" in out
        assert "verification: verified" in out
        assert "engine: STUB" in out
        assert "log:" in out
        assert "manifest:" in out
        assert "phases:" in out

    def test_cli_run_failure_exit_1_with_log_tail(
        self, monkeypatch, capsys, tmp_path
    ):
        path = make_media_file(tmp_path)
        state = RunState(file_path=path, ok=False, error="encode blew up")
        log_path = tmp_path / "runs" / "x.log"
        log_path.parent.mkdir(parents=True)
        state.log = RunLog(path=str(log_path))
        state.log.write("phase encode: started")
        state.log.write("error in phase encode: encode blew up")
        state.manifest_path = str(tmp_path / "runs" / "x.manifest.json")

        def failing_run_file(*args, **kwargs):
            exc = RunError("encode blew up")
            exc.state = state  # type: ignore[attr-defined]
            raise exc

        monkeypatch.setattr(
            cli, "find_tools",
            lambda **kwargs: ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
        monkeypatch.setattr(cli, "run_file", failing_run_file)
        rc = cli.main(["run", path])
        assert rc == 1
        err = capsys.readouterr().err
        assert "encode blew up" in err
        assert "run log:" in err
        assert "run manifest:" in err


class TestRunFilePhases:
    def test_happy_path_sequences_all_phases(self, tmp_path):
        result, runner = run_happy(tmp_path)
        state = result.state
        assert [p.name for p in state.phases] == [
            PHASE_PROBE, PHASE_PLAN, PHASE_PREPARE,
            PHASE_UPSCALE, PHASE_ENCODE, PHASE_VERIFY,
        ]
        assert state.current_phase == PHASE_DONE
        assert all(p.duration_seconds is not None for p in state.phases)
        assert state.ok is True
        assert state.error is None
        assert state.finished_at is not None
        assert state.last_activity_at >= state.started_at

    def test_happy_path_records_all_command_lines(self, tmp_path):
        result, runner = run_happy(tmp_path)
        commands = result.state.commands
        assert len(commands) == 4  # probe, prepare, encode, verify
        probe_cmd, prep_cmd, enc_cmd, ver_cmd = commands
        assert probe_cmd[0] == "ffprobe"
        assert probe_cmd[1:] == probe_args(result.state.file_path)
        assert prep_cmd[0] == "ffmpeg" and "setsar=1" in " ".join(prep_cmd)
        assert "libx265" in enc_cmd
        assert ver_cmd[0] == "ffprobe" and result.plan.output_path in ver_cmd
        # every command the state recorded is exactly what the seam received
        received = [[b, *list(a)] for b, a in runner.commands]
        for cmd in commands:
            assert cmd in received

    def test_happy_path_plan_and_output_paths(self, tmp_path):
        result, _ = run_happy(tmp_path)
        plan = result.plan
        assert plan.process_verdict == "ready"
        assert plan.video_bitrate_kbps > 0
        assert Path(plan.output_path).name == "movie.vidlift.720p.hevc.mp4"
        assert result.state.output_path == plan.output_path
        assert Path(result.state.prepared_path).exists()
        assert Path(result.state.mezzanine_path).exists()

    def test_happy_path_log_file_and_tail(self, tmp_path):
        result, _ = run_happy(tmp_path)
        log = result.state.log
        assert log is not None
        assert os.path.isfile(log.path)
        text = Path(log.path).read_text()
        assert "phase probe: started" in text
        assert "phase verify: done" in text
        assert "run complete" in text
        assert any("phase" in line for line in log.tail())
        tail_two = log.tail(2)
        assert len(tail_two) == 2
        assert "manifest:" in tail_two[0]
        assert "run complete" in tail_two[1]

    def test_happy_path_manifest_records_artifacts(self, tmp_path):
        result, _ = run_happy(tmp_path)
        manifest_path = result.manifest_path
        assert manifest_path == result.state.manifest_path
        assert os.path.isfile(manifest_path)
        manifest = json.loads(Path(manifest_path).read_text())
        assert manifest["run"]["ok"] is True
        assert manifest["run"]["engine_simulated"] is True
        assert manifest["run"]["input_file"] == result.state.file_path
        assert manifest["tools"] == {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"}
        assert manifest["plan"]["output_geometry"] == [1280, 720]
        assert manifest["probe"]["source_class"] == "xvid-divx-avi"
        assert manifest["verification"]["ok"] is True
        assert manifest["verification"]["actual_geometry"] == [1280, 720]
        assert manifest["artifacts"]["output_path"] == result.plan.output_path
        assert len(manifest["commands"]) == 4
        assert [p["name"] for p in manifest["phases"]] == [
            "probe", "plan", "prepare", "upscale", "encode", "verify",
        ]
        assert all(p["duration_seconds"] is not None for p in manifest["phases"])

    def test_stub_engine_receives_prepared_file(self, tmp_path):
        engine = StubEngine()
        run_happy(tmp_path, engine=engine)
        assert engine.calls, "engine must be invoked with the prepared path"

    def test_state_to_dict_is_manifest_shaped(self, tmp_path):
        result, _ = run_happy(tmp_path)
        data = result.state.to_dict()
        assert data["current_phase"] == PHASE_DONE
        assert data["ok"] is True
        assert data["log_path"] == result.state.log.path
        assert data["engine_simulated"] is True
        assert data["verification"]["ok"] is True
        assert data["plan_output_path"] == result.plan.output_path


class TestOutputDirectoryCreation:
    """Regression: ffmpeg never creates a missing output directory.

    `vidlift run --output-dir NEW_DIR` used to die mid-encode with a
    bare "Error opening output files: No such file or directory"
    because nothing created the planned output directory. The runner
    now creates it (with missing parents) during the plan phase,
    before any prepare/encode command runs.
    """

    def test_ensure_output_dir_creates_missing_parents(self, tmp_path):
        target = tmp_path / "a" / "b" / "c" / "movie.vidlift.720p.hevc.mp4"
        ensure_output_dir(str(target))
        assert (tmp_path / "a" / "b" / "c").is_dir()
        ensure_output_dir(str(target))  # an existing directory is a no-op

    def test_ensure_output_dir_wraps_os_errors(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("occupied")  # a FILE where a directory is needed
        with pytest.raises(RunError, match="cannot create output directory"):
            ensure_output_dir(str(blocker / "movie.vidlift.720p.hevc.mp4"))

    def test_run_creates_missing_output_dir_and_writes_there(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        out_root = tmp_path / "out" / "nested"  # does not exist yet
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            output_dir=str(out_root), log_dir=str(tmp_path / "runs"),
        )
        assert Path(result.plan.output_path).parent == out_root
        assert out_root.is_dir()
        assert os.path.isfile(result.plan.output_path)
        log_text = Path(result.state.log.path).read_text()
        assert f"output dir ready: {out_root}" in log_text

    def test_output_dir_exists_when_encode_command_runs(self, tmp_path):
        dir_at_encode: list[bool] = []

        class EncodeSpy(FakeRunner):
            def __call__(self, binary, args):
                if "libx265" in args:  # the encode command, exactly once
                    dir_at_encode.append(Path(args[-1]).parent.is_dir())
                return super().__call__(binary, args)

        path = make_media_file(tmp_path)
        runner = EncodeSpy(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            output_dir=str(tmp_path / "out"), log_dir=str(tmp_path / "runs"),
        )
        assert dir_at_encode == [True]
        assert result.ok is True

    def test_existing_output_dir_contents_survive(self, tmp_path):
        out = tmp_path / "out"
        out.mkdir()
        (out / "keep.txt").write_text("precious")
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            output_dir=str(out), log_dir=str(tmp_path / "runs"),
        )
        assert (out / "keep.txt").read_text() == "precious"  # dir not wiped

    def test_uncreatable_output_dir_fails_at_plan_named(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a directory")
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(RunError, match="cannot create output directory") as excinfo:
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                output_dir=str(blocker), log_dir=str(tmp_path / "runs"),
            )
        state = excinfo.value.state  # type: ignore[attr-defined]
        assert state.current_phase == PHASE_PLAN
        assert "cannot create output directory" in state.error
        assert os.path.isfile(state.manifest_path)
        manifest = json.loads(Path(state.manifest_path).read_text())
        assert manifest["run"]["error_phase"] == PHASE_PLAN
        # no prepare/encode command ever ran
        assert all("setsar=1" not in " ".join(c) for c in state.commands)


class TestPrepareCommand:
    def test_prepare_normalizes_to_square_pixels(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        prep = find_command(result.state.commands, "setsar=1")
        # 640x272 SAR 1:1 -> displayed size is unchanged; scale+setsar
        vf = prep[prep.index("-vf") + 1]
        assert vf == "scale=640:272,setsar=1"
        assert prep[prep.index("-c:v") + 1] == "ffv1"

    def test_anamorphic_sar_normalizes_to_displayed_size(self, tmp_path):
        path = make_media_file(tmp_path, "dvdrip.mkv")
        payload = probe_payload(
            codec="mpeg2video", width=720, height=576, sar="64:45",
            container="matroska,webm",
        )
        runner = FakeRunner(payload)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        prep = find_command(result.state.commands, "setsar=1")
        vf = prep[prep.index("-vf") + 1]
        # 720x576 at SAR 64:45 displays as 1024x576
        assert vf == "scale=1024:576,setsar=1"

    def test_interlaced_flag_adds_bwdif_progressive_does_not(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload(field_order="tt"))
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        prep = find_command(result.state.commands, "setsar=1")
        assert "bwdif" in prep[prep.index("-vf") + 1]

        path2 = make_media_file(tmp_path, "movie2.avi")
        runner2 = FakeRunner(probe_payload(field_order="progressive"))
        result2 = run_file(
            path2, tools=tools, probe_runner=runner2, command_runner=runner2,
            log_dir=str(tmp_path / "runs2"),
        )
        prep2 = find_command(result2.state.commands, "setsar=1")
        assert "bwdif" not in prep2[prep2.index("-vf") + 1]


class TestEncodeCommand:
    def test_encode_carries_audio_and_text_subtitles(self, tmp_path):
        path = make_media_file(tmp_path, "subbed.avi")
        payload = probe_payload(
            subtitles=[{"codec_name": "srt", "tags": {"language": "eng"}}]
        )
        runner = FakeRunner(payload)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        enc = find_command(result.state.commands, "libx265")
        text = " ".join(enc)
        assert "mov_text" in text
        assert "-map" in enc and "1:a:0" in enc
        assert f"-b:v {result.plan.video_bitrate_kbps}k" in text
        assert f"-b:a {result.plan.audio.bitrate_kbps}k" in text
        assert "map_chapters" in text

    def test_encode_drops_image_subtitles_entirely(self, tmp_path):
        path = make_media_file(tmp_path, "vobsub.avi")
        payload = probe_payload(
            subtitles=[{"codec_name": "vobsub", "tags": {"language": "eng"}}]
        )
        runner = FakeRunner(payload)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        enc = find_command(result.state.commands, "libx265")
        text = " ".join(enc)
        assert "mov_text" not in text
        assert "1:s?" not in text  # image subs are listed, never carried

    def test_encode_no_audio_maps_video_only(self, tmp_path):
        path = make_media_file(tmp_path, "silent.avi")
        payload = probe_payload(audio=[])
        runner = FakeRunner(payload)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        enc = find_command(result.state.commands, "libx265")
        text = " ".join(enc)
        assert "-b:a" not in text
        assert "1:a:0" not in text


class TestRunFileFailures:
    def test_already_hd_file_is_rejected_at_plan(self, tmp_path):
        path = make_media_file(tmp_path, "hd.mp4")
        payload = probe_payload(
            codec="h264", width=1920, height=1080, container="mov,mp4",
        )
        runner = FakeRunner(payload)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(RunError, match="not runnable"):
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"),
            )

    def test_no_video_file_is_rejected_at_plan(self, tmp_path):
        path = make_media_file(tmp_path, "audio-only.avi")
        payload = json.dumps(
            {
                "streams": [
                    {"index": 0, "codec_type": "audio",
                     "codec_name": "mp3", "channels": 2}
                ],
                "format": {"format_name": "avi", "duration": "5400.0"},
            }
        )
        runner = FakeRunner(payload)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(PlanError, match="no video"):
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"),
            )

    def test_failure_state_and_manifest_survive(self, tmp_path):
        path = make_media_file(tmp_path, "hd.mp4")
        payload = probe_payload(codec="h264", width=1920, height=1080)
        runner = FakeRunner(payload)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(RunError) as excinfo:
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"),
            )
        state = excinfo.value.state  # type: ignore[attr-defined]
        assert state.ok is False
        assert state.current_phase == PHASE_PLAN
        assert "already-hd" in state.error
        assert os.path.isfile(state.manifest_path)
        manifest = json.loads(Path(state.manifest_path).read_text())
        assert manifest["run"]["ok"] is False
        assert manifest["run"]["error_phase"] == PHASE_PLAN
        assert "already-hd" in manifest["run"]["error"]
        # no prepare/encode command ever ran
        assert all("setsar=1" not in " ".join(c) for c in state.commands)

    def test_prepare_failure_raises_video_tool_error(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload(), prepare_exit=3)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(VideoToolError) as excinfo:
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"),
            )
        assert excinfo.value.phase == PHASE_PREPARE
        assert excinfo.value.exit_code == 3
        assert "prepare command failed" in str(excinfo.value)
        assert excinfo.value.state.current_phase == PHASE_PREPARE  # type: ignore[attr-defined]

    def test_encode_failure_raises_video_tool_error(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload(), encode_exit=2)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(VideoToolError) as excinfo:
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"),
            )
        assert excinfo.value.phase == PHASE_ENCODE
        assert excinfo.value.state.mezzanine_path is not None  # type: ignore[attr-defined]

    def test_encode_producing_no_output_fails_loudly(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload(), encode_writes_output=False)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(RunError, match="produced no file"):
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"),
            )

    def test_engine_failure_records_state(self, tmp_path):
        path = make_media_file(tmp_path)
        engine = FakeEngine(RunError("engine exploded"))
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(RunError, match="engine exploded") as excinfo:
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                engine=engine, log_dir=str(tmp_path / "runs"),
            )
        state = excinfo.value.state  # type: ignore[attr-defined]
        assert state.current_phase == PHASE_UPSCALE
        assert state.mezzanine_path is None
        assert state.manifest_path is not None

    def test_cancel_before_upscale_stops_at_upscale_phase(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(RunError, match="cancelled") as excinfo:
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"), cancel=lambda: True,
            )
        assert excinfo.value.state.current_phase == PHASE_UPSCALE  # type: ignore[attr-defined]


class TestVerification:
    def _plan(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        return result.plan, tools

    def test_missing_output_file_fails_exists_check(self, tmp_path):
        plan, tools = self._plan(tmp_path)
        Path(plan.output_path).unlink()
        runner = FakeRunner(probe_payload())
        result = verify_output(tools, plan, runner=runner)
        assert result.ok is False
        assert result.exists is False
        assert result.decodes is False
        assert "output file not written" in result.error

    def test_ffprobe_failure_fails_decode_check(self, tmp_path):
        plan, tools = self._plan(tmp_path)
        runner = FakeRunner(probe_payload(), verify_exit=1)
        result = verify_output(tools, plan, runner=runner)
        assert result.ok is False
        assert result.exists is True
        assert result.decodes is False
        assert "ffprobe failed" in result.error

    def test_geometry_mismatch_fails_verification(self, tmp_path):
        plan, tools = self._plan(tmp_path)
        runner = FakeRunner(probe_payload(), output_geometry=(640, 360))
        result = verify_output(tools, plan, runner=runner)
        assert result.ok is False
        assert result.exists is True
        assert result.decodes is True
        assert result.geometry_ok is False
        assert result.actual_geometry == (640, 360)
        assert "geometry mismatch" in result.error

    def test_matching_geometry_passes(self, tmp_path):
        plan, tools = self._plan(tmp_path)
        runner = FakeRunner(probe_payload(), output_geometry=(1280, 720))
        result = verify_output(tools, plan, runner=runner)
        assert result.ok is True
        assert result.exists and result.decodes and result.geometry_ok
        assert result.error is None

    def test_full_run_with_geometry_mismatch_keeps_rich_manifest(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload(), output_geometry=(640, 360))
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(RunError, match="verification failed") as excinfo:
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"),
            )
        state = excinfo.value.state  # type: ignore[attr-defined]
        assert state.verification is not None
        assert state.verification.geometry_ok is False
        manifest = json.loads(Path(state.manifest_path).read_text())
        # the rich manifest (not the lean failure manifest) is kept:
        # it carries probe/plan/verification detail
        assert manifest["verification"]["geometry_ok"] is False
        assert manifest["probe"]["source_class"] == "xvid-divx-avi"
        assert manifest["run"]["ok"] is False

    def test_verification_command_uses_ffprobe_on_output(self, tmp_path):
        plan, tools = self._plan(tmp_path)
        cmd = build_verification_command(tools, plan.output_path)
        assert cmd[0] == tools.ffprobe
        assert plan.output_path in cmd
        assert "-show_streams" in cmd