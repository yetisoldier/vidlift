"""v0.4.14 validation fixes: language-tag + Mode A mux seam tests.

Karen's M1 (High): mux_language collapsed every non-English code to
'und', so a ko-detected library slice got tagged 'und' in the muxed
MP4 — breaking the language-tag promise for exactly the foreign
content (Eric's ko/zh library) the feature exists for. The honesty
rule stays for UNCONFIRMED streams (unknown/empty -> und); DETECTED
languages get their real ISO 639-2 tag.
"""

from __future__ import annotations

import subprocess

import pytest

from vidlift.subtitles import mux_language
from vidlift.transcribe import TranscriptSegment, WordStamp


def _real_ffmpeg_runner(binary: str, args: list[str]):
    """The real subprocess runner shaped like subs_job._default_runner —
    the mux seam's injected-runner contract stays honest here."""
    proc = subprocess.run([binary, *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


class TestDetectedLanguagesGetRealTags:
    """The M1 fix: ko/zh/ja/en (faster-whisper's 639-1 output) and the
    common remainder map to ISO 639-2; unrecognized stays honestly und."""

    @pytest.mark.parametrize(
        "code,tag",
        [
            ("ko", "kor"),
            ("zh", "zho"),
            ("ja", "jpn"),
            ("en", "eng"),
            ("de", "deu"),
            ("es", "spa"),
            ("fr", "fra"),
            ("ru", "rus"),
            ("it", "ita"),
            ("pt", "por"),
        ],
    )
    def test_6391_codes_map_to_6392(self, code, tag):
        assert mux_language(code) == tag

    @pytest.mark.parametrize(
        "code,tag",
        [
            ("kor", "kor"),
            ("zho", "zho"),
            ("jpn", "jpn"),
            ("eng", "eng"),
            ("deu", "deu"),
            ("chi", "chi"),   # 639-2/B bibliographic twin, pass through
            ("ger", "ger"),   # ffmpeg accepts both twins
            ("cmn", "cmn"),   # Mandarin macrolanguage code
        ],
    )
    def test_existing_6392_codes_pass_through(self, code, tag):
        assert mux_language(code) == tag

    @pytest.mark.parametrize(
        "code", ["", "und", "undeclared-anything", "xx", "zz-top"]
    )
    def test_unconfirmed_stays_und(self, code):
        # the v0.2.16 honesty rule UNCHANGED for unconfirmed streams
        assert mux_language(code) == "und"


class TestMuxTagRoundTrip:
    """The tags must survive a REAL ffmpeg subrip mux into MKV (the
    language descriptor round-trips through ffprobe verbatim; v0.4.24
    container ADR — Matroska carries `-metadata:s:s` tags identically) —
    pinned once with a real mux, the level the defect actually
    surfaced."""

    @pytest.mark.parametrize(
        "code,tag", [("ko", "kor"), ("zh", "zho"), ("ja", "jpn")]
    )
    def test_ko_zh_ja_survive_real_mux(self, tmp_path, code, tag):
        src = tmp_path / "slice.mkv"
        srt = tmp_path / "cue.srt"
        srt.write_text(
            "1\r\n00:00:00,000 --> 00:00:00,400\r\nx\r\n", encoding="utf-8"
        )
        # Real minimal v+a MKV base (lavfi, no external inputs), then
        # the PRODUCTION mux seam (the §5 pinned shape: temp-sibling +
        # os.replace, `-c copy -c:s subrip` + language tag) — the mp4/
        # mov_text round-trip this class used to pin left with the
        # MP4 container (v0.4.24 ADR).
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=64x64:d=0.5",
                "-f", "lavfi", "-i", "anullsrc=d=0.5",
                "-shortest",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac",
                str(src),
            ],
            check=True, capture_output=True, text=True,
        )
        from vidlift.subtitles import mux_srt_into_video

        mux_srt_into_video(
            str(src), str(srt), language=code,
            ffmpeg="ffmpeg", runner=_real_ffmpeg_runner,
        )
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "s:0",
                "-show_entries", "stream=codec_name:stream_tags=language",
                "-of", "default=nw=1", str(src),
            ],
            check=True, capture_output=True, text=True,
        )
        assert "codec_name=subrip" in probe.stdout
        assert f"language={tag}" in probe.stdout


class TestModeACueTimingWordOnsets:
    """Fix 4's unit contract at the cue builder: cue starts come from
    the FIRST WORD timestamp (not the segment/VAD window start) with a
    small readability lead-in, ends from the last word end + lead-out;
    low-confidence cues (music-region hallucinations) are dropped."""

    def test_segment_start_bias_replaced_by_word_onset(self):
        # Karen's measured shape: the engine pins word[0] to the segment
        # start, but a drift fixture must PROVE the mapping: a segment
        # whose start is 300ms before its first word builds the cue on
        # the WORD time (the honest anchor), not the segment window.
        seg = TranscriptSegment(
            start=10.000, end=12.000, text="Hello world now",
            words=[
                WordStamp(10.300, 10.600, "Hello", 0.95),
                WordStamp(10.700, 10.950, "world", 0.95),
                WordStamp(11.000, 11.900, "now", 0.95),
            ],
        )
        from vidlift.transcribe import segments_to_cues

        cues = segments_to_cues([seg])
        assert len(cues) == 1
        # start == first word start; NOT the segment start (the 300ms
        # bias shape Karen measured)
        assert cues[0].start_ms == 10300
        # end == last word end + the 120ms readability hold
        assert cues[0].end_ms == 11900 + 120
        assert cues[0].text == "Hello world now"

    def test_low_probability_cue_is_dropped(self):
        # Karen's music-edge garbage signature: garbled cues with low
        # average word probability (measured: real dialogue 0.75-0.995,
        # music-edge hallucinations 0.013-0.30). Below the floor -> no
        # cue at all.
        from vidlift.transcribe import segments_to_cues, WORD_PROB_FLOOR

        seg = TranscriptSegment(
            start=5.0, end=7.0, text="tell it have they hailed us",
            words=[
                WordStamp(5.0, 5.4, "tell", 0.3),
                WordStamp(5.5, 5.8, "it", 0.2),
                WordStamp(5.9, 6.4, "have", 0.5),
                WordStamp(6.5, 7.0, "they", 0.4),
            ],
            no_speech_prob=0.82,
        )
        cues = segments_to_cues([seg])
        assert cues == []

    def test_real_dialogue_cue_survives_the_floor(self):
        from vidlift.transcribe import segments_to_cues

        seg = TranscriptSegment(
            start=1.0, end=3.0, text="Everything looks good from here.",
            words=[
                WordStamp(1.0, 1.6, "Everything", 0.99),
                WordStamp(1.7, 2.0, "looks", 0.99),
                WordStamp(2.1, 2.5, "good", 0.98),
                WordStamp(2.6, 3.0, "from", 0.98),
                WordStamp(3.0, 3.4, "here.", 0.97),
            ],
        )
        cues = segments_to_cues([seg])
        assert len(cues) == 1
        assert cues[0].text == "Everything looks good from here."

    def test_single_word_debris_over_no_speech_window_dropped(self):
        # Karen's measured music-edge shape: a one-word hallucination
        # ('Oh', p=.30) inside a segment the engine flagged no_speech
        # .82. The word-prob floor alone can't see it (0.30 < 0.5 does
        # drop it) — the no_speech signal pins the wordless/debris
        # shapes; this pins the combined rule.
        from vidlift.transcribe import segments_to_cues

        seg = TranscriptSegment(
            start=5.0, end=5.6, text="Oh",
            words=[WordStamp(5.0, 5.3, "Oh", 0.30)],
            no_speech_prob=0.82,
        )
        assert segments_to_cues([seg]) == []

    def test_wordless_segment_over_no_speech_window_dropped(self):
        # the engine emitted text but NO word timestamps and flagged the
        # window as noise — the wordless fallback must not resurrect it
        from vidlift.transcribe import segments_to_cues

        seg = TranscriptSegment(
            start=8.0, end=9.0, text="mumbled words",
            words=[],
            no_speech_prob=0.9,
        )
        assert segments_to_cues([seg]) == []

    def test_wordless_segment_with_clean_speech_kept(self):
        # no word timestamps but honest speech (low no_speech): the
        # fallback cue keeps the engine's segment bounds (never lose
        # subtitles on an engine that omits word data)
        from vidlift.transcribe import segments_to_cues

        seg = TranscriptSegment(
            start=8.0, end=9.0, text="clean line",
            words=[],
            no_speech_prob=0.1,
        )
        cues = segments_to_cues([seg])
        assert len(cues) == 1
        assert cues[0].start_ms == 8000