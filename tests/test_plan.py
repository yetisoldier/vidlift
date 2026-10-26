from __future__ import annotations

import json

import pytest

from vidlift.plan import (
    BatchPlan,
    PlanError,
    build_batch_plan,
    build_file_plan,
    estimate_video_bitrate_kbps,
    output_geometry,
)
from vidlift.probe import classify, parse_probe


def ffprobe_json(
    *,
    format_name: str,
    duration: str = "5400.0",
    video: dict | None = None,
    audio: list[dict] | None = None,
    subtitles: list[dict] | None = None,
) -> str:
    def stream(index: int, codec_type: str, **fields: object) -> dict:
        data: dict = {"index": index, "codec_type": codec_type}
        data.update(fields)
        return data

    streams: list[dict] = []
    if video is not None:
        streams.append(stream(0, "video", **video))
    for offset, a in enumerate(audio or [], start=1):
        streams.append(stream(offset, "audio", **a))
    for offset, s in enumerate(subtitles or [], start=1 + len(audio or [])):
        streams.append(stream(offset, "subtitle", **s))
    return json.dumps(
        {
            "streams": streams,
            "format": {"format_name": format_name, "duration": duration},
        }
    )


def report_for(
    *,
    path: str = "movie.avi",
    width: int = 640,
    height: int = 272,
    sar: str = "1:1",
    duration: str = "5400.0",
    codec: str = "mpeg4",
    container: str = "avi",
    audio_channels: int = 2,
    field_order: str = "progressive",
):
    return classify(
        parse_probe(
            path,
            ffprobe_json(
                format_name=container,
                duration=duration,
                video={
                    "codec_name": codec,
                    "width": width,
                    "height": height,
                    "sample_aspect_ratio": sar,
                    "field_order": field_order,
                    "r_frame_rate": "25/1",
                    "avg_frame_rate": "25/1",
                },
                audio=[{"codec_name": "aac", "channels": audio_channels}],
                subtitles=[{"codec_name": "srt", "tags": {"language": "eng"}}],
            ),
        )
    )


def test_output_geometry_preserves_widescreen_shape_under_1280():
    report = report_for(width=640, height=360)
    assert output_geometry(report) == (1280, 720)


def test_output_geometry_preserves_four_three_shape():
    report = report_for(
        path="dvd.mkv",
        width=720,
        height=576,
        sar="16:15",
        codec="mpeg2video",
        container="matroska,webm",
    )
    assert output_geometry(report) == (960, 720)


def test_file_plan_names_output_and_estimates_size():
    plan = build_file_plan(report_for(path="/media/Movie.avi"), gb_per_hour=1.5)
    assert plan.process_verdict == "ready"
    assert plan.output_path == "/media/Movie.720p.hevc.mp4"
    assert plan.estimated_output_bytes == 2_250_000_000
    assert plan.audio.label == "stereo"
    assert "mov_text" in plan.subtitle_note


def test_file_plan_uses_output_dir():
    plan = build_file_plan(
        report_for(path="/media/Movie.avi"), output_dir="/output"
    )
    assert plan.output_path == "/output/Movie.720p.hevc.mp4"


def test_hd_file_is_planned_as_skip_verdict_not_ready():
    plan = build_file_plan(
        report_for(
            path="hd.mp4",
            width=1920,
            height=1080,
            codec="h264",
            container="mov,mp4,m4a,3gp,3g2,mj2",
        )
    )
    assert plan.process_verdict == "already-hd"


def test_batch_plan_counts_ready_files_and_total_size():
    batch = build_batch_plan(
        [
            report_for(path="a.avi", duration="3600.0"),
            report_for(path="b.avi", duration="1800.0"),
        ]
    )
    assert isinstance(batch, BatchPlan)
    assert batch.runnable_count == 2
    assert batch.total_estimated_bytes == 2_250_000_000


def test_estimate_bitrate_accounts_for_audio():
    stereo = estimate_video_bitrate_kbps(target_gb_per_hour=1.5, audio_channels=2)
    surround = estimate_video_bitrate_kbps(target_gb_per_hour=1.5, audio_channels=6)
    assert surround < stereo


def test_invalid_target_raises():
    with pytest.raises(PlanError):
        output_geometry(report_for(), target="1080p")


# ---- v0.4.8 b22: plan expectations stay profile-agnostic (design D6) -------------


def test_b22_expectation_unchanged_for_dvd_rip():
    """b22 (design D6): the DVD-MPEG2-rip expectation string is what the
    brief pins; plan.py grew no profile awareness, so the string is the
    same for both profiles by construction (build_file_plan takes no
    profile argument)."""
    from vidlift.plan import build_file_plan
    from vidlift.probe import SOURCE_DVD_MPEG2_RIP

    report = report_for(
        path="dvd.vob",
        width=640,
        height=480,
        codec="mpeg2video",
        container="mpeg",
        duration="4.0",
    )
    assert report.source_class == SOURCE_DVD_MPEG2_RIP
    plan = build_file_plan(report)
    assert (
        plan.expectation
        == "best 720p that source can give; good, not transformative"
    )
    # additive: build_file_plan accepts no profile kwarg at all —
    # inspect its signature for the D6 claim
    import inspect

    params = inspect.signature(build_file_plan).parameters
    assert "profile" not in params
    assert "upscale_profile" not in params


def test_b22_expectation_strings_stable_across_all_source_classes():
    import inspect

    from vidlift import plan as plan_mod

    # the four pinned strings, verbatim (any profile-aware drift in
    # plan.py breaks this test)
    report_avi = report_for(path="m.avi", codec="mpeg4", container="avi")
    assert (
        plan_mod._expectation(report_avi)
        == "cleaner on a TV, but detail missing from the rip cannot be recovered"
    )
    report_dvd = report_for(
        path="d.vob", codec="mpeg2video", container="mpeg"
    )
    assert (
        plan_mod._expectation(report_dvd)
        == "best 720p that source can give; good, not transformative"
    )
    report_h264 = report_for(
        path="s.mp4", codec="h264", container="mov,mp4,m4a,3gp,3g2,mj2"
    )
    assert (
        plan_mod._expectation(report_h264)
        == "likely modest improvement; inspect the sample before a full run"
    )
