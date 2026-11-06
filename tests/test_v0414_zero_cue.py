"""v0.4.14 validation fixes: zero-cue disposition (Karen's M2).

A transcription that produces ZERO cues used to write a 3-byte BOM-only
SRT that then failed the ffmpeg remux with a raw parse error. Now the
core NAMED-fails before writing anything:
  - Mode A (upscale) fails OPEN with the honest status note;
  - Mode B (standalone subs job) lands the job FAILED with the named
    error the user sees (TranscriptionError -> executor ->
    registry failed state).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_v0414_subs_job import no_lock

from vidlift.transcribe import (
    LanguageGuess,
    TranscriptSegment,
    TranscriptionError,
    TranscriptionResult,
    WordStamp,
    transcribe_source_to_srt,
)


# ---- the core: named zero-cue error, no file written ------------------------


class _SilentEngine:
    """The VAD-empty / music-only signature: extracts fine, emits
    language but ZERO segments."""

    def __init__(self, model_path: str, *, device: str = "auto",
                 vad_on: bool = True) -> None:
        pass

    def detect_language(self, wav_path: str, *, offset_seconds: float = 0.0):
        return LanguageGuess(code="en", confidence=0.9)

    def transcribe_segments(self, wav_path: str, *, language: str,
                            on_segment=None):
        return []


class _ZeroCueEngine(_SilentEngine):
    """Segments exist but every word is below the confidence floor and
    the alignment pass drops the rest — also zero cues at the end."""

    def transcribe_segments(self, wav_path: str, *, language: str,
                            on_segment=None):
        return [
            TranscriptSegment(
                start=1.0, end=2.0, text="mumble mumble",
                words=[
                    # avg 0.05 — below WORD_PROB_FLOOR (music garbage)
                    WordStamp(1.0, 1.4, "mumble", 0.05),
                ],
            ),
        ]


class _OkRunner:
    """Records commands; succeeds at everything."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, binary: str, args: list[str]):
        argv = tuple(args)
        self.commands.append((binary, argv))
        joined = " ".join(argv)
        if "pcm_s16le" in joined:
            Path(argv[-1]).write_bytes(b"wav")
            return 0, "", ""
        return 0, "", ""


@pytest.fixture
def _no_engine_env(monkeypatch):
    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)


def _patch_model(monkeypatch, tmp_path):
    """Model resolution must succeed without network/cache: pin the
    resolver to a fake dir."""
    import vidlift.transcribe as mod

    fake = tmp_path / "fake-model"
    fake.mkdir(exist_ok=True)
    monkeypatch.setattr(
        mod, "resolve_model_dir", lambda pin=None, **kw: str(fake)
    )


@pytest.mark.usefixtures("_no_engine_env")
def test_zero_cue_raises_named_error_before_writing_srt(
    tmp_path, monkeypatch
):
    _patch_model(monkeypatch, tmp_path)
    out = tmp_path / "movie.mp4"
    out.write_bytes(b"out")
    runner = _OkRunner()
    with pytest.raises(TranscriptionError) as excinfo:
        transcribe_source_to_srt(
            str(tmp_path / "src.mp4"),
            output_path=str(out),
            workdir=str(tmp_path / "wd"),
            ffmpeg="ffmpeg",
            runner=runner,
            engine_factory=lambda model_dir: _SilentEngine(model_dir),
        )
    # the named error the user sees (Mode B) / the run note (Mode A)
    assert "no speech detected in audio" in str(excinfo.value)
    # NOTHING was written: no BOM-only SRT anywhere
    srt = out.with_suffix(".srt")
    assert not srt.exists()
    # the wav scratch was still cleaned up (the finally discipline)
    assert not (tmp_path / "wd" / "source-audio-16k.wav").exists()


@pytest.mark.usefixtures("_no_engine_env")
def test_zero_cue_after_confidence_floor_also_named(tmp_path, monkeypatch):
    """Segments whose every cluster is below the word-confidence floor
    produce zero cues — the same named error (the music-garbage-only
    file is indistinguishable from 'no speech' at the SRT level)."""
    _patch_model(monkeypatch, tmp_path)
    out = tmp_path / "movie.mp4"
    out.write_bytes(b"out")
    with pytest.raises(TranscriptionError) as excinfo:
        transcribe_source_to_srt(
            str(tmp_path / "src.mp4"),
            output_path=str(out),
            workdir=str(tmp_path / "wd"),
            ffmpeg="ffmpeg",
            runner=_OkRunner(),
            engine_factory=lambda model_dir: _ZeroCueEngine(model_dir),
        )
    assert "no speech detected in audio" in str(excinfo.value)
    assert not out.with_suffix(".srt").exists()


# ---- Mode A: the run fails OPEN with the honest note ------------------------


def test_mode_a_zero_cue_fails_open_with_status_note(tmp_path, monkeypatch):
    """Zero cues in Mode A = NO subtitles carried, run still OK, the
    subtitles block records the named reason (like every other
    fail-open), and the mux that used to choke on the BOM-only file
    never runs."""
    import subprocess

    from vidlift.run import ToolBox, run_file

    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)

    src = tmp_path / "subless.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=size=128x96:rate=10:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest", str(src),
        ],
        check=True, capture_output=True, text=True,
    )

    def factory(model_dir):
        return _SilentEngine(model_dir)

    result = run_file(
        str(src),
        engine_request="stub",
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        output_dir=str(tmp_path / "out"),
        log_dir=str(tmp_path / "runs"),
        sample_classify=False,
        transcription_enabled=None,
        transcribe_engine_factory=factory,
    )
    state = result.state
    assert state.ok, "zero cues must NEVER fail the upscale (fail-open)"
    assert state.subtitles["status"] == "failed-open"
    assert "no speech detected in audio" in state.subtitles["reason"]
    out = Path(state.output_path)
    assert out.is_file()                       # the upscale shipped
    assert not out.with_suffix(".srt").exists()  # no empty sidecar lie
    # and no mov_text mux ran (there was nothing to mux)
    manifest = json.loads(Path(result.manifest_path).read_text())


# ---- Mode B: the job fails with the named error -----------------------------


class _SubsRunner(_OkRunner):
    """Like _OkRunner plus the probe JSON + mux effect the subs job
    needs (only reached when cues exist — zero-cue jobs fail first)."""

    def __call__(self, binary: str, args: list[str]):
        argv = tuple(args)
        self.commands.append((binary, argv))
        joined = " ".join(argv)
        if "-show_format" in argv and "-show_streams" in argv:
            return 0, json.dumps({
                "streams": [
                    {"index": 0, "codec_type": "video", "codec_name": "mpeg4",
                     "width": 640, "height": 272, "sample_aspect_ratio": "1:1",
                     "field_order": "progressive", "r_frame_rate": "25/1",
                     "avg_frame_rate": "25/1"},
                    {"index": 1, "codec_type": "audio", "codec_name": "mp3",
                     "channels": 2, "tags": {"language": "und"}},
                ],
                "format": {"format_name": "avi", "duration": "60.0"},
            }), ""
        if "pcm_s16le" in joined:
            Path(argv[-1]).write_bytes(b"wav")
            return 0, "", ""
        if "-c:s" in argv and "mov_text" in argv:
            Path(argv[-1]).write_bytes(b"muxed-out")
            return 0, "", ""
        return 0, "", ""


@pytest.mark.usefixtures("_no_engine_env")
def test_mode_b_zero_cue_job_fails_named(tmp_path, monkeypatch, no_lock):
    """Karen's M2, Mode B: a zero-cue transcription lands the job FAILED
    with the NAMED reason (previously a raw ffmpeg parse error from
    muxing a BOM-only SRT). Exercises the real executor path that
    Eric's queue surfaces: run_subs_job -> TranscriptionError ->
    JobOutcome(ok=False, error=...)."""
    import shutil

    import vidlift.mcp_bridge.jobs as jobs_mod
    from vidlift.mcp_bridge.jobs import JobExecutor
    from vidlift.mcp_bridge.registry import Registry

    _patch_model(monkeypatch, tmp_path)
    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")
    runner = _SubsRunner()
    root = tmp_path / "state"
    shutil.rmtree(root, ignore_errors=True)
    registry = Registry(state_dir=str(root))

    class _Services:
        pass

    _services = _Services()
    _services.registry = registry
    _services.state_dir = root
    _services.probe_runner = None
    _services.command_runner = None
    _services.engine = None

    def notify_new_work(self):
        pass

    _Services.notify_new_work = notify_new_work

    from vidlift.subs_job import run_subs_job as _real_run_subs_job

    def fake_run_subs_job(path, **kw):
        return _real_run_subs_job(
            path,
            ffmpeg="ffmpeg",
            probe_runner=runner,
            runner=runner,
            engine_factory=lambda p: _SilentEngine(p),
            workdir=str(tmp_path / "wd"),
            **{k: v for k, v in kw.items()
               if k in ("should_cancel", "state_root", "model_pin",
                        "device_pin", "language_pin", "force")},
        )

    monkeypatch.setattr(
        "vidlift.subs_job.run_subs_job", fake_run_subs_job
    )
    record = registry.enqueue(kind="subs", paths=[str(src)], options={})
    executor = JobExecutor(_services)
    registry.claim_next()
    executor._run_job(record)
    final = registry.require(record.job_id)
    assert final.state == "failed"
    # the user sees WHY (named), not a raw ffmpeg parse error
    assert "no speech detected in audio" in final.error
    # nothing was muxed or left behind
    assert not (tmp_path / "movie.subs.mp4").exists()
    assert not (tmp_path / "movie.subs.srt").exists()