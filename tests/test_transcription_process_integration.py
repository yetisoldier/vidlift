"""Mode B integration with spawned, deliberately hung synthetic native worker."""
import os
import time
import wave
from types import SimpleNamespace

import pytest

from vidlift import subs_job, transcribe
from vidlift.run import EngineCancelled
from vidlift.transcribe import TranscriptSegment


@pytest.fixture(autouse=True)
def isolated_cuda_latch():
    transcribe.reset_cuda_failure_cache()
    yield
    transcribe.reset_cuda_failure_cache()


def fake_process(pipe, requested, model_dir, wav_path, language_pin, vad_on):
    attempts = os.environ.get("VIDLIFT_TEST_ATTEMPT_LOG")
    if attempts:
        with open(attempts, "a") as log:
            log.write(requested + "\n")
    pipe.send(("resolved", "cpu" if requested == "cpu" else "cuda"))
    if requested != "cpu":
        while True:
            time.sleep(1)
    pipe.send(("decode", None))
    pipe.send(("progress", TranscriptSegment(start=0, end=1, text="Hello there")))
    pipe.send(("result", ("ok", "cpu", "en", None, [])))
    pipe.close()


def always_hung_process(pipe, requested, model_dir, wav_path, language_pin, vad_on):
    pipe.send(("resolved", "cpu" if requested == "cpu" else "cuda"))
    while True:
        time.sleep(1)


def trickle_process(pipe, requested, model_dir, wav_path, language_pin, vad_on):
    pipe.send(("resolved", "cuda"))
    pipe.send(("decode", None))
    while True:
        pipe.send(("progress", TranscriptSegment(start=0, end=1, text="hello there")))
        time.sleep(0.05)


def cuda_success_process(pipe, requested, model_dir, wav_path, language_pin, vad_on):
    """v0.4.20 (S1): a worker whose CUDA attempt SUCCEEDS — module-level
    so the spawn child can pickle it."""
    log = os.environ.get("VIDLIFT_TEST_ATTEMPT_LOG")
    if log:
        with open(log, "a") as f:
            f.write(requested + "\n")
    pipe.send(("resolved", "cuda"))
    pipe.send(("decode", None))
    pipe.send(("progress", TranscriptSegment(start=0, end=1, text="Hello there")))
    pipe.send(("result", ("ok", "cuda", "en", None, [])))
    pipe.close()


def prepare(monkeypatch, tmp_path):
    src = tmp_path / "source.mkv"
    src.write_bytes(b"source sentinel")
    monkeypatch.setattr(subs_job, "probe_file", lambda *a, **k: object())
    monkeypatch.setattr(subs_job, "classify", lambda _: SimpleNamespace(video=True, audio=[1], subtitles=[]))
    monkeypatch.setattr(subs_job, "source_has_usable_subtitles", lambda _: False)
    monkeypatch.setattr(transcribe, "resolve_model_dir", lambda *a, **k: "dummy")
    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_PROCESS", fake_process)
    monkeypatch.setattr(transcribe, "_WORKER_INIT_TIMEOUT", 0.6)
    monkeypatch.setattr(transcribe, "_WORKER_DECODE_TIMEOUT", 1.0)

    class Lock:
        def acquire(self, **kwargs): return True
        def release(self): pass

    def runner(exe, args):
        with wave.open(args[-1], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\0\0" * 16000)
        return 0, "", ""

    def mux(source, srt, *, output_path, **kwargs):
        assert os.path.exists(srt)
        with open(output_path, "wb") as f:
            f.write(b"muxed")

    monkeypatch.setattr(subs_job, "mplex", mux)
    monkeypatch.setattr(subs_job, "verify_mux", lambda *a, **k: {"ok": True, "kind": "synthetic-worker-integration-probe"})
    return src, runner, Lock


def test_mode_b_hung_cuda_then_cpu_completes(monkeypatch, tmp_path):
    src, runner, lock = prepare(monkeypatch, tmp_path)
    progress, devices = [], []
    result = subs_job.run_subs_job(
        str(src), ffmpeg="fake", runner=runner, gpu_lock_factory=lock,
        device_pin="cuda", on_progress=lambda *a: progress.append(a),
        on_device=lambda *a: devices.append(a),
    )
    assert src.read_bytes() == b"source sentinel"
    assert result.device == "cpu"
    assert any("init_timeout" in note for note in result.notes)
    assert progress == [(1, 0)]
    assert devices == [("cpu", "cuda")]
    assert os.path.exists(result.srt_path)


def test_mode_b_cancel_hung_cuda_does_not_write_or_fallback(monkeypatch, tmp_path):
    src, runner, lock = prepare(monkeypatch, tmp_path)
    started = time.monotonic()
    with pytest.raises(EngineCancelled):
        subs_job.run_subs_job(
            str(src), ffmpeg="fake", runner=runner, gpu_lock_factory=lock,
            device_pin="cuda", should_cancel=lambda: time.monotonic() - started > 0.25,
            replace_source=True,
        )
    assert src.read_bytes() == b"source sentinel"
    assert not (tmp_path / "source.subs.mp4").exists()
    assert not (tmp_path / "source.subs.srt").exists()
    # v0.4.20 (S1): the latch is batch-scoped — a lookup under any batch id
    # must be empty after a cancelled run (cancellation never latches).
    assert transcribe._cached_cuda_failure("b") is None


def test_mode_b_cpu_retry_timeout_preserves_source_and_existing_sidecar(monkeypatch, tmp_path):
    src, runner, lock = prepare(monkeypatch, tmp_path)
    sidecar = tmp_path / "source.subs.srt"
    sidecar.write_bytes(b"existing sidecar")
    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_PROCESS", always_hung_process)
    with pytest.raises(transcribe.TranscriptionError, match="engine_timeout"):
        subs_job.run_subs_job(
            str(src), ffmpeg="fake", runner=runner, gpu_lock_factory=lock,
            device_pin="cuda", replace_source=True,
        )
    assert src.read_bytes() == b"source sentinel"
    assert sidecar.read_bytes() == b"existing sidecar"
    assert not (tmp_path / "source.subs.mp4").exists()


def test_real_worker_mode_b_constructor_hang_and_cpu_retry(monkeypatch, tmp_path):
    src, runner, lock = prepare(monkeypatch, tmp_path)
    # Spawn imports these stand-in vendor modules afresh, so this exercises
    # the production worker and IPC rather than a replacement worker.
    modules = tmp_path / "modules"
    package = modules / "faster_whisper"
    package.mkdir(parents=True)
    (modules / "ctranslate2.py").write_text(
        'def get_supported_compute_types(device):\n    return {"float16"}\n'
    )
    (package / "utils.py").write_text('def get_assets_path():\n    return "."\n')
    (package / "__init__.py").write_text(
        'import os, time\n'
        'from types import SimpleNamespace\n'
        'class WhisperModel:\n'
        '    def __init__(self, model_path, device, **kwargs):\n'
        '        with open(os.environ["VIDLIFT_TEST_ATTEMPTS"], "a") as f:\n'
        '            f.write(device + "\\n")\n'
        '        if device == "cuda":\n'
        '            while True: time.sleep(1)\n'
        '    def transcribe(self, *args, **kwargs):\n'
        '        seg = SimpleNamespace(start=0., end=1., text="Hello there", words=[], no_speech_prob=0.)\n'
        '        return iter([seg]), None\n'
    )
    attempts = tmp_path / "attempts.txt"
    monkeypatch.syspath_prepend(str(modules))
    monkeypatch.setenv("VIDLIFT_TEST_ATTEMPTS", str(attempts))
    monkeypatch.setenv("VIDLIFT_TRANSCRIPTION_ENGINE", "1")
    monkeypatch.setattr(transcribe, "vad_asset_report", lambda: {"vad_asset": "present"})
    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_PROCESS", transcribe._transcription_process)
    monkeypatch.setattr(transcribe, "_WORKER_INIT_TIMEOUT", 2.0)
    result = subs_job.run_subs_job(
        str(src), ffmpeg="fake", runner=runner, gpu_lock_factory=lock,
        device_pin="cuda", language_pin="en",
    )
    assert attempts.read_text().splitlines() == ["cuda", "cpu"]
    assert result.device == "cpu"
    assert any("init_timeout" in note for note in result.notes)
    assert src.read_bytes() == b"source sentinel"


def test_gpu_timeout_latches_for_same_batch_and_explicit_reset(monkeypatch, tmp_path):
    """v0.4.20 (S1): the latch is BATCH-SCOPED — the same batch identity
    short-circuits to cpu; an explicit reset lets even that batch re-probe."""
    src, runner, lock = prepare(monkeypatch, tmp_path)
    attempts = tmp_path / "attempts"
    monkeypatch.setenv("VIDLIFT_TEST_ATTEMPT_LOG", str(attempts))

    def run(source, batch_id):
        return subs_job.run_subs_job(
            str(source), ffmpeg="fake", runner=runner,
            gpu_lock_factory=lock, device_pin="cuda", batch_id=batch_id,
        )

    first = run(src, "batch-1")
    second_src = tmp_path / "second.mkv"
    second_src.write_bytes(b"second sentinel")
    second = run(second_src, "batch-1")  # same batch → cpu shortcut
    assert attempts.read_text().splitlines() == ["cuda", "cpu", "cpu"]
    assert first.device == second.device == "cpu"
    # v0.4.20 (S4): the note carries the D6 reason code + detail
    assert any("init_timeout" in note for note in second.notes)
    assert src.read_bytes() == b"source sentinel"
    assert second_src.read_bytes() == b"second sentinel"
    transcribe.reset_cuda_failure_cache()
    third_src = tmp_path / "third.mkv"
    third_src.write_bytes(b"third sentinel")
    run(third_src, "batch-1")  # reset → even the same batch re-probes
    assert attempts.read_text().splitlines() == ["cuda", "cpu", "cpu", "cuda", "cpu"]


def test_batch_scoped_latch_different_batch_reprobes(monkeypatch, tmp_path):
    """v0.4.20 (S1) spec arm: Batch A fails on CUDA → latch; the same
    batch_id still short-circuits to cpu; a different batch id re-probes
    cuda — NO cpu shortcut for it."""
    src, runner, lock = prepare(monkeypatch, tmp_path)
    attempts = tmp_path / "attempts"
    monkeypatch.setenv("VIDLIFT_TEST_ATTEMPT_LOG", str(attempts))

    def run(source, batch_id):
        return subs_job.run_subs_job(
            str(source), ffmpeg="fake", runner=runner,
            gpu_lock_factory=lock, device_pin="cuda", batch_id=batch_id,
        )

    first = run(src, "batch-a")  # cuda attempt times out → cpu retry
    assert attempts.read_text().splitlines() == ["cuda", "cpu"]
    assert transcribe._cached_cuda_failure("batch-a") is not None

    second_src = tmp_path / "second.mkv"
    second_src.write_bytes(b"second sentinel")
    run(second_src, "batch-a")  # same batch as the failure → cpu shortcut
    assert attempts.read_text().splitlines() == ["cuda", "cpu", "cpu"]

    third_src = tmp_path / "third.mkv"
    third_src.write_bytes(b"third sentinel")
    run(third_src, "batch-b")  # different batch → re-probes cuda
    assert attempts.read_text().splitlines() == ["cuda", "cpu", "cpu", "cuda", "cpu"]


def test_no_batch_identity_never_latches(monkeypatch, tmp_path):
    """v0.4.20 (S1): callers without a batch identity get no latching —
    every call re-probes (the honest behavior for direct/library use)."""
    src, runner, lock = prepare(monkeypatch, tmp_path)
    attempts = tmp_path / "attempts"
    monkeypatch.setenv("VIDLIFT_TEST_ATTEMPT_LOG", str(attempts))

    def run(source):
        return subs_job.run_subs_job(
            str(source), ffmpeg="fake", runner=runner,
            gpu_lock_factory=lock, device_pin="cuda",
        )  # no batch_id

    first = run(src)
    second_src = tmp_path / "second.mkv"
    second_src.write_bytes(b"second sentinel")
    second = run(second_src)
    assert attempts.read_text().splitlines() == ["cuda", "cpu", "cuda", "cpu"]
    assert transcribe._cached_cuda_failure("any-batch") is None
    assert first.device == second.device == "cpu"


def test_strict_cpu_pin_never_latches(monkeypatch, tmp_path):
    """v0.4.20 (S1) spec arm: a strict CPU pin never latches CUDA and is
    unaffected by any latch — it goes straight to cpu."""
    src, runner, lock = prepare(monkeypatch, tmp_path)
    attempts = tmp_path / "attempts"
    monkeypatch.setenv("VIDLIFT_TEST_ATTEMPT_LOG", str(attempts))

    result = subs_job.run_subs_job(
        str(src), ffmpeg="fake", runner=runner, gpu_lock_factory=lock,
        device_pin="cpu", batch_id="batch-x",
    )
    assert attempts.read_text().splitlines() == ["cpu"]  # no cuda attempt at all
    assert result.device == "cpu"
    assert transcribe._cached_cuda_failure("batch-x") is None


def test_successful_cuda_attempt_clears_latch(monkeypatch, tmp_path):
    """v0.4.20 (S1) spec arm: a successful CUDA attempt clears the latch —
    a later run of an earlier-failed batch re-probes cuda."""
    src, runner, lock = prepare(monkeypatch, tmp_path)

    attempts = tmp_path / "attempts"
    monkeypatch.setenv("VIDLIFT_TEST_ATTEMPT_LOG", str(attempts))

    def run(source, batch_id):
        return subs_job.run_subs_job(
            str(source), ffmpeg="fake", runner=runner,
            gpu_lock_factory=lock, device_pin="cuda", batch_id=batch_id,
        )

    first = run(src, "batch-a")  # cuda times out → cpu retry, latch set
    assert attempts.read_text().splitlines() == ["cuda", "cpu"]
    assert transcribe._cached_cuda_failure("batch-a") is not None

    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_PROCESS", cuda_success_process)
    second_src = tmp_path / "second.mkv"
    second_src.write_bytes(b"second sentinel")
    second = run(second_src, "batch-b")  # different batch: cuda SUCCEEDS
    assert attempts.read_text().splitlines() == ["cuda", "cpu", "cuda"]
    assert second.device == "cuda"
    # the success cleared the latch for every batch
    assert transcribe._cached_cuda_failure("batch-a") is None

    third_src = tmp_path / "third.mkv"
    third_src.write_bytes(b"third sentinel")
    run(third_src, "batch-a")  # earlier-failed batch re-probes cuda now
    assert attempts.read_text().splitlines() == ["cuda", "cpu", "cuda", "cuda"]


# ---- F5 (v0.4.20 S8): non-device child failure classification ----------

def vad_missing_worker(pipe, requested, model_dir, wav_path, language_pin, vad_on):
    """Child that dies with a device-independent packaging fault."""
    log = os.environ.get("VIDLIFT_TEST_ATTEMPT_LOG")
    if log:
        with open(log, "a") as f:
            f.write(requested + "\n")
    pipe.send(("resolved", "cpu" if requested == "cpu" else "cuda"))
    pipe.send(("result", ("error", "init", 0, "VadAssetMissingError")))
    pipe.close()


def unknown_error_worker(pipe, requested, model_dir, wav_path, language_pin, vad_on):
    """Child that dies with an ordinary (device-plausible) fault."""
    log = os.environ.get("VIDLIFT_TEST_ATTEMPT_LOG")
    if log:
        with open(log, "a") as f:
            f.write(requested + "\n")
    pipe.send(("resolved", "cpu" if requested == "cpu" else "cuda"))
    pipe.send(("result", ("error", "init", 0, "RuntimeError")))
    pipe.close()


def test_non_device_child_error_does_not_latch_or_retry(monkeypatch, tmp_path):
    """F5: a device-independent fault must not latch CUDA and must not burn
    the one CPU retry — exactly one child spawn total."""
    attempts = tmp_path / "attempts"
    monkeypatch.setenv("VIDLIFT_TEST_ATTEMPT_LOG", str(attempts))
    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_PROCESS", vad_missing_worker)
    with pytest.raises(transcribe.TranscriptionError, match="device-independent"):
        transcribe._isolated_transcription(
            "dummy-model", str(tmp_path / "missing.wav"), "cuda", "", True,
            lambda: False, None, None, None, batch_id="b",
        )
    assert attempts.read_text().splitlines() == ["cuda"]
    assert transcribe._cached_cuda_failure("b") is None


def test_unknown_child_error_latches_and_retries_once(monkeypatch, tmp_path):
    """F5 regression pin: an ordinary error type keeps today's behaviour —
    latch + exactly one CPU retry. v0.4.20 (S1/S4): the latch is batch-
    scoped and stores a D6 ``(reason_code, detail)`` pair."""
    attempts = tmp_path / "attempts"
    monkeypatch.setenv("VIDLIFT_TEST_ATTEMPT_LOG", str(attempts))
    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_PROCESS", unknown_error_worker)
    with pytest.raises(transcribe.TranscriptionError, match="init failed"):
        transcribe._isolated_transcription(
            "dummy-model", str(tmp_path / "missing.wav"), "cuda", "", True,
            lambda: False, None, None, None, batch_id="b",
        )
    assert attempts.read_text().splitlines() == ["cuda", "cpu"]
    # init-phase child error on a CUDA attempt → vram_or_init_failure (D6)
    assert transcribe._cached_cuda_failure("b") == ("vram_or_init_failure", "RuntimeError")


def test_trickle_hits_absolute_deadline_preserving_source(monkeypatch, tmp_path):
    src, runner, lock = prepare(monkeypatch, tmp_path)
    sidecar = tmp_path / "source.subs.srt"
    sidecar.write_bytes(b"existing sidecar")
    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_PROCESS", trickle_process)
    monkeypatch.setattr(transcribe, "_WORKER_DECODE_TIMEOUT", 0.3)
    monkeypatch.setattr(transcribe, "_WORKER_DECODE_SECONDS_PER_AUDIO_SECOND", 0.0)
    monkeypatch.setattr(transcribe, "_WORKER_ABSOLUTE_BASE_SECONDS", 0.6)
    monkeypatch.setattr(transcribe, "_WORKER_ABSOLUTE_SECONDS_PER_AUDIO_SECOND", 0.0)
    progress = []
    # v0.4.20 (S4): the absolute deadline maps to decode_timeout (D6) —
    # it bounds the whole attempt while the code names the decode window.
    with pytest.raises(transcribe.TranscriptionError, match="decode_timeout"):
        subs_job.run_subs_job(
            str(src), ffmpeg="fake", runner=runner, gpu_lock_factory=lock,
            device_pin="cuda", replace_source=True, batch_id="b",
            on_progress=lambda *args: progress.append(args),
        )
    assert len(progress) >= 3
    assert src.read_bytes() == b"source sentinel"
    assert sidecar.read_bytes() == b"existing sidecar"
    assert not (tmp_path / "source.subs.mp4").exists()
    # Progress is already observable: fail, never retry or poison this batch.
    assert transcribe._cached_cuda_failure("b") is None
