"""v0.4.14 Mode A (upscale-time transcription) run-seam regression tests.

Karen's validation found the BLOCKER: run.py's transcribed branch called
``transcribe_source_to_srt(..., engine=...)`` against the core's real
``engine_factory`` signature — an UNCAUGHT TypeError that killed every
real Mode A upscale of a subtitle-less file with audio. The plan-seam
tests never drove the branch, so the suite shipped green while the
shipped default path could not run end to end.

These tests drive the REAL run.py transcribed branch end to end over a
real ffmpeg-made fixture; ONLY the whisper engine is mocked (a stub
factory via run_file's own ``transcribe_engine_factory`` seam) — all of
run.py's plan resolution, model resolve, GPU-phase lock, audio extract,
and subrip mux code EXECUTES.

The fail-open contract is also pinned here: any TranscriptionError
inside the branch must NOT fail the run — Mode A degrades to the same
honest 'failed-open' disposition the OCR path has (the upscale ships
without subtitles; the warning names what happened).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from vidlift.run import run_file

pytestmark = pytest.mark.usefixtures("transcription_engine_on")


# ---- real fixture + fake engine --------------------------------------------


def _make_source(tmp_path: Path) -> Path:
    """A real 3s mp4 WITH audio audio-tone and NO subtitle stream (the
    Mode A trigger shape). ffmpeg makes it; the suite already relies on
    ffmpeg presence (tool discovery)."""
    src = tmp_path / "subless.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=128x96:rate=10:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest",
            str(src),
        ],
        check=True, capture_output=True, text=True,
    )
    assert src.is_file()
    return src


class TextualEngine:
    """The engine seam: deterministic en segments with word stamps."""

    def __init__(self, model_path: str, *, device: str = "auto",
                 vad_on: bool = True) -> None:
        self.model_path = model_path

    def detect_language(self, wav_path: str, *, offset_seconds: float = 0.0):
        from vidlift.transcribe import LanguageGuess

        return LanguageGuess(code="en", confidence=0.9)

    def transcribe_segments(self, wav_path: str, *, language: str,
                            on_segment=None):
        from vidlift.transcribe import TranscriptSegment, WordStamp

        segs = [
            TranscriptSegment(
                start=0.5, end=1.5, text=" Hello there.",
                words=[
                    WordStamp(0.5, 1.0, "Hello", 0.95),
                    WordStamp(1.1, 1.5, "there.", 0.95),
                ],
            ),
            TranscriptSegment(
                start=2.0, end=2.8, text=" General Kenobi.",
                words=[
                    WordStamp(2.0, 2.5, "General", 0.95),
                    WordStamp(2.6, 2.8, "Kenobi.", 0.95),
                ],
            ),
        ]
        for s in segs:
            if on_segment is not None:
                on_segment(s)
        return segs


class ExplodingEngine(TextualEngine):
    """detects + extracts fine, then raises — the mid-upscale failure
    the fail-open policy must survive."""

    def transcribe_segments(self, wav_path: str, *, language: str,
                            on_segment=None):
        from vidlift.transcribe import TranscriptionError

        raise TranscriptionError("engine exploded mid-transcription")


def _run(tmp_path: Path, factory, **kwargs):
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    from vidlift.run import ToolBox

    return run_file(
        str(tmp_path / "subless.mp4"),
        engine_request="stub",
        output_dir=str(out_dir),
        log_dir=str(tmp_path / "runs"),
        sample_classify=False,
        transcription_enabled=None,  # the locked default: ON
        transcribe_engine_factory=factory,
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        **kwargs,
    )


# ---- the regression: the real branch runs (the BLOCKER) ---------------------


def test_mode_a_transcribed_branch_runs_end_to_end(tmp_path):
    """Karen's blocker: the branch crashed with
    ``TypeError: transcribe_source_to_srt() got an unexpected keyword
    argument 'engine'`` on EVERY real Mode A run. The real run.py code
    path must complete: model resolved via the factory seam, audio
    extracted through ffmpeg, SRT written beside the output, subrip
    muxed with the detected language tag, run OK."""
    src = _make_source(tmp_path)
    made = {}
    def factory(model_dir):
        made["model_dir"] = model_dir
        return TextualEngine(model_dir)
    result = _run(tmp_path, factory)
    state = result.state
    assert state.ok, f"Mode A run failed: {state.error}"
    # the real branch resolved the model THROUGH the factory seam
    assert "model_dir" in made
    # the ffmpeg real-run sanity: the output decodes (3s smoke)
    output = Path(state.output_path)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width",
         "-of", "json", str(output)],
        capture_output=True, text=True,
    )
    assert probe.returncode == 0
    streams = json.loads(probe.stdout)["streams"]
    # the encode is libx265; the 128x96 (wide) source plans to the
    # 720p-frame target geometry (960x720 for 4:3)
    assert streams and streams[0]["codec_name"] == "hevc"
    assert int(streams[0]["width"]) == 960
    srt = output.with_suffix(".srt")
    assert srt.is_file(), f"no KEEP sidecar at {srt}"
    srt_text = srt.read_text(encoding="utf-8-sig")
    assert "Hello there." in srt_text
    assert "General Kenobi." in srt_text
    assert "-->" in srt_text
    # the subtitles phase carried (subrip into the mkv)
    assert state.subtitles["status"] == "carried"
    assert state.subtitles["transcription"]["language"] == "en"
    assert state.subtitles["embedded"]["codec"] == "subrip"


def test_mode_a_real_mux_command_uses_detected_language_tag(tmp_path):
    """M1 seam check at the Mode A call site: the mux command into the
    finished encode carries the detected language mapped through
    mux_language (en -> eng here; ko -> kor is pinned at the mux layer)."""
    src = _make_source(tmp_path)
    from vidlift.run import ToolBox

    commands: list[tuple] = []

    class Recorder:
        def __call__(self, binary, args):
            commands.append((binary, tuple(args)))
            argv = tuple(args)
            joined = " ".join(argv)
            if "-show_format" in argv and "-show_streams" in argv:
                return 0, json.dumps({
                    "streams": [
                        {"index": 0, "codec_type": "video",
                         "codec_name": "h264", "width": 128, "height": 96},
                        {"index": 1, "codec_type": "audio",
                         "codec_name": "aac", "channels": 2},
                    ],
                    "format": {"format_name": "mov", "duration": "3.0"},
                }), ""
            if "-show_streams" in argv:
                # the verify re-probe of the encode output: report the
                # PLANNED 720p-frame geometry (960x720 for a wide 4:3)
                return 0, json.dumps({
                    "streams": [{"codec_type": "video", "codec_name": "hevc",
                                 "width": 960, "height": 720}]
                }), ""
            if "-f null" in joined:
                return 0, "", ""  # the full-decode smoke
            if "pcm_s16le" in joined:
                # the transcription audio extract (the sync anchor):
                # write the wav so the engine seam gets real input
                Path(argv[-1]).write_bytes(b"wav")
                return 0, "", ""
            cmd_path = Path(argv[-1]) if argv else None
            if cmd_path is not None and str(cmd_path).endswith(
                (".prepared.mkv", ".mkv")
            ):
                cmd_path.write_bytes(b"x")
                return 0, "", ""
            return 0, "", ""

    def factory(model_dir):
        return TextualEngine(model_dir)

    run_file(
        str(src),
        engine_request="stub",
        output_dir=str(tmp_path / "out"),
        log_dir=str(tmp_path / "runs"),
        sample_classify=False,
        transcription_enabled=None,
        transcribe_engine_factory=factory,
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=Recorder(),
        command_runner=Recorder(),
    )
    mux = [argv for _b, argv in commands
           if "-c:s" in argv and "subrip" in argv]
    assert mux, "no subrip mux command ran in the subtitles phase"
    assert "-metadata:s:s:0" in mux[-1]
    tag_idx = mux[-1].index("-metadata:s:s:0")
    assert mux[-1][tag_idx + 1] == "language=eng"


def test_mode_a_transcription_failure_fails_open(tmp_path):
    """The fail-open policy (the OCR path's §B.8 rule): a transcription
    failure mid-upscale must NOT fail the run — the upscale ships, the
    subtitles phase records failed-open with the named reason, and no
    half-written sidecar lies about having succeeded."""
    src = _make_source(tmp_path)
    def factory(model_dir):
        return ExplodingEngine(model_dir)
    result = _run(tmp_path, factory)
    state = result.state
    assert state.ok, (
        "a transcription failure must NEVER fail the upscale "
        f"(fail-open contract): {state.error}"
    )
    assert state.subtitles["status"] == "failed-open"
    assert "transcription unavailable" in state.subtitles["reason"]
    assert "engine exploded" in state.subtitles["reason"]
    # no sidecar claiming success
    out = Path(state.output_path)
    sidecar = out.with_suffix(".srt")
    # the failed-open sidecar disposition: nothing written by the dead
    # transcribe call, so nothing should exist
    assert not sidecar.exists() or sidecar.read_text(
        encoding="utf-8-sig"
    ).strip() == ""