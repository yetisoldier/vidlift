"""v0.4.15.4 transcription-core gates: F-10's bounded language-detect
decode + F-11's self-healing model cache.

The suite's own discipline holds here: NO network (the fetch seam is
injected, or the conftest guard dies loudly), NO real model, NO
faster-whisper import (the engine unit pin builds the adapter via
``__new__`` with a stub model; the decode pins feed real wav fixtures
built with the stdlib ``wave`` module).
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

import vidlift.transcribe as transcribe_mod
from vidlift.transcribe import (
    DETECT_AUDIO_SECONDS,
    TranscriptionError,
    _decode_audio_prefix,
    resolve_model_dir,
)


@pytest.fixture
def engine_on(monkeypatch):
    """resolve_model_dir honors VIDLIFT_TRANSCRIPTION_ENGINE=0 (the
    suite's autouse guard); these pins exercise the real resolver, so
    the guard is lifted per-test."""
    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)


def _wav_pattern(i: int) -> int:
    return ((i * 7) % 3200) - 1600


def _write_wav(path: Path, *, seconds: float, rate: int = 16000) -> int:
    """Deterministic 16 kHz mono s16 wav (stdlib only): sample i reads
    ``_wav_pattern(i)``. Returns the total sample count."""
    total = int(seconds * rate)
    samples = [_wav_pattern(i) for i in range(total)]
    import array

    data = array.array("h", samples)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(data.tobytes())
    return total


def _wav_samples(path: Path) -> np.ndarray:
    """The file's raw s16 samples as the float32/32768 array decode
    semantics promise (the mirror of faster-whisper's chain)."""
    with wave.open(str(path), "rb") as wav:
        raw = np.frombuffer(wav.readframes(2**31), dtype=np.int16)
    return raw.astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# F-10: WhisperEngine.detect_language decodes a BOUNDED prefix only
# ---------------------------------------------------------------------------


def test_detect_language_routes_through_bounded_prefix(monkeypatch):
    """The unit pin (no engine, no decoder): detect_language delegates
    the whole decode to _decode_audio_prefix — the helper receives the
    offset through and keeps its DETECT_AUDIO_SECONDS window default;
    the model receives EXACTLY the helper's array."""
    calls: dict = {}
    expected = np.arange(8, dtype=np.float32)

    def fake_prefix(
        path,
        *,
        offset_seconds=0.0,
        limit_seconds=DETECT_AUDIO_SECONDS,
        sampling_rate=16000,
    ):
        calls["path"] = path
        calls["offset_seconds"] = offset_seconds
        calls["limit_seconds"] = limit_seconds
        calls["sampling_rate"] = sampling_rate
        return expected

    monkeypatch.setattr(transcribe_mod, "_decode_audio_prefix", fake_prefix)

    class _StubModel:
        def detect_language(self, audio):
            calls["audio"] = audio
            return "ko", 0.9, None

    engine = transcribe_mod.WhisperEngine.__new__(
        transcribe_mod.WhisperEngine
    )
    engine.model = _StubModel()
    guess = engine.detect_language("/media/show.wav", offset_seconds=5.0)
    assert (guess.code, guess.confidence) == ("ko", 0.9)
    assert calls["path"] == "/media/show.wav"
    assert calls["offset_seconds"] == 5.0
    assert calls["limit_seconds"] == DETECT_AUDIO_SECONDS
    assert calls["sampling_rate"] == 16000
    assert calls["audio"] is expected


def test_f10_full_window_decode_is_sample_exact(tmp_path):
    """(a) a whole-file-sized window under the limit decodes EVERYTHING,
    sample-exact against the wav's own s16 content (the s16→float32
    scaling mirrors faster-whisper's /32768)."""
    wav_path = tmp_path / "short.wav"
    total = _write_wav(wav_path, seconds=2.0)
    audio = _decode_audio_prefix(str(wav_path))
    assert audio.dtype == np.float32
    assert audio.size == total  # ± 0: the aligned stream is exact
    np.testing.assert_allclose(
        audio, _wav_samples(wav_path), rtol=0.0, atol=0.0
    )


def test_f10_limit_seconds_bounds_the_window(tmp_path):
    """(b) limit_seconds=0.5 on a 2 s source returns the FIRST 8000
    samples — half-open [offset, offset+limit), sample-exact."""
    wav_path = tmp_path / "bounded.wav"
    _write_wav(wav_path, seconds=2.0)
    audio = _decode_audio_prefix(str(wav_path), limit_seconds=0.5)
    assert audio.size == 8000
    np.testing.assert_allclose(
        audio, _wav_samples(wav_path)[:8000], rtol=0.0, atol=0.0
    )


def test_f10_offset_seconds_shifts_the_window(tmp_path):
    """(c) offset_seconds=1.0 shifts the window's START — [16000:..] of
    a 2 s source, sample-exact; offset+limit composition slices the
    half-open window [offset, offset+limit)."""
    wav_path = tmp_path / "shifted.wav"
    _write_wav(wav_path, seconds=2.0)
    samples = _wav_samples(wav_path)

    shifted = _decode_audio_prefix(str(wav_path), offset_seconds=1.0)
    assert shifted.size == samples.size - 16000
    np.testing.assert_allclose(
        shifted, samples[16000:], rtol=0.0, atol=0.0
    )

    windowed = _decode_audio_prefix(
        str(wav_path), offset_seconds=1.0, limit_seconds=0.5
    )
    assert windowed.size == 8000
    np.testing.assert_allclose(
        windowed, samples[16000:24000], rtol=0.0, atol=0.0
    )


def test_f10_decode_is_bounded_on_a_long_source(tmp_path):
    """The memory-shape pin (the acceptance's mechanism bound): a
    source far longer than the window yields AT MOST
    DETECT_AUDIO_SECONDS * sampling_rate samples (+ one resampler
    frame) — the bound is structural, the helper never decodes past
    stop."""
    wav_path = tmp_path / "long.wav"
    _write_wav(wav_path, seconds=200.0)  # ≫ the 120 s window
    audio = _decode_audio_prefix(str(wav_path))
    bound = DETECT_AUDIO_SECONDS * 16000
    assert audio.size <= bound + 1600  # + one resampler frame
    assert audio.size == bound  # aligned stream: the window is exact


# ---------------------------------------------------------------------------
# F-11: the model cache is self-healing
# ---------------------------------------------------------------------------


def _model_dir(state_root) -> Path:
    return Path(state_root) / "whisper" / "models" / "medium"


def test_g1_dying_fetch_leaves_no_staging_debris(
    tmp_path, engine_on, monkeypatch
):
    """Karen's G1 evidence, promoted: the REAL fetcher's mid-copy
    failure (the ENOSPC family — the connection dies mid-copy with a
    staged partial ``.part.tmp``) fails as TranscriptionError AND
    leaves neither ``.part`` nor ``.part.tmp`` in the model dir — the
    old fetcher stranded the staging file on exactly this break."""
    state_root = tmp_path / "state"

    class _DyingConnection:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            raise OSError(28, "No space left on device")

    # The conftest's autouse network guard patches the SAME
    # monkeypatched attribute; pytest stacks per-fixture patches and
    # restores them LIFO, so the scope of this stub is this test only
    # (loopback URL shape kept for honesty: no packet can leave).
    monkeypatch.setattr(
        transcribe_mod.urllib.request,
        "urlopen",
        lambda url, *a, **k: _DyingConnection(),
    )
    with pytest.raises(TranscriptionError):
        resolve_model_dir("medium", state_root=state_root)
    model_dir = _model_dir(state_root)
    assert model_dir.is_dir()
    assert sorted(p.name for p in model_dir.iterdir()) == [], (
        "a failed fetch stranded staging debris (.part/.part.tmp)"
    )


def test_g1_fetch_interrupt_leaves_no_tmp(tmp_path, monkeypatch):
    """The BaseException rationale, pinned: KeyboardInterrupt (or any
    cancel-style control flow) through the REAL fetcher removes the
    staging file — an interrupted download never strands debris."""
    from vidlift.transcribe import _default_fetcher

    class _InterruptedResponse:
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        transcribe_mod.urllib.request,
        "urlopen",
        lambda url, *a, **k: _InterruptedResponse(),
    )
    fetch = _default_fetcher()
    target = tmp_path / "model.bin"
    with pytest.raises(KeyboardInterrupt):
        fetch("http://127.0.0.1:9/x", target)
    assert list(tmp_path.iterdir()) == [], "the .tmp survivor strand"


def test_g2_marker_without_model_bin_refetches(tmp_path, engine_on):
    """Karen's G2 evidence, promoted: a ``.complete`` marker WITHOUT
    model.bin (killed mid-write, manual cleanup) must NOT short-circuit
    — the resolver re-enters the size/sha-verified fetch path (the pin
    is the ABSENCE of the short-circuit: the injected fetch IS invoked)
    instead of returning a dir that fails engine construction later
    with a confusing error."""
    state_root = tmp_path / "state"
    model_dir = _model_dir(state_root)
    model_dir.mkdir(parents=True)
    (model_dir / ".complete").write_text(
        "medium\n" + "0" * 64 + "\n", encoding="utf-8"
    )

    calls: list[str] = []

    def refusing_fetch(url, target_path):
        calls.append(target_path.name)
        raise TranscriptionError(f"forced by the G2 pin: {target_path.name}")

    with pytest.raises(TranscriptionError) as excinfo:
        resolve_model_dir(
            "medium", state_root=state_root, fetch=refusing_fetch
        )
    assert calls == ["config.json.part"], (
        "the marker-without-model.bin debris short-circuited the "
        "resolver (stranded the confusing engine-construction failure)"
    )
    assert "forced by the G2 pin" in str(excinfo.value)


def test_marker_with_model_bin_keeps_short_circuiting(tmp_path, engine_on):
    """The F-11 complement (non-regression): a marker WITH model.bin is
    the honest complete cache — resolution still short-circuits with
    zero fetch work."""
    state_root = tmp_path / "state"
    model_dir = _model_dir(state_root)
    model_dir.mkdir(parents=True)
    (model_dir / ".complete").write_text("medium\n0\n", encoding="utf-8")
    (model_dir / "model.bin").write_bytes(b"stub-model-bin")

    def exploding_fetch(url, target_path):
        raise AssertionError("the marker-present path must not fetch")

    resolved = resolve_model_dir(
        "medium", state_root=state_root, fetch=exploding_fetch
    )
    assert resolved == str(model_dir)