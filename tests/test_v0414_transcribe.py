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
        assert plan.subtitle_plan.codec_name == "subrip"
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

# --------------------------------------------- v0.4.15 single-word hold-cap
def _hold_cap_segments():
    """Karen's Firefly cue-16 verbatim shape: one real word ('can't',
    p=.81). The smear lives in the WORD END STAMP — the VAD never
    snapped the segment shut, so the word's end stamp rode the segment
    end (10.16 s) while the audible word ended at 2.0 s: the cue reads
    10.28 s, an 8.28 s hold past the audible word."""
    from vidlift.transcribe import TranscriptSegment, WordStamp

    return [
        TranscriptSegment(
            start=1.5, end=10.16, text=" can't",
            words=[WordStamp(1.5, 10.16, "can't", 0.81)],
            no_speech_prob=0.3,
        )
    ]


def _hold_cap_segments_multi_word():
    from vidlift.transcribe import TranscriptSegment, WordStamp

    return [
        TranscriptSegment(
            start=1.5, end=10.16, text=" can't stop",
            words=[
                WordStamp(1.5, 2.0, "can't", 0.81),
                WordStamp(2.1, 2.3, "stop", 0.9),
            ],
            no_speech_prob=0.3,
        )
    ]


def _write_wav(path, *, with_word=True, word_start_s=1.5,
               word_end_s=2.0, speech_after_s=None, speech_amp=0.05,
               speech_end_s=4.5, duration_s=12.0):
    """16 kHz mono s16 wav: the audible word (200 Hz sine burst
    1.5-2.0 s by default — the 'can't' the engine stamped) plus an
    optional after-word burst; deterministic, stdlib-only (the
    fixture recipe the design pins)."""
    import array
    import math
    import wave

    rate = 16000
    total = int(duration_s * rate)
    samples = array.array("h", bytes(2 * total))

    def _burst(start_s: float, end_s: float, amp: float) -> None:
        start = int(start_s * rate)
        end = min(int(end_s * rate), total)
        for i in range(start, end):
            t = i / rate
            v = amp * math.sin(2 * math.pi * 200.0 * t)
            samples[i] = int(v * 32767)

    if with_word:
        _burst(word_start_s, word_end_s, 0.05)
    if speech_after_s is not None:
        _burst(speech_after_s, speech_end_s, speech_amp)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(samples.tobytes())


def test_hold_cap_long_hold_quiet_after_trims(tmp_path):
    """RED-FIRST (no cap on 385118f): single word, 8.28 s hold, wav
    SILENT after the word -> cue end caps to word_end + 120 ms
    exactly (2.120 s from Karen's 2.0 s word end)."""
    from vidlift.transcribe import segments_to_cues

    wav = tmp_path / "scratch.wav"
    _write_wav(wav)  # the audible word 1.5-2.0, then silence
    cues = segments_to_cues(_hold_cap_segments(), hold_cap_wav=str(wav))
    assert len(cues) == 1
    assert cues[0].end_ms == 2120  # word end 2000 + 120 readability hold
    assert cues[0].text == "can't"
    assert cues[0].start_ms == 1500


def test_hold_cap_long_hold_speech_after_survives(tmp_path):
    """RED-FIRST: the dramatic-hold survivor — same cue shape but the
    after-word window carries a voiced burst (RMS ~0.05 >> 0.01) so
    the measured guard refuses the trim; end stays 10.28 s
    (word smear + the standard hold, whisper behaved for END here)."""
    from vidlift.transcribe import segments_to_cues

    wav = tmp_path / "scratch.wav"
    _write_wav(wav, speech_after_s=2.5, speech_end_s=3.5)
    cues = segments_to_cues(_hold_cap_segments(), hold_cap_wav=str(wav))
    assert len(cues) == 1
    assert cues[0].end_ms == 10280  # NOT trimmed


def test_hold_cap_short_hold_single_word_untouched(tmp_path):
    """Rule arm 1 negative control: a <=2.5 s hold never trims."""
    from vidlift.transcribe import TranscriptSegment, WordStamp, segments_to_cues

    wav = tmp_path / "scratch.wav"
    _write_wav(wav)
    seg = TranscriptSegment(
        start=1.5, end=4.0, text=" can't",
        words=[WordStamp(1.5, 4.0, "can't", 0.81)],
        no_speech_prob=0.3,
    )
    cues = segments_to_cues([seg], hold_cap_wav=str(wav))
    assert len(cues) == 1
    # 2.12 s past the audible word end (2.0) — under the 2.5 s cap:
    # the cue end stays the builder's word-stamp hold, untouched.
    assert cues[0].end_ms == 4120


def test_hold_cap_multi_word_long_cue_untouched(tmp_path):
    """Rule arm 2 negative control: len(cluster) == 1 ONLY; a
    multi-word long cue rides untouched even over silence."""
    from vidlift.transcribe import segments_to_cues

    wav = tmp_path / "scratch.wav"
    _write_wav(wav)
    cues = segments_to_cues(
        _hold_cap_segments_multi_word(), hold_cap_wav=str(wav)
    )
    assert len(cues) == 1
    assert cues[0].text == "can't stop"
    # end = last word end (2.3 s) + 120 ms; the smear case is the
    # single-word shape — multi-word clusters end on their own last word
    assert cues[0].end_ms == 2420


def test_hold_cap_no_wav_never_trims():
    """RED-FIRST: hold_cap_wav=None (the pure path, and any wav-less
    arm) conservatively NEVER trims — guard-first discipline."""
    from vidlift.transcribe import segments_to_cues

    cues = segments_to_cues(_hold_cap_segments(), hold_cap_wav=None)
    assert len(cues) == 1
    assert cues[0].end_ms == 10280  # the smeared end survives


def test_hold_cap_wav_missing_file_never_trims(tmp_path):
    """An unreadable wav is 'no wav' for the gate — never trim on a
    measurement we could not make."""
    from vidlift.transcribe import segments_to_cues

    cues = segments_to_cues(
        _hold_cap_segments(), hold_cap_wav=str(tmp_path / "absent.wav")
    )
    assert cues[0].end_ms == 10280


def test_hold_cap_quiet_band_boundary(tmp_path):
    """The 0.01 gate rides Karen's measured band: an after-word RMS of
    0.005 trims; 0.02 (quiet dialogue) refuses."""
    from vidlift.transcribe import segments_to_cues

    quiet = tmp_path / "quiet.wav"
    _write_wav(quiet, speech_after_s=2.1, speech_end_s=11.9,
               speech_amp=0.005)
    cues = segments_to_cues(_hold_cap_segments(), hold_cap_wav=str(quiet))
    assert cues[0].end_ms == 2120  # after-word RMS 0.005 < 0.01 -> trim

    speech = tmp_path / "speech.wav"
    _write_wav(speech, speech_after_s=2.5, speech_end_s=3.5,
               speech_amp=0.02)
    cues2 = segments_to_cues(_hold_cap_segments(), hold_cap_wav=str(speech))
    assert cues2[0].end_ms == 10280  # guard window carries 0.02 -> refuses


def test_hold_cap_trim_note_reported(tmp_path):
    """The trim count reaches the notes sink (rides
    TranscriptionResult.notes in production)."""
    from vidlift.transcribe import segments_to_cues

    wav = tmp_path / "scratch.wav"
    _write_wav(wav)
    notes: list[str] = []
    segments_to_cues(
        _hold_cap_segments(), hold_cap_wav=str(wav), notes=notes
    )
    assert notes == ["trimmed 1 stuck single-word cue hold(s)"]
    # and NOTHING when nothing trimmed
    notes2: list[str] = []
    segments_to_cues(
        _hold_cap_segments_multi_word(), hold_cap_wav=str(wav),
        notes=notes2,
    )
    assert notes2 == []
