"""Boundary tests for the sample-based classification slice (P4).

The three-way classification from bluray-to-mp4's v0.2.19 design
Section 3.3, adopted verbatim: progressive / hard 3:2 telecine
(fieldmatch+decimate to 24000/1001) / genuine interlace (bwdif at
field rate). All headless: canned idet statistics fixtures, fake
runners, no real video decode.

Boundary discipline (clause f): each arm asserts the EXACT filter
string in the recorded prepare command plus the rate pin the P1
adapter re-probes on the prepared file; the regression arm pins that
unflagged progressive sources get NO deinterlace filter at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vidlift.classify_sample import (
    DEFAULT_SAMPLE_SECONDS,
    INTERLACED_FILTER,
    PROGRESSIVE,
    TELECINE,
    TELECINE_FILTER,
    TELECINE_OUTPUT_RATE,
    build_idet_command,
    chain_for,
    classify_interlace_sample,
    parse_idet_output,
)
from vidlift.probe import classify, parse_probe
from vidlift.run import (
    ToolBox,
    build_prepare_command,
    run_file,
)

__all__ = []  # tests only


# ---- canned fixtures ---------------------------------------------------------


def probe_payload(
    *,
    field_order: str = "progressive",
    fps: str = "30000/1001",
) -> str:
    return json.dumps(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "mpeg2video",
                    "width": 720,
                    "height": 480,
                    "sample_aspect_ratio": "32:27",
                    "field_order": field_order,
                    "r_frame_rate": fps,
                    "avg_frame_rate": fps,
                }
            ],
            "format": {"format_name": "mpeg", "duration": "5400.0"},
        }
    )


def make_report(
    *, field_order: str = "progressive", fps: str = "30000/1001"
):
    return classify(
        parse_probe(
            "/media/dvdrip.mpg",
            probe_payload(field_order=field_order, fps=fps),
        )
    )


def idet_stderr(
    *,
    tff: int = 0,
    bff: int = 0,
    progressive: int = 0,
    undetermined: int = 0,
) -> str:
    """A canned idet transcript (the ffmpeg stderr shape)."""
    return (
        "[Parsed_idet @ 0x1] Repeated Fields: Top: 0 Bottom: 0\n"
        f"[Parsed_idet @ 0x1] Single frame detection: TFF: {tff} "
        f"BFF: {bff} Progressive: {progressive} "
        f"Undetermined: {undetermined}\n"
        f"[Parsed_idet @ 0x1] Multi frame detection: TFF: {tff} "
        f"BFF: {bff} Progressive: {progressive} "
        f"Undetermined: {undetermined}\n"
    )


class IdetRunner:
    """Fake seam returning a canned idet transcript."""

    def __init__(self, stderr: str, *, exit_code: int = 0) -> None:
        self.stderr = stderr
        self.exit_code = exit_code
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        self.commands.append((binary, tuple(args)))
        return self.exit_code, "", self.stderr


# ---- idet command + parse units ----------------------------------------------


def test_idet_command_shape_is_pinned():
    cmd = build_idet_command("/bin/ffmpeg", "/media/movie.mpg")
    assert cmd == [
        "/bin/ffmpeg",
        "-hide_banner", "-nostdin",
        "-t", "60",
        "-i", "/media/movie.mpg",
        "-vf", "idet",
        "-an", "-sn",
        "-f", "null", "-",
    ]


def test_idet_sample_length_clamps():
    for requested, expected in ((5, 10), (60, 60), (500, 120), (None, 60)):
        cmd = build_idet_command(
            "/bin/ffmpeg", "/x", sample_seconds=requested
        )
        assert cmd[cmd.index("-t") + 1] == str(expected)


def test_parse_idet_output_extracts_statistics():
    single, multi, repeated = parse_idet_output(idet_stderr(tff=12, progressive=3))
    assert single["TFF"] == 12 and single["Progressive"] == 3
    assert multi["BFF"] == 0
    assert repeated == {"Top": 0, "Bottom": 0}
    # garbage transcript parses to zeros (never raises)
    single, _multi, _repeated = parse_idet_output("no idet lines here")
    assert single["Progressive"] == 0


# ---- the three classification arms ---------------------------------------------


def test_progressive_arm_classifies_clean_frames():
    # ~90% clean single-frame detections: progressive
    verdict = classify_interlace_sample(
        "/media/movie.mpg",
        ffmpeg="/bin/ffmpeg",
        runner=IdetRunner(idet_stderr(tff=3, progressive=27)),
    )
    assert verdict.kind == PROGRESSIVE
    assert "27/30" in verdict.detail
    # the prepare-chain consequence: NO deinterlace filter at all
    chain = chain_for(verdict, make_report())
    assert chain.filters == []


def test_telecine_arm_classifies_32_cadence():
    # the 2-of-5 clean-frame cadence: 40% progressive, 60% combed
    verdict = classify_interlace_sample(
        "/media/movie.mpg",
        ffmpeg="/bin/ffmpeg",
        runner=IdetRunner(idet_stderr(tff=18, progressive=12)),
    )
    assert verdict.kind == TELECINE
    assert "3:2 telecine" in verdict.detail
    chain = chain_for(verdict, make_report())
    # the reference IVTC chain, verbatim, plus the 24000/1001 rate pin
    assert chain.filters == [TELECINE_FILTER]
    assert TELECINE_FILTER == (
        "fieldmatch=combmatch=full,bwdif=mode=send_field:deint=interlaced,decimate"
    )
    assert chain.output_rate == TELECINE_OUTPUT_RATE == "24000/1001"
    assert chain.fps_filter is None


def test_interlace_arm_classifies_genuine_interlace():
    # near-zero clean frames: genuine interlace
    verdict = classify_interlace_sample(
        "/media/movie.mpg",
        ffmpeg="/bin/ffmpeg",
        runner=IdetRunner(idet_stderr(tff=28, progressive=1)),
    )
    assert verdict.kind == "interlaced"
    assert "genuine interlace" in verdict.detail
    # NTSC source: bwdif at field rate, fps doubled as an exact rational
    chain = chain_for(verdict, make_report(fps="30000/1001"))
    assert chain.filters == [INTERLACED_FILTER]
    assert INTERLACED_FILTER == "bwdif=mode=send_field:parity=auto"
    assert chain.fps_filter == "fps=60000/1001"  # the reference value
    # PAL source: 25/1 -> 50/1
    chain_pal = chain_for(verdict, make_report(fps="25/1"))
    assert chain_pal.fps_filter == "fps=50/1"


def test_ambiguous_sample_is_undecidable_never_a_guess():
    verdict = classify_interlace_sample(
        "/media/movie.mpg",
        ffmpeg="/bin/ffmpeg",
        # 50/50 split inside neither arm's band: ambiguous
        runner=IdetRunner(idet_stderr(tff=15, progressive=15)),
    )
    assert verdict.kind is None
    assert "ambiguous" in verdict.detail
    assert chain_for(verdict, make_report()) is None


def test_failed_idet_pass_is_undecidable_never_fatal():
    verdict = classify_interlace_sample(
        "/media/movie.mpg",
        ffmpeg="/bin/ffmpeg",
        runner=IdetRunner("whatever", exit_code=1),
    )
    assert verdict.kind is None
    assert "sample pass failed" in verdict.detail
    assert chain_for(verdict, make_report()) is None


def test_all_zero_statistics_are_undecidable():
    verdict = classify_interlace_sample(
        "/media/movie.mpg",
        ffmpeg="/bin/ffmpeg",
        runner=IdetRunner(idet_stderr()),
    )
    assert verdict.kind is None
    assert "no usable single-frame detections" in verdict.detail


# ---- prepare-command boundary (the exact filter string) --------------------------


def _prepare_cmd(sample_chain=None, *, field_order="tt", fps="30000/1001"):
    from vidlift.plan import build_file_plan

    report = make_report(field_order=field_order, fps=fps)
    plan = build_file_plan(report)
    tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
    return build_prepare_command(
        tools, plan, "/w/x.prepared.mkv", sample_chain=sample_chain
    )


def test_prepare_command_progressive_arm_exact_string():
    verdict = classify_interlace_sample(
        "/media/movie.mpg",
        ffmpeg="/bin/ffmpeg",
        runner=IdetRunner(idet_stderr(tff=1, progressive=99)),
    )
    chain = chain_for(verdict, make_report())
    cmd = _prepare_cmd(chain)
    vf = cmd[cmd.index("-vf") + 1]
    # 720x480 SAR 32:27 displays as 854x480; NO deinterlace filter
    assert vf == "scale=854:480,setsar=1"
    assert "-r" not in cmd


def test_prepare_command_telecine_arm_exact_string_and_rate():
    verdict = classify_interlace_sample(
        "/media/movie.mpg",
        ffmpeg="/bin/ffmpeg",
        runner=IdetRunner(idet_stderr(tff=18, progressive=12)),
    )
    chain = chain_for(verdict, make_report())
    cmd = _prepare_cmd(chain)
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == f"scale=854:480,setsar=1,{TELECINE_FILTER}"
    # the rate pin rides as -r BEFORE -y (an output option)
    assert cmd[cmd.index("-r") + 1] == "24000/1001"
    assert cmd.index("-r") < cmd.index("-y")


def test_prepare_command_interlace_arm_exact_string_and_field_rate():
    verdict = classify_interlace_sample(
        "/media/movie.mpg",
        ffmpeg="/bin/ffmpeg",
        runner=IdetRunner(idet_stderr(tff=49, progressive=1)),
    )
    chain = chain_for(verdict, make_report(fps="30000/1001"))
    cmd = _prepare_cmd(chain)
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == f"scale=854:480,setsar=1,{INTERLACED_FILTER},fps=60000/1001"
    assert "-r" not in cmd  # the interlace arm pins cadence via fps, not -r


def test_prepare_command_undecidable_falls_back_to_flag_heuristic():
    cmd = _prepare_cmd(None)  # undecidable -> the pre-P4 fallback
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == "scale=854:480,setsar=1,bwdif=mode=send_field"


def test_regression_unflagged_progressive_gets_no_deinterlace():
    # the P4 regression pin: an unflagged progressive source NEVER gets
    # a deinterlace filter, before or after P4
    cmd = _prepare_cmd(None, field_order="progressive")
    vf = cmd[cmd.index("-vf") + 1]
    assert vf == "scale=854:480,setsar=1"
    assert "bwdif" not in vf and "fieldmatch" not in vf and "decimate" not in vf


# ---- runner integration (the recorded prepare command + re-probe clock) ----------


class RunnerWithIdet:
    """The full fake seam for a P4 runner test: canned probe JSON,
    canned idet transcript for the sample pass, prepare/encode/verify
    effects (verify reports the planned output geometry)."""

    def __init__(
        self,
        payload: str,
        *,
        idet_stderr: str,
        prepared_rate: str = "60000/1001",
    ) -> None:
        self.payload = payload
        self.idet_stderr = idet_stderr
        self.prepared_rate = prepared_rate
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        self.commands.append((binary, tuple(args)))
        if "-show_format" in args:
            return 0, self.payload, ""
        if "idet" in args:
            return 0, "", self.idet_stderr
        if "-show_streams" in args and "-show_format" not in args:
            # the streams-only command is the verify probe on the encode
            # output: report the PLANNED geometry (854x480 source at
            # 16:9 plans to 1280x720) so verification passes
            return 0, json.dumps(
                {"streams": [{"codec_type": "video", "width": 1280, "height": 720}]}
            ), ""
        if args and str(args[-1]).endswith(".prepared.mkv"):
            Path(args[-1]).write_bytes(b"prepared-stub")
            return 0, "", ""
        if args and str(args[-1]).endswith(".720p.hevc.mp4"):
            Path(args[-1]).write_bytes(b"output-stub")
            return 0, "", ""
        if args and args[-1] == "-" and "null" in args:
            return 0, "", ""
        return 1, "", f"unexpected: {binary} {' '.join(args)}"


def _runner_payload(field_order: str = "tt") -> str:
    return json.dumps(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "mpeg2video",
                    "width": 720,
                    "height": 480,
                    "sample_aspect_ratio": "32:27",
                    "field_order": field_order,
                    "r_frame_rate": "30000/1001",
                    "avg_frame_rate": "30000/1001",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "mp3",
                    "channels": 2,
                },
            ],
            "format": {"format_name": "mpeg", "duration": "5400.0"},
        }
    )


def _run_with_idet(tmp_path, idet_stderr, *, field_order="tt"):
    path = tmp_path / "dvdrip.mpg"
    path.write_text("")
    runner = RunnerWithIdet(_runner_payload(field_order), idet_stderr=idet_stderr)
    tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
    result = run_file(
        str(path),
        tools=tools,
        probe_runner=runner,
        command_runner=runner,
        log_dir=str(tmp_path / "runs"),
    )
    return result, runner


def test_runner_telecine_flow_end_to_end(tmp_path):
    # flagged file + telecine-shaped idet: the recorded prepare command
    # carries the exact IVTC chain, the sample verdict lands in state
    # and manifest, and the idet pass ran exactly once through the seam
    result, runner = _run_with_idet(
        tmp_path, idet_stderr(tff=18, progressive=12)
    )
    state = result.state
    idet_cmds = [c for _b, c in runner.commands if "idet" in c]
    assert len(idet_cmds) == 1
    prepare = next(
        c for _b, c in runner.commands
        if str(c[-1]).endswith(".prepared.mkv")
    )
    vf = prepare[prepare.index("-vf") + 1]
    assert vf == f"scale=854:480,setsar=1,{TELECINE_FILTER}"
    assert prepare[prepare.index("-r") + 1] == "24000/1001"
    assert state.sample_verdict == {
        "kind": "telecine",
        "detail": state.sample_verdict["detail"],
        "single": {"TFF": 18, "BFF": 0, "Progressive": 12, "Undetermined": 0},
        "sample_seconds": 60.0,
    }
    manifest = json.loads(Path(result.manifest_path).read_text())
    assert manifest["run"]["sample_verdict"]["kind"] == "telecine"


def test_runner_interlace_flow_end_to_end(tmp_path):
    result, runner = _run_with_idet(
        tmp_path, idet_stderr(tff=49, progressive=1)
    )
    prepare = next(
        c for _b, c in runner.commands
        if str(c[-1]).endswith(".prepared.mkv")
    )
    vf = prepare[prepare.index("-vf") + 1]
    assert vf == (
        "scale=854:480,setsar=1,bwdif=mode=send_field:parity=auto,"
        "fps=60000/1001"
    )
    assert result.state.sample_verdict["kind"] == "interlaced"


def test_runner_progressive_flag_recovery_end_to_end(tmp_path):
    # the flag LIED (stream says tt, idet sees clean frames): the
    # classification overrides the flag and NO deinterlace runs
    result, runner = _run_with_idet(
        tmp_path, idet_stderr(tff=1, progressive=99)
    )
    prepare = next(
        c for _b, c in runner.commands
        if str(c[-1]).endswith(".prepared.mkv")
    )
    vf = prepare[prepare.index("-vf") + 1]
    assert vf == "scale=854:480,setsar=1"
    assert result.state.sample_verdict["kind"] == "progressive"


def test_runner_undecidable_falls_back_to_flag_heuristic(tmp_path):
    # ambiguous statistics: the flag heuristic runs, the verdict says
    # undecidable, the run still completes
    result, runner = _run_with_idet(
        tmp_path, idet_stderr(tff=15, progressive=15)
    )
    prepare = next(
        c for _b, c in runner.commands
        if str(c[-1]).endswith(".prepared.mkv")
    )
    vf = prepare[prepare.index("-vf") + 1]
    assert vf == "scale=854:480,setsar=1,bwdif=mode=send_field"
    assert result.state.sample_verdict["kind"] is None
    assert "ambiguous" in result.state.sample_verdict["detail"]


def test_runner_unflagged_progressive_never_runs_idet(tmp_path):
    # the classification runs ONLY for flagged files: unflagged sources
    # skip the sample pass entirely (no idet command ever recorded)
    result, runner = _run_with_idet(
        tmp_path, idet_stderr(tff=28), field_order="progressive"
    )
    assert all("idet" not in c for _b, c in runner.commands)
    prepare = next(
        c for _b, c in runner.commands
        if str(c[-1]).endswith(".prepared.mkv")
    )
    vf = prepare[prepare.index("-vf") + 1]
    assert vf == "scale=854:480,setsar=1"  # no deinterlace, ever
    assert result.state.sample_verdict is None


def test_runner_sample_classify_disable_flag_keeps_old_behavior(tmp_path):
    # sample_classify=False: the flag heuristic drives prepare directly
    # (the escape hatch for callers that know better)
    path = tmp_path / "dvdrip.mpg"
    path.write_text("")
    runner = RunnerWithIdet(_runner_payload("tt"), idet_stderr="unused")
    tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
    result = run_file(
        str(path),
        tools=tools,
        probe_runner=runner,
        command_runner=runner,
        log_dir=str(tmp_path / "runs"),
        sample_classify=False,
    )
    assert all("idet" not in c for _b, c in runner.commands)
    prepare = next(
        c for _b, c in runner.commands
        if str(c[-1]).endswith(".prepared.mkv")
    )
    vf = prepare[prepare.index("-vf") + 1]
    assert vf == "scale=854:480,setsar=1,bwdif=mode=send_field"
    assert result.state.sample_verdict is None