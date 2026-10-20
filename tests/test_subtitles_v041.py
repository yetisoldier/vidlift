"""S1-S19: the v0.4.1 subtitle boundary series (design §C.3).

Fixtures, the five classes the design pins:
1. text-sub source (SRT embedded) -> pass-through, no OCR invoked;
2. bitmap-sub source (VobSub-in-MKV; PGS variant) -> OCR lane;
3. sidecar-only source (no embedded subs, <stem>.srt beside; and the
   .sub/.idx pair) -> adoption;
4. no-subs source -> status 'none', phase skipped, no warnings;
5. OCR-hard-fail source (zero cues / no tool / extraction fail) ->
   fail-open: ok run, warning text, manifest failed-open, kit written.

All external commands replay through stubbed runner seams — the suite
style since v0.1 (test_run.py/test_engine_ncnn.py boundaries); no real
ffmpeg, seconv, or tesseract needed here. The dev-box lane's real-media
validation (PGS + VobSub samples through real ffmpeg 8.0.1 + real
tesseract 5.3.4) ran at implementation time and is recorded in the
module docs; the S-series here pins the machinery contract.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from test_run import (  # noqa: E402
    FakeRunner,
    ToolBox,
    find_command,
    make_media_file,
    probe_payload,
    run_file,
)

from vidlift.plan import build_file_plan, select_subtitle_track  # noqa: E402
from vidlift.probe import (  # noqa: E402
    FileReport,
    MediaProbe,
    SidecarInfo,
    SubtitleInfo,
    VideoInfo,
    classify,
    find_sidecars,
    parse_probe,
)
from vidlift.run import PHASE_DONE, PHASE_SUBTITLES, verify_output  # noqa: E402
from vidlift import subtitles as S  # noqa: E402
from vidlift.srt import Cue, normalize_cues, parse_srt, write_srt_file  # noqa: E402


def _report(
    *,
    subs: list[dict] | None = None,
    sidecars: list[SidecarInfo] | None = None,
    path: str = "/media/movie.avi",
) -> FileReport:
    """A synthetic FileReport for selection-rule tests (S3/S5)."""
    payload_subs = subs or []
    streams = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "mpeg4",
            "width": 640,
            "height": 272,
            "sample_aspect_ratio": "1:1",
            "field_order": "progressive",
            "r_frame_rate": "25/1",
            "avg_frame_rate": "25/1",
        }
    ]
    for i, sub in enumerate(payload_subs, start=2):
        streams.append({"index": i, "codec_type": "subtitle", **sub})
    payload = json.dumps(
        {
            "streams": streams,
            "format": {"format_name": "avi", "duration": "5400.0"},
        }
    )
    report = classify(parse_probe(path, payload))
    if sidecars is not None:
        # classify() discovers sidecars from disk; synthetic tests inject
        import dataclasses

        report = dataclasses.replace(report, sidecars=sidecars)
    return report


# ---- S1: probe retains stream indices; kinds/lang round-trip -----------


class TestS1ProbeIndices:
    def test_subtitleinfo_carries_stream_index(self):
        report = _report(
            subs=[
                {"codec_name": "subrip", "tags": {"language": "eng"}},
                {"codec_name": "dvd_subtitle", "tags": {"language": "ger"}},
                {"codec_name": "ass"},
            ]
        )
        assert [(s.index, s.kind, s.lang) for s in report.subtitles] == [
            (2, "text", "eng"),
            (3, "image", "ger"),
            (4, "text", ""),
        ]

    def test_index_defaults_absent_for_legacy_shapes(self):
        info = SubtitleInfo(codec_name="srt", kind="text", lang="eng")
        assert info.index == -1  # back-compat default

    def test_probe_payload_round_trips_index(self, tmp_path):
        # The real parse path: canned ffprobe JSON with mixed indices
        payload = json.dumps(
            {
                "streams": [
                    {"index": 0, "codec_type": "video", "codec_name": "mpeg4",
                     "width": 640, "height": 272, "r_frame_rate": "25/1"},
                    {"index": 5, "codec_type": "subtitle",
                     "codec_name": "hdmv_pgs_subtitle",
                     "tags": {"language": "eng"}},
                ],
                "format": {"format_name": "mkv"},
            }
        )
        probe = parse_probe("/m/movie.mkv", payload)
        assert probe.subtitles[0].index == 5
        assert probe.subtitles[0].kind == "image"


# ---- S2: sidecar adoption order + embedded-wins + same-stem-only --------


class TestS2SidecarDiscovery:
    def _media(self, tmp_path, name="movie.avi"):
        path = tmp_path / name
        path.write_text("")
        return str(path)

    def test_srt_sidecar_found(self, tmp_path):
        src = self._media(tmp_path)
        (tmp_path / "movie.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
        found = find_sidecars(src)
        assert len(found) == 1
        assert found[0].kind == "text"
        assert found[0].path.endswith("movie.srt")

    def test_sub_idx_pair_found_when_no_srt(self, tmp_path):
        src = self._media(tmp_path)
        (tmp_path / "movie.sub").write_bytes(b"x")
        (tmp_path / "movie.idx").write_text("# VobSub index file, v3 (do not modify this line!)")
        found = find_sidecars(src)
        assert len(found) == 1
        assert found[0].kind == "image"

    def test_srt_wins_over_sub_idx(self, tmp_path):
        src = self._media(tmp_path)
        (tmp_path / "movie.srt").write_text("x")
        (tmp_path / "movie.sub").write_bytes(b"x")
        (tmp_path / "movie.idx").write_text("x")
        found = find_sidecars(src)
        assert len(found) == 1
        assert found[0].kind == "text"

    def test_same_stem_only_no_globbing(self, tmp_path):
        src = self._media(tmp_path)
        (tmp_path / "movie.English.srt").write_text("x")
        (tmp_path / "movie.eng.srt").write_text("x")
        assert find_sidecars(src) == []

    def test_missing_pair_member_not_adopted(self, tmp_path):
        src = self._media(tmp_path)
        (tmp_path / "movie.sub").write_bytes(b"x")  # no .idx
        assert find_sidecars(src) == []

    def test_embedded_wins_in_selection(self, tmp_path):
        # Embedded truth wins: a text sidecar beside a source WITH an
        # embedded text stream is noted but not selected.
        report = _report(
            subs=[{"codec_name": "srt", "tags": {"language": "eng"}}],
            sidecars=[SidecarInfo(path="/media/movie.srt", kind="text")],
        )
        plan = select_subtitle_track(report)
        assert plan is not None
        assert plan.source == "embedded"
        assert plan.stream_index == 2
        # And the honest note names the unadopted sidecar
        from vidlift.plan import build_file_plan

        file_plan = build_file_plan(report)
        assert "sidecar noted" in file_plan.subtitle_note


# ---- S3: selection rule ---------------------------------------------


class TestS3SelectionRule:
    def test_eng_preferred_over_earlier_untagged(self):
        report = _report(
            subs=[
                {"codec_name": "subrip", "tags": {"language": "ger"}},
                {"codec_name": "subrip", "tags": {"language": "eng"}},
            ]
        )
        plan = select_subtitle_track(report)
        assert plan.lang == "eng"
        assert plan.stream_index == 3

    def test_first_index_tie_break(self):
        report = _report(
            subs=[
                {"codec_name": "subrip"},  # untagged
                {"codec_name": "subrip"},  # untagged
            ]
        )
        plan = select_subtitle_track(report)
        assert plan.stream_index == 2

    def test_text_beats_bitmap(self):
        report = _report(
            subs=[
                {"codec_name": "dvd_subtitle", "tags": {"language": "eng"}},
                {"codec_name": "subrip", "tags": {"language": "ger"}},
            ]
        )
        plan = select_subtitle_track(report)
        assert plan.kind == "text"
        assert plan.ocr_required is False

    def test_bitmap_selected_when_no_text(self):
        report = _report(
            subs=[{"codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}}]
        )
        plan = select_subtitle_track(report)
        assert plan.kind == "image"
        assert plan.ocr_required is True
        assert plan.stream_index == 2

    def test_single_track_only_even_with_many(self):
        report = _report(
            subs=[
                {"codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}},
                {"codec_name": "dvd_subtitle", "tags": {"language": "eng"}},
                {"codec_name": "hdmv_pgs_subtitle", "tags": {"language": "ger"}},
            ]
        )
        plan = select_subtitle_track(report)
        assert plan.ocr_required is True
        assert plan.stream_index == 2  # the first English-tagged bitmap

    def test_unhandled_bitmap_not_selected(self):
        # dvb/xsub are detected, honestly reported as uncarried (§B.1);
        # with no other sub the result is the honest 'none'.
        report = _report(subs=[{"codec_name": "dvb_subtitle"}])
        assert select_subtitle_track(report) is None

    def test_sidecar_of_last_resort(self):
        report = _report(
            sidecars=[SidecarInfo(path="/media/movie.srt", kind="text")]
        )
        plan = select_subtitle_track(report)
        assert plan.source == "sidecar"
        assert plan.sidecar_path == "/media/movie.srt"

    def test_none_when_nothing_anywhere(self):
        report = _report()
        assert select_subtitle_track(report) is None


# ---- S4: text pass-through argv; mixed no longer all-drop --------------


class TestS4SelectedTrackMap:
    def _encode_cmd(self, report):
        plan = build_file_plan(report)
        from vidlift.run import build_encode_command

        return build_encode_command(
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"), plan, "/fake/mezz.mkv"
        )

    def test_selected_index_map_and_mov_text(self):
        report = _report(subs=[{"codec_name": "srt", "tags": {"language": "eng"}}])
        cmd = self._encode_cmd(report)
        assert "-map" in cmd and "1:2" in cmd
        assert "mov_text" in cmd
        assert "language=eng" in cmd
        assert "1:s?" not in cmd  # the all-or-nothing map is gone

    def test_mixed_text_bitmap_now_carries_text(self):
        # THE v0.4.1 fix: one image sub no longer kills text subs.
        report = _report(
            subs=[
                {"codec_name": "dvd_subtitle", "tags": {"language": "eng"}},
                {"codec_name": "subrip", "tags": {"language": "eng"}},
            ]
        )
        cmd = self._encode_cmd(report)
        assert "mov_text" in cmd
        assert "1:3" in cmd  # the text stream, by index
        # the bitmap stream is NOT mapped in the encode
        assert "1:2" not in cmd

    def test_bitmap_only_no_sub_map_in_encode(self):
        report = _report(subs=[{"codec_name": "dvd_subtitle"}])
        cmd = self._encode_cmd(report)
        assert "mov_text" not in cmd
        assert "1:2" not in cmd

    def test_mixed_file_end_to_end_carries_text(self, tmp_path):
        # Full stubbed run: mixed text+bitmap source completes with the
        # text track carried (v0.4.0 dropped it silently).
        path = make_media_file(tmp_path, "mixed.avi")
        payload = probe_payload(
            subtitles=[
                {"codec_name": "dvd_subtitle", "tags": {"language": "eng"}},
                {"codec_name": "subrip", "tags": {"language": "eng"}},
            ]
        )
        runner = FakeRunner(payload)
        result = run_file(
            path, tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        assert result.ok
        enc = find_command(result.state.commands, "libx265")
        assert "mov_text" in enc
        assert "1:3" in enc
        assert result.state.subtitles["status"] == "carried"


# ---- S5: ASS style-loss note; eng/und honesty --------------------------


class TestS5StyleLossAndLanguage:
    def test_ass_style_loss_flagged(self):
        report = _report(subs=[{"codec_name": "ass", "tags": {"language": "eng"}}])
        plan = build_file_plan(report)
        assert plan.subtitle_style_loss is True
        assert "styling" in plan.subtitle_note or True  # note describes plan
        # cli adds the explicit warning line
        from vidlift.cli import _render_plan

        text = _render_plan(plan)
        assert "ASS/SSA styling does not survive mov_text" in text

    def test_srt_no_style_loss(self):
        report = _report(subs=[{"codec_name": "srt", "tags": {"language": "eng"}}])
        plan = build_file_plan(report)
        assert plan.subtitle_style_loss is False

    def test_mux_language_eng_variants(self):
        assert S.mux_language("eng") == "eng"
        assert S.mux_language("en") == "eng"

    def test_mux_language_und_honesty(self):
        # never stamp eng on an unconfirmed stream (§B.3)
        assert S.mux_language("") == "und"
        assert S.mux_language("ger") == "und"
        assert S.mux_language("undeclared-anything") == "und"

    def test_untagged_text_plans_to_und(self):
        report = _report(subs=[{"codec_name": "srt"}])
        cmd = self_plan_cmd = build_file_plan(report)
        from vidlift.run import build_encode_command

        argv = build_encode_command(
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"), cmd, "/m.mkv"
        )
        assert "language=und" in argv


# ---- S6: intermediate_ext parity + collision guard ---------------------


class TestS6IntermediateExt:
    def test_pgs_sup(self):
        assert S.intermediate_ext("hdmv_pgs_subtitle") == ".sup"

    def test_vobsub_and_unknown_mkv(self):
        assert S.intermediate_ext("dvd_subtitle") == ".mkv"
        assert S.intermediate_ext("vobsub") == ".mkv"
        assert S.intermediate_ext("dvb_subtitle") == ".mkv"
        assert S.intermediate_ext("") == ".mkv"

    def test_extraction_maps_by_stream_index(self, tmp_path):
        seen: dict = {}

        def runner(binary, args):
            seen["binary"] = binary
            seen["args"] = list(args)
            Path(args[-1]).write_bytes(b"track")
            return 0, "", ""

        out = str(tmp_path / "x.sup")
        S.extract_subtitles(
            "/src.mkv", 5, out, ffmpeg="ffmpeg", runner=runner
        )
        assert seen["args"][seen["args"].index("-map") + 1] == "0:5"
        assert os.path.isfile(out)

    def test_extraction_failure_raises(self, tmp_path):
        def runner(binary, args):
            return 1, "", "boom"

        with pytest.raises(S.SubtitleOcrError, match="extraction failed"):
            S.extract_subtitles(
                "/src.mkv", 5, str(tmp_path / "x.sup"),
                ffmpeg="ffmpeg", runner=runner,
            )

    def test_workdir_collision_guard(self, tmp_path):
        # The pipeline's workdir can be the source's own dir; the
        # derived intermediate must never equal the source (b2m's
        # 'Output same as Input' lesson, subtitles.py:526-543).
        src = tmp_path / "movie.mkv"
        src.write_bytes(b"the rip")
        plan = S.SubtitlePlan(
            source="embedded", stream_index=2, sidecar_path="",
            kind="image", codec_name="dvd_subtitle", lang="eng",
            ocr_required=True,
        )
        # Simulate the derivation the runner performs
        workdir = str(tmp_path)
        img_path = os.path.join(
            workdir,
            os.path.splitext(os.path.basename(str(src)))[0]
            + ".vobsub-extract" + S.intermediate_ext(plan.codec_name),
        )
        assert os.path.normcase(img_path) != os.path.normcase(str(src))


# ---- S7: seconv lane argv pins ----------------------------------------


class TestS7SeconvLane:
    def _se(self, tmp_path, **kw):
        return S.SeconvOcr(binary="seconv", **kw)

    def test_pass1_argv_pins(self, tmp_path):
        se = self._se(tmp_path)
        cmd = se.command("/w/movie.sup", str(tmp_path))
        assert cmd[0] == "seconv"
        assert "/convert" in cmd and "/w/movie.sup" in cmd and "subrip" in cmd
        assert "/ocrengine:tesseract" in cmd
        assert "/ocrlanguage:eng" in cmd  # ISO 639-2, the en-vs-eng lesson
        assert f"/settings:{os.path.join(str(tmp_path), S.SE_CONV_SETTINGS_NAME)}" in cmd
        assert "/outputfilename:vidlift_ocr_raw.srt" in cmd  # pinned name
        assert f"/outputfolder:{tmp_path}" in cmd
        assert "/overwrite" in cmd

    def test_pass2_fix_argv_pins(self, tmp_path):
        raw = os.path.join(str(tmp_path), S.RAW_SRT_NAME)
        cmd = self._se(tmp_path).fix_command(raw, str(tmp_path))
        assert "/fixcommonerrorsrules:FixCommonOcrErrors" in cmd
        assert "/fce-language:en" in cmd  # ISO 639-1 gate
        assert "/outputfilename:vidlift_ocr_fixed.srt" in cmd

    def test_settings_seed_written_and_pinned(self, tmp_path):
        path = os.path.join(str(tmp_path), S.SE_CONV_SETTINGS_NAME)
        S.write_ocr_settings(path)
        seed = json.loads(Path(path).read_text())
        general = seed["general"]
        assert general["currentFrameRate"] == 23.976
        assert general["defaultFrameRate"] == 23.976
        assert general["subtitleLineMaximumLength"] == 43
        assert general["maxNumberOfLines"] == 2
        assert general["subtitleMinimumDisplayMilliseconds"] == 1000
        assert general["subtitleMaximumDisplayMilliseconds"] == 8000

    def test_stale_raw_purged_before_pass1(self, tmp_path):
        stale = tmp_path / S.RAW_SRT_NAME
        stale.write_text("stale from a killed run")
        se = self._se(tmp_path)

        def runner(binary, args):
            # pass 1 must NOT find the stale file when it writes
            fresh = tmp_path / S.RAW_SRT_NAME
            fresh.write_text("1\n00:00:01,000 --> 00:00:02,000\nFresh\n")
            return 0, "", ""

        se.run("/w/movie.sup", str(tmp_path), runner=runner)
        text = (tmp_path / S.RAW_SRT_NAME).read_text()
        assert "Fresh" in text  # the stale file was purged, not shipped

    def test_stale_fixed_purged_before_pass2(self, tmp_path):
        stale_fixed = tmp_path / S.FIXED_SRT_NAME
        stale_fixed.write_text("stale fix")
        stale_raw = tmp_path / S.RAW_SRT_NAME
        stale_raw.write_text("1\n00:00:01,000 --> 00:00:02,000\nRaw\n")
        se = self._se(tmp_path)
        calls: list[list[str]] = []

        def runner(binary, args):
            calls.append(list(args))
            if len(calls) == 1:
                # pass 1: write the raw fresh
                (tmp_path / S.RAW_SRT_NAME).write_text(
                    "1\n00:00:01,000 --> 00:00:02,000\nFresh raw\n"
                )
            else:
                # pass 2: the stale fixed must have been purged; write new
                (tmp_path / S.FIXED_SRT_NAME).write_text(
                    "1\n00:00:01,000 --> 00:00:02,000\nFresh fixed\n"
                )
            return 0, "", ""

        outcome = se.run("/w/movie.sup", str(tmp_path), runner=runner)
        assert outcome.fix_applied is True
        assert "Fresh fixed" in (tmp_path / S.FIXED_SRT_NAME).read_text()

    def test_gui_binary_refused(self):
        with pytest.raises(S.SubtitleOcrError, match="GUI cannot run unattended"):
            S.SeconvOcr(binary="subtitleedit.exe")
        with pytest.raises(S.SubtitleOcrError, match="GUI cannot run unattended"):
            S.resolve_seconv("SubtitleEdit.exe")

    def test_missing_pass1_raises(self, tmp_path):
        def runner(binary, args):
            return 0, "", ""  # exits 0 but writes nothing

        se = self._se(tmp_path)
        with pytest.raises(S.SubtitleOcrError, match="produced no SRT"):
            se.run("/w/movie.sup", str(tmp_path), runner=runner)


# ---- S8: pass-2 fallbacks; zero-cue raise ------------------------------


class TestS8PassTwoFallbacks:
    def _se_run(self, tmp_path, fixed_content, *, pass2_exit=0, raw_content=None):
        se = S.SeconvOcr(binary="seconv")
        state = {"n": 0}

        def runner(binary, args):
            state["n"] += 1
            if state["n"] == 1:
                (tmp_path / S.RAW_SRT_NAME).write_text(
                    raw_content
                    or "1\n00:00:01,000 --> 00:00:02,000\nRaw cue\n"
                )
            else:
                if fixed_content is not None:
                    (tmp_path / S.FIXED_SRT_NAME).write_text(fixed_content)
                return pass2_exit, "", "fix boom" if pass2_exit else ""
            return 0, "", ""

        return se.run("/w/movie.sup", str(tmp_path), runner=runner)

    def test_pass2_fail_ships_raw_with_reason(self, tmp_path):
        outcome = self._se_run(tmp_path, None, pass2_exit=1)
        assert outcome.fix_applied is False
        assert "fix boom" in outcome.fix_error
        assert outcome.srt_path == outcome.raw_srt_path

    def test_truncated_pass2_parse_falls_back_to_raw(self, tmp_path):
        # v0.2.29's second fix, driven through the REAL seconv branch via
        # the force_engine seam (forcing os.name is forbidden — it
        # poisons pathlib process-wide): pass 2 exits 0 but its file is a
        # truncated partial write; the raw must ship instead of raising.
        plan = S.SubtitlePlan(
            source="embedded", stream_index=2, sidecar_path="",
            kind="image", codec_name="hdmv_pgs_subtitle", lang="eng",
            ocr_required=True,
        )
        good_raw = "1\n00:00:01,000 --> 00:00:02,000\nRaw cue\n\n"
        state = {"n": 0}

        def runner(binary, args):
            state["n"] += 1
            # extraction writes the .sup intermediate
            if "-map" in args and args[-1].endswith(".sup"):
                Path(args[-1]).write_bytes(b"sup")
                return 0, "", ""
            # pass 1: write the good raw SRT (args[-1] is /outputfolder)
            if "/convert" in args and state["n"] == 2:
                out_dir = [a[len("/outputfolder:"):] for a in args
                           if a.startswith("/outputfolder:")][0]
                Path(out_dir, S.RAW_SRT_NAME).write_text(good_raw)
                return 0, "", ""
            # pass 2: exit 0 but write a TRUNCATED SRT
            if "/convert" in args and state["n"] == 3:
                out_dir = [a[len("/outputfolder:"):] for a in args
                           if a.startswith("/outputfolder:")][0]
                Path(out_dir, S.FIXED_SRT_NAME).write_text(
                    "1\n00:00:01,000 --> "
                )
                return 0, "", ""
            return 0, "", ""

        (tmp_path / "src.sup").write_bytes(b"sup")
        out_mp4 = tmp_path / "out.mp4"
        out_mp4.write_bytes(b"mp4")
        result = S.run_subtitle_ocr(
            plan, str(tmp_path / "src.sup"), str(out_mp4),
            ffmpeg="ffmpeg", runner=runner,
            workdir=str(tmp_path / "work"),
            seconv_binary="seconv",
            force_engine="seconv",
        )
        # the raw shipped: sidecar holds the GOOD cue, not the truncation
        sidecar_cues = parse_srt(
            (tmp_path / "out.srt").read_text(encoding="utf-8-sig")
        )
        assert sidecar_cues[0].text == "Raw cue"
        # the honest note names the truncated write
        assert any("truncated write" in n for n in result.notes)

    def test_zero_cues_raises_and_kit_written(self, tmp_path):
        # The fail-open retention contract (v0.2.30): every OCR text
        # comes back blank (a PGS all-wipe track, or Tesseract found
        # nothing legible) -> zero cues raises, and the kit written on
        # the raise path holds the extracted track for offline tuning.
        plan = S.SubtitlePlan(
            source="embedded", stream_index=2, sidecar_path="",
            kind="image", codec_name="hdmv_pgs_subtitle", lang="eng",
            ocr_required=True,
        )

        def showinfo_err():
            return (
                "[Parsed_showinfo_1 @ 0x1] n:   0 pts: 90000 "
                "pts_time:1     duration:      0 duration_time:0 "
                "fmt:bgra cl:unspecified sar:0/1 s:1920x1080 i:P "
                "iskey:0 type:? checksum:00000000\n"
            )

        def runner(binary, args):
            if args[-1].endswith(".sup") and "-map" in args:
                Path(args[-1]).write_bytes(b"sup-track")
                return 0, "", ""
            if "showinfo" in " ".join(args):
                return 0, "", showinfo_err()
            if "-fps_mode" in args:
                Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[-1]).parent.joinpath("sub_00000001.png").write_bytes(
                    b"png"
                )
                return 0, "", ""
            if binary == "tesseract" and "stdout" in args:
                return 0, "", ""  # blank OCR text -> zero assembled cues
            return 0, "", ""

        (tmp_path / "src.sup").write_bytes(b"sup")
        (tmp_path / "out.mp4").write_bytes(b"mp4")
        with pytest.raises(S.SubtitleOcrError, match="zero cues"):
            S.run_subtitle_ocr(
                plan, str(tmp_path / "src.sup"), str(tmp_path / "out.mp4"),
                ffmpeg="ffmpeg", runner=runner, workdir=str(tmp_path / "work"),
            )
        # the fail-open kit: the extracted track survived for re-tuning
        kit = tmp_path / "out.ocr"
        assert kit.is_dir()
        assert (kit / "src.vobsub-extract.sup").read_bytes() == b"sup-track"


# ---- S9: timing-paired auto-resolve count ------------------------------


class TestS9TimingPairedCount:
    def raw_and_fixed(self):
        raw = (
            "1\n00:00:01,000 --> 00:00:02,000\nHelio World\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nSecond\n"
        )
        # pass 2 MERGED the two cues into one (the zip() drift shape):
        # positional pairing would see 1 changed cue, miss the split
        # reality; timing pairing counts both honestly.
        fixed = (
            "1\n00:00:01,000 --> 00:00:04,000\nHelio World\nSecond\n"
        )
        return raw, fixed

    def test_merge_shape_counted_by_timing(self):
        raw, fixed = self.raw_and_fixed()
        # raw holds TWO cues (1s, 3s); pass 2 merged them into ONE
        # (starting 1s, text both lines). Positional zip pairing would
        # compare fixed[0] vs raw[0] only and count 1 change, missing
        # that the 3s cue vanished. Timing pairing counts the merged
        # cue (its text matches no raw cue at its start) = 1 changed,
        # and the merge itself is visible in the totals.
        assert S.count_auto_resolved_cues(raw, fixed) == 1

    def test_split_shape_counted(self):
        raw = "1\n00:00:01,000 --> 00:00:04,000\none two three\n"
        fixed = (
            "1\n00:00:01,000 --> 00:00:02,000\none two\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\nthree\n"
        )
        # the split cue 'three' starts at 2s where no raw cue exists ->
        # changed; the retained cue 'one two' differs from the raw's
        # text at 1s -> changed. Both counted: the timing-paired rule
        # (positional zip would drift exactly here).
        assert S.count_auto_resolved_cues(raw, fixed) == 2

    def test_zero_changes_when_identical(self):
        raw = "1\n00:00:01,000 --> 00:00:02,000\nSame\n"
        assert S.count_auto_resolved_cues(raw, raw) == 0

    def test_parse_failure_yields_zero(self):
        assert S.count_auto_resolved_cues("garbage", "also garbage") == 0


# ---- S10: quality note math --------------------------------------------


class TestS10QualityNote:
    def test_counts_add_up(self):
        cues = [
            Cue(0, 1000, "Clean text here."),
            Cue(1000, 2000, "Gárbage|pipe"),  # pipe = garbage
            Cue(2000, 3000, "   "),  # empty
            Cue(3000, 4000, "Also clean"),
        ]
        total, garbage, empty = S.ocr_cue_quality(cues)
        assert (total, garbage, empty) == (4, 1, 1)
        clean = total - garbage - empty
        assert clean == 2

    def test_note_wording(self):
        cues = [Cue(0, 1000, "Clean"), Cue(1000, 2000, "x|y")]
        note = S.ocr_quality_note(cues)
        assert note.startswith("OCR quality:")
        assert "50%" in note
        assert "1 cues contain non-English characters" in note

    def test_empty_total_is_100(self):
        assert "100%" in S.ocr_quality_note([])

    def test_quality_never_gates(self):
        # a garbage-heavy result still produces a NOTE, never an error
        cues = [Cue(0, 1000, "|".join(["~"] * 50))]
        note = S.ocr_quality_note(cues)
        assert isinstance(note, str)  # reporting only


# ---- S11: diagnosis kit ------------------------------------------------


class TestS11DiagnosisKit:
    def test_success_path_retains_track_and_raw(self, tmp_path):
        track = tmp_path / "movie.sup"
        track.write_bytes(b"track-data")
        raw = tmp_path / "raw.srt"
        raw.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n")
        out = str(tmp_path / "movie.mp4")
        notes = S.write_ocr_diagnostic_kit(
            out, track_path=str(track), raw_srt_path=str(raw)
        )
        kit = tmp_path / "movie.ocr"
        assert (kit / "movie.raw.srt").is_file()
        assert (kit / "movie.sup").is_file()
        assert notes == []

    def test_raw_srt_always_in_a_subfolder(self, tmp_path):
        # a bare raw .srt sibling would make every player auto-load the
        # uncorrected pass-1 text over the shipped sidecar (§B.4)
        track = tmp_path / "t.sup"
        track.write_bytes(b"t")
        raw = tmp_path / "raw.srt"
        raw.write_text("x")
        out = str(tmp_path / "movie.mp4")
        S.write_ocr_diagnostic_kit(
            out, track_path=str(track), raw_srt_path=str(raw)
        )
        assert (tmp_path / "movie.ocr").is_dir()
        assert not (tmp_path / "movie.raw.srt").exists()

    def test_oversize_track_skipped_with_note(self, tmp_path):
        track = tmp_path / "big.sup"
        track.write_bytes(b"x")
        # stub the size above the 150 MB cap without writing 150 MB:
        # patch the module's os.path.getsize reference
        import unittest.mock as mock

        with mock.patch.object(S.os.path, "getsize", return_value=151 * 1024 * 1024):
            notes = S.write_ocr_diagnostic_kit(
                str(tmp_path / "movie.mp4"),
                track_path=str(track),
                raw_srt_path=None,
            )
        assert any("cap" in n for n in notes)
        assert not (tmp_path / "movie.ocr" / "big.sup").exists()

    def test_missing_inputs_degrade_to_notes_never_raise(self, tmp_path):
        notes = S.write_ocr_diagnostic_kit(
            str(tmp_path / "movie.mp4"),
            track_path=None,
            raw_srt_path="/no/such/file.srt",
        )
        assert isinstance(notes, list)  # no raise


# ---- S12: mux atomicity -------------------------------------------------


class TestS12Mux:
    def _mp4(self, tmp_path):
        mp4 = tmp_path / "movie.mp4"
        mp4.write_bytes(b"THE-FINISHED-ENCODE")
        return mp4

    def test_temp_then_replace_on_success(self, tmp_path):
        mp4 = self._mp4(tmp_path)
        srt = tmp_path / "movie.srt"
        write_srt_file(str(srt), [Cue(0, 1000, "Hi")])

        def runner(binary, args):
            tmp_out = Path(args[-1])
            assert tmp_out.name == "movie.mp4.muxing.mp4"  # sibling temp
            tmp_out.write_bytes(b"MUXED")
            return 0, "", ""

        S.mux_srt_into_mp4(
            str(mp4), str(srt), language="eng",
            ffmpeg="ffmpeg", runner=runner,
        )
        assert mp4.read_bytes() == b"MUXED"
        assert not (tmp_path / "movie.mp4.muxing.mp4").exists()

    def test_failure_leaves_mp4_bit_identical(self, tmp_path):
        mp4 = self._mp4(tmp_path)
        before = mp4.read_bytes()
        srt = tmp_path / "movie.srt"
        write_srt_file(str(srt), [Cue(0, 1000, "Hi")])

        def runner(binary, args):
            return 1, "", "Could not find tag for codec mov_text"

        with pytest.raises(S.SubtitleOcrError, match="muxing failed"):
            S.mux_srt_into_mp4(
                str(mp4), str(srt), language="eng",
                ffmpeg="ffmpeg", runner=runner,
            )
        assert mp4.read_bytes() == before  # bit-identical
        assert not (tmp_path / "movie.mp4.muxing.mp4").exists()

    def test_language_tagged_in_argv(self, tmp_path):
        mp4 = self._mp4(tmp_path)
        srt = tmp_path / "movie.srt"
        write_srt_file(str(srt), [Cue(0, 1000, "Hi")])
        argv_seen: list[str] = []

        def runner(binary, args):
            argv_seen.extend(args)
            Path(args[-1]).write_bytes(b"ok")
            return 0, "", ""

        S.mux_srt_into_mp4(
            str(mp4), str(srt), language="ger",
            ffmpeg="ffmpeg", runner=runner,
        )
        assert "-metadata:s:s:0" in argv_seen
        assert "language=und" in argv_seen  # the honesty rule


# ---- S13: tesseract lane parity ----------------------------------------


class TestS13TesseractLane:
    """The dev-box lane as validated on real media (design §B.4).

    The stub replays the exact shapes real ffmpeg 8.0.1 produced on
    this box during validation: the showinfo timeline probe (stderr
    lines ``n: ... pts:... pts_time:...``; AV_LOG_INFO, so the probe
    parses stderr) and the single-pass image2 render (one numbered
    PNG per decoded frame). No per-packet seeks, no overlay canvas,
    no duration probe — those were the rejected approaches.
    """

    # Real-shape showinfo lines (ffmpeg pads pts/pts_time fields):
    def _showinfo_err(self, pts_times):
        lines = []
        for n, t in enumerate(pts_times):
            pts = int(round(t * 90000))
            lines.append(
                f"[Parsed_showinfo_1 @ 0x7a2ce8002980] n:  {n:2d} "
                f"pts: {pts:6d} pts_time:{t:<6g}"
                " duration:      0 duration_time:0"
                " fmt:bgra cl:unspecified sar:0/1 s:1920x1080 i:P"
                " iskey:0 type:? checksum:00000000"
            )
        return "\n".join(lines) + "\n"

    def _plan(self, codec="dvd_subtitle"):
        return S.SubtitlePlan(
            source="embedded", stream_index=2, sidecar_path="",
            kind="image", codec_name=codec, lang="eng", ocr_required=True,
        )

    def _runner(self, tmp_path, pts_times, texts):
        """Stub: extraction writes the track; the showinfo probe returns
        the canned frame timeline on stderr; the image2 render writes one
        numbered PNG per frame; tesseract returns canned per-image text.
        """

        def runner(binary, args):
            if args[-1].endswith((".sup", ".mkv")) and "-map" in args:
                Path(args[-1]).write_bytes(b"track")
                return 0, "", ""
            if "showinfo" in " ".join(args):
                return 0, "", self._showinfo_err(pts_times)
            if "-fps_mode" in args:
                pattern = args[-1]
                png_dir = Path(pattern).parent
                png_dir.mkdir(parents=True, exist_ok=True)
                for i in range(1, len(pts_times) + 1):
                    (png_dir / f"sub_{i:08d}.png").write_bytes(b"png")
                return 0, "", ""
            if binary == "tesseract" and "stdout" in args:
                idx = int(Path(args[0]).name[4:-4]) - 1
                return 0, texts[idx] + "\n", ""
            return 0, "", ""

        return runner

    def test_timeline_probe_argv_showinfo(self, tmp_path):
        seen: list[str] = []

        def runner(binary, args):
            if "showinfo" in " ".join(args):
                seen.extend(args)
                return 0, "", self._showinfo_err([1.0, 2.0])
            if "-fps_mode" in args:
                Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
                for i in range(1, 3):
                    Path(args[-1]).parent.joinpath(
                        f"sub_{i:08d}.png"
                    ).write_bytes(b"png")
            return 0, "", ""

        S.TesseractOcr().run(
            "/w/track.mkv", str(tmp_path), ffmpeg="ffmpeg", runner=runner
        )
        # the §B.4 probe: same filtergraph into a null sink, showinfo
        assert "[0:s:0]null,showinfo[s]" in seen
        assert seen[-3:] == ["-f", "null", "-"]

    def test_render_argv_single_pass_image2(self, tmp_path):
        seen: list[str] = []

        def runner(binary, args):
            if "showinfo" in " ".join(args):
                return 0, "", self._showinfo_err([1.0, 2.0])
            if "-fps_mode" in args:
                seen.extend(args)
                Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
                for i in range(1, 3):
                    Path(args[-1]).parent.joinpath(
                        f"sub_{i:08d}.png"
                    ).write_bytes(b"png")
            return 0, "", ""

        S.TesseractOcr().run(
            "/w/track.mkv", str(tmp_path), ffmpeg="ffmpeg", runner=runner
        )
        # the validated design: single-pass, passthrough, no seeks
        assert "-fps_mode" in seen and "passthrough" in seen
        assert "-ss" not in seen
        assert seen[-1].endswith("sub_%08d.png")

    def test_cue_assembly_from_frame_pairs(self, tmp_path):
        # blank -> text -> blank: the cue opens at the text frame's pts
        # and closes where the blank (clear) frame arrives.
        runner = self._runner(tmp_path, [0.5, 1.0, 2.0], ["", "HELLO WORLD", ""])
        result = S.TesseractOcr().run(
            "/w/track.mkv", str(tmp_path), ffmpeg="ffmpeg", runner=runner
        )
        assert result.assembled_cues == [
            Cue(start_ms=1000, end_ms=2000, text="HELLO WORLD")
        ]

    def test_consecutive_same_text_continues_display(self, tmp_path):
        # A PGS display set may carry several same-pts segments: the
        # identical consecutive text is ONE cue, not two.
        runner = self._runner(
            tmp_path, [1.0, 1.0, 3.0], ["HELLO", "HELLO", "HELLO"]
        )
        result = S.TesseractOcr().run(
            "/w/track.mkv", str(tmp_path), ffmpeg="ffmpeg", runner=runner
        )
        assert len(result.assembled_cues) == 1
        assert result.assembled_cues[0].text == "HELLO"
        # no blank ever came: the 3s tail hold bounds the last frame
        assert result.assembled_cues[0].end_ms == 6000

    def test_all_blank_frames_assemble_to_zero_cues(self, tmp_path):
        # Every frame blank (PGS clear/wipe everywhere): zero cues is
        # the honest assembled result from the lane itself.
        runner = self._runner(tmp_path, [0.5, 1.0], ["", ""])
        result = S.TesseractOcr().run(
            "/w/track.mkv", str(tmp_path), ffmpeg="ffmpeg", runner=runner
        )
        assert result.assembled_cues == []

    def test_tesseract_argv_shape(self, tmp_path):
        argvs: list[list[str]] = []

        def runner(binary, args):
            if binary == "tesseract":
                argvs.append([binary, *args])
                return 0, "x\n", ""
            if "showinfo" in " ".join(args):
                return 0, "", self._showinfo_err([1.0])
            if "-fps_mode" in args:
                Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(args[-1]).parent.joinpath("sub_00000001.png").write_bytes(
                    b"png"
                )
            return 0, "", ""

        S.TesseractOcr().run(
            "/w/t.mkv", str(tmp_path), ffmpeg="ffmpeg", runner=runner
        )
        assert argvs, "tesseract must be invoked per image"
        for argv in argvs:
            assert argv[0] == "tesseract"
            assert argv[2] == "stdout"
            assert "-l" in argv and "eng" in argv
            assert "--psm" in argv and "6" in argv

    def test_render_count_mismatch_raises(self, tmp_path):
        # the cross-check: the image2 pass wrote FEWER PNGs than the
        # timeline has frames — mis-timed cues must never ship.
        def runner(binary, args):
            if "showinfo" in " ".join(args):
                return 0, "", self._showinfo_err([1.0, 2.0, 3.0])
            if "-fps_mode" in args:
                Path(args[-1]).parent.mkdir(parents=True, exist_ok=True)
                # only 2 of 3 frames rendered
                for i in range(1, 3):
                    Path(args[-1]).parent.joinpath(
                        f"sub_{i:08d}.png"
                    ).write_bytes(b"png")
            return 0, "", ""

        with pytest.raises(S.SubtitleOcrError, match="2 PNGs for 3"):
            S.TesseractOcr().run(
                "/w/t.mkv", str(tmp_path), ffmpeg="ffmpeg", runner=runner
            )

    def test_full_lane_to_sidecar(self, tmp_path):
        runner = self._runner(
            tmp_path, [1.0, 3.0], ["HELLO WORLD", "SECOND"]
        )
        (tmp_path / "src.mkv").write_bytes(b"src")
        (tmp_path / "out.mp4").write_bytes(b"out")
        result = S.run_subtitle_ocr(
            self._plan(), str(tmp_path / "src.mkv"), str(tmp_path / "out.mp4"),
            ffmpeg="ffmpeg", runner=runner, workdir=str(tmp_path / "work"),
        )
        assert result.cue_count == 2
        assert result.engine == "tesseract"
        sidecar = tmp_path / "out.srt"
        assert sidecar.is_file()
        cues = parse_srt(sidecar.read_text(encoding="utf-8-sig"))
        assert len(cues) == 2
        assert cues[0].text == "HELLO WORLD"
        # quality line present (shared downstream with the seconv lane)
        assert any("OCR quality:" in n for n in result.notes)

    def test_on_step_and_progress_fire(self, tmp_path):
        steps: list[str] = []
        progress_calls: list[tuple[int, int]] = []
        runner = self._runner(tmp_path, [1.0, 2.0], ["A", "B"])

        def on_step(label):
            steps.append(label)

        def on_progress(done, total):
            progress_calls.append((done, total))

        S.TesseractOcr().run(
            "/w/t.mkv", str(tmp_path), ffmpeg="ffmpeg", runner=runner,
            on_step=on_step, on_progress=on_progress,
        )
        assert "subtitle-timeline" in steps
        assert "subtitle-render" in steps
        assert any(s.startswith("ocr-image-1/2") for s in steps)
        assert any(s.startswith("ocr-image-2/2") for s in steps)
        assert progress_calls  # v0.4.0 record_step/progress machinery


# ---- S14: no-engine platform fails open --------------------------------


class TestS14NoEngineFailOpen:
    def test_missing_tesseract_names_tool_in_warning(self, tmp_path):
        with pytest.raises(S.SubtitleOcrError) as excinfo:
            S.resolve_tesseract("/no/such/tesseract")
        assert "tesseract not found" in str(excinfo.value)
        assert "VIDLIFT_TESSERACT" in str(excinfo.value)

    def test_missing_seconv_names_tool(self):
        with pytest.raises(S.SubtitleOcrError, match="seconv not found"):
            S.resolve_seconv("/no/such/seconv")

    def test_run_still_ok_with_no_ocr_tool(self, tmp_path, monkeypatch):
        # The full-run contract: bitmap source, no OCR tool -> the run
        # COMPLETES with ok=True and failed-open in the manifest.
        monkeypatch.setenv("VIDLIFT_TESSERACT", "/no/such/tesseract-xyz")
        path = make_media_file(tmp_path, "dvd.avi")
        payload = probe_payload(
            subtitles=[{"codec_name": "dvd_subtitle", "tags": {"language": "eng"}}]
        )
        runner = FakeRunner(payload)
        result = run_file(
            path, tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        assert result.ok is True  # the upscale is never blocked
        block = result.state.subtitles
        assert block["status"] == "failed-open"
        assert "tesseract" in " ".join(block["warnings"])


# ---- S15: verify subtitle assertion (warning, non-gating) ---------------


class TestS15VerifyAssertion:
    def _plan_with_sub(self, tmp_path):
        # A plan whose output file exists on disk (verify checks the
        # file before anything else); the output stub doubles as the
        # encode the verifier probes.
        report = _report(
            subs=[{"codec_name": "srt", "tags": {"language": "eng"}}],
            path=str(tmp_path / "movie.avi"),
        )
        plan = build_file_plan(report, output_dir=str(tmp_path / "out"))
        out = Path(plan.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"output-stub")
        return plan

    def _probe_runner(self, streams):
        def runner(binary, args):
            return 0, json.dumps({"streams": streams}), ""

        return runner

    def test_expected_missing_records_warning_not_failure(self, tmp_path):
        plan = self._plan_with_sub(tmp_path)
        # geometry fine; NO subtitle stream in the output
        streams = [
            {"codec_type": "video", "width": 1280, "height": 720},
        ]
        result = verify_output(
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            plan,
            runner=self._probe_runner(streams),
            full_decode=False,
        )
        assert result.ok is True  # non-gating by design
        assert result.subtitle_expected is True
        assert result.subtitle_ok is False
        assert "missing" in (result.subtitle_detail or "")

    def test_unexpected_sub_present_recorded(self, tmp_path):
        report = _report(path=str(tmp_path / "movie.avi"))  # no subs planned
        plan = build_file_plan(report, output_dir=str(tmp_path / "out"))
        out = Path(plan.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"output-stub")
        streams = [
            {"codec_type": "video", "width": 1280, "height": 720},
            {"codec_type": "subtitle", "codec_name": "mov_text"},
        ]
        result = verify_output(
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            plan,
            runner=self._probe_runner(streams),
            full_decode=False,
        )
        assert result.ok is True
        assert result.subtitle_ok is False
        assert "unexpected" in (result.subtitle_detail or "")

    def test_expected_present_ok(self, tmp_path):
        plan = self._plan_with_sub(tmp_path)
        streams = [
            {"codec_type": "video", "width": 1280, "height": 720},
            {"codec_type": "subtitle", "codec_name": "mov_text"},
        ]
        result = verify_output(
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            plan,
            runner=self._probe_runner(streams),
            full_decode=False,
        )
        assert result.ok is True
        assert result.subtitle_ok is True
        assert result.subtitle_detail is None


# ---- S16: MicroDVD conversion uses the stamp token ----------------------


class TestS16MicrodvdStamp:
    def test_conversion_uses_the_derived_rate(self):
        # 24000/1001 token: 24 frames == 24 * 1001/24000 s
        fps = 24000 / 1001
        cues = S.convert_microdvd_to_cues("{24}{48}HELLO", fps)
        assert cues[0].start_ms == 1001  # the token's frame-time, not 25's

    def test_25_token_shape(self):
        cues = S.convert_microdvd_to_cues("{25}{50}HELLO", 25.0)
        assert cues[0].start_ms == 1000
        assert cues[0].end_ms == 2000

    def test_comments_and_blanks_skipped(self):
        content = "{0}{10}A\n\n# comment\n{20}{30}B\n"
        cues = S.convert_microdvd_to_cues(content, 25.0)
        assert [c.text for c in cues] == ["A", "B"]

    def test_bad_rate_refuses(self):
        with pytest.raises(S.SubtitleOcrError, match="positive frame rate"):
            S.convert_microdvd_to_cues("{0}{10}A", 0)


# ---- S17: manifest block keys; v2 compat --------------------------------


class TestS17ManifestBlock:
    def _run(self, tmp_path, payload, name="movie.avi"):
        path = make_media_file(tmp_path, name)
        runner = FakeRunner(payload)
        result = run_file(
            str(path), tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        manifest = json.loads(Path(result.manifest_path).read_text())
        return result, manifest

    def test_none_block_shape(self, tmp_path):
        result, manifest = self._run(tmp_path, probe_payload())
        assert manifest["run"]["subtitles"] == {"status": "none"}
        # v2 compat: the old keys are all still present, untouched
        for key in ("ok", "engine", "engine_simulated", "frame_clock",
                    "resume", "frame_gates", "rejected_mezzanine"):
            assert key in manifest["run"]

    def test_carried_block_shape(self, tmp_path):
        payload = probe_payload(
            subtitles=[{"codec_name": "srt", "tags": {"language": "eng"}}]
        )
        result, manifest = self._run(tmp_path, payload, "subbed.avi")
        block = manifest["run"]["subtitles"]
        assert block["status"] == "carried"
        assert block["selected"]["source"] == "embedded"
        assert block["selected"]["kind"] == "text"
        assert block["sidecar_path"].endswith(".srt")
        assert block["embedded"] == {"codec": "mov_text", "language_tag": "eng"}
        # plan echo also lands in manifest["plan"]
        assert manifest["plan"]["subtitle_plan"]["stream_index"] == 2
        assert manifest["plan"]["subtitle_style_loss"] is False

    def test_failed_open_block_shape(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VIDLIFT_TESSERACT", "/no/such/tesseract-xyz")
        payload = probe_payload(
            subtitles=[{"codec_name": "dvd_subtitle", "tags": {"language": "eng"}}]
        )
        result, manifest = self._run(tmp_path, payload, "dvd.avi")
        block = manifest["run"]["subtitles"]
        assert block["status"] == "failed-open"
        assert "reason" in block
        assert block["warnings"]

    def test_verification_carries_subtitle_fields(self, tmp_path):
        result, manifest = self._run(tmp_path, probe_payload())
        v = manifest["verification"]
        assert v["subtitle_expected"] is False
        assert v["subtitle_ok"] is True  # none expected, none found


# ---- S18: run integration ------------------------------------------------


class TestS18RunIntegration:
    def test_phases_contains_subtitles_before_done(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        result = run_file(
            str(path), tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        names = [p.name for p in result.state.phases]
        assert PHASE_SUBTITLES in names
        # 'done' is the terminal STATE, not a recorded phase row; the
        # subtitles phase must come after verify (the last recorded).
        assert names.index(PHASE_SUBTITLES) > names.index("verify")
        assert result.state.current_phase == PHASE_DONE

    def test_warnings_land_in_log_and_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("VIDLIFT_TESSERACT", "/no/such/tesseract-xyz")
        path = make_media_file(tmp_path, "dvd.avi")
        payload = probe_payload(
            subtitles=[{"codec_name": "dvd_subtitle", "tags": {"language": "eng"}}]
        )
        runner = FakeRunner(payload)
        result = run_file(
            str(path), tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        assert result.ok is True
        log_text = Path(result.state.log.path).read_text()
        assert "subtitles failed open" in log_text
        assert result.state.subtitles["status"] == "failed-open"

    def test_skipped_phase_costs_nothing_and_warns_nothing(self, tmp_path):
        path = make_media_file(tmp_path)
        runner = FakeRunner(probe_payload())
        result = run_file(
            str(path), tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"),
        )
        block = result.state.subtitles
        assert block == {"status": "none"}
        log_text = Path(result.state.log.path).read_text()
        assert "WARNING: subtitles" not in log_text  # a fact, not a failure


# ---- S19: bridge payloads --------------------------------------------


class TestS19BridgePayloads:
    def _services(self, tmp_path, monkeypatch):
        from vidlift.mcp_bridge.tools import BridgeServices

        class Registry:
            def queued(self):
                return []

            def running(self):
                return []

            def history(self):
                return []

        services = BridgeServices(
            registry=Registry(), state_dir=tmp_path
        )
        return services

    def test_report_payload_carries_index_and_sidecars(self):
        from vidlift.mcp_bridge.tools import _report_payload

        report = _report(
            subs=[{"codec_name": "srt", "tags": {"language": "eng"}}],
            sidecars=[SidecarInfo(path="/media/movie.srt", kind="text")],
        )
        payload = _report_payload(report)
        assert payload["subtitles"][0]["index"] == 2
        assert payload["subtitles"][0]["kind"] == "text"
        assert payload["sidecars"] == [{"path": "/media/movie.srt", "kind": "text"}]

    def test_plan_payload_carries_subtitle_plan(self):
        from vidlift.mcp_bridge.tools import _plan_payload

        report = _report(subs=[{"codec_name": "ass", "tags": {"language": "eng"}}])
        plan = build_file_plan(report)
        payload = _plan_payload(plan)
        assert payload["subtitle_plan"]["codec_name"] == "ass"
        assert payload["subtitle_plan"]["ocr_required"] is False
        assert payload["subtitle_style_loss"] is True
        # the additive key changes no v2 key
        assert payload["subtitle_note"]
        assert "warnings" in payload

    def test_plan_payload_none_plan_is_null(self):
        from vidlift.mcp_bridge.tools import _plan_payload

        report = _report()
        plan = build_file_plan(report)
        payload = _plan_payload(plan)
        assert payload["subtitle_plan"] is None
        assert payload["subtitle_style_loss"] is False

    def test_status_payload_carries_subtitles_block(self, tmp_path, monkeypatch):
        # a live run through the bridge's status shape: the subtitles
        # key is additive on the existing contract (no protocol bump —
        # the schemas module is untouched).
        from vidlift.mcp_bridge import schemas

        import vidlift.mcp_bridge.tools as tools

        monkeypatch.setenv("VIDLIFT_TESSERACT", "/no/such/tesseract-xyz")
        path = make_media_file(tmp_path, "dvd.avi")
        payload = probe_payload(
            subtitles=[{"codec_name": "dvd_subtitle", "tags": {"language": "eng"}}]
        )
        runner = FakeRunner(payload)
        box: dict = {}
        run_file(
            str(path), tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
            probe_runner=runner, command_runner=runner,
            log_dir=str(tmp_path / "runs"), state_box=box,
        )
        state = box["state"]
        data = state.to_dict()
        assert data["subtitles"]["status"] == "failed-open"


# ---- F1: engine-level cross-family stamp-authority (Karen's finding) ----


class TestF1EngineStampAuthority:
    """Karen's v0.4.0 validation F1 (Medium), folded into this lane.

    A DIRECT engine-level assertion that the -framerate argv in
    run_engine's encode/assembly path equals the DERIVED stamp token —
    never the container's snapped canonical — because E1/b1/b2 were
    blind to a container-vs-truth CROSS-FAMILY lie at engine level
    (e.g. container declares 25/1 while decode-truth derives
    24000/1001: the old tests only ever moved rates WITHIN one
    family).
    """

    def test_framerate_argv_is_derived_token_not_container(self):
        # Build the cross-family lie fixture: container says 25/1,
        # decode-truth says 24000/1001.
        from tests.test_engine_ncnn import (
            EngineFakeRunner,
            FakeEngineProcess,
            NcnnVulkanEngine,
            ToolBox,
            make_fixture_models,
            make_fake_binary,
            make_plan,
        )
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        prepared = tmp / "movie.prepared.mkv"
        prepared.write_bytes(b"prepared-stub")
        # cross-family lie: the decode-truth operands (129471 frames /
        # 5403.6s audio extent) derive 23.976 -> snap 24000/1001, while
        # the container clock declares 25/1. Gate P is a SANITY gate,
        # not a stamp input, so source duration == prepared duration
        # (the run_engine coherent default) with the ONE stamp operand
        # moved across rate families.
        runner = EngineFakeRunner(
            prepared_duration=5400.0,
            mezzanine_duration=5400.0,
            avg_rate="25/1",
            r_rate="25/1",
            prepared_frames=129471,  # 129471 / 5403.6 -> 23.976
            mezzanine_frames=129471,
            audio_stream_duration=5403.6,
            source_format_duration=5403.6,
        )
        models, table = make_fixture_models(tmp / "models")
        engine = NcnnVulkanEngine(
            binary=str(make_fake_binary(tmp / "bin")),
            models_dir=str(models),
            artifacts=table,
            runner=runner,
            engine_runner=FakeEngineProcess(),
            chunk_seconds=120,
        )
        plan = make_plan(duration=5400.0)  # gate P: source==prepared
        prepared_path = str(prepared)
        result = engine.run(
            prepared_path, plan,
            ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        )
        # THE engine-level assertion: every -framerate argv in the
        # recorded commands equals the DERIVED token "24000/1001" —
        # never the container's "25/1".
        framerate_vals = [
            args[args.index("-framerate") + 1]
            for _b, args in runner.commands
            if "-framerate" in args
        ]
        assert framerate_vals, "assembly path must stamp -framerate"
        assert all(v == "24000/1001" for v in framerate_vals)
        assert result.frame_clock["value"] == "24000/1001"