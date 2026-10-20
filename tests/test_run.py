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
    EngineCancelled,
    FakeEngine,
    MissingToolError,
    RunError,
    RunLog,
    RunResult,
    RunState,
    StubEngine,
    ToolBox,
    UpscaleError,
    VideoToolError,
    build_decode_command,
    build_encode_command,
    build_prepare_command,
    build_verification_command,
    ensure_output_dir,
    find_tools,
    run_file,
    verify_output,
)


# ---- b-marker execution registry (suite-wide span; D1 companion) ----

from _bcanary import EXECUTED_B, register_b


def marker(name: str) -> None:
    register_b(name)


from vidlift.engine_ncnn import NcnnVulkanEngine  # v0.4.0 R-series


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
    duration: str = "5400.0",
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
            "format": {"format_name": container, "duration": duration},
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
        decode_exit: int = 0,
        idet_stderr: str = "",
    ) -> None:
        self.payload = payload
        self.output_geometry = output_geometry
        self.prepare_exit = prepare_exit
        self.encode_exit = encode_exit
        self.encode_writes_output = encode_writes_output
        self.verify_exit = verify_exit
        self.decode_exit = decode_exit
        self.idet_stderr = idet_stderr
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        self.commands.append((binary, tuple(args)))
        # Order matters: the probe command contains BOTH -show_format and
        # -show_streams; the verify command only -show_streams; the idet
        # sample pass is "-vf idet" to null; the full decode pass writes
        # to null without idet; and the verify/prepared/encode commands
        # all end in a file path suffix.
        if "-show_format" in args:
            return 0, self.payload, ""
        if "idet" in args:
            return 0, "", self.idet_stderr or (
                "[Parsed_idet] frame detection: TFF: 0 BFF: 0 "
                "Progressive: 0 Undetermined: 0\n"
                "[Parsed_idet] Single frame detection: TFF: 0 BFF: 0 "
                "Progressive: 0 Undetermined: 0\n"
            )
        if "-show_streams" in args:
            if self.verify_exit != 0:
                return self.verify_exit, "", "verify boom"
            w, h = self.output_geometry
            return 0, json.dumps(
                {"streams": [{"codec_type": "video", "width": w, "height": h}]}
            ), ""
        if args and args[-1] == "-" and "null" in args:
            if self.decode_exit != 0:
                return self.decode_exit, "", "decode boom"
            return 0, "", ""
        if args and args[-1].endswith(".prepared.mkv"):
            if self.prepare_exit == 0:
                Path(args[-1]).write_bytes(b"prepared-stub")
            return self.prepare_exit, "", "prepare boom" if self.prepare_exit else ""
        if args and args[-1].endswith(".vidlift.720p.hevc.mp4") and "-i" in args and any(a.endswith(".srt") for a in args):
            # v0.4.1: the mov_text mux into the finished MP4 writes a
            # temp sibling that mux_srt_into_mp4 then replaces over the
            # output (temp-then-replace atomicity)
            if self.encode_exit == 0:
                Path(args[-1]).write_bytes(b"muxed-output-stub")
            return self.encode_exit, "", "mux boom" if self.encode_exit else ""
        if args and args[-1].endswith(".srt") and "-map" in args:
            # v0.4.1: embedded-text sidecar extraction (-c:s srt)
            if self.prepare_exit == 0:
                Path(args[-1]).write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n")
            return self.prepare_exit, "", "srt boom" if self.prepare_exit else ""
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
        # v0.4.1: the subtitles phase slots between verify and done,
        # optional by presence — this source has no subs, so it runs
        # and records 'skipped' in milliseconds.
        assert [p.name for p in state.phases] == [
            PHASE_PROBE, PHASE_PLAN, PHASE_PREPARE,
            PHASE_UPSCALE, PHASE_ENCODE, PHASE_VERIFY,
            "subtitles",
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
        assert len(commands) == 5  # probe, prepare, encode, verify, decode
        probe_cmd, prep_cmd, enc_cmd, ver_cmd, dec_cmd = commands
        assert probe_cmd[0] == "ffprobe"
        assert probe_cmd[1:] == probe_args(result.state.file_path)
        assert prep_cmd[0] == "ffmpeg" and "setsar=1" in " ".join(prep_cmd)
        assert "libx265" in enc_cmd
        assert ver_cmd[0] == "ffprobe" and result.plan.output_path in ver_cmd
        # the full decode pass (b12) decodes the encode output to null
        assert dec_cmd[0] == "ffmpeg" and result.plan.output_path in dec_cmd
        assert dec_cmd[dec_cmd.index("-f") + 1:] == ["null", "-"]
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
        # v0.4.1: probe+prepare+encode+verify-probe+decode = 5 with no
        # subtitle track (the skipped phase adds no commands); the
        # phases list gains the skipped subtitles phase.
        assert len(manifest["commands"]) == 5
        assert [p["name"] for p in manifest["phases"]] == [
            "probe", "plan", "prepare", "upscale", "encode", "verify",
            "subtitles",
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
        # v0.4.1: the SELECTED track maps by explicit probe index (the
        # all-or-nothing -map 1:s? is gone); language is tagged honestly.
        # elements are '-map' '1:2' as separate argv tokens
        assert "-map" in enc and "1:2" in enc
        assert "language=eng" in text
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

class TestEngineSelection:
    """--engine {auto,stub,ncnn} resolution (design Section 5)."""

    def test_auto_without_engine_tools_falls_to_stub_with_named_reason(
        self, tmp_path, monkeypatch
    ):
        # the conftest guard points VIDLIFT_REALESRGAN_NCNN at a
        # nonexistent path: auto must fall to the stub, note the reason
        from vidlift.engine_ncnn import select_engine

        engine, note = select_engine("auto")
        assert type(engine).__name__ == "StubEngine"
        assert note is not None and "auto: stub engine" in note
        assert "not found" in note  # names what failed

    def test_stub_request_forces_stub_no_probe(self):
        from vidlift.engine_ncnn import select_engine

        engine, note = select_engine("stub")
        assert type(engine).__name__ == "StubEngine"
        assert note is None

    def test_ncnn_request_missing_binary_is_named_setup_error(self, monkeypatch):
        from vidlift.engine_ncnn import select_engine

        monkeypatch.setenv("VIDLIFT_REALESRGAN_NCNN", "/no/such/engine")
        with pytest.raises(MissingToolError) as excinfo:
            select_engine("ncnn")
        assert excinfo.value.tool == "realesrgan-ncnn-vulkan"
        assert "not found" in str(excinfo.value)

    def test_unknown_request_is_refused(self):
        from vidlift.engine_ncnn import select_engine

        with pytest.raises(UpscaleError, match="unknown engine request"):
            select_engine("turbo")

    def test_run_file_default_engine_is_stub_with_note(self, tmp_path):
        # run_file's default (engine=None, request auto) lands on the
        # stub under the test guard, and the manifest says exactly why
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        manifest = json.loads(Path(result.manifest_path).read_text())
        assert manifest["run"]["engine"] == "StubEngine"
        assert manifest["run"]["engine_simulated"] is True
        assert "auto: stub engine" in manifest["run"]["engine_note"]

    def test_forced_stub_engine_flag_reaches_the_runner(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"), engine_request="stub",
        )
        manifest = json.loads(Path(result.manifest_path).read_text())
        assert manifest["run"]["engine"] == "StubEngine"
        assert manifest["run"]["engine_simulated"] is True
        assert manifest["run"]["engine_note"] == "stub engine (no Real-ESRGAN)"


class TestFailOpen:
    """b7 (clause d): a failing engine NEVER kills the job."""

    def test_b7_fail_open_end_to_end_completes_with_honest_manifest(
        self, tmp_path
    ):
        marker("b7")
        # the OCR-babysit lesson as a test: the engine dies mid-run
        # with the gate-M reason; the run COMPLETES, the output is the
        # synchronized re-encode (verify stage green), and the manifest
        # states verbatim what shipped and why.
        reason = "gate M: mezz 6838.0 vs prep 6853.1"
        path = make_media_file(tmp_path)
        engine = FakeEngine(UpscaleError(reason))
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            engine=engine, log_dir=str(tmp_path / "runs"),
        )
        state = result.state
        # the run COMPLETED and verified
        assert result.ok is True
        assert state.current_phase == PHASE_DONE
        assert state.verification is not None and state.verification.ok
        assert [p.name for p in state.phases] == [
            PHASE_PROBE, PHASE_PLAN, PHASE_PREPARE,
            PHASE_UPSCALE, PHASE_ENCODE, PHASE_VERIFY,
            "subtitles",  # v0.4.1: skipped (no track selected)
        ]
        assert state.mezzanine_path == state.prepared_path
        # the engine was attempted and is named
        assert state.engine_name == "FakeEngine"
        # manifest v2 honesty fields, verbatim
        manifest = json.loads(Path(result.manifest_path).read_text())
        run = manifest["run"]
        assert run["fail_open"] is True
        assert run["fail_open_reason"] == reason  # verbatim
        assert run["engine_simulated"] is True  # shipped pixels are NOT AI
        assert run["engine"] == "FakeEngine"  # names the attempted engine
        assert run["engine_note"] == f"fail open: {reason}"
        assert run["engine_model"] is None
        assert run["ok"] is True
        # the shipped output really is a real verified re-encode
        assert manifest["verification"]["ok"] is True
        assert manifest["verification"]["full_decode_ok"] is True
        # the run log carries the red-level warning
        log_text = Path(state.log.path).read_text()
        assert "failing open" in log_text
        assert reason in log_text

    def test_b7_fail_open_note_flows_to_state_and_manifest_identically(
        self, tmp_path
    ):
        marker("b7")
        reason = "no usable frame clock on prepared file /x/y: neither parses"
        path = make_media_file(tmp_path)
        engine = FakeEngine(UpscaleError(reason))
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            engine=engine, log_dir=str(tmp_path / "runs"),
        )
        state = result.state
        manifest = json.loads(Path(result.manifest_path).read_text())
        # byte-identical flow-through (clause d at the boundary)
        assert state.engine_note == manifest["run"]["engine_note"]
        assert state.fail_open_reason == manifest["run"]["fail_open_reason"]
        assert state.engine_simulated == manifest["run"]["engine_simulated"]

    def test_fail_open_output_decodes_and_matches_plan(self, tmp_path):
        # highest boundary (clause f): the fail-open OUTPUT itself —
        # not just the manifest claim — is a real 720p file
        path = make_media_file(tmp_path)
        engine = FakeEngine(UpscaleError("engine exploded"))
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            engine=engine, log_dir=str(tmp_path / "runs"),
        )
        assert os.path.isfile(result.plan.output_path)
        assert result.verification.geometry_ok is True
        assert result.verification.full_decode_ok is True

    def test_plain_runerror_from_engine_still_fails_the_run(self, tmp_path):
        # fail-open covers ONLY UpscaleError; a plain RunError from a
        # broken engine is a real failure (the stub's contract class)
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
        assert state.fail_open is False
        assert state.cancelled is False


class TestEngineCancelOutcome:
    """EngineCancelled is a cancellation, never a failure (clause d)."""

    def test_cancel_in_engine_records_cancelled_outcome(self, tmp_path):
        path = make_media_file(tmp_path)
        engine = FakeEngine(EngineCancelled("cancelled during Real-ESRGAN chunk 1/2"))
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(EngineCancelled) as excinfo:
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                engine=engine, log_dir=str(tmp_path / "runs"),
            )
        state = excinfo.value.state  # type: ignore[attr-defined]
        assert state.cancelled is True
        assert state.ok is False
        assert state.current_phase == PHASE_UPSCALE
        assert state.error is not None and "cancelled" in state.error
        # the manifest records a cancellation outcome, not a failure
        manifest = json.loads(Path(state.manifest_path).read_text())
        assert manifest["run"]["outcome"] == "cancelled"
        assert manifest["run"]["cancelled"] is True
        assert manifest["run"]["ok"] is False
        # and it still carries the honesty fields
        assert manifest["run"]["engine"] == "FakeEngine"
        assert "engine_simulated" in manifest["run"]

    def test_stub_pre_frame_cancel_is_engine_cancelled_now(self, tmp_path):
        # StubEngine's pre-frame cancel raised plain RunError before
        # P2; the seam contract now raises EngineCancelled
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(EngineCancelled):
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"), cancel=lambda: True,
            )

    def test_cancel_writes_no_output_file(self, tmp_path):
        path = make_media_file(tmp_path)
        engine = FakeEngine(EngineCancelled("cancelled before extraction"))
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(EngineCancelled):
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                engine=engine, log_dir=str(tmp_path / "runs"),
            )
        # a cancelled run never leaves a claimed-complete output
        assert not os.path.isfile(
            str(Path(path).with_name("movie.vidlift.720p.hevc.mp4"))
        )


class TestFullDecodeVerification:
    """b12: the full decode pass runs on the encode output and its
    failure fails verification."""

    def test_decode_command_shape_is_pinned(self):
        marker("b12")
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        cmd = build_decode_command(tools, "/out/movie.vidlift.720p.hevc.mp4")
        assert cmd == [
            "ffmpeg",
            "-v", "error", "-nostdin",
            "-i", "/out/movie.vidlift.720p.hevc.mp4",
            "-f", "null", "-",
        ]

    def test_full_decode_runs_on_happy_path_and_passes(self, tmp_path):
        marker("b12")
        result, runner = run_happy(tmp_path)
        assert result.verification.full_decode_ok is True
        # the decode command was both recorded and executed via the seam
        decode_cmds = [
            c for c in result.state.commands if c[-1] == "-" and "null" in c
        ]
        assert len(decode_cmds) == 1
        assert ("ffmpeg", tuple(decode_cmds[0][1:])) in runner.commands

    def test_full_decode_failure_fails_verification(self, tmp_path):
        marker("b12")
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload(), decode_exit=1)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(RunError, match="verification failed") as excinfo:
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"),
            )
        state = excinfo.value.state  # type: ignore[attr-defined]
        assert state.verification is not None
        assert state.verification.full_decode_ok is False
        assert state.verification.geometry_ok is True  # got that far
        assert "full decode failed" in state.verification.error
        manifest = json.loads(Path(state.manifest_path).read_text())
        assert manifest["verification"]["full_decode_ok"] is False
        assert manifest["verification"]["ok"] is False

    def test_full_decode_skipped_when_geometry_mismatch(self, tmp_path):
        marker("b12")
        # decode is only worth running on otherwise-good outputs
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload(), output_geometry=(640, 360))
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        with pytest.raises(RunError, match="verification failed"):
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"),
            )
        decode_cmds = [
            c for c in runner.commands if "-" in c[1] and c[0] == "ffmpeg"
            and list(c[1])[-1] == "-"
        ]
        assert decode_cmds == []  # the decode pass never ran

    def test_verify_output_full_decode_flag_off(self, tmp_path):
        marker("b12")
        # the flag exists so future callers (bridge probes) can run the
        # cheap checks alone
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        plan = result.plan
        # geometry-only verify (full_decode=False) skips the pass
        fake = FakeRunner(probe_payload())
        check = verify_output(
            tools, plan, runner=fake, full_decode=False
        )
        assert check.ok is True
        assert check.full_decode_ok is False
        assert len(fake.commands) == 1  # only the ffprobe check ran


class TestManifestSchemaV2:
    """Schema v2 additive fields (design Section 11); v1 unchanged."""

    def test_v1_fields_unchanged_byte_for_byte(self, tmp_path):
        result, _ = run_happy(tmp_path)
        manifest = json.loads(Path(result.manifest_path).read_text())
        run = manifest["run"]
        # v1 core fields, present with the same meaning as ever
        assert run["input_file"] == result.state.file_path
        assert run["ok"] is True
        assert run["engine_simulated"] is True
        assert "started_at" in run and "finished_at" in run
        assert "error" in run
        assert manifest["tools"] == {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"}
        assert manifest["probe"]["source_class"] == "xvid-divx-avi"
        assert manifest["plan"]["output_geometry"] == [1280, 720]
        assert "phases" in manifest and "commands" in manifest
        assert "verification" in manifest and "artifacts" in manifest

    def test_v2_fields_present_with_null_rule_on_stub(self, tmp_path):
        # additive fields are present with the null/absent rule: the
        # stub carries no model/clock/gates, and that is stated, not
        # omitted silently
        result, _ = run_happy(tmp_path)
        manifest = json.loads(Path(result.manifest_path).read_text())
        run = manifest["run"]
        assert run["engine_model"] is None
        assert run["engine_native_scale"] is None
        assert run["fail_open"] is False
        assert run["fail_open_reason"] is None
        assert run["frame_clock"] is None
        assert run["model_hashes_verified"] is None
        assert run["duration_gates"] is None
        assert run["benchmark_constant"] is None
        assert run["cancelled"] is False
        assert run["outcome"] == "succeeded"

    def test_state_to_dict_carries_v2_fields(self, tmp_path):
        result, _ = run_happy(tmp_path)
        data = result.state.to_dict()
        for key in (
            "engine_name", "engine_model", "engine_native_scale",
            "fail_open", "fail_open_reason", "frame_clock",
            "duration_gates", "model_hashes_verified", "progress",
            "benchmark_constant", "cancelled",
        ):
            assert key in data, f"state.to_dict() lost v2 field {key}"


class TestProgressPlumbing:
    """RunState.progress fed by the adapter's on_progress seam."""

    def test_stub_run_leaves_progress_null(self, tmp_path):
        # the stub reports no frame work: no frames_done/frames_total
        # ever appear; v0.4.0 adds step labels, so "no frame work" is
        # pinned as the absence of the frame counters, not a null dict
        result, _ = run_happy(tmp_path)
        assert result.state.progress is None or (
            "frames_done" not in (result.state.progress or {})
            and "frames_total" not in (result.state.progress or {})
        )

    def test_engine_progress_lands_in_state(self, tmp_path):
        seen: list[tuple[int, int]] = []

        class ProgressEngine(StubEngine):
            def run(self, prepared_path, plan, tools, *, cancel=None,
                    on_progress=None, on_step=None):
                if on_progress is not None:
                    on_progress(120, 7500)
                    on_progress(240, 7500)
                seen.append(True)
                return super().run(
                    prepared_path, plan, tools, cancel=cancel,
                    on_progress=on_progress, on_step=on_step,
                )

        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            engine=ProgressEngine(), log_dir=str(tmp_path / "runs"),
        )
        assert seen == [True]
        progress = result.state.progress or {}
        assert progress["frames_done"] == 240
        assert progress["frames_total"] == 7500
        # v0.4.0: the step label rides the same dict (B.6) and survives
        # the frame-progress merge — never replacing it
        assert progress.get("step")


class TestHighestBoundaryPromiseSweep:
    """b8 (clause f): the engine-touching user-visible promises, one
    test each at the command/manifest boundary."""

    def test_b8_output_filename_promise_at_manifest_boundary(self, tmp_path):
        marker("b8")
        result, _ = run_happy(tmp_path)
        # the promise: outputs are named <stem>.vidlift.720p.hevc.mp4
        assert Path(result.plan.output_path).name == (
            "movie.vidlift.720p.hevc.mp4"
        )
        manifest = json.loads(Path(result.manifest_path).read_text())
        assert manifest["plan"]["output_path"] == result.plan.output_path
        assert manifest["artifacts"]["output_path"] == result.plan.output_path

    def test_b8_geometry_promise_at_command_and_manifest(self, tmp_path):
        marker("b8")
        result, runner = run_happy(tmp_path)
        # the promise: final geometry equals plan geometry; asserted on
        # the encode command line AND the manifest AND verification
        plan = result.plan
        enc = find_command(result.state.commands, "libx265")
        vf = enc[enc.index("-vf") + 1]
        assert vf == f"scale={plan.output_width}:{plan.output_height}:flags=lanczos"
        assert result.verification.actual_geometry == (
            (plan.output_width, plan.output_height)
        )
        manifest = json.loads(Path(result.manifest_path).read_text())
        assert manifest["verification"]["actual_geometry"] == [
            plan.output_width, plan.output_height,
        ]
        assert manifest["verification"]["geometry_ok"] is True

    def test_b8_audio_maps_from_the_original_file(self, tmp_path):
        marker("b8")
        # the promise: audio comes from the ORIGINAL file, never the
        # video-only mezzanine — pinned on the encode command line
        result, _ = run_happy(tmp_path)
        enc = find_command(result.state.commands, "libx265")
        # first -i is the mezzanine; the second -i is the ORIGINAL
        inputs = [enc[i + 1] for i, a in enumerate(enc) if a == "-i"]
        assert len(inputs) == 2
        assert inputs[0] == result.state.mezzanine_path
        assert inputs[1] == result.state.file_path
        assert "1:a:0" in enc  # audio stream is mapped from input 1

    def test_b8_manifest_honesty_fields_promise(self, tmp_path):
        marker("b8")
        # the promise: every manifest states its engine and simulated
        # flag; a fail-open manifest states what shipped and why
        reason = "upscale mezzanine duration gate: mismatch"
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        engine = FakeEngine(UpscaleError(reason))
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            engine=engine, log_dir=str(tmp_path / "runs"),
        )
        manifest = json.loads(Path(result.manifest_path).read_text())
        run = manifest["run"]
        assert run["engine"] == "FakeEngine"
        assert run["engine_simulated"] is True
        assert run["fail_open"] is True
        assert run["fail_open_reason"] == reason
        # and the same fields on the state, byte-identical
        assert result.state.fail_open_reason == run["fail_open_reason"]
        assert result.state.engine_note == run["engine_note"]

    def test_b8_fail_open_output_filename_promise(self, tmp_path):
        marker("b8")
        # a fail-open run still produces the exact promised filename
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        engine = FakeEngine(UpscaleError("gate M: mezz 1.0 vs prep 6838.0"))
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            engine=engine, log_dir=str(tmp_path / "runs"),
        )
        assert Path(result.plan.output_path).name == (
            "movie.vidlift.720p.hevc.mp4"
        )
        assert os.path.isfile(result.plan.output_path)


class TestNoAutoTileEver:
    """The explicit tiling prohibition at the runner boundary."""

    def test_no_engine_command_ever_pairs_t0(self, tmp_path):
        # the v0.2.23 seam-line lesson: -t 0 (driver auto-tile) can
        # never appear in an engine command line. With the stub as the
        # test/CI engine there are no engine commands at all, which is
        # the strongest form of the guarantee; the b5 series pins the
        # adapter side. Here we assert the stub path constructs no
        # engine command with -t 0 and no encode/prepare command with it
        # either (ffmpeg has no -t 0 pairing in our pinned commands).
        result, runner = run_happy(tmp_path)
        for command in result.state.commands:
            for i, arg in enumerate(command):
                if arg == "-t":
                    assert command[i + 1] != "0", (
                        f"-t 0 appeared in {command}"
                    )

    def test_cli_accepts_engine_flag(self):
        args = cli.build_parser().parse_args(
            ["run", "--engine", "ncnn", "--realesrgan", "/r/e", "movie.avi"]
        )
        assert args.engine == "ncnn"
        assert args.realesrgan == "/r/e"
        args2 = cli.build_parser().parse_args(["run", "movie.avi"])
        assert args2.engine == "auto"  # the default


# ---- v0.4.0 R-series: decode-truth gates + heartbeat + manifest (B.9) ------


class TestV04GatesAndHeartbeat:
    """R1-R3 (design B.9): the new gates route through the UNCHANGED
    fail-open path; the heartbeat keeps status honest during long
    commands; the manifest's v2 additive fields are present on
    success and on gate-trip fail-open."""

    def _ncnn_engine(self, tmp_path, *, mezzanine_duration):
        """A real NcnnVulkanEngine over decode-truth fake seams whose
        mezzanine trips the clock gate (a coherent small fixture:
        7500 decoded frames, 300 s, 25 fps). The fake seams come from
        the engine boundary module (same suite, same rootdir imports)."""
        import importlib

        engine_tests = importlib.import_module("test_engine_ncnn")
        EngineFakeRunner = engine_tests.EngineFakeRunner
        FakeEngineProcess = engine_tests.FakeEngineProcess
        make_fake_binary = engine_tests.make_fake_binary
        make_fixture_models = engine_tests.make_fixture_models

        prepared_root = tmp_path / "engine"
        prepared_root.mkdir(exist_ok=True)
        models, table = make_fixture_models(prepared_root / "models")
        runner = EngineFakeRunner(
            prepared_duration=300.0,
            mezzanine_duration=mezzanine_duration,
            prepared_frames=7500,
            audio_stream_duration=300.0,
            source_format_duration=300.0,
        )
        engine = NcnnVulkanEngine(
            binary=str(make_fake_binary(prepared_root / "bin")),
            models_dir=str(models),
            artifacts=table,
            runner=runner,
            engine_runner=FakeEngineProcess(),
            chunk_seconds=120,
        )
        return engine, runner

    def test_r1_clock_gate_trip_fails_open_with_retention_in_reason(
        self, tmp_path
    ):
        marker("r1")
        engine, _runner = self._ncnn_engine(
            tmp_path, mezzanine_duration=450.0
        )
        path = make_media_file(tmp_path, "gate-trip.avi")
        payload = probe_payload(duration="300.0")
        runner = FakeRunner(payload)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            engine=engine, log_dir=str(tmp_path / "runs"),
        )
        state = result.state
        # the UNCHANGED fail-open contract: the run completed and
        # verified; the shipped output is the synchronized re-encode
        assert result.ok is True
        assert state.fail_open is True
        assert state.engine_simulated is True
        assert "mezzanine_vs_audio" in state.fail_open_reason
        # the reason names the retention paths verbatim (B.5)
        assert "rejected mezzanine retained at" in state.fail_open_reason
        assert "diagnostic at" in state.fail_open_reason
        # B.8: the manifest carries the retained-reject paths
        manifest = json.loads(Path(result.manifest_path).read_text())
        run = manifest["run"]
        assert run["fail_open"] is True
        assert run["fail_open_reason"] == state.fail_open_reason
        assert run["rejected_mezzanine"] == state.rejected_mezzanine
        assert run["rejected_mezzanine"]["path"].endswith(".rejected-") or (
            ".rejected-" in run["rejected_mezzanine"]["path"]
        )
        # the retained artifacts exist on disk
        assert Path(state.rejected_mezzanine["path"]).is_file()
        assert Path(state.rejected_mezzanine["diagnostic_path"]).is_file()
        # and the v2 additive fields are present (None on this path)
        assert run["frame_gates"] is None
        assert run["resume"] is None

    def test_r3_manifest_v2_additive_fields_on_success_and_fail_open(
        self, tmp_path
    ):
        marker("r3")
        # success path (stub engine): the additive keys exist with the
        # null rule — the stub carries none of them
        result, _runner = run_happy(tmp_path)
        manifest = json.loads(Path(result.manifest_path).read_text())
        run = manifest["run"]
        assert "frame_gates" in run and run["frame_gates"] is None
        assert "resume" in run and run["resume"] is None
        assert "rejected_mezzanine" in run
        assert run["rejected_mezzanine"] is None
        # state.to_dict() carries the same fields (the bridge contract)
        data = result.state.to_dict()
        for key in ("frame_gates", "resume", "rejected_mezzanine"):
            assert key in data

        # and on a clean engine success they are POPULATED
        engine, _runner2 = self._ncnn_engine(
            tmp_path, mezzanine_duration=300.0
        )
        path = make_media_file(tmp_path, "clean-engine.avi")
        payload = probe_payload(duration="300.0")
        runner = FakeRunner(payload)
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        result2 = run_file(
            path, tools=tools, probe_runner=runner, command_runner=runner,
            engine=engine, log_dir=str(tmp_path / "runs2"),
        )
        manifest2 = json.loads(Path(result2.manifest_path).read_text())
        run2 = manifest2["run"]
        assert run2["engine_simulated"] is False
        assert run2["frame_gates"]["mezzanine_vs_prepared"]["ok"] is True
        assert run2["frame_gates"]["mezzanine_vs_prepared"]["expected"] == 7500
        assert run2["resume"] == {
            "checkpoint_used": False, "chunks_reused": 0,
        }
        assert run2["frame_clock"]["source"] == "derived"
        assert run2["frame_clock"]["value"] == "25"
        assert run2["duration_gates"]["mezzanine_vs_audio"]["ok"] is True

    def test_r2_status_during_long_encode_shows_activity_and_step(
        self, tmp_path, monkeypatch
    ):
        marker("r2")
        import threading
        import time as _time

        import vidlift.run as run_module

        monkeypatch.setattr(run_module, "HEARTBEAT_TOUCH_SECONDS", 0.05)
        monkeypatch.setattr(run_module, "HEARTBEAT_LOG_SECONDS", 10.0)
        encode_entered = threading.Event()

        class SlowEncodeRunner(FakeRunner):
            def __call__(self, binary, args):
                if "libx265" in args:
                    encode_entered.set()
                    _time.sleep(0.3)  # long enough for the shrunk pump
                return super().__call__(binary, args)

        path = make_media_file(tmp_path)
        runner = SlowEncodeRunner(probe_payload())
        tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
        state_box: dict = {}

        def _run():
            run_file(
                path, tools=tools, probe_runner=runner, command_runner=runner,
                log_dir=str(tmp_path / "runs"), state_box=state_box,
            )

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        assert encode_entered.wait(timeout=5)
        activity_samples: list[float] = []
        step_samples: list[str] = []
        deadline = _time.time() + 5
        while _time.time() < deadline:
            state = state_box.get("state")
            if state is not None:
                if state.progress and state.progress.get("step"):
                    step_samples.append(state.progress["step"])
                activity_samples.append(state.last_activity_at)
            if not thread.is_alive():
                break
            _time.sleep(0.03)
        thread.join(timeout=10)
        # R2: the status payload's step names the encode while it runs
        assert "encode" in step_samples
        # and last_activity_at ADVANCED during the long encode (the
        # v0.3.0 blind window read as frozen)
        assert len(activity_samples) >= 2
        assert activity_samples[-1] > activity_samples[0]


# ---- b7/b8/b12 execution canary (runs LAST in this file) -----------------


def test_b7_b8_b12_execution_canary_run_boundary_tests_ran():
    missing = {"b7", "b8", "b12"} - EXECUTED_B
    assert not missing, (
        f"run-file boundary tests did not execute: {sorted(missing)}"
    )
