"""Tests for the v0.1 probe/classify core.

All tests run through the runner seam with canned ffprobe JSON; no
real ffprobe or video files are needed. The probe checks that the
file exists before running, so each test touches an empty placeholder
whose content is irrelevant (the runner never actually reads it).
"""

from __future__ import annotations

import json

import pytest

from vidlift.probe import (
    FileReport,
    ProbeError,
    classify,
    displayed_size,
    parse_probe,
    probe_and_classify,
    probe_file,
)


def make_media_file(tmp_path, name: str) -> str:
    """Touch an empty placeholder file the fake runner will 'probe'."""
    path = tmp_path / name
    path.write_text("")
    return str(path)


def ffprobe_json(
    *,
    format_name: str,
    duration: str = "5400.0",
    video: dict | None = None,
    audio: list[dict] | None = None,
    subtitles: list[dict] | None = None,
) -> str:
    """Build minimal but realistic ffprobe JSON for one file."""

    def stream(index: int, codec_type: str, **fields: object) -> dict:
        stream: dict = {"index": index, "codec_type": codec_type}
        stream.update(fields)
        return stream

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


def fake_runner(payload: str, returncode: int = 0, stderr: str = ""):
    """Runner seam: replay a fixed ffprobe payload for any call."""

    def run(binary: str, args: list[str]) -> tuple[int, str, str]:
        return returncode, payload, stderr

    return run


def avi_xvid_payload(**overrides: object) -> str:
    """The expected common case: a ~700 MB XviD rip in an AVI."""
    video = {
        "codec_name": "mpeg4",
        "width": 640,
        "height": 272,
        "sample_aspect_ratio": "1:1",
        "display_aspect_ratio": "2.35:1",
        "field_order": "progressive",
        "r_frame_rate": "25/1",
        "avg_frame_rate": "25/1",
    }
    video.update(overrides)  # type: ignore[arg-type]
    return ffprobe_json(
        format_name="avi",
        video=video,
        audio=[
            {"codec_name": "mp3", "channels": 2, "tags": {"language": "und"}}
        ],
    )


class TestHappyPathXvidAvi:
    def test_classifies_as_xvid_divx_avi(self, tmp_path):
        path = make_media_file(tmp_path, "movie.avi")
        report = probe_and_classify(
            path, runner=fake_runner(avi_xvid_payload())
        )
        assert isinstance(report, FileReport)
        assert report.source_class == "xvid-divx-avi"
        assert report.upscale_verdict == "candidate"
        assert (report.displayed_width, report.displayed_height) == (640, 272)

    def test_reads_video_stream_fields(self, tmp_path):
        path = make_media_file(tmp_path, "movie.avi")
        report = probe_and_classify(
            path, runner=fake_runner(avi_xvid_payload())
        )
        video = report.video
        assert video is not None
        assert video.codec_name == "mpeg4"
        assert (video.width, video.height) == (640, 272)
        assert video.sar == (1, 1)
        assert video.avg_frame_rate == pytest.approx(25.0)
        assert video.field_order == "progressive"

    def test_reports_audio_channels_and_duration(self, tmp_path):
        path = make_media_file(tmp_path, "movie.avi")
        report = probe_and_classify(
            path, runner=fake_runner(avi_xvid_payload())
        )
        assert len(report.audio) == 1
        assert report.audio[0].channels == 2
        assert report.duration_seconds == pytest.approx(5400.0)
        assert report.container == "avi"

    def test_no_subtitles_means_empty_list(self, tmp_path):
        path = make_media_file(tmp_path, "movie.avi")
        report = probe_and_classify(
            path, runner=fake_runner(avi_xvid_payload())
        )
        assert report.subtitles == []


class TestPalSpeedup:
    def test_progressive_25fps_flags_pal_suspect(self, tmp_path):
        path = make_media_file(tmp_path, "movie.avi")
        report = probe_and_classify(
            path, runner=fake_runner(avi_xvid_payload())
        )
        assert report.pal_speedup_suspected is True

    def test_23976_progressive_does_not_flag(self, tmp_path):
        path = make_media_file(tmp_path, "movie.avi")
        report = probe_and_classify(
            path,
            runner=fake_runner(
                avi_xvid_payload(
                    r_frame_rate="24000/1001",
                    avg_frame_rate="24000/1001",
                )
            ),
        )
        assert report.pal_speedup_suspected is False

    def test_25fps_interlaced_does_not_flag(self, tmp_path):
        # Genuine PAL interlaced video (field_order tt) is native
        # content, not a speedup transfer; the heuristic must not
        # fire on it.
        path = make_media_file(tmp_path, "capture.mpg")
        report = probe_and_classify(
            path,
            runner=fake_runner(
                ffprobe_json(
                    format_name="mpeg",
                    video={
                        "codec_name": "mpeg2video",
                        "width": 720,
                        "height": 576,
                        "sample_aspect_ratio": "64:45",
                        "display_aspect_ratio": "16:9",
                        "field_order": "tt",
                        "r_frame_rate": "25/1",
                        "avg_frame_rate": "25/1",
                    },
                )
            ),
        )
        assert report.pal_speedup_suspected is False
        assert report.interlaced_flagged is True

    def test_missing_rate_does_not_flag(self, tmp_path):
        path = make_media_file(tmp_path, "movie.avi")
        report = probe_and_classify(
            path,
            runner=fake_runner(
                avi_xvid_payload(r_frame_rate="0/0", avg_frame_rate="")
            ),
        )
        assert report.pal_speedup_suspected is False


class TestUnknownSarHandling:
    def test_zero_colon_one_sar_is_treated_as_square(self, tmp_path):
        # "0:1" is ffprobe's undetermined-SAR marker; the honest
        # default is square pixels, never a crash or a guess.
        path = make_media_file(tmp_path, "movie.avi")
        report = probe_and_classify(
            path,
            runner=fake_runner(
                avi_xvid_payload(sample_aspect_ratio="0:1")
            ),
        )
        assert report.video is not None
        assert report.video.sar is None
        assert (report.displayed_width, report.displayed_height) == (640, 272)

    def test_missing_sar_field_defaults_to_square(self, tmp_path):
        path = make_media_file(tmp_path, "movie.avi")
        payload = ffprobe_json(
            format_name="avi",
            video={
                "codec_name": "mpeg4",
                "width": 512,
                "height": 384,
                "field_order": "progressive",
                "r_frame_rate": "25/1",
                "avg_frame_rate": "25/1",
            },
        )
        report = probe_and_classify(
            path, runner=fake_runner(payload)
        )
        assert report.video is not None
        assert report.video.sar is None
        assert (report.displayed_width, report.displayed_height) == (512, 384)

    def test_anamorphic_dvd_sar_scales_width(self, tmp_path):
        # 720x576 at SAR 64:45 displays as 1024x576 (16:9 PAL table
        # row from the design doc).
        path = make_media_file(tmp_path, "dvdrip.mkv")
        report = probe_and_classify(
            path,
            runner=fake_runner(
                ffprobe_json(
                    format_name="matroska,webm",
                    video={
                        "codec_name": "mpeg2video",
                        "width": 720,
                        "height": 576,
                        "sample_aspect_ratio": "64:45",
                        "display_aspect_ratio": "16:9",
                        "field_order": "progressive",
                        "r_frame_rate": "25/1",
                        "avg_frame_rate": "25/1",
                    },
                )
            ),
        )
        assert (report.displayed_width, report.displayed_height) == (1024, 576)
        assert report.source_class == "dvd-mpeg2-rip"

    def test_displayed_size_rounds_odd_width_up_to_even(self):
        # 720x480 at SAR 32:27 is 853.33 -> 854 (even, encode-friendly).
        assert displayed_size(720, 480, (32, 27)) == (854, 480)


class TestMovContainer:
    def test_mov_with_h264_sd_classifies(self, tmp_path):
        path = make_media_file(tmp_path, "clip.mov")
        payload = ffprobe_json(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            duration="3600.0",
            video={
                "codec_name": "h264",
                "width": 640,
                "height": 480,
                "sample_aspect_ratio": "1:1",
                "field_order": "progressive",
                "r_frame_rate": "30000/1001",
                "avg_frame_rate": "30000/1001",
            },
            audio=[{"codec_name": "aac", "channels": 2}],
            subtitles=[{"codec_name": "mov_text", "tags": {"language": "eng"}}],
        )
        report = probe_and_classify(path, runner=fake_runner(payload))
        assert report.container == "mov,mp4,m4a,3gp,3g2,mj2"
        assert report.source_class == "h264-sd"
        assert report.upscale_verdict == "candidate"
        assert report.subtitles[0].kind == "text"

    def test_image_subtitles_are_typed_image(self, tmp_path):
        path = make_media_file(tmp_path, "dvdrip.mkv")
        payload = ffprobe_json(
            format_name="matroska,webm",
            video={
                "codec_name": "mpeg2video",
                "width": 720,
                "height": 576,
                "sample_aspect_ratio": "64:45",
                "field_order": "progressive",
                "r_frame_rate": "25/1",
                "avg_frame_rate": "25/1",
            },
            subtitles=[{"codec_name": "vobsub", "tags": {"language": "eng"}}],
        )
        report = probe_and_classify(path, runner=fake_runner(payload))
        assert report.subtitles[0].kind == "image"


class TestVerdicts:
    def test_hd_file_is_already_hd_not_candidate(self, tmp_path):
        path = make_media_file(tmp_path, "hd.mp4")
        payload = ffprobe_json(
            format_name="mov,mp4,m4a,3gp,3g2,mj2",
            video={
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "sample_aspect_ratio": "1:1",
                "field_order": "progressive",
                "r_frame_rate": "24000/1001",
                "avg_frame_rate": "24000/1001",
            },
        )
        report = probe_and_classify(path, runner=fake_runner(payload))
        assert report.upscale_verdict == "already-hd"
        # 1080p h264 is neither an SD h264 source nor any other
        # named class.
        assert report.source_class == "other"

    def test_no_video_stream_reports_no_video(self, tmp_path):
        path = make_media_file(tmp_path, "audio.avi")
        payload = ffprobe_json(
            format_name="avi",
            video=None,
            audio=[{"codec_name": "mp3", "channels": 2}],
        )
        report = probe_and_classify(path, runner=fake_runner(payload))
        assert report.upscale_verdict == "no-video"
        assert report.video is None
        assert report.source_class == "other"


class TestMissingFile:
    def test_missing_file_raises_probe_error(self, tmp_path):
        with pytest.raises(ProbeError, match="no such file"):
            probe_file(str(tmp_path / "ghost.avi"))

    def test_probe_and_classify_missing_file_raises(self, tmp_path):
        with pytest.raises(ProbeError):
            probe_and_classify(str(tmp_path / "ghost.avi"))

    def test_directory_raises_probe_error(self, tmp_path):
        with pytest.raises(ProbeError, match="no such file"):
            probe_file(str(tmp_path))


class TestRunnerSeamErrors:
    def test_ffprobe_nonzero_exit_raises(self, tmp_path):
        path = make_media_file(tmp_path, "movie.avi")
        runner = fake_runner("", returncode=1, stderr="boom: corrupt")
        with pytest.raises(ProbeError, match="ffprobe failed"):
            probe_file(path, runner=runner)

    def test_unparsable_json_raises(self, tmp_path):
        path = make_media_file(tmp_path, "movie.avi")
        with pytest.raises(ProbeError, match="unparsable"):
            probe_file(path, runner=fake_runner("not json"))

    def test_parse_probe_rejects_bad_json(self):
        with pytest.raises(ProbeError):
            parse_probe("x.avi", "{oops")


class TestClassifyPureFunction:
    def test_classify_builds_report_from_raw_probe(self):
        probe = parse_probe("movie.avi", avi_xvid_payload())
        report = classify(probe)
        assert report.source_class == "xvid-divx-avi"
        assert report.pal_speedup_suspected is True
        assert report.interlaced_flagged is False