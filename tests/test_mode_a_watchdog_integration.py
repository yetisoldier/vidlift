"""Mode A's real run_file branch with spawned synthetic native workers."""
import os
import time
from pathlib import Path

import pytest

from test_v0414_mode_a_seam import _make_source, _run
from vidlift import transcribe
from vidlift.run import EngineCancelled
from vidlift.transcribe import TranscriptSegment

pytestmark = pytest.mark.usefixtures("transcription_engine_on")


@pytest.fixture(autouse=True)
def isolated_cuda_latch():
    transcribe.reset_cuda_failure_cache()
    yield
    transcribe.reset_cuda_failure_cache()


def _mode_a_worker(pipe, requested, model_dir, wav_path, language_pin, vad_on):
    stage = os.environ["VIDLIFT_TEST_HANG_STAGE"]
    device = "cpu" if requested == "cpu" else "cuda"
    pipe.send(("resolved", device))
    if device == "cuda":
        if stage == "decode":
            pipe.send(("decode", None))
        Path(os.environ["VIDLIFT_TEST_STARTED"]).write_text("started")
        while True:
            time.sleep(1)
    Path(os.environ["VIDLIFT_TEST_STARTED"] + ".cpu").write_text("started")
    pipe.send(("decode", None))
    pipe.send(("progress", TranscriptSegment(start=0.5, end=1.5, text="Hello there")))
    pipe.send(("result", ("ok", "cpu", "en", None, [])))
    pipe.close()


def _silent_worker(pipe, requested, model_dir, wav_path, language_pin, vad_on):
    pipe.send(("resolved", "cpu"))
    pipe.send(("decode", None))
    time.sleep(0.7)  # longer than the base deadline, shorter than audio allowance
    pipe.send(("progress", TranscriptSegment(start=0.5, end=1.5, text="Hello there")))
    pipe.send(("result", ("ok", "cpu", "en", None, [])))
    pipe.close()


def _always_hung_worker(pipe, requested, model_dir, wav_path, language_pin, vad_on):
    pipe.send(("resolved", "cpu" if requested == "cpu" else "cuda"))
    while True:
        time.sleep(1)


def _setup(monkeypatch, tmp_path, stage):
    src = _make_source(tmp_path)
    original = src.read_bytes()
    monkeypatch.setattr(transcribe, "resolve_model_dir", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(transcribe, "vad_asset_report", lambda: {"vad_asset": "present"})
    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_PROCESS", _mode_a_worker)
    monkeypatch.setattr(transcribe, "_WORKER_INIT_TIMEOUT", 0.7)
    monkeypatch.setattr(transcribe, "_WORKER_DECODE_TIMEOUT", 0.3)
    monkeypatch.setattr(transcribe, "_WORKER_DECODE_SECONDS_PER_AUDIO_SECOND", 0.2)
    marker = tmp_path / "started"
    monkeypatch.setenv("VIDLIFT_TEST_STARTED", str(marker))
    monkeypatch.setenv("VIDLIFT_TEST_HANG_STAGE", stage)
    return src, original, marker


@pytest.mark.parametrize("stage", ["init", "decode"])
def test_mode_a_hung_worker_retries_cpu_and_muxes(monkeypatch, tmp_path, stage):
    src, original, marker = _setup(monkeypatch, tmp_path, stage)
    result = _run(tmp_path, None)
    assert marker.exists()
    assert result.state.ok, result.state.error
    assert result.state.subtitles is not None
    info = result.state.subtitles["transcription"]
    assert info["device"] == "cpu"
    assert any(f"{stage}_timeout" in note for note in info["notes"])
    assert result.state.subtitles["status"] == "carried"
    assert result.state.output_path is not None
    assert Path(result.state.output_path).with_suffix(".srt").exists()
    assert src.read_bytes() == original


def test_mode_a_cancellation_during_hung_worker_preserves_source(monkeypatch, tmp_path):
    src, original, marker = _setup(monkeypatch, tmp_path, "decode")
    with pytest.raises(EngineCancelled):
        _run(tmp_path, None, cancel=marker.exists, replace_source=True)
    assert src.read_bytes() == original
    assert not Path(str(marker) + ".cpu").exists()
    assert not list((tmp_path / "out").glob("*.srt"))
    # v0.4.20 (S1): the latch is batch-scoped — a lookup under any batch id
    # must be empty after a cancelled run (cancellation never latches).
    assert transcribe._cached_cuda_failure("any-batch") is None


def test_mode_a_both_attempts_timeout_does_not_replace_source(monkeypatch, tmp_path):
    src, original, _ = _setup(monkeypatch, tmp_path, "init")
    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_PROCESS", _always_hung_worker)
    result = _run(tmp_path, None, replace_source=True)
    assert result.state.ok, result.state.error
    assert result.state.subtitles is not None
    assert result.state.subtitles["status"] == "failed-open"
    assert "engine_timeout" in result.state.subtitles["reason"]
    assert any("source replacement skipped" in w for w in result.state.subtitles["warnings"])
    assert src.read_bytes() == original
    assert result.state.output_path is not None
    assert Path(result.state.output_path).exists()


def test_mode_a_leading_silence_uses_duration_allowance(monkeypatch, tmp_path):
    src, original, _ = _setup(monkeypatch, tmp_path, "init")
    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_PROCESS", _silent_worker)
    monkeypatch.setattr(transcribe, "_WORKER_ABSOLUTE_BASE_SECONDS", 0.05)
    monkeypatch.setattr(transcribe, "_WORKER_ABSOLUTE_SECONDS_PER_AUDIO_SECOND", 0.5)
    result = _run(tmp_path, None)
    assert result.state.ok, result.state.error
    assert result.state.subtitles is not None
    assert result.state.subtitles["status"] == "carried"
    assert src.read_bytes() == original


@pytest.mark.parametrize("stage", ["probe", "init", "decode"])
def test_mode_a_real_worker_native_standins_hang_then_cpu(monkeypatch, tmp_path, stage):
    src, original, _ = _setup(monkeypatch, tmp_path, stage)
    modules = tmp_path / "modules"
    pkg = modules / "faster_whisper"
    pkg.mkdir(parents=True)
    (modules / "ctranslate2.py").write_text(
        'import os,time\n'
        'def get_supported_compute_types(device):\n'
        '    if os.environ["VIDLIFT_TEST_HANG_STAGE"] == "probe":\n'
        '        while True: time.sleep(1)\n'
        '    return {"float16"}\n'
    )
    (pkg / "utils.py").write_text('def get_assets_path():\n    return "."\n')
    (pkg / "__init__.py").write_text(
        'import os,time\n'
        'from types import SimpleNamespace\n'
        'class WhisperModel:\n'
        '    def __init__(self, model_path, device, **kwargs):\n'
        '        self.device = device\n'
        '        if device == "cuda" and os.environ["VIDLIFT_TEST_HANG_STAGE"] == "init":\n'
        '            while True: time.sleep(1)\n'
        '    def detect_language(self, audio):\n'
        '        return "en", 0.9, []\n'
        '    def transcribe(self, *args, **kwargs):\n'
        '        if self.device == "cuda" and os.environ["VIDLIFT_TEST_HANG_STAGE"] == "decode":\n'
        '            while True: time.sleep(1)\n'
        '        seg = SimpleNamespace(start=0.5,end=1.5,text="Hello there",words=[],no_speech_prob=0.)\n'
        '        return iter([seg]), None\n'
    )
    # The real language-detection adapter decodes the audio prefix via av;
    # supply a tiny Python stand-in in the child (the fixture wav is already
    # 16 kHz mono s16le — exactly the resampler target — so it passes
    # through untouched). Keeps the suite hermetic: no av dependency.
    (modules / "av.py").write_text(
        'import wave\n'
        'import numpy as np\n'
        '\n'
        'class _Frame:\n'
        '    def __init__(self, samples):\n'
        '        self._samples = samples  # int16 ndarray (n,)\n'
        '    def to_ndarray(self):\n'
        '        return self._samples.reshape(1, -1)\n'
        '\n'
        'class _Resampler:\n'
        "    def __init__(self, **kwargs): pass\n"
        '    def resample(self, frame):\n'
        '        if frame is None:\n'
        '            return iter(())\n'
        '        yield frame  # already s16 mono @16k: passthrough\n'
        '\n'
        'class _Container:\n'
        '    def __init__(self, path):\n'
        '        w = wave.open(path)\n'
        '        raw = w.readframes(w.getnframes())\n'
        '        w.close()\n'
        '        self._frame = _Frame(np.frombuffer(raw, dtype=np.int16).copy())\n'
        '    def decode(self, audio=0):\n'
        '        yield self._frame\n'
        '    def __enter__(self): return self\n'
        '    def __exit__(self, *exc): return False\n'
        '\n'
        'def open(path, mode="r", metadata_errors=None):\n'
        '    return _Container(path)\n'
        '\n'
        '_resampler_ns = type("R", (), {"AudioResampler": _Resampler})()\n'
        'audio = type("A", (), {"resampler": _resampler_ns})()\n'
    )
    monkeypatch.syspath_prepend(str(modules))
    monkeypatch.setattr(transcribe, "_TRANSCRIPTION_PROCESS", transcribe._transcription_process)
    monkeypatch.setattr(transcribe, "_WORKER_INIT_TIMEOUT", 4.0)
    result = _run(tmp_path, None)
    assert result.state.ok, result.state.error
    assert result.state.subtitles is not None
    info = result.state.subtitles["transcription"]
    assert info["device"] == "cpu"
    assert any("timeout" in note for note in info["notes"])
    assert result.state.subtitles["status"] == "carried"
    assert src.read_bytes() == original
