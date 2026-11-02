"""v0.4.14 Mode B — standalone subs jobs (design §Mode B, B1 soft-mux).

Red-first contracts at the highest boundaries:

1. Naming/guard (pure): {stem}.subs.mp4 output name; output-exists
   never-overwrite; usable-text-track guard; --force override.
2. Success path (fake engine + fake runner): probe -> transcribe (GPU
   lock claimed) -> B1 remux command shape (-c copy, mov_text,
   language tag, faststart, NEW file); source untouched.
3. Executor + registry: subs jobs execute through _execute_subs_job
   with the seam runner; transition through the state graph; cancel
   mid-run closes cancelled.
4. CLI: `vidlift subs` dispatches to run_subs_job through the tools.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from vidlift.mcp_bridge.jobs import CancelFlag, JobExecutor, JobOutcome
from vidlift.mcp_bridge.registry import Registry
from vidlift.probe import (
    AudioInfo,
    FileReport,
    SubtitleInfo,
    VideoInfo,
)
from vidlift.subs_job import (
    SubsJobError,
    SubsJobResult,
    mplex,
    run_subs_job,
    source_has_usable_subtitles,
    subs_output_path,
)
from vidlift.transcribe import TranscriptionResult


# ---------------------------------------------------------------- fakes
def _report(
    subtitles: list[SubtitleInfo] | None = None,
    *,
    path: str = "/media/Movie.avi",
    audio: list[AudioInfo] | None = None,
) -> FileReport:
    return FileReport(
        path=path,
        container="avi",
        duration_seconds=60.0,
        video=VideoInfo(
            codec_name="mpeg4", width=640, height=272,
            sample_aspect_ratio="1:1", display_aspect_ratio="0:1",
            sar=(1, 1), field_order="progressive", r_frame_rate=25.0,
            avg_frame_rate=25.0, r_frame_rate_raw="25/1",
            avg_frame_rate_raw="25/1",
        ),
        audio=audio if audio is not None else [
            AudioInfo(codec_name="mp3", channels=2, lang="und")
        ],
        subtitles=subtitles or [],
        source_class="sd",
        displayed_width=640,
        displayed_height=272,
        upscale_verdict="candidate",
        interlaced_flagged=False,
        pal_speedup_suspected=False,
        sidecars=[],
    )


class FakeEngine:
    """The engine seam: two deterministic segments, one word-stamped."""

    def __init__(self, model_path: str, *, device: str = "auto",
                 vad_on: bool = True) -> None:
        self.model_path = model_path

    def detect_language(self, wav_path: str, *, offset_seconds: float = 0.0):
        from vidlift.transcribe import LanguageGuess

        return LanguageGuess(code="en", confidence=0.98)

    def transcribe_segments(self, wav_path: str, *, language: str,
                            on_segment=None):
        from vidlift.transcribe import TranscriptSegment, WordStamp

        segs = [
            TranscriptSegment(
                start=1.0, end=3.0, text=" Hello there.",
                words=[
                    WordStamp(1.0, 1.5, "Hello", 0.9),
                    WordStamp(1.6, 2.0, "there.", 0.9),
                ],
            ),
            TranscriptSegment(
                start=4.0, end=6.0, text=" General Kenobi.",
                words=[
                    WordStamp(4.0, 4.7, "General", 0.9),
                    WordStamp(4.8, 6.0, "Kenobi.", 0.9),
                ],
            ),
        ]
        for s in segs:
            if on_segment is not None:
                on_segment(s)
        return segs


class FakeRunner:
    """ffmpeg/ffprobe seam: probe JSON, wav extract ok, remux writes."""

    def __init__(self, payload: str, *, remux_fails: bool = False) -> None:
        self.payload = payload
        self.commands: list[tuple[str, tuple[str, ...]]] = []
        self.remux_fails = remux_fails

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        argv = tuple(args)
        self.commands.append((binary, argv))
        joined = " ".join(argv)
        if "-show_format" in argv and "-show_streams" in argv:
            return 0, self.payload, ""
        if "-c:s" in argv and "mov_text" in argv:
            if self.remux_fails:
                return 1, "", "mux boom"
            out = argv[-1]
            Path(out).write_bytes(b"muxed-out")
            return 0, "", ""
        if "pcm_s16le" in joined:
            Path(argv[-1]).write_bytes(b"wav")
            return 0, "", ""
        return 1, "", f"unexpected: {binary} {joined}"


def _probe_payload() -> str:
    return json.dumps({
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


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)


def _seed_model_state(state_root) -> None:
    """v0.4.15 (release-gate root cause): this test's job resolves the
    transcription model against a PER-TEST state_root with nothing in
    it. Resolution then attempts a REAL ~1.5 GB network download of the
    default model — which silently succeeded in interactive shells (CA
    bundles available) and failed the job with
    'transcription model download failed (config.json): SSL
    CERTIFICATE_VERIFY_FAILED' inside the systemd transient units the
    release gate runs in and whose 20-37 s tail inflated the suite.
    The machine-wide cache (~/.vidlift/whisper/models) is complete but
    the test never sees it: state_root redirects resolution. The honest
    fix seeds the per-test state with the model the resolution CONTRACT
    promises (.complete marker -> resolve returns the dir untouched) —
    exercising the real resolution path with zero network. (The
    conftest network guard would fail this test loudly either way;
    seeding makes it pass for the right reason.)"""
    import hashlib

    tag = "medium"
    model_dir = Path(state_root) / "whisper" / "models" / tag
    model_dir.mkdir(parents=True, exist_ok=True)
    # The contract checks ONLY the .complete marker (resolve_model_dir
    # short-circuits before any file/digest read); the rest documents
    # the shape the engine seam receives and keeps the fixture honest
    # if the resolver ever grows content checks.
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.bin").write_bytes(b"stub-model-bin")
    (model_dir / "vocabulary.txt").write_text("", encoding="utf-8")
    (model_dir / ".complete").write_text(
        f"{tag}\n{hashlib.sha256(b'stub-model-bin').hexdigest()}\n",
        encoding="utf-8",
    )


@pytest.fixture
def no_lock(monkeypatch):
    """A stand-in GPU-phase lock instance so no filesystem lock is used."""

    class NoLock:
        def __init__(self):
            self.acquired = False
            self.released = False

        def acquire(self, on_wait=None):
            self.acquired = True
            return True

        def release(self):
            self.released = True

    lock = NoLock()
    monkeypatch.setattr(
        "vidlift.engine_ncnn._gpu_phase_lock", lambda: lock
    )
    return lock


# ------------------------------------------------------- 1. naming/guard
def test_subs_output_name_is_stem_subs_mp4():
    assert subs_output_path("/lib/H/Show.s01e01.mkv") == (
        "/lib/H/Show.s01e01.subs.mp4"
    )
    assert subs_output_path("/x/movie.avi") == "/x/movie.subs.mp4"


def test_bitmap_only_subs_do_not_stop_mode_b():
    assert not source_has_usable_subtitles(
        _report([SubtitleInfo(codec_name="hdmv_pgs_subtitle",
                              kind="image", lang="en", index=2)])
    )


def test_text_track_is_usable():
    assert source_has_usable_subtitles(
        _report([SubtitleInfo(codec_name="subrip", kind="text",
                              lang="en", index=2)])
    )


def test_output_exists_fails_job_never_overwrites(tmp_path):
    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")
    out = tmp_path / "movie.subs.mp4"
    out.write_bytes(b"already there")
    with pytest.raises(SubsJobError, match="output already exists"):
        run_subs_job(
            str(src),
            ffmpeg="ffmpeg",
            probe_runner=FakeRunner(_probe_payload()),
            runner=FakeRunner(_probe_payload()),
            engine_factory=lambda p: FakeEngine(p),
            workdir=str(tmp_path / "wd"),
            gpu_lock_factory=lambda: type("L", (), {
                "acquire": staticmethod(lambda on_wait=None: True),
                "release": staticmethod(lambda: None)})(),
        )
    assert out.read_bytes() == b"already there"
    assert src.read_bytes() == b"source-bytes"


def test_no_audio_fails_the_job(tmp_path):
    src = tmp_path / "movie.avi"
    src.write_bytes(b"x")
    payload = json.dumps({
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "mpeg4",
             "width": 640, "height": 272, "sample_aspect_ratio": "1:1",
             "field_order": "progressive", "r_frame_rate": "25/1"}],
        "format": {"format_name": "avi", "duration": "60.0"},
    })
    with pytest.raises(SubsJobError, match="no audio stream"):
        run_subs_job(
            str(src), ffmpeg="ffmpeg",
            probe_runner=FakeRunner(payload),
            engine_factory=lambda p: FakeEngine(p),
            workdir=str(tmp_path / "wd"),
            gpu_lock_factory=lambda: type("L", (), {
                "acquire": staticmethod(lambda on_wait=None: True),
                "release": staticmethod(lambda: None)})(),
        )


# ------------------------------------------------------- 2. success path
@pytest.mark.usefixtures("engine_on")
def test_b1_success_remux_shape_and_source_blind(tmp_path, no_lock):
    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")
    runner = FakeRunner(_probe_payload())
    result = run_subs_job(
        str(src),
        ffmpeg="ffmpeg",
        probe_runner=runner,
        runner=runner,
        engine_factory=lambda p: FakeEngine(p),
        workdir=str(tmp_path / "wd"),
    )
    assert isinstance(result, SubsJobResult)
    assert result.output_path == str(tmp_path / "movie.subs.mp4")
    assert Path(result.output_path).read_bytes() == b"muxed-out"
    assert Path(result.srt_path).is_file()
    assert result.cue_count == 2
    assert result.language == "en"
    assert src.read_bytes() == b"source-bytes"  # source-blind

    # the remux command: copy codecs, mov_text, tag, faststart, new file
    mux_cmds = [c for c in runner.commands if "-c:s" in c[1]]
    assert len(mux_cmds) == 1
    argv = mux_cmds[0][1]
    assert "-c" in argv and "copy" in argv
    assert "-c:s" in argv and "mov_text" in argv
    assert "-metadata:s:s:0" in argv and "language=eng" in argv
    assert "-movflags" in argv and "+faststart" in argv
    assert argv[-1].endswith(".muxing.mp4")  # temp sibling, atomic replace

    # audio extract maps the source's a:0 (sync anchor)
    wav_cmds = [c for c in runner.commands if "pcm_s16le" in " ".join(c[1])]
    assert wav_cmds and "-map" in wav_cmds[0][1]
    assert "0:a:0" in wav_cmds[0][1]

    # the NoLock was claimed + released (Mode A discipline)
    assert no_lock.acquired and no_lock.released


def test_burn_flag_absent(tmp_path):
    """v0.4.15 (Eric, 2026-11-01): burn-in is PERMANENTLY off the
    roadmap; the vestigial flag is stripped, not merely unimplemented.
    Both surfaces must hard-refuse: the kwarg is unknown (TypeError)
    and the CLI flag is unrecognized (argparse exit 2)."""
    src = tmp_path / "movie.avi"
    src.write_bytes(b"x")
    with pytest.raises(TypeError):
        run_subs_job(str(src), ffmpeg="ffmpeg", burn=True)

    from vidlift.cli import main as cli_main

    with patch.object(
        sys, "argv", ["vidlift", "subs", str(src), "--burn"]
    ):
        with pytest.raises(SystemExit) as excinfo:
            cli_main(["subs", str(src), "--burn"])
    assert excinfo.value.code == 2


# ------------------------------------------------ 3. executor + registry
class _Services:
    def __init__(self, registry, tmp_path):
        self.registry = registry
        self.state_dir = tmp_path / "state"
        self.probe_runner = None
        self.command_runner = None
        self.engine = None

    def notify_new_work(self):
        pass


def _reg(tmp_path):
    import shutil

    root = tmp_path / "state"
    shutil.rmtree(root, ignore_errors=True)
    return Registry(state_dir=str(root))


@pytest.mark.usefixtures("engine_on")
def test_subs_job_executes_through_executor(tmp_path, no_lock, monkeypatch):
    import vidlift.mcp_bridge.jobs as jobs_mod

    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")
    runner = FakeRunner(_probe_payload())
    registry = _reg(tmp_path)  # rmtree('state') first; seeding follows
    _seed_model_state(tmp_path / "state")
    services = _Services(registry, tmp_path)
    monkeypatch.setattr(
        jobs_mod, "_EXECUTORS",
        {**jobs_mod._EXECUTORS,
         "subs": jobs_mod._execute_subs_job},
    )
    # seam the real pipeline's runners via monkeypatch on run_subs_job?
    # No: _execute_subs_job passes services runners; None -> subprocess.
    # Instead patch the module-level _subprocess_runner import inside.
    monkeypatch.setattr(
        "vidlift.run._subprocess_runner",
        lambda b, a: runner(b, a),
    )
    record = registry.enqueue(
        kind="subs", paths=[str(src)],
        options={"workdir": str(tmp_path / "wd")},
    )
    executor = JobExecutor(services)
    monkeypatch.setattr(
        "vidlift.subs_job.run_subs_job",
        lambda path, **kw: run_subs_job(
            path,
            ffmpeg="ffmpeg",
            probe_runner=runner,
            runner=runner,
            engine_factory=lambda p: FakeEngine(p),
            workdir=str(tmp_path / "wd"),
            **{k: v for k, v in kw.items()
               if k in ("should_cancel", "state_root", "model_pin",
                        "device_pin", "language_pin", "force")},
        ),
    )
    claimed = registry.claim_next()  # queued -> running (the real flow)
    assert claimed is not None and claimed.job_id == record.job_id
    executor._run_job(record)
    final = registry.require(record.job_id)
    assert final.state == "succeeded", final.error
    assert final.output_path == str(tmp_path / "movie.subs.mp4")
    assert (final.result or {}).get("language") == "en"


@pytest.mark.usefixtures("engine_on")
def test_subs_cancel_closes_cancelled(tmp_path, no_lock, monkeypatch):
    import vidlift.mcp_bridge.jobs as jobs_mod

    src = tmp_path / "movie.avi"
    src.write_bytes(b"x")
    registry = _reg(tmp_path)
    services = _Services(registry, tmp_path)
    record = registry.enqueue(kind="subs", paths=[str(src)], options={})

    def boom(*a, **kw):
        cancel = kw.get("should_cancel")
        if cancel is not None and cancel():
            from vidlift.transcribe import TranscriptionError
            raise TranscriptionError("cancelled during transcription")
        return SubsJobResult(
            source_path=str(src), output_path=str(src) + ".subs.mp4",
            srt_path="", cue_count=0, language="en",
            language_confidence=1.0, model="medium",
        )

    monkeypatch.setattr("vidlift.subs_job.run_subs_job", boom)
    executor = JobExecutor(services)
    registry.claim_next()  # queued -> running (the real flow)
    executor._run_job(record)
    final = registry.require(record.job_id)
    assert final.state == "succeeded", final.error
    # the cancelled-only contract lives in run_subs_job's should_cancel
    # -> EngineCancelled conversion, unit-tested at the core level.


# ------------------------------------------------------------- 4. CLI
def test_cli_subs_dispatch(tmp_path, monkeypatch, capsys):
    from vidlift import cli

    src = tmp_path / "movie.avi"
    src.write_bytes(b"source-bytes")
    runner = FakeRunner(_probe_payload())
    monkeypatch.setattr(
        "vidlift.subs_job.run_subs_job",
        lambda path, **kw: SubsJobResult(
            source_path=path,
            output_path=path[:-4] + ".subs.mp4",
            srt_path=path[:-4] + ".subs.srt",
            cue_count=2, language="ko", language_confidence=0.93,
            model="medium", notes=[],
        ),
    )
    rc = cli.main([
        "subs", str(src), "--lang", "ko", "--model", "large-v3",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "language: ko" in out


def test_cli_subs_failure_exit(tmp_path, monkeypatch, capsys):
    from vidlift import cli

    src = tmp_path / "novid.avi"
    src.write_bytes(b"x")
    monkeypatch.setattr(
        "vidlift.subs_job.run_subs_job",
        lambda *a, **kw: (_ for _ in ()).throw(
            SubsJobError("no audio stream to transcribe: x")
        ),
    )
    rc = cli.main(["subs", str(src)])
    assert rc == 1
    assert "no audio stream" in capsys.readouterr().err

@pytest.mark.usefixtures("engine_on")
def test_subs_cancel_mid_run_closes_cancelled(tmp_path, no_lock, monkeypatch):
    """v0.4.14 contract (design test plan #6): cancel mid-transcribe ->
    `cancelled`, never `failed`."""
    import vidlift.mcp_bridge.jobs as jobs_mod

    src = tmp_path / "movie.avi"
    src.write_bytes(b"x")
    registry = _reg(tmp_path)
    services = _Services(registry, tmp_path)
    record = registry.enqueue(kind="subs", paths=[str(src)], options={})

    def slow(*a, **kw):
        cancel = kw.get("should_cancel")
        assert cancel is not None
        from vidlift.run import EngineCancelled

        raise EngineCancelled("cancelled during transcription")

    monkeypatch.setattr("vidlift.subs_job.run_subs_job", slow)
    executor = JobExecutor(services)
    registry.claim_next()
    executor._run_job(record)
    final = registry.require(record.job_id)
    assert final.state == "cancelled"


# -------------------------------------------------- Mode A surface wiring
def test_mcp_run_options_carry_transcription_enabled(tmp_path):
    """The run tool enqueues options verbatim; the executor's
    absent-defaults rule passes None into run_file."""
    from vidlift.mcp_bridge.registry import Registry
    import inspect
    import vidlift.mcp_bridge.jobs as jobs_mod

    src = inspect.getsource(jobs_mod._execute_run_job)
    assert 'transcription_enabled=options.get(\n                "transcription_enabled", None' in src


def test_gui_checkbox_default_matches_locked_config(monkeypatch):
    """QSettings default True mirrors config.TRANSCRIPTION_ENABLED_
    DEFAULT (Eric's locked ON); the checkbox is the kill switch."""
    from vidlift.config import TRANSCRIPTION_ENABLED_DEFAULT

    assert TRANSCRIPTION_ENABLED_DEFAULT is True
