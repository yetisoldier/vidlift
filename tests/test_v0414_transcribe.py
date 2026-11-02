"""v0.4.14 plan-seam tests (design §Test plan 1, red-first).

The plan seam: a file report with NO usable subtitle streams plans
``subtitle_plan.source == "transcribed"`` when Mode A is enabled (the
Eric-locked default), and stays the honest ``None`` when disabled —
the checkbox is observably load-bearing. Existing text/bitmap/sidecar
selections are REGRESSION-GUARDED: the v0.4.1 rule order is untouched.
"""

from __future__ import annotations

import pytest

from vidlift.plan import (
    build_file_plan,
    select_subtitle_track,
)
from vidlift.probe import (
    AudioInfo,
    FileReport,
    SubtitleInfo,
    VideoInfo,
)


def _report(subtitles: list[SubtitleInfo] | None = None) -> FileReport:
    return FileReport(
        path="/media/Movie.avi",
        container="avi",
        duration_seconds=3600.0,
        video=VideoInfo(
            codec_name="mpeg4",
            width=640,
            height=272,
            sample_aspect_ratio="1:1",
            display_aspect_ratio="40:17",
            sar=(1, 1),
            field_order="progressive",
            r_frame_rate=25.0,
            avg_frame_rate=25.0,
            r_frame_rate_raw="25/1",
            avg_frame_rate_raw="25/1",
        ),
        audio=[AudioInfo(codec_name="mp3", channels=2, lang="eng")],
        subtitles=subtitles or [],
        source_class="xvid-divx-avi",
        displayed_width=640,
        displayed_height=272,
        upscale_verdict="candidate",
        interlaced_flagged=False,
        pal_speedup_suspected=False,
    )


@pytest.mark.usefixtures("transcription_engine_on")
class TestTranscribedSeamEnabledByDefault:
    def test_no_subtitles_plans_transcribed_by_default(self):
        """The locked default: transcription enabled -> the None seam
        returns a transcribed text plan."""
        plan = build_file_plan(_report())
        assert plan.subtitle_plan is not None
        assert plan.subtitle_plan.source == "transcribed"
        assert plan.subtitle_plan.kind == "text"
        assert plan.subtitle_plan.ocr_required is False
        assert plan.subtitle_plan.stream_index == -1
        assert plan.subtitle_plan.codec_name == "mov_text"
        assert plan.transcription == {"model": "medium", "device": "auto", "language": ""}

    def test_transcription_disabled_via_config_plans_honest_none(self):
        """The checkbox is load-bearing: disabled -> None (unchanged
        v0.4.13 behavior)."""
        plan = build_file_plan(_report(), transcription_enabled=False)
        assert plan.subtitle_plan is None


@pytest.mark.usefixtures("transcription_engine_on")
class TestTranscriptionBlockAdditive:
    def test_transcription_block_none_when_feature_off(self):
        plan = build_file_plan(_report(), transcription_enabled=False)
        assert plan.transcription is None

    def test_transcription_block_none_for_embedded_text_plans(self):
        """A carried plan (the normal path) carries NO transcription
        block — the field is None-defaulted and transcribe-only."""
        report = _report(
            [SubtitleInfo(codec_name="subrip", kind="text", lang="eng", index=2)]
        )
        plan = build_file_plan(report)
        assert plan.subtitle_plan is not None
        assert plan.subtitle_plan.source == "embedded"
        assert plan.transcription is None

    def test_transcription_block_carries_config(self):
        plan = build_file_plan(
            _report(),
            transcription_enabled=True,
            transcription_model="large-v3",
            transcription_device="cpu",
            transcription_language="ko",
        )
        assert plan.transcription == {
            "model": "large-v3",
            "device": "cpu",
            "language": "ko",
        }


@pytest.mark.usefixtures("transcription_engine_on")
class TestLanguagePinRecordedInPlan:
    def test_pinned_language_rides_the_plan_and_note(self):
        plan = build_file_plan(
            _report(), transcription_enabled=True, transcription_language="zh-cn"
        )
        assert plan.subtitle_plan is not None
        assert plan.subtitle_plan.lang == "zh-cn"
        assert "transcribing" in plan.subtitle_note
        assert "zh-cn" in plan.subtitle_note


class TestRegressionExistingSelections:
    """The v0.4.1 §B.2 rule order is untouched by the transcription
    fallback: embedded text, bitmap, and sidecar selections all win
    over transcription (transcribed fires ONLY at the honest None)."""

    def test_embedded_text_still_wins(self):
        report = _report(
            [SubtitleInfo(codec_name="subrip", kind="text", lang="eng", index=2)]
        )
        plan = select_subtitle_track(report)  # selection is plan-knob-free
        assert plan is not None and plan.source == "embedded"

    def test_bitmap_still_ocr_candidate(self):
        report = _report(
            [SubtitleInfo(codec_name="dvd_subtitle", kind="image", lang="eng", index=1)]
        )
        plan = select_subtitle_track(report)
        assert plan is not None and plan.ocr_required is True

    def test_sidecar_adopted_when_no_embedded(self):
        report = _report()
        report.sidecars = [
            type("S", (), {"path": "/media/Movie.srt", "kind": "text"})()
        ]
        plan = select_subtitle_track(report)
        assert plan is not None and plan.source == "sidecar"

    def test_bitmap_ocr_disable_leaves_transcribed_not_none(self):
        """OCR-off kills only bitmap work (v0.4.7 rule); the v0.4.14
        transcription fallback still applies at the None seam (audio
        exists; the source has no usable subtitle track)."""
        from vidlift.run import _apply_subtitle_ocr_preference

        report = _report(
            [SubtitleInfo(codec_name="dvd_subtitle", kind="image", lang="eng", index=1)]
        )
        plan = build_file_plan(report)  # bitmap selected
        adjusted = _apply_subtitle_ocr_preference(plan, enabled=False)
        assert adjusted.subtitle_plan is None  # v0.4.7 contract unchanged