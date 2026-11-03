"""v0.4.15.2 CUDA fallback tests (bob-design-v04152-cuda-fallback.md, red-first).

The contract: a CUDA device decision (the resolver/probe says cuda) must
never detonate the job when the CUDA runtime proves hostile — cuBLAS/
cuDNN library rot, OOM on construction or on the first decode, an
explicit ``--device cuda`` pin on a cuda-less box.
``make_whisper_engine_with_fallback`` wraps construction in an honest,
logged, one-way fallback to CPU (int8) with the exception type in the
note; the probe (``resolve_transcription_device``) is untouched.

NAMED DEVIATION from the design's policy table (row 3), accepted by the
implementation agent because the design's own fallback-on-`cpu`-from-pin
arm mandates it and the wrapper cannot see whether the resolver took the
``requested in ("cuda", "cpu")`` early return: a cpu RESOLUTION retries
the construction once on device="cpu" (covers an unmet cuda pin handed
back verbatim mid-migration). Only an explicitly REQUESTED cpu pin
refuses to retry (table row 4 verbatim — nothing below CPU, a note
would be a lie). Rows 1, 2, 4 and all honesty surfaces are doc-verbatim.

Seam rule (the module's own): the real ``WhisperEngine`` / faster-whisper
is never imported or constructed here; the injectable ``ctor=`` seam
stands in for the constructor, exactly like the fake engines in
test_v0414_mode_a_seam.py. The suite-wide engine-off env guard is lifted
only where the wrapper's own guard contract is under test. The oneway
discipline is pinned: a mid-run decode failure after successful CUDA
decodes propagates — retries are for environment guesses at the seam,
not for real faults that already surfaced inside a healthy phase.
"""

from __future__ import annotations

import wave
import builtins

import pytest

from vidlift.transcribe import (
    TranscriptSegment,
    TranscriptionError,
    WordStamp,
    make_whisper_engine_with_fallback,
    resolve_transcription_device,
)
from tests._helpers import make_wrapper


# ---- the fake engine the wrapper produces -----------------------------------


class _StubEngine:
    """Shape-compatible stand-in for whatever engine the ctor built."""

    def __init__(self, model_path: str, *, device: str = "auto",
                 cpu_threads: int = 4, vad_on: bool = True) -> None:
        self.device = device
        self.model_path = model_path
        self.calls: list[str] = []

    def detect_language(self, audio_path, *, offset_seconds=0.0):
        from vidlift.transcribe import LanguageGuess

        return LanguageGuess(code="en", confidence=0.9)

    def transcribe_segments(self, audio_path, *, language, on_segment=None):
        self.calls.append(language)
        segs = [
            TranscriptSegment(
                start=0.5,
                end=1.5,
                text=" Hello there.",
                words=[WordStamp(0.5, 1.0, "Hello", 0.95),
                       WordStamp(1.1, 1.5, "there.", 0.95)],
            ),
        ]
        for s in segs:
            if on_segment is not None:
                on_segment(s)
        return segs


def _engine_on(monkeypatch) -> None:
    # The wrapper consults the engine-off guard (make_whisper_engine's
    # contract); every test here injects ctor=, but the guard contract
    # itself is pinned in TestCpuArmRefusals via this helper.
    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)


# ---- tier 1: construction fallback ------------------------------------------


class TestTier1Construction:
    def test_auto_cuda_ctor_failure_falls_back_to_cpu(
        self, monkeypatch, capsys
    ):
        """Design red-first 1: the probe resolved cuda, the ctor explodes
        -> a cpu engine is built from the SAME model path (int8 row via
        device="cpu"), the note fired once, the WARNING logged honestly."""
        _engine_on(monkeypatch)
        built: list[tuple[str, str]] = []

        def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
            built.append((device, model_path))
            if device == "cuda":
                raise RuntimeError(
                    "Library cublas could not be loaded (win cudnn rot)"
                )
            return _StubEngine(model_path, device=device)

        notes: list[str] = []
        engine = make_wrapper(
            "m",
            device="auto",
            resolved_device="cuda",
            cpu_threads=3,
            vad_on=True,
            on_fallback=notes.append,
            ctor=ctor,
        )
        assert engine.device == "cpu"
        assert built == [("cuda", "m"), ("cpu", "m")]  # one attempt per device
        assert notes == ["cuda unavailable — fell back to cpu (RuntimeError)"]
        out = capsys.readouterr().err
        assert out.startswith("WARNING: CUDA") or out.startswith(
            "WARNING: [vidlift.transcribe]"
        )
        assert "falling back to CPU (int8)" in out
        assert "Library cublas could not be loaded" in out


class TestTier1Wording:
    def test_explicit_cuda_pin_failure_wording_names_the_pin(
        self, monkeypatch, capsys
    ):
        """Design table row 2: an explicit cuda pin that fails falls back
        too, with wording that says the requested pin failed — the auto
        arm must not claim the user asked for anything."""
        _engine_on(monkeypatch)
        notes: list[str] = []

        def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
            if device == "cuda":
                raise RuntimeError("Library cublas could not be loaded")
            return _StubEngine(model_path, device=device)

        engine = make_wrapper(
            "m",
            device="cuda",
            resolved_device="cuda",
            on_fallback=notes.append,
            ctor=ctor,
        )
        assert engine.device == "cpu"
        assert notes == ["cuda unavailable — fell back to cpu (RuntimeError)"]
        err = capsys.readouterr().err
        assert "requested --device cuda" in err
        assert "falling back to CPU (int8)" in err

    def test_failure_note_message_shape_is_type_and_message(self, monkeypatch):
        """Design §4: the note's exception segment is '<exc type>' — the
        message text (cuBLAS-vs-OOM forensics) goes to the LOG line, the
        type to the manifest-sized note."""
        _engine_on(monkeypatch)
        notes: list[str] = []

        def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
            if device == "cuda":
                raise OSError("dnn error: bad cu file")
            return _StubEngine(model_path, device=device)

        make_wrapper("m", resolved_device="cuda", on_fallback=notes.append, ctor=ctor)
        assert notes == ["cuda unavailable — fell back to cpu (OSError)"]

    def test_cuda_success_makes_no_note_and_no_log(self, monkeypatch, capsys):
        """CUDA that WORKS is untouched: no callback, no stderr noise."""
        _engine_on(monkeypatch)
        notes: list[str] = []
        engine = make_wrapper(
            "m", resolved_device="cuda", on_fallback=notes.append, ctor=_StubEngine
        )
        assert engine.device == "cuda"
        assert notes == []
        assert capsys.readouterr().err == ""

    def test_probe_cpu_arm_retries_once_on_cpu(self, monkeypatch):
        """NAMED DEVIATION (file docstring): a cpu RESOLUTION (probe or
        unmet cuda pin) retries construction once on device="cpu". An
        explicit cuda pin handed back verbatim (unmet pin) also lands
        here and is rescued; the wrapper keys the CUDA wording on the
        requested pin."""
        _engine_on(monkeypatch)
        built: list[str] = []

        def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
            built.append(device)
            if len(built) == 1:
                raise RuntimeError("transient constructor rot")
            return _StubEngine(model_path, device=device)

        engine = make_wrapper(
            "m", device="auto", resolved_device="cpu", ctor=ctor
        )
        assert engine.device == "cpu"
        assert built == ["cpu", "cpu"]


class TestCpuArmRefusals:
    def test_explicit_cpu_pin_ctor_failure_raises_no_retry(self, monkeypatch):
        """Design table row 4 verbatim: cpu requested, cpu resolution ->
        raise as today; nothing below CPU. No note: a fallback note
        without a fallback would be dishonest."""
        _engine_on(monkeypatch)
        built: list[str] = []

        def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
            built.append(device)
            raise RuntimeError("cpu is broken too")

        notes: list[str] = []
        with pytest.raises(RuntimeError, match="cpu is broken too"):
            make_wrapper(
                "m", device="cpu", on_fallback=notes.append, ctor=ctor
            )
        assert built == ["cpu"]
        assert notes == []

    def test_cpu_retry_failing_raises_the_cpu_error(self, monkeypatch):
        """Tier 1 does not loop: the cpu retry failing raises honestly
        (the cpu path is the last resort; its failure is the real error);
        the fallback note still rode the run because the CUDA attempt
        DID fall back — the disposition names what happened."""
        _engine_on(monkeypatch)
        built: list[str] = []

        def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
            built.append(device)
            if device == "cuda":
                raise RuntimeError("Library cublas could not be loaded")
            raise RuntimeError("cpu is missing too")

        notes: list[str] = []
        with pytest.raises(RuntimeError, match="cpu is missing too"):
            make_wrapper(
                "m", resolved_device="cuda", on_fallback=notes.append, ctor=ctor
            )
        assert built == ["cuda", "cpu"]

    def test_engine_off_env_still_names_the_guard(self, monkeypatch):
        """The engine-off guard contract is preserved by the wrapper's
        default ctor path: VIDLIFT_TRANSCRIPTION_ENGINE=0 -> the same
        TranscriptionError make_whisper_engine raises."""
        monkeypatch.setenv("VIDLIFT_TRANSCRIPTION_ENGINE", "0")
        with pytest.raises(TranscriptionError, match="engine disabled by env"):
            make_wrapper("m", device="auto", resolved_device="cpu")

    def test_faster_whisper_import_error_still_honest(self, monkeypatch):
        """ImportError (the package is missing) keeps make_whisper_engine's
        named TranscriptionError on BOTH tiers — a missing package is not
        an environment guess; there is no cpu engine without faster-whisper."""
        _engine_on(monkeypatch)
        real_import = builtins.__import__

        def _no_faster_whisper(name, *a, **kw):
            if name == "faster_whisper":
                raise ImportError("No module named 'faster_whisper'")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _no_faster_whisper)
        with pytest.raises(
            TranscriptionError, match="faster-whisper is not installed"
        ):
            make_wrapper("m", device="auto", resolved_device="cpu")


# ---- tier 2: first-decode fallback ------------------------------------------


class _GpuHostileEngine:
    """Construction SUCCEEDS on cuda; the first transcribe_segments call
    raises DURING LAZY ITERATION (model.transcribe returns a generator —
    the first CUDA OOM surfaces on the first pull, not on the call).
    Shape-compatible with the engine interface."""

    def __init__(self, model_path: str, *, device: str = "auto",
                 cpu_threads: int = 4, vad_on: bool = True) -> None:
        self.device = device
        self.model_path = model_path
        self.calls: list[str] = []
        self.detected: list[str] = []

    def detect_language(self, audio_path, *, offset_seconds=0.0):
        from vidlift.transcribe import LanguageGuess

        self.detected.append(audio_path)
        return LanguageGuess(code="en", confidence=0.9)

    def transcribe_segments(self, audio_path, *, language, on_segment=None):
        self.calls.append(language)
        raise RuntimeError("CUDA error: out of memory on the first pull")


class _CpuGoodEngine(_StubEngine):
    pass


class TestTier2FirstDecode:
    def test_first_decode_oom_rebuilds_cpu_and_retries(
        self, monkeypatch, capsys
    ):
        """Design red-first 3: ctor succeeds on cuda; the FIRST decode
        call fails during lazy iteration -> the cuda engine is disposed
        (the wrapper's dispose hook), a cpu engine is built, THAT call is
        retried once, and real segments return."""
        _engine_on(monkeypatch)
        built: list[str] = []
        disposed: list[str] = []
        engines: list = []

        def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
            if device == "cuda" and not built:
                built.append(device)
                eng = _GpuHostileEngine(model_path, device=device)
                engines.append(eng)
                return eng
            built.append(device)
            eng = _CpuGoodEngine(model_path, device=device)
            engines.append(eng)
            return eng

        notes: list[str] = []
        engine = make_wrapper(
            "m",
            resolved_device="cuda",
            on_fallback=notes.append,
            ctor=ctor,
            on_dispose=lambda device_path: disposed.append(device_path),
        )
        # construction succeeded on cuda: honest so far
        assert engine.device == "cuda"
        assert notes == []
        assert disposed == []

        segs = engine.transcribe_segments("wav", language="en")
        assert [s.text for s in segs] == [" Hello there."]
        assert built == ["cuda", "cpu"]  # the cpu retry is a NEW engine
        assert len(disposed) == 1  # the hostile cuda engine was disposed
        # the RETRIED call went to the cpu engine with the SAME arguments
        assert engines[0].calls == ["en"]
        assert engines[1].calls == ["en"]
        assert notes == ["cuda unavailable — fell back to cpu (RuntimeError)"]
        err = capsys.readouterr().err
        assert "falling back to CPU (int8)" in err
        # tier 2 names the phase (construction vs decode forensics)
        assert "first decode" in err or "decode" in err

    def test_healthy_cuda_second_call_untouched(self, monkeypatch, capsys):
        """Design red-first 4: a decode failure on call >=2 propagates —
        no rebuild, no note, no retry. A mid-run CUDA failure after
        successful decode is a real fault, not an environment guess."""
        _engine_on(monkeypatch)
        attempts: list[int] = []

        class HealthyCuda(_StubEngine):
            def transcribe_segments(self, audio_path, *, language, on_segment=None):
                attempts.append(1)
                if len(attempts) >= 2:
                    raise RuntimeError("mid-run cuda decode fault")
                return super().transcribe_segments(
                    audio_path, language=language, on_segment=on_segment
                )

        engine = make_wrapper(
            "m", resolved_device="cuda", ctor=lambda mp, *, device="auto", cpu_threads=4, vad_on=True: HealthyCuda(mp, device=device)
        )
        first = engine.transcribe_segments("wav", language="en")
        assert [s.text for s in first] == [" Hello there."]
        with pytest.raises(RuntimeError, match="mid-run cuda decode fault"):
            engine.transcribe_segments("wav", language="en")
        assert len(attempts) == 2
        assert engine.device == "cuda"  # no engine was ever rebuilt
        assert capsys.readouterr().err == ""

    def test_cpu_engine_is_plain_after_probe_cpu_resolution(
        self, monkeypatch, capsys
    ):
        """Design scoping: tier 2 is keyed to CUDA-built engines ONLY
        (the device decision). A probe-cpu engine is handed back as a
        plain engine; its first decode failure RAISES as today (no
        rebuild outside the device decision, no note, no stderr noise)."""
        _engine_on(monkeypatch)
        tries: list[int] = []

        class FlakyCpu(_StubEngine):
            def transcribe_segments(self, audio_path, *, language, on_segment=None):
                tries.append(1)
                if len(tries) == 1:
                    raise RuntimeError("spurious decode failure")
                return super().transcribe_segments(
                    audio_path, language=language, on_segment=on_segment
                )

        def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
            return FlakyCpu(model_path, device=device)

        engine = make_wrapper(
            "m", device="auto", resolved_device="cpu", ctor=ctor
        )
        assert type(engine) is FlakyCpu  # no tier-2 rider on probe-cpu
        with pytest.raises(RuntimeError, match="spurious decode failure"):
            engine.transcribe_segments("wav", language="en")
        assert tries == [1]
        assert capsys.readouterr().err == ""

    def test_explicit_cuda_pin_gets_tier2_rider(self, monkeypatch, capsys):
        """The cuda PIN arm (requested cuda, resolved cuda) rides the
        healthy-cuda shape: construction succeeds -> tier-2 observed
        engine; a first-decode CUDA failure rebuilds on cpu with the
        requested-pin wording on the stderr forensics."""
        _engine_on(monkeypatch)
        built: list[str] = []

        def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
            built.append(device)
            if device == "cuda":
                eng = _GpuHostileEngine(model_path, device=device)
                return eng
            return _CpuGoodEngine(model_path, device=device)

        notes: list[str] = []
        engine = make_wrapper(
            "m", device="cuda", resolved_device="cuda",
            on_fallback=notes.append, ctor=ctor,
        )
        assert engine.device == "cuda"
        segs = engine.transcribe_segments("wav", language="en")
        assert [s.text for s in segs] == [" Hello there."]
        assert built == ["cuda", "cpu"]
        assert notes == ["cuda unavailable — fell back to cpu (RuntimeError)"]
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "falling back to CPU (int8)" in err


# ---- run.py seam wiring (design red-first 6) --------------------------------


def _silence_wav(tmp_path):
    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)  # 100 ms silence; hold-cap guard ok
    return wav


def _wav_creating_runner(wav_path):
    """A runner stub that mimics the ffmpeg extractor's effect (creates
    the 16k mono wav at the target path), returning success."""
    def _run(binary, args):
        wav = str(wav_path)
        if args and args[-1] == wav and not __import__("os").path.exists(wav):
            dir_of = __import__("os").path.dirname(wav)
            if dir_of:
                __import__("os").makedirs(dir_of, exist_ok=True)
            with wave.open(wav, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(b"\x00\x00" * 1600)
        return (0, "", "")
    return _run


class TestCoreFactoryWiring:
    def test_core_default_factory_is_the_fallback_wrapper(
        self, monkeypatch, tmp_path
    ):
        """Design red-first 6: when transcribe_source_to_srt is called
        WITHOUT engine_factory, its default factory must be the fallback
        wrapper (device=device_pin). A ctor that fails ONLY on cuda,
        under a resolver forced to 'cuda', proves the default path
        wraps: the run completes on the cpu engine and the fallback note
        rides the result (Mode A honesty surfaces) or the exception
        (Mode B — the core raises through)."""
        _engine_on(monkeypatch)
        built: list[str] = []

        def ctor(model_path, *, device="auto", cpu_threads=4, vad_on=True):
            built.append(device)
            if device == "cuda":
                raise RuntimeError("Library cublas could not be loaded")
            return _StubEngine(model_path, device=device)

        monkeypatch.setattr(
            "vidlift.transcribe.make_whisper_engine", ctor
        )
        import vidlift.transcribe as transcribe_mod

        monkeypatch.setattr(
            transcribe_mod, "resolve_model_dir", lambda pin, **kw: "m"
        )
        monkeypatch.setattr(
            transcribe_mod, "resolve_transcription_device", lambda pin: "cuda"
        )
        wav = _silence_wav(tmp_path)
        result = transcribe_mod.transcribe_source_to_srt(
            str(wav),
            output_path=str(tmp_path / "out.mp4"),
            workdir=str(tmp_path / "work"),
            ffmpeg="ffmpeg",
            runner=_wav_creating_runner(tmp_path / "work" / "source-audio-16k.wav"),
            device_pin="auto",
        )
        assert built == ["cuda", "cpu"]
        assert result.notes == ["cuda unavailable — fell back to cpu (RuntimeError)"]
        assert result.cue_count >= 1
        assert result.model == "m"

    def test_injected_engine_factory_still_wins(self, monkeypatch, tmp_path):
        """The seam rule: an injected engine_factory is used VERBATIM, no
        fallback wrapping happens around it (the Mode A/B suites' stub
        engines must never be silently wrapped), and NO fallback note is
        fabricated."""
        engine = _StubEngine("x", device="stub")
        monkeypatch.setenv("VIDLIFT_TRANSCRIPTION_ENGINE", "1")  # irrelevant here
        import vidlift.transcribe as transcribe_mod

        monkeypatch.setattr(
            transcribe_mod, "resolve_model_dir", lambda pin, **kw: "m"
        )
        monkeypatch.setattr(
            transcribe_mod, "resolve_transcription_device", lambda pin: "cuda"
        )
        wav = _silence_wav(tmp_path)
        result = transcribe_mod.transcribe_source_to_srt(
            str(wav),
            output_path=str(tmp_path / "out.mp4"),
            workdir=str(tmp_path / "work"),
            ffmpeg="ffmpeg",
            runner=_wav_creating_runner(tmp_path / "work" / "source-audio-16k.wav"),
            engine_factory=lambda model_dir: engine,
            device_pin="cuda",
        )
        assert result.cue_count >= 1
        assert not any("cuda unavailable" in n for n in result.notes)


# ---- probe semantics untouched ----------------------------------------------


class TestProbeUntouched:
    def test_unknown_pin_still_raises(self):
        with pytest.raises(TranscriptionError, match="unknown transcription device"):
            resolve_transcription_device("tpu")

    def test_explicit_pins_pass_through(self):
        assert resolve_transcription_device("cpu") == "cpu"
        assert resolve_transcription_device("cuda") == "cuda"

    def test_untouched_probe_reports_what_the_box_claims(self, monkeypatch):
        """The probe is the CLAIM, the fallback reports the DID: forcing
        the ctranslate2 probe to 'see' devices keeps returning cuda —
        the wrapper is the safety net, not a changed probe."""
        import sys
        import types

        fake_ct2 = types.ModuleType("ctranslate2")
        fake_ct2.get_cuda_device_count = lambda: 2
        monkeypatch.setitem(sys.modules, "ctranslate2", fake_ct2)
        assert resolve_transcription_device("auto") == "cuda"