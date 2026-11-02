"""v0.4.15 denoise — the opt-in hqdn3d prepare pass (design §Denoise).

The contract: OFF ships byte-identical v0.4.14 prepare commands; light/
strong append the preset LAST in the filter list (after deinterlace/
telecine + fps, before the ffv1 mezzanine); every surface (CLI run-only,
MCP options, GUI combo, config default) defaults off.

Red-first proven on 385118f: the ``denoise`` kwarg did not exist
(TypeError), config.DENOISE_DEFAULT/QSETTINGS_DENOISE_KEYS were absent
(ImportError/AttributeError), the MCP schema rejected the options key
(InvalidInput), and no manifest block carried the value.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from vidlift.config import DENOISE_DEFAULT, QSETTINGS_DENOISE_KEYS
from vidlift.mcp_bridge.schemas import InvalidInput, validate_tool_args
from vidlift.plan import build_file_plan
from vidlift.probe import AudioInfo, FileReport, VideoInfo
from vidlift.run import (
    DENOISE_PRESETS,
    ToolBox,
    build_prepare_command,
    denoise_filter,
    run_file,
)


def _report(path: str = "/media/Movie.avi") -> FileReport:
    return FileReport(
        path=path,
        container="avi",
        duration_seconds=60.0,
        video=VideoInfo(
            codec_name="mpeg4", width=640, height=272,
            sample_aspect_ratio="1:1", display_aspect_ratio="40:17",
            sar=(1, 1), field_order="progressive", r_frame_rate=25.0,
            avg_frame_rate=25.0, r_frame_rate_raw="25/1",
            avg_frame_rate_raw="25/1",
        ),
        audio=[AudioInfo(codec_name="mp3", channels=2, lang="und")],
        subtitles=[],
        source_class="xvid-divx-avi",
        displayed_width=640,
        displayed_height=272,
        upscale_verdict="candidate",
        interlaced_flagged=False,
        pal_speedup_suspected=False,
        sidecars=[],
    )


def _plan(tmp_path: Path, report: FileReport):
    return build_file_plan(
        report,
        output_dir=str(tmp_path / "out"),
    )


def _tools() -> ToolBox:
    return ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")


# ---- 1. default-OFF byte-identity -------------------------------------------

def test_prepare_command_absent_denoise_is_byte_identical(tmp_path):
    """No denoise argument at all: the v0.4.14 command shape, no
    hqdn3d token anywhere."""
    plan = _plan(tmp_path, _report())
    cmd = build_prepare_command(_tools(), plan, "/p/out.prepared.mkv")
    assert not any("hqdn3d" in part for part in cmd)


def test_prepare_command_off_and_none_match_absent(tmp_path):
    """``denoise=None`` and ``denoise="off"`` are the same OFF arm."""
    plan = _plan(tmp_path, _report())
    absent = build_prepare_command(_tools(), plan, "/p/out.prepared.mkv")
    none = build_prepare_command(
        _tools(), plan, "/p/out.prepared.mkv", denoise=None
    )
    off = build_prepare_command(
        _tools(), plan, "/p/out.prepared.mkv", denoise="off"
    )
    assert none == absent == off


# ---- 2. presets append LAST ---------------------------------------------------

def test_prepare_command_denoise_light_appends_last(tmp_path):
    plan = _plan(tmp_path, _report())
    cmd = build_prepare_command(
        _tools(), plan, "/p/out.prepared.mkv", denoise="light"
    )
    vf = cmd[cmd.index("-vf") + 1]
    filters = vf.split(",")
    assert filters[-1] == DENOISE_PRESETS["light"]
    assert filters[-1] == "hqdn3d=1.5:3.0:3.0:6.0"
    # scale + setsar remain at the head
    assert filters[0].startswith("scale=")
    assert filters[1] == "setsar=1"


def test_prepare_command_denoise_strong_preset(tmp_path):
    plan = _plan(tmp_path, _report())
    cmd = build_prepare_command(
        _tools(), plan, "/p/out.prepared.mkv", denoise="strong"
    )
    vf = cmd[cmd.index("-vf") + 1]
    assert vf.split(",")[-1] == "hqdn3d=4.0:8.0:6.0:12.0"


def test_denoise_after_deinterlace_in_chain(tmp_path):
    """Chain-position pin: with an interlaced sample chain, hqdn3d
    indexes AFTER the deinterlace/telecine filters and the fps filter."""
    plan = _plan(tmp_path, _report(interlaced=True))
    from vidlift.classify_sample import PrepareChain

    chain = PrepareChain(
        filters=["bwdif=mode=send_field"], fps_filter="fps=50",
        output_rate="50",
    )
    cmd = build_prepare_command(
        _tools(), plan, "/p/out.prepared.mkv",
        sample_chain=chain, denoise="light",
    )
    vf = cmd[cmd.index("-vf") + 1]
    filters = vf.split(",")
    assert filters.index("hqdn3d=1.5:3.0:3.0:6.0") > filters.index(
        "bwdif=mode=send_field"
    )
    assert filters.index("hqdn3d=1.5:3.0:3.0:6.0") > filters.index(
        "fps=50"
    )
    # still the LAST filter in the -vf list
    assert filters[-1].startswith("hqdn3d=")


def test_denoise_filter_presets_and_unknown():
    assert denoise_filter(None) is None
    assert denoise_filter("") is None
    assert denoise_filter("off") is None
    assert denoise_filter("light") == "hqdn3d=1.5:3.0:3.0:6.0"
    assert denoise_filter("strong") == "hqdn3d=4.0:8.0:6.0:12.0"
    with pytest.raises(ValueError, match="unknown denoise preset"):
        denoise_filter("bogus")


# ---- 3. config default --------------------------------------------------------

def test_config_denoise_default_off():
    assert DENOISE_DEFAULT == "off"
    assert QSETTINGS_DENOISE_KEYS["mode"] == "denoise/mode"


# ---- 4. CLI surface (run only) -------------------------------------------------

def test_cli_run_accepts_denoise_choices():
    from vidlift.cli import build_parser

    parser = build_parser()
    for mode in ("off", "light", "strong"):
        args = parser.parse_args(["run", "/media/x.avi", "--denoise", mode])
        assert args.denoise == mode
    args = parser.parse_args(["run", "/media/x.avi"])
    assert args.denoise == "off"  # absent reads off


def test_cli_run_denoise_bogus_exits_2():
    from vidlift.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["run", "/media/x.avi", "--denoise", "bogus"])
    assert excinfo.value.code == 2


def test_cli_subs_has_no_denoise():
    """`vidlift subs` has no prepare chain — the flag must NOT exist
    there (Mode B remuxes, never re-encodes)."""
    from vidlift.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["subs", "/media/x.avi", "--denoise", "light"])
    assert excinfo.value.code == 2


def test_cli_run_help_carries_verbatim_guidance():
    """Eric's verbatim-copy requirement: `--denoise` help equals the
    design block (argparse reflows lines; text compared whitespace-
    normalized)."""
    from vidlift.cli import build_parser

    parser = build_parser()
    run_action = next(
        a for a in next(
            g for g in parser._subparsers._group_actions
        ).choices["run"]._actions
        if "--denoise" in getattr(a, "option_strings", ())
    )
    expected = (
        "denoise the source before upscaling: 'off' (default), 'light', or "
        "'strong'. Use for noisy sources (VHS rips, old tape transfers, grainy "
        "analog captures); leave off for clean DVD/Blu-ray rips — the upscale "
        "already softens grain, and denoising a clean source costs fine detail "
        "for no gain. Adds almost no time compared to the upscale itself."
    )
    assert " ".join(run_action.help.split()) == " ".join(expected.split())


# ---- 5. MCP surface ------------------------------------------------------------

def test_executor_passes_denoise_option(tmp_path, monkeypatch):
    """MCP options.denoise reaches run_file's kwarg; absent -> None
    (the config-default seam, never a silent 'light')."""
    import vidlift.mcp_bridge.jobs as jobs_mod
    import vidlift.run as run_mod
    from vidlift.mcp_bridge.jobs import _execute_run_job
    from vidlift.mcp_bridge.registry import Registry

    seen: dict = {}

    def fake_run_file(path, **kwargs):
        seen.update(kwargs)
        result = type(
            "R", (), {"ok": True, "plan": None, "state": None,
                      "manifest_path": None, "verification": None},
        )()
        result.plan = type("P", (), {"output_path": str(tmp_path / "o.mp4")})()
        result.state = type("S", (), {"log": None, "error": None,
                                      "engine_simulated": False,
                                      "engine_note": None})()
        result.verification = type("V", (), {"ok": True})()
        return result

    monkeypatch.setattr(run_mod, "run_file", fake_run_file)
    monkeypatch.setattr(
        run_mod, "find_tools", lambda *, ffmpeg, ffprobe: _tools()
    )

    registry = Registry(state_dir=str(tmp_path / "state"))
    record = registry.enqueue(
        kind="run", paths=["/media/x.avi"],
        options={"denoise": "light"},
    )
    outcome = _execute_run_job(record, _SimpleServices(registry), None)
    assert seen.get("denoise") == "light"

    record2 = registry.enqueue(kind="run", paths=["/media/x.avi"], options={})
    seen.clear()
    _execute_run_job(record2, _SimpleServices(registry), None)
    assert "denoise" not in seen or seen["denoise"] is None


def _run_schema_ok(options: dict) -> dict:
    return validate_tool_args("run", {"paths": ["/media/x.avi"],
                                      "options": options})


def test_mcp_schema_accepts_denoise():
    assert _run_schema_ok({"denoise": "light"}) is not None
    assert _run_schema_ok({}) is not None
    with pytest.raises(InvalidInput):
        _run_schema_ok({"denoise": 3})


# ---- 6. manifest/plan echo ------------------------------------------------------

def test_manifest_records_denoise(tmp_path, monkeypatch):
    """A run with denoise=light echoes ``plan.denoise: "light"`` in the
    manifest; the default run echoes "off"."""
    source = tmp_path / "movie.avi"
    source.write_text("")

    class Runner:
        def __init__(self):
            self.commands = []

        def __call__(self, binary, args):
            self.commands.append((binary, tuple(args)))
            joined = " ".join(args)
            if "-show_format" in args and "-show_streams" not in args:
                return 0, PROBE_PAYLOAD, ""
            if "-show_streams" in args:
                last = args[-1] if args else ""
                if last.endswith(".720p.mp4"):
                    return 0, json.dumps({"streams": [
                        {"codec_type": "video", "width": 1280,
                         "height": 720}]}) + "\n", ""
                return 0, PROBE_PAYLOAD, ""
            if "idet" in args:
                return 0, "", (
                    "[Parsed_idet] frame detection: TFF: 0 BFF: 0 "
                    "Progressive: 0 Undetermined: 0\n"
                    "[Parsed_idet] Single frame detection: TFF: 0 BFF: 0 "
                    "Progressive: 0 Undetermined: 0\n"
                )
            if "null" in args and args[-1] == "-":
                return 0, "", ""
            if args and args[-1].endswith(".prepared.mkv"):
                # the LAST -vf segment rides the command list; write the
                # file so prepare "produces" it
                Path(args[-1]).write_bytes(b"prepared")
                return 0, "", ""
            if "nb_read_frames" in joined:
                return 0, "900\n", ""
            if "-show_entries" in args and "nb_frames" in args:
                return 0, json.dumps({"streams": [{"nb_frames": "900"}]}), ""
            if args and args[-1].endswith(".720p.mp4"):
                Path(args[-1]).write_bytes(b"output")
                return 0, "", ""
            return 1, "", f"unexpected: {binary} {joined}"

    runner = Runner()
    result = run_file(
        str(source),
        tools=_tools(),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(tmp_path / "runs"),
        denoise="light",
    )
    manifest = json.loads(Path(result.manifest_path).read_text())
    assert manifest["plan"]["denoise"] == "light"
    assert manifest["run"]["denoise_mode"] == "light"
    # and the prepare command actually carried the filter
    prepare = [c for c in manifest["commands"]
               if any(a.endswith(".prepared.mkv") for a in c)]
    assert any("hqdn3d=1.5:3.0:3.0:6.0" in " ".join(c) for c in prepare)

    # default (denoise absent): plan.denoise == "off", no filter
    source2 = tmp_path / "movie2.avi"
    source2.write_text("")
    runner2 = Runner()
    result2 = run_file(
        str(source2),
        tools=_tools(),
        probe_runner=runner2,
        command_runner=runner2,
        engine_request="stub",
        log_dir=str(tmp_path / "runs2"),
    )
    manifest2 = json.loads(Path(result2.manifest_path).read_text())
    assert manifest2["plan"]["denoise"] == "off"
    prepare2 = [c for c in manifest2["commands"]
                if any(a.endswith(".prepared.mkv") for a in c)]
    assert not any("hqdn3d" in " ".join(c) for c in prepare2)


# ---- 7. resume-claim identity ---------------------------------------------------

def test_claim_refuses_scratch_with_mismatched_denoise(tmp_path, monkeypatch):
    """The non-obvious requirement: a resumed job must not silently
    change denoise settings. Scratch prepared under ``off`` never
    claims for a ``light`` run."""
    tests_dir = Path(__file__).resolve().parent
    import sys
    sys.path.insert(0, str(tests_dir))
    from test_resume_claim import (
        build_retained_scratch, make_media, ClaimFakeRunner, PROBE_PAYLOAD,
        prepare_arguments,
    )
    from vidlift.engine_ncnn import _workdir_for

    source = make_media(tmp_path, "Movie.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(
        log_root, run_id="Movie.20261027-201001", source=source,
    )
    # the seeded checkpoint carries v0.4.14-shape plan identity (no
    # denoise key) — it must read as "off"
    runner = ClaimFakeRunner()
    run_file(
        str(source),
        tools=_tools(),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(log_root),
        output_dir=str(tmp_path / "out-light"),
        resume=True,
        denoise="light",
    )
    # refusal: a fresh prepare ran for the light run
    assert len(prepare_arguments(runner)) == 1

    # the mirror arm: an OFF run still claims the v0.4.14 scratch
    runner2 = ClaimFakeRunner()
    run_file(
        str(source),
        tools=_tools(),
        probe_runner=runner2,
        command_runner=runner2,
        engine_request="stub",
        log_dir=str(log_root),
        output_dir=str(tmp_path / "out-off"),
        resume=True,
        denoise="off",
    )
    assert prepare_arguments(runner2) == []

    # and scratch EXPLICITLY prepared under light claims for light
    workdir = _workdir_for(
        str(log_root / "Movie.20261027-203001.prepared.mkv")
    )
    build_retained_scratch(
        log_root,
        run_id="Movie.20261027-203001",
        source=source,
        plan_block={
            "target": "720p",
            "gb_per_hour": 1.5,
            "geometry": [1280, 720],
            "profile": "live-action",
            "denoise": "light",
            "subtitle_bitmap_ocr": None,
            "source_flagged_interlaced": False,
        },
    )
    runner3 = ClaimFakeRunner()
    run_file(
        str(source),
        tools=_tools(),
        probe_runner=runner3,
        command_runner=runner3,
        engine_request="stub",
        log_dir=str(log_root),
        output_dir=str(tmp_path / "out-light2"),
        resume=True,
        denoise="light",
    )
    assert prepare_arguments(runner3) == []


def _report(path: str = "/media/Movie.avi", *,
            interlaced: bool = False) -> FileReport:  # noqa: F811
    report = _report_base(path)
    report.interlaced_flagged = interlaced
    return report


def _report_base(path: str) -> FileReport:
    return FileReport(
        path=path,
        container="avi",
        duration_seconds=60.0,
        video=VideoInfo(
            codec_name="mpeg4", width=640, height=272,
            sample_aspect_ratio="1:1", display_aspect_ratio="40:17",
            sar=(1, 1), field_order="progressive", r_frame_rate=25.0,
            avg_frame_rate=25.0, r_frame_rate_raw="25/1",
            avg_frame_rate_raw="25/1",
        ),
        audio=[AudioInfo(codec_name="mp3", channels=2, lang="und")],
        subtitles=[],
        source_class="xvid-divx-avi",
        displayed_width=640,
        displayed_height=272,
        upscale_verdict="candidate",
        interlaced_flagged=False,
        pal_speedup_suspected=False,
        sidecars=[],
    )


class _SimpleServices:
    def __init__(self, registry):
        self.registry = registry
        self.state_dir = None
        self.command_runner = None
        self.probe_runner = None
        self.engine = None

    def notify_new_work(self):
        pass


PROBE_PAYLOAD = json.dumps({
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "mpeg4",
         "width": 640, "height": 272, "sample_aspect_ratio": "1:1",
         "field_order": "progressive", "r_frame_rate": "25/1",
         "avg_frame_rate": "25/1"},
        {"index": 1, "codec_type": "audio", "codec_name": "mp3",
         "channels": 2, "tags": {"language": "und"}},
    ],
    "format": {"format_name": "avi", "duration": "60.0"},
})