"""Speech transcription to SRT subtitles for VidLift (v0.4.16).

The v0.4.14 design §"Transcription core": one shared transcription core
under Modes A (upscale fallback) and B (standalone subs). faster-whisper
(CTranslate2, in-process) is the engine; the module mirrors
``subtitles.py``'s fail-open, explicit-resolution discipline:

- Every model download goes through the injectable ``fetch`` seam; the
  model cache is machine-shared under the state dir and the model.bin
  is sha256-pinned (Systran/faster-whisper published LFS hashes), so a
  truncated or tampered download can never load.
- Every transcription run goes behind the ``Engine`` interface seam;
  unit tests drive deterministic stubs, NEVER the real model, NEVER a
  download, NEVER a GPU.
- Sync discipline (the load-bearing requirement): VAD prefilter (Silero,
  bundled in faster-whisper) + word-level timestamps + the alignment
  verification pass — a cue whose aligned midpoint lands >250 ms from
  the whisper midpoint is rebuilt on aligned positions; cues with
  alignment confidence < ALIGN_CONFIDENCE_DROP are dropped (music-region
  garbage signature). Hallucination suppressors (no_speech 0.6,
  compression_ratio 2.4) ride the engine call.
- Language: per-file auto-detect with a config pin override. Detected
  language + confidence are REPORTED; below-confidence warns and
  proceeds with the top guess (fail-open discipline).
- Translation to English is a NON-GOAL (design §Language): there is no
  code path for it — no ``task`` parameter exists on this module, so
  the feature is rejected structurally, not by flag.

The KEEP contract: ``transcribe_sidecar_srt_path`` writes
``{output-stem}.srt`` next to the finished file in BOTH modes, exactly
like ``subtitles.sidecar_srt_path``.

CPU fallback: ``device="cpu"`` with int8 compute (config pin
``transcription/device`` values auto|cuda|cpu) runs ~1x realtime —
acceptable for Mode B batch use and documented, not deeply optimized.
v0.4.15.2 adds the HONEST CUDA fallback (
``make_whisper_engine_with_fallback``): a CUDA runtime that proves
hostile only after the probe (library rot, OOM at construction or on
the first decode) falls back to that CPU row with a stderr WARNING and
a manifest note — never silently, never twice mid-run.
"""

from __future__ import annotations

import array
import contextlib
import hashlib
import os
import re
import sys
import threading
import time
import wave
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from hashlib import sha256 as _sha256
from pathlib import Path

from .config import TRANSCRIPTION_ENABLED_DEFAULT
from .srt import Cue, normalize_cues

__all__ = [
    "ALIGNED_MIDPOINT_REBUILD_MS",
    "ALIGN_CONFIDENCE_DROP",
    "DEFAULT_MODEL",
    "KNOWN_MODELS",
    "MIN_DOWNLOAD_SIZE",
    "MODEL_SHA256",
    "MODEL_LOCK_STALE_SECONDS",
    "TRANSCRIPTION_ENABLED_DEFAULT",
    "TranscribeEngine",
    "TranscriptionError",
    "TranscriptionResult",
    "WhisperEngine",
    "ModelInstallBoard",
    "align_verify_cues",
    "CUE_LEAD_IN_MS",
    "CUE_LEAD_OUT_MS",
    "detect_language",
    "make_whisper_engine",
    "make_whisper_engine_with_fallback",
    "model_cache_dir",
    "model_download_url_for",
    "model_install_state",
    "resolve_transcription_device",
    "resolve_model_dir",
    "segments_to_cues",
    "SINGLE_WORD_HOLD_CAP_MS",
    "HOLD_QUIET_RMS",
    "HOLD_MEASURE_WINDOW_S",
    "HOLD_MIN_KEEP_MS",
    "SEGMENT_NO_SPEECH_DROP",
    "transcribe_sidecar_srt_path",
    "transcribe_source_to_srt",
    "WORD_PROB_FLOOR",
]


class TranscriptionError(RuntimeError):
    """Transcription failed.

    Mode B (standalone subs) fails the JOB on this — transcription is
    its whole point. Mode A catches it and fails OPEN exactly like the
    OCR path (the upscale ships without subtitles, the warning names
    the failure; an upscale must never fail because subtitles could
    not be made).
    """


# ---- engine seam ------------------------------------------------------------
#
# The boundary the test plan pins: ALL unit tests run against stubs of
# this interface; the real faster-whisper import lives ONLY inside
# ``make_whisper_engine`` (a factory seam, mirroring plan.attach_estimate's
# import-deferral discipline). The interface is the minimal surface the
# core consumes: language detection and one segment-yielding transcribe.


@dataclass
class TranscriptSegment:
    """One engine-emitted segment; times are source-timeline seconds."""

    start: float
    end: float
    text: str
    words: list["WordStamp"] = field(default_factory=list)
    no_speech_prob: float = 0.0


@dataclass
class WordStamp:
    """One word-level timestamp (CT2 token alignment)."""

    start: float | None
    end: float | None
    word: str
    probability: float = 1.0


@dataclass
class LanguageGuess:
    """What language detection concluded (reported, never trusted)."""

    code: str  # ISO 639-1 as faster-whisper reports it ("" when unknown)
    confidence: float


class TranscribeEngine:
    """The engine interface (see module docstring for the seam rule)."""

    def detect_language(
        self, audio_path: str, *, offset_seconds: float = 0.0
    ) -> LanguageGuess:
        raise NotImplementedError

    def transcribe_segments(
        self,
        audio_path: str,
        *,
        language: str,
        on_segment: Callable[[TranscriptSegment], None] | None = None,
    ) -> list[TranscriptSegment]:
        """Transcribe ``audio_path`` (16 kHz mono wav) and return the
        segments (also delivered incrementally through ``on_segment`` so
        the caller can cancel between segments and publish progress)."""
        raise NotImplementedError


# The real engine: constructed ONLY through make_whisper_engine, so the
# test suite (which never calls the factory) never imports the package.


class WhisperEngine(TranscribeEngine):
    """faster-whisper adapter (WhisperModel per engine instance).

    The constructor signature IS the honest config surface: model tag or
    explicit path, device, compute type. The design's hallucination
    suppressors and VAD/word-timestamp discipline are pinned HERE, in
    the one place the real call lives.
    """

    def __init__(
        self,
        model_path: str,
        *,
        device: str = "auto",
        cpu_threads: int = 4,
        vad_on: bool = True,
    ) -> None:
        from faster_whisper import WhisperModel  # deferred: test seam rule

        self.vad_on = vad_on
        compute = "int8_float16" if device == "cuda" else "int8"
        self.model = WhisperModel(
            model_path,
            device=device,
            compute_type=compute,
            cpu_threads=cpu_threads,
        )

    def detect_language(
        self, audio_path: str, *, offset_seconds: float = 0.0
    ) -> LanguageGuess:
        audio = _decode_audio_prefix(
            audio_path, offset_seconds=offset_seconds
        )
        lang, confidence, _all_probs = self.model.detect_language(audio)
        return LanguageGuess(code=lang or "", confidence=float(confidence))

    def transcribe_segments(
        self,
        audio_path: str,
        *,
        language: str,
        on_segment: Callable[[TranscriptSegment], None] | None = None,
    ) -> list[TranscriptSegment]:
        segments, _info = self.model.transcribe(
            audio_path,
            language=language or None,  # None = auto-detect per file
            vad_filter=self.vad_on,
            vad_parameters={"min_silence_duration_ms": 300},
            word_timestamps=True,
            # Hallucination suppressors (design §Sync quality): faster-
            # whisper's built-in gates. NOTE: deliberately NO ``task``
            # parameter — translation is a structural non-goal.
            no_speech_threshold=0.6,
            compression_ratio_threshold=2.4,
            condition_on_previous_text=False,
        )
        out: list[TranscriptSegment] = []
        for seg in segments:
            words = [
                WordStamp(
                    start=(float(w.start) if w.start is not None else None),
                    end=(float(w.end) if w.end is not None else None),
                    word=str(w.word),
                    probability=float(w.probability),
                )
                for w in (getattr(seg, "words", None) or [])
            ]
            segment = TranscriptSegment(
                start=float(seg.start),
                end=float(seg.end),
                text=str(seg.text).strip(),
                words=words,
                no_speech_prob=float(
                    getattr(seg, "no_speech_prob", 0.0) or 0.0
                ),
            )
            out.append(segment)
            if on_segment is not None:
                on_segment(segment)
        return out


# ---- language-detect audio window (v0.4.15.4, F-10) --------------------------
#
# The docstring's promised bound, now enforced: faster-whisper's
# detect consumes ~30 s of features from the wav; decoding the WHOLE
# file spent ~1 GB of RAM on a 4 h source for a 30 s answer. 120 s is
# ample headroom, never a quality gate. (The constant lives HERE, ahead
# of its only consumer — the helper below binds it as a def-time
# default — and is cross-referenced beside LANGUAGE_CONFIDENCE_FLOOR in
# the language section, where the discipline constants cluster; the
# language section owns the confidence docs, this section owns the
# decode-window math.)
DETECT_AUDIO_SECONDS = 120


def _decode_audio_prefix(
    audio_path: str,
    *,
    offset_seconds: float = 0.0,
    limit_seconds: float = DETECT_AUDIO_SECONDS,
    sampling_rate: int = 16000,
):
    """Bounded memory twin of faster_whisper.audio.decode_audio.

    v0.4.15.4 (F-10): decode ONLY [offset, offset+limit) — a 4h source
    must not spend ~1 GB of RAM for a 30s language answer. Mirrors
    faster-whisper's resample chain (s16 mono @16k -> float32/32768)
    with sample-precise early stop. Returns an np.float32 mono array.
    Version-drift watch: the mirrored source is
    ``faster_whisper.audio.decode_audio`` (faster-whisper >=1.2, av
    14.2.0 — pyproject pins ``av<15``); the accounting behavior is
    pinned in tests, so an av upgrade that changes the chain fails
    there instead of silently drifting.
    """
    import av
    import numpy as np

    resampler = av.audio.resampler.AudioResampler(
        format="s16", layout="mono", rate=sampling_rate
    )
    start = int(offset_seconds * sampling_rate)
    stop = start + int(limit_seconds * sampling_rate)
    pcm = np.empty(0, dtype=np.int16)  # accumulated s16 mono samples
    done = False
    with av.open(audio_path, mode="r", metadata_errors="ignore") as container:
        for frame in container.decode(audio=0):
            for rframe in resampler.resample(frame):
                block = np.asarray(rframe.to_ndarray()).reshape(-1)
                pcm = np.concatenate([pcm, block.astype(np.int16, copy=False)])
                if pcm.size >= stop:
                    done = True
                    break
            if done:
                break
    if not done:  # flush the resampler tail on short files
        for rframe in resampler.resample(None):
            block = np.asarray(rframe.to_ndarray()).reshape(-1)
            pcm = np.concatenate([pcm, block.astype(np.int16, copy=False)])
    audio = pcm[start:stop].astype(np.float32) / 32768.0
    return audio


def _engine_disabled_by_env() -> bool:
    """The suite-wide engine-off guard (tests/conftest.py): production
    never sets this variable. Every transcription entrypoint honors it
    — no model resolution, no network, no cache, no faster_whisper
    import — while the caller still gets the honest fail-open."""
    return os.environ.get("VIDLIFT_TRANSCRIPTION_ENGINE", "") == "0"


def make_whisper_engine(
    model_path: str,
    *,
    device: str = "auto",
    cpu_threads: int = 4,
    vad_on: bool = True,
) -> TranscribeEngine:
    """The engine factory seam (the ONLY import site of faster_whisper).

    Tests stub the engine and never call this; production callers get
    the real adapter. An ImportError here names the package honestly
    (Mode A degrades open on it; Mode B fails the job with the same
    message). Honors VIDLIFT_TRANSCRIPTION_ENGINE=0 (engine off).
    """
    if _engine_disabled_by_env():
        raise TranscriptionError(
            "transcription engine disabled by env "
            "(VIDLIFT_TRANSCRIPTION_ENGINE=0)"
        )
    try:
        return WhisperEngine(
            model_path, device=device, cpu_threads=cpu_threads, vad_on=vad_on
        )
    except ImportError as exc:
        raise TranscriptionError(
            "faster-whisper is not installed (vidlift's transcription "
            f"engine package): {exc}"
        ) from exc


# ---- v0.4.15.2: the honest CUDA fallback factory ------------------------------
#
# The probe (resolve_transcription_device) is a CLAIM: "this box has a
# CUDA device the driver exposes". Three failure classes slip past that
# claim and only detonate later, inside the GPU phase lock, after expensive
# phases may already have run: library rot (cuBLAS/cuDNN mismatch,
# incomplete PyInstaller payload), OOM at construction or on the first
# decode (VRAM bookkeeping is deliberately not part of the phase lock),
# and an explicit cuda pin on a box with no usable runtime. The honesty
# rule: never a dead job when a working CPU path exists — never silently,
# either. The fallback is ONE-WAY, logged to stderr, and carries the
# exception type in the manifest note. A mid-run decode failure after
# successful CUDA decodes propagates: that is a real fault inside a
# proven-healthy device, not an environment guess at a seam.


def _describe_exception(exc: BaseException) -> str:
    """The manifest-sized forensics: the concrete exception TYPE name.
    The full message goes to the log line, where there is room for it;
    the note keeps the type so the cuBLAS-vs-OOM question is answerable
    from the manifest without code branches."""
    return type(exc).__name__


_CUDA_FALLBACK_NOTE_FMT = "cuda unavailable — fell back to cpu ({})"
# D1 honesty (Karen v0.4.15.2 validation): a cpu RESOLUTION that
# needed the construction retry never claims a CUDA event — the
# note names what actually happened on a box that may have no CUDA
# at all. "{0}" keeps a literal "{...}" in the text safe.
_CPU_RETRY_NOTE_FMT = "construction failed on cpu ({0}); retry succeeded."


def make_whisper_engine_with_fallback(
    model_path: str,
    *,
    device: str = "auto",
    resolved_device: str | None = None,
    cpu_threads: int = 4,
    vad_on: bool = True,
    on_fallback: Callable[[str], None] | None = None,
    on_dispose: Callable[[str], None] | None = None,
    ctor: Callable[..., TranscribeEngine] | None = None,
) -> TranscribeEngine:
    """The v0.4.15.2 resilient engine factory (tier 1 + tier 2).

    Builds the engine through ``ctor`` (default: :func:`make_whisper_engine`,
    the ONLY faster_whisper import site). Fallback policy (one shot, one
    way, never silent):

    - Construction on a CUDA resolution fails (broad catch: CTranslate2
      raises a zoo of RuntimeError/OSError/cuda-specific types) -> exactly
      one ``device="cpu"`` rebuild from the SAME model path; stderr
      WARNING + manifest note; the returned engine still carries
      tier-2 protection for its first decode.
    - Construction on a ``cpu`` RESOLUTION (probe-cpu, or an explicit
      cuda pin the resolver had to hand back verbatim — cuda-less box,
      migration in progress) fails -> the same one ``device="cpu"``
      retry. The stderr line names ``--device cuda --device`` wording
      ONLY when the caller actually requested cuda; the auto arm gets
      the neutral wording.
    - ``device == "cpu"`` (explicit pin) and construction raises ->
      raise through as today. Nothing below CPU; a note without a
      fallback would be dishonest.
    - The cpu retry failing -> its exception raises (the cpu path is the
      last resort; its failure is the real error).
    - Tier 2 (CUDA-built engines only — the design scopes the first-decode
      rescue to the device decision, and widening without field evidence
      is guessing): the first ``detect_language`` / ``transcribe_segments``
      call failing -> the CUDA model is released, one cpu engine is built
      through the same seam, and the SAME call is re-run once. After the
      first success, failures propagate unchanged.
    - :class:`TranscriptionError` and ``ImportError`` from the engine
      package (the guard, the missing package) are NOT device guesses —
      they raise through on every arm.

    Note shape (design §4): ``cuda unavailable — fell back to cpu
    (<exc type>)``; the stderr forensics carry ``<type>: <message>``.

    ``resolved_device`` is the caller's device RESOLUTION for this job
    (run.py's transcribe_device); when omitted it defaults to
    ``device`` — callers that already resolved should always pass it.
    ``on_fallback`` receives the manifest note (the caller owns where it
    rides, e.g. run.py's transcribe_engine_note); ``on_dispose`` records
    the engine replacement (the caller's bookkeeping seam).
    """
    if _engine_disabled_by_env():
        raise TranscriptionError(
            "transcription engine disabled by env "
            "(VIDLIFT_TRANSCRIPTION_ENGINE=0)"
        )

    requested = device if device else "auto"
    resolved = resolved_device if resolved_device else requested
    build = ctor if ctor is not None else make_whisper_engine

    def _build(
        model_path_arg: str, *, device: str | None = None, **_ignored
    ) -> TranscribeEngine:
        try:
            return build(
                model_path_arg,
                device=(device if device is not None else resolved),
                cpu_threads=cpu_threads,
                vad_on=vad_on,
            )
        except ImportError as exc:
            raise TranscriptionError(
                "faster-whisper is not installed (vidlift's transcription "
                f"engine package): {exc}"
            ) from exc

    try:
        engine = _build(model_path, device=resolved)
    except TranscriptionError:
        # honest refusals (the guard, the missing package) name the
        # condition exactly as make_whisper_engine does — not device
        # guesses, not rescuable by any device.
        raise
    except Exception as exc:  # noqa: BLE001 - the deliberate broad catch
        if requested == "cpu":
            # design table row 4: the caller pinned the last resort and
            # it failed -> raise as today. No note: none happened.
            raise
        try:
            from .run import EngineCancelled  # lazy: run.py imports us

            engine = _build(model_path, device="cpu")
        except EngineCancelled as cpu_exc:
            # D2 honesty (Karen validation): a cancel during the cpu
            # retry is control flow, not a device guess. The CUDA note
            # stays honest (the CUDA attempt DID fall back), but its
            # forensics name the cancel, never the cpu path's failure.
            if on_fallback is not None:
                on_fallback(
                    _CUDA_FALLBACK_NOTE_FMT.format(
                        type(cpu_exc).__name__
                    )
                )
            raise cpu_exc from exc
        except Exception as cpu_exc:  # noqa: BLE001
            # the cpu LAST RESORT failed: its error is the real one.
            raise cpu_exc from exc
        # D1 honesty (Karen validation): the wording keys on the
        # RESOLUTION (the truth), not the request (the intent) — a box
        # with no CUDA never claims "cuda unavailable" in its
        # forensics. The explicit-pin arm says so only when the
        # resolution actually was cuda.
        if resolved == "cuda":
            note = _CUDA_FALLBACK_NOTE_FMT.format(_describe_exception(exc))
        else:
            note = _CPU_RETRY_NOTE_FMT.format(_describe_exception(exc))
        if on_fallback is not None:
            on_fallback(note)
        if resolved == "cuda":
            if requested == "cuda":
                print(
                    "WARNING: [vidlift.transcribe] requested --device "
                    f"cuda but CUDA failed ({type(exc).__name__}: {exc}); "
                    "falling back to CPU (int8)",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    "WARNING: [vidlift.transcribe] CUDA transcription "
                    f"unavailable ({type(exc).__name__}: {exc}); "
                    "falling back to CPU (int8)",
                    file=sys.stderr,
                    flush=True,
                )
        else:
            print(
                "WARNING: [vidlift.transcribe] engine construction on "
                f"cpu failed ({type(exc).__name__}: {exc}); retrying "
                "once on cpu (int8)",
                file=sys.stderr,
                flush=True,
            )
        if resolved != "cuda":
            # a cpu resolution that needed the retry stays a plain cpu
            # engine (the design scopes tier 2 to CUDA-built engines).
            return engine
        return _FallbackEngine(
            engine,
            model_path=model_path,
            initial_device="cpu",
            ctor=_build,
            cpu_threads=cpu_threads,
            vad_on=vad_on,
            note=note,
            on_fallback=on_fallback,
            on_dispose=on_dispose,
        )

    if resolved == "cuda":
        # the healthy CUDA construction: hand back the tier-2-observed
        # wrapper (the first decode is still unproven).
        return _FallbackEngine(
            engine,
            model_path=model_path,
            initial_device="cuda",
            ctor=_build,
            cpu_threads=cpu_threads,
            vad_on=vad_on,
            note=None,
            on_fallback=on_fallback,
            on_dispose=on_dispose,
        )
    return engine


class _FallbackEngine(TranscribeEngine):
    """Tier-2 rider (v0.4.15.2): wraps a CUDA-built engine until its
    first DECODE proves the device (or executes the ONE cpu
    rebuild). Construction-side fallbacks wrap too; their note is
    already Spoken for, so a post-fallback decode failure re-raises
    (never a second rescue, never a second note) — the design's
    once-per-seam discipline."""

    def __init__(
        self,
        engine: TranscribeEngine,
        *,
        model_path: str,
        initial_device: str,
        ctor: Callable[[str], TranscribeEngine],
        cpu_threads: int = 4,
        vad_on: bool = True,
        note: str | None = None,
        on_fallback: Callable[[str], None] | None = None,
        on_dispose: Callable[[str], None] | None = None,
    ) -> None:
        self._engine = engine
        self._model_path = model_path
        self._ctor = ctor
        self._cpu_threads = cpu_threads
        self._vad_on = vad_on
        self._note = note
        self._on_fallback = on_fallback
        self._on_dispose = on_dispose
        # D3 (Karen validation): the tier-2 gate is the first DECODE
        # (transcribe_segments) ONLY. Language detection is not a decode
        # — in the default flow it runs FIRST and must never consume
        # this gate, or the design's headline case (OOM at the first
        # decode, exactly where the GUI's upscale may hold VRAM) is
        # left unguarded. The detect call keeps its own one-shot arm
        # (a detect-time CUDA failure rebuilds once on cpu, and the
        # first decode below still rides the fresh decode gate on the
        # rebuilt cpu engine).
        self._decode_gate_done = False
        # the detect arm's own one-shot flag (per-method gates; D3):
        self._detect_gate_done = False
        # the note the detect arm's rebuild just earned (spoken to the
        # caller by _rebuild_cpu); the wrapper adopts it as its own
        # _note AFTER the rescue so the rebuilt cpu engine has NO
        # further rescues (the once-per-seam discipline).
        self._pending_note: str | None = None
        # shape-compatibility: the device the caller is currently riding
        # (updated to "cpu" by the tier-2 rebuild).
        self.device = initial_device

    def detect_language(
        self, audio_path: str, *, offset_seconds: float = 0.0
    ) -> LanguageGuess:
        """Language detection keeps its own one-shot rescue (the first
        detect call) but NEVER consumes the decode gate (D3): in the
        default flow detect runs first, and the first transcribe is
        still unproven there. The gate here is the detect call itself,
        not the tier-2 decode seam."""
        if self._detect_gate_done:
            return self._engine.detect_language(
                audio_path, offset_seconds=offset_seconds
            )
        self._detect_gate_done = True
        try:
            return self._engine.detect_language(
                audio_path, offset_seconds=offset_seconds
            )
        except Exception as exc:  # noqa: BLE001 - the deliberate broad catch
            from .run import EngineCancelled  # lazy: run.py imports us

            # D2 (Karen validation): a cancel is control flow, not a
            # device guess — EngineCancelled re-raises before any
            # rebuild/forensics (the gate state does not matter).
            if isinstance(exc, EngineCancelled) or self._note is not None:
                # construction already fell back: the honest no-second-
                # rescue rule -> raise as today.
                raise
            self._rebuild_cpu(exc)
            self._note = self._pending_note or self._note
            return self._engine.detect_language(
                audio_path, offset_seconds=offset_seconds
            )

    def transcribe_segments(
        self,
        audio_path: str,
        *,
        language: str,
        on_segment: Callable[[TranscriptSegment], None] | None = None,
    ) -> list[TranscriptSegment]:
        """First DECODE call: faster-whisper's transcribe returns a
        LAZY generator, so a CUDA OOM surfaces on iteration — the
        engine's own segment loop (vidlift's WhisperEngine adapter
        materializes it) makes the failure land inside this call,
        which is exactly the seam the gate catches. The rebuild
        re-runs the SAME call once on CPU. Language detection does NOT
        consume this gate (D3): the default flow detects first, and
        the first decode is still unproven here."""
        if self._decode_gate_done:
            return self._engine.transcribe_segments(
                audio_path, language=language, on_segment=on_segment
            )
        self._decode_gate_done = True
        try:
            return self._engine.transcribe_segments(
                audio_path, language=language, on_segment=on_segment
            )
        except Exception as exc:  # noqa: BLE001 - the deliberate broad catch
            from .run import EngineCancelled  # lazy: run.py imports us

            # D2 (Karen validation): a cancel is control flow, not a
            # device guess — EngineCancelled re-raises before any
            # rebuild/forensics (the gate state does not matter).
            if isinstance(exc, EngineCancelled) or self._note is not None:
                # construction already fell back: raise as today.
                raise
            self._rebuild_cpu(exc)
            self._note = self._pending_note or self._note
            return self._engine.transcribe_segments(
                audio_path, language=language, on_segment=on_segment
            )

    def _rebuild_cpu(self, exc: Exception) -> None:
        """The ONE cpu rebuild: stderr WARNING (the honest log line with
        <type>: <message> forensics), the note callback, the CUDA model
        release + the caller's dispose hook, then the rebuild through
        the same ctor seam on device="cpu". The rebuild failing raises
        its real error (the cpu path is the last resort)."""
        # D2 (Karen validation): EngineCancelled is control flow, not a
        # device guess — no warning, no note, no rebuild; the callers
        # never reach this seam with a cancel (they re-raise first), so
        # this arm is defensive for direct _rebuild_cpu entry.
        from .run import EngineCancelled  # lazy: run.py imports us

        if isinstance(exc, EngineCancelled):
            raise

        print(
            f"WARNING: [vidlift.transcribe] CUDA transcription failed on "
            f"first decode ({type(exc).__name__}: {exc}); "
            "falling back to CPU (int8)",
            file=sys.stderr,
            flush=True,
        )
        if self._on_fallback is not None:
            self._on_fallback(
                _CUDA_FALLBACK_NOTE_FMT.format(_describe_exception(exc))
            )
        replaced = self._engine
        try:
            if getattr(replaced, "model", None) is not None:
                # release the CTranslate2 model reference (its VRAM)
                replaced.model = None
        except Exception:  # noqa: BLE001 - disposal is best-effort
            pass
        if self._on_dispose is not None:
            try:
                self._on_dispose(
                    f"disposed {type(replaced).__name__} ({self.device})"
                )
            except Exception:  # noqa: BLE001 - the hook never blocks
                pass
        self._engine = self._ctor(
            self._model_path,
            device="cpu",
            cpu_threads=self._cpu_threads,
            vad_on=self._vad_on,
        )
        self._pending_note = _CUDA_FALLBACK_NOTE_FMT.format(
            _describe_exception(exc)
        )
        self.device = "cpu"


# ---- model delivery (first-run download, sha256-pinned) ---------------------

DEFAULT_MODEL = "medium"
KNOWN_MODELS = ("small", "medium", "large-v3")

# Systran/faster-whisper model.bin LFS sha256 pins (fetched from the HF
# API at implementation time; the download is verified against these —
# a corrupted or MITM'd model.bin can never load).
MODEL_SHA256: dict[str, str] = {
    "small": "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
    "medium": "9b45e1009dcc4ab601eff815b61d80e60ce3fd8c74c1a14f4a282258286b51ae",
    "large-v3": "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1",
}

# The non-.bin support files (config/tokenizer/vocabulary) are small
# non-LFS files: pinned by SIZE against the repo listing (fetched at
# implementation time).
MODEL_SUPPORT_FILES: dict[str, tuple[tuple[str, int], ...]] = {
    "small": (
        ("config.json", 2370),
        ("tokenizer.json", 2203239),
        ("vocabulary.txt", 459861),
    ),
    "medium": (
        ("config.json", 2257),
        ("tokenizer.json", 2203239),
        ("vocabulary.txt", 459861),
    ),
    "large-v3": (
        ("config.json", 2394),
        ("preprocessor_config.json", 340),
        ("tokenizer.json", 2480617),
        ("vocabulary.json", 1068114),
    ),
}

HF_REPO = "Systran/faster-whisper-{tag}"
MIN_DOWNLOAD_SIZE = 1_000_000  # 1 MB: below this the fetch is wrong


def model_download_url_for(tag: str, filename: str) -> str:
    """The HF resolve URL for one file in one model repo."""
    repo = HF_REPO.format(tag=tag).replace("/tree/", "/resolve/")
    return f"https://huggingface.co/{repo}/resolve/main/{filename}"


def model_cache_dir(state_root: str | os.PathLike | None) -> Path:
    """The machine-shared model cache root (design §Packaging): under
    the state dir beside the benchmark cache and the job registry."""
    if state_root is None:
        from .benchmark import state_dir as default_state_dir

        state_root = default_state_dir()
    return Path(state_root) / "whisper" / "models"


MODEL_LOCK_STALE_SECONDS = 1800  # 30 min: heartbeats keep an active lock young
# v0.4.16 (D-4): a degraded cache dir (read-only mid-wait) must break the
# wait-out, never spin — degrade-OPEN after this many CONSECUTIVE
# non-FileExistsError OSErrors (PermissionError degrades immediately).
MODEL_LOCK_OERROR_DEGRADE_ROUNDS = 3


@contextlib.contextmanager
def _model_download_lock(target_dir: Path, on_wait=None):
    """Per-tag download critical section (v0.4.16, model-management ADR-5).

    O_CREAT|O_EXCL lockfile ``<tag>/.download.lock`` — create-close, never
    held open (no Windows sharing-violation trap). The download's own
    progress callback heartbeats its mtime (≤1 Hz), so a live writer never
    looks stale; a lock older than MODEL_LOCK_STALE_SECONDS is dead weight
    (killed process) and is reclaimed once. Degrade-OPEN on an unwritable
    cache dir: proceed WITHOUT the lock — the sha256 pin remains the
    integrity backstop; never a hang, never a refusal (the GPU-phase-lock
    honesty rule, mirrored). The caller re-runs its fast-path check after
    acquire: the winner likely finished while we waited.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    lock = target_dir / ".download.lock"
    waited = 0.0
    rounds = 0
    # v0.4.16 (D-4): consecutive non-FileExistsError OSErrors — the
    # bounded trigger below; reset by ordinary contention (a
    # FileExistsError means the filesystem is still writable).
    oserror_streak = 0
    held_token = False
    try:
        while True:
            try:
                lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                # Normal contention (NOT a permission failure). The
                # stale-check runs BEFORE sleeping: a dead writer's
                # lock is reclaimed unilaterally (the retry IS the
                # correctness); a live one earns a labeled wait.
                rounds += 1
                oserror_streak = 0  # contention != degradation (D-4)
                try:
                    stale = (
                        time.time() - os.stat(lock).st_mtime
                        > MODEL_LOCK_STALE_SECONDS
                    )
                except OSError:
                    stale = False  # stat/unlink races with the other process
                if stale:
                    try:
                        os.unlink(lock)
                    except OSError:
                        pass  # races are retries, not errors
                    continue
                if on_wait is not None:
                    try:
                        on_wait(_lock_holder_pid(lock), waited)
                    except Exception:
                        pass  # a label hook never blocks the download
                time.sleep(1.0)
                waited += 1.0
            except OSError as exc:
                # v0.4.16 (model management, ADR-5): Degrade-OPEN on
                # the FIRST create attempt (permissions, read-only
                # fs) — proceed WITHOUT the lock (yield False); the
                # sha256 pin remains the integrity backstop. Never
                # a hang, never a refusal.
                # v0.4.16 (D-4, Karen remediation): a MID-WAIT
                # degradation (the cache dir made read-only while we
                # wait on a live contender) degrades-OPEN too — a
                # PermissionError on ANY round, or
                # MODEL_LOCK_OERROR_DEGRADE_ROUNDS consecutive
                # non-FileExistsError OSErrors, breaks out the same
                # way, so the downloading thread can never spin
                # silently forever. Ordinary stat/open races keep the
                # 1 Hz retry cadence (an intervening FileExistsError
                # resets the streak — contention is not degradation).
                if (
                    rounds == 0
                    or isinstance(exc, PermissionError)
                    or oserror_streak + 1 >= MODEL_LOCK_OERROR_DEGRADE_ROUNDS
                ):
                    break
                oserror_streak += 1
                # a later-round failure is a stat/open race — retry, but
                # never as a hot loop (the same 1 Hz cadence the
                # FileExistsError arm uses)
                time.sleep(1.0)
                waited += 1.0
                continue  # stat/unlink races are retries, not errors
            else:
                try:
                    os.write(
                        lock_fd, f"{os.getpid()}\n{time.time()}\n".encode("utf-8")
                    )
                finally:
                    os.close(lock_fd)  # create-close, never held open
                held_token = True
                break
        yield held_token
    finally:
        # Release only when held; unlink races (a stale-reclaimer freed
        # it already) are not errors. Never held open, so nothing can
        # leak a Windows sharing violation.
        if held_token:
            try:
                os.unlink(lock)
            except OSError:
                pass


def _lock_heartbeat(callback, lock_path: Path) -> Callable[[int, int | None], None]:
    """v0.4.16 (model management, ADR-5): wrap a download-progress
    callback so every beat ALSO refreshes the lock's mtime (≤1 Hz) —
    a live download keeps its lock young with zero extra threads.
    ``callback=None`` installs a no-op beat: the heartbeat exists
    purely to keep the lock young, never to invent observables."""
    last = [0.0]

    def _heartbeat(done: int, total: int | None) -> None:
        now = time.time()
        if now - last[0] >= 1.0:
            last[0] = now
            try:
                os.utime(lock_path, None)
            except OSError:
                pass  # the lock is a courtesy, never a failure source
        if callback is not None:
            callback(done, total)

    return _heartbeat


def _lock_holder_pid(lock: Path) -> int | None:
    """Best-effort holder pid from the lockfile's first line — content
    is ``pid\\ntimestamp\\n``; anything unreadable stays None (the wait
    label renders ``?``, exactly like the GPU lock's unknown holder)."""
    try:
        head = (lock.read_text(encoding="utf-8").split("\n") or [""])[0].strip()
        return int(head) if head else None
    except (OSError, ValueError):
        return None


def resolve_model_dir(
    pin: str | None = None,
    *,
    state_root: str | os.PathLike | None = None,
    fetch=None,
    on_download_progress: Callable[[int, int | None], None] | None = None,
    on_lock_wait: Callable[[int | None, float], None] | None = None,
) -> str:
    """Resolve the transcription engine model: explicit pin (a user
    model PATH or a known tag) -> the machine-shared cache.

    Known tag: download on first use into
    ``<state_root>/whisper/models/<tag>/`` with the sha256-pinned
    model.bin (``.part`` resume — a size-matching staged ``.part``
    short-circuits the fetch; MIN_DOWNLOAD_SIZE guard; final size +
    digest checked before rename). Honest progress through
    ``on_download_progress(done_bytes, total_bytes_or_None)`` — now
    TRUE, not decorative (v0.4.16, ADR-3): every copied chunk of the
    default fetcher fires it, and ``total_bytes_or_None`` is the
    server's Content-Length or None when absent. A known tag whose
    cache exists just returns the path (no fetch).

    An unknown pin is treated as a PATH the user supplied (the config
    pin override large-v3/user-path discipline); a readable directory
    wins verbatim, a missing one raises TranscriptionError naming it.

    v0.4.16 (model management, ADR-5): the download critical section
    (from the failed fast-path through the ``.complete`` marker write)
    serializes per tag on ``<tag>/.download.lock`` — a second acquirer
    re-runs the fast-path check after acquiring (the winner likely
    finished while it waited: no double fetch), ``on_lock_wait(
    holder_pid_or_None, waited_seconds)`` mirrors the GPU lock's wait
    label, and an unwritable cache dir degrades OPEN (no lock, sha256
    still gates completion).
    """
    if _engine_disabled_by_env():
        raise TranscriptionError(
            "transcription engine disabled by env "
            "(VIDLIFT_TRANSCRIPTION_ENGINE=0)"
        )
    if pin and pin not in KNOWN_MODELS:
        candidate = Path(pin)
        if candidate.is_dir():
            return str(candidate)
        raise TranscriptionError(
            f"transcription model not found: '{pin}' is neither a known "
            f"model ({', '.join(KNOWN_MODELS)}) nor an existing path"
        )
    tag = pin or DEFAULT_MODEL
    cache = model_cache_dir(state_root)
    target = cache / tag
    marker = target / ".complete"
    if marker.is_file() and (target / "model.bin").is_file():
        return str(target)
    # v0.4.15.4 (F-11): a marker WITHOUT model.bin is self-contradictory
    # debris (killed mid-write or manual deletion) — fall through to the
    # size/sha-verified re-fetch instead of returning a dir that fails
    # engine construction with a confusing error.
    expected_sha = MODEL_SHA256.get(tag)
    if expected_sha is None:
        raise TranscriptionError(
            f"no sha256 pin for model '{tag}'; refusing an unpinned "
            "transcription model download"
        )
    # v0.4.16 (model management, ADR-3/5): the progress consumer that
    # rides into _download_file. The caller's callback (when one was
    # supplied) is wrapped so every beat ALSO heartbeats the download
    # lock's mtime (≤1 Hz) — a live download never looks stale. With
    # no caller consumer:
    #   - the DEFAULT fetcher still installs a NO-OP beat (its
    #     per-chunk channel keeps the lock young on the legacy
    #     no-progress path), while
    #   - an INJECTED fetch keeps the legacy 2-arg no-progress shape
    #     byte-identical (M-1: the conditional-arity discipline —
    #     legacy test fakes never receive a third argument).
    use_default_fetcher = fetch is None
    if use_default_fetcher:
        fetch = _default_fetcher()
    lock_path = target / ".download.lock"
    if on_download_progress is not None or use_default_fetcher:
        progress = _lock_heartbeat(on_download_progress, lock_path)
    else:
        progress = None
    with _model_download_lock(target, on_wait=on_lock_wait):
        # Second-acquirer fast path (ADR-5): re-run the contract check
        # AFTER acquiring — the winning racer may have completed while
        # we waited for the lock.
        if (target / ".complete").is_file() and (target / "model.bin").is_file():
            return str(target)
        for filename, expected_size in MODEL_SUPPORT_FILES.get(tag, ()):
            path = target / filename
            if not path.is_file() or path.stat().st_size != expected_size:
                _download_file(
                    target,
                    filename,
                    model_download_url_for(tag, filename),
                    expected_size=expected_size,
                    fetch=fetch,
                    on_progress=progress,
                )
        bin_path = target / "model.bin"
        if (
            not bin_path.is_file()
            or bin_path.stat().st_size < MIN_DOWNLOAD_SIZE
        ):
            _download_file(
                target,
                "model.bin",
                model_download_url_for(tag, "model.bin"),
                expected_size=None,
                fetch=fetch,
                on_progress=progress,
            )
        digest = _sha256_file(bin_path)
        if digest != expected_sha:
            try:
                os.remove(bin_path)
            except OSError:
                pass
            raise TranscriptionError(
                f"transcription model '{tag}' failed its sha256 pin "
                f"(got {digest[:16]}..., expected {expected_sha[:16]}...); "
                "the download was deleted — retry to refetch"
            )
        marker.write_text(f"{tag}\n{expected_sha}\n", encoding="utf-8")
        return str(target)


def _default_fetcher() -> Callable:
    """The real HTTP fetcher: URL -> ``<target>.part`` (one file, no
    double suffixes — the caller's ``.part`` IS the staging name).

    v0.4.16 (model management, ADR-3): the contract is
    ``Callable[[str, Path], None]  ->  Callable[[str, Path,
    Callable[[int, int | None], None] | None], None]`` —
    ``fetch(url, target_path, on_progress=None)``; ``on_progress(done_bytes,
    total_bytes_or_None)`` fires per copied chunk when supplied. Call sites
    pass the third argument ONLY when a progress consumer exists
    (conditional arity — the shape that keeps every legacy 2-arg fake
    valid on no-progress paths)."""

    def fetch(url: str, target_path: Path, on_progress=None) -> None:
        tmp = target_path.with_name(target_path.name + ".tmp")
        size_hint: int | None = None
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                length = response.headers.get("Content-Length")
                if length:
                    size_hint = int(length)
                tmp.parent.mkdir(parents=True, exist_ok=True)
                done = 0
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if on_progress is not None:
                            on_progress(done, size_hint)
                if size_hint is not None and tmp.stat().st_size != size_hint:
                    raise TranscriptionError(
                        f"download of {target_path.name} was truncated "
                        f"({size_hint} bytes expected)"
                    )
                os.replace(tmp, target_path)
        except BaseException:
            # v0.4.15.4 (F-11): a failed/interrupted fetch must never
            # strand the .part.tmp staging file (os.replace consumed
            # the name on success, so this is failure-only cleanup).
            # BaseException — not Exception — so KeyboardInterrupt and
            # cancel-style control flow don't strand debris either.
            tmp.unlink(missing_ok=True)
            raise

    return fetch


def _download_file(
    target_dir: Path,
    filename: str,
    url: str,
    *,
    expected_size: int | None,
    fetch: Callable,
    on_progress: Callable[[int, int | None], None] | None,
) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    part = target_dir / (filename + ".part")
    if part.is_file() and expected_size is not None and part.stat().st_size == expected_size:
        os.replace(part, target_dir / filename)
        return
    try:
        # v0.4.16 (model management, ADR-3): conditional-arity discipline —
        # the third argument is passed ONLY when a progress consumer
        # exists (the shape that keeps every legacy 2-arg fake valid on
        # no-progress paths).
        if on_progress is not None:
            fetch(url, part, on_progress)
        else:
            fetch(url, part)
    except TranscriptionError:
        raise
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        raise TranscriptionError(
            f"transcription model download failed ({filename}): {exc}"
        ) from exc
    if not part.is_file():
        raise TranscriptionError(
            f"transcription model download produced no file for "
            f"'{filename}'"
        )
    size = part.stat().st_size
    # The 1 MB sanity floor applies ONLY to model.bin; the small
    # support files (config/tokenizer/vocabulary, a few KB each) are
    # size-pinned exactly instead.
    floor = MIN_DOWNLOAD_SIZE if filename == "model.bin" else 0
    if size < floor:
        try:
            part.unlink()
        except OSError:
            pass
        raise TranscriptionError(
            f"transcription model download for '{filename}' is too small "
            f"({size} bytes); deleted — check network access"
        )
    if expected_size is not None and size != expected_size:
        try:
            part.unlink()
        except OSError:
            pass
        raise TranscriptionError(
            f"transcription model download for '{filename}' has size "
            f"{size}, expected {expected_size}; deleted — retry"
        )
    os.replace(part, target_dir / filename)
    if on_progress is not None:
        on_progress(size, expected_size)


def _sha256_file(path: Path) -> str:
    digest = _sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# ---- install state (v0.4.16, model management ADR-1/2/4) --------------------


class ModelInstallBoard:
    """In-memory, process-local install/install-attempt truth (v0.4.16,
    ADR-4). The GUI's install worker (and only it, today) writes; the
    status surfaces read. No file, no mirror — the board dies with the
    process, which is honest: a dead install is not downloading. One
    threading.Lock guards a single-slot snapshot dict; readers copy under
    the lock and never hold it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slot: dict | None = None

    # -- writers (the GUI install worker's thread) --------------------------

    def begin(self, tag: str) -> None:
        """An install attempt starts: state=downloading, bytes 0/None."""
        with self._lock:
            self._slot = {
                "tag": tag,
                "state": "downloading",
                "bytes_done": 0,
                "bytes_total": None,
                "file": None,
                "error": None,
                "updated_at": time.time(),
            }

    def update(self, *, bytes_done: int, bytes_total: int | None, file: str) -> None:
        """A download beat; keeps the slot's freshness stamp current."""
        with self._lock:
            if self._slot is not None and self._slot["state"] == "downloading":
                self._slot["bytes_done"] = int(bytes_done)
                self._slot["bytes_total"] = (
                    int(bytes_total) if bytes_total is not None else None
                )
                self._slot["file"] = file
                self._slot["updated_at"] = time.time()

    def succeed(self, tag: str) -> None:
        """The install landed; state=installed (disk agrees; the UI
        re-checks anyway)."""
        with self._lock:
            self._slot = {
                "tag": tag,
                "state": "installed",
                "bytes_done": None,
                "bytes_total": None,
                "file": None,
                "error": None,
                "updated_at": time.time(),
            }

    def fail(self, tag: str, error: str) -> None:
        """The install failed: state=not-installed, error=message."""
        with self._lock:
            self._slot = {
                "tag": tag,
                "state": "not-installed",
                "bytes_done": None,
                "bytes_total": None,
                "file": None,
                "error": error,
                "updated_at": time.time(),
            }

    # -- readers --------------------------------------------------------------

    def snapshot(self) -> dict | None:
        """The board's one slot (copied under the lock), or None when
        nothing was ever recorded (fresh process). Keys are exactly the
        ``transcription_model`` payload keys plus ``updated_at``."""
        with self._lock:
            slot = self._slot
        return dict(slot) if slot is not None else None


def model_install_state(
    tag: str,
    *,
    state_root: str | os.PathLike | None = None,
    board: ModelInstallBoard | None = None,
    progress_download: dict | None = None,
) -> dict:
    """The additive ``transcription_model`` block (v0.4.16).

    Derive-at-read, never persist a state string. Order (architecture §6):
    1. ``progress_download`` (a live job's ``progress["model_download"]``):
       the in-flight truth — ``downloading`` with its byte fields.
    2. a fresh board snapshot whose ``state == "downloading"`` (a GUI
       install in flight while no job rides this record).
    3. the disk predicate — ADR-1, the resolve fast-path contract verbatim:
       ``.complete`` marker AND ``model.bin`` present → ``installed``;
       else ``not-installed``.
    4. OSError on the disk probe → ``state="unknown"`` (honest: a
       permissions failure is not "missing").
    ``bytes_*``/``file`` are null unless downloading; ``error`` rides only a
    board ``fail`` snapshot (until the next begin/succeed clears it)."""
    cache_dir = model_cache_dir(state_root) / tag

    def _disk_installed() -> bool:
        # ADR-1: the resolve fast-path's OWN predicate, verbatim — the
        # indicator and the resolver can never disagree.
        return (
            Path(cache_dir, ".complete").is_file()
            and Path(cache_dir, "model.bin").is_file()
        )

    try:
        disk_installed = _disk_installed()
    except OSError:
        state = "unknown"
        error = None
    else:
        state = "installed" if disk_installed else "not-installed"
        error = None
    bytes_done: int | None = None
    bytes_total: int | None = None
    file: str | None = None
    live = progress_download if isinstance(progress_download, dict) else None
    if live is not None and live.get("bytes_done") is not None:
        try:
            parsed_done = int(live["bytes_done"])  # may raise on junk
            raw_total = live.get("bytes_total")
            parsed_total = int(raw_total) if raw_total is not None else None
        except (TypeError, ValueError):
            # S-11 (foreign-state tolerance): a malformed live fact
            # degrades to the disk derivation, never a crash.
            live = None
        else:
            # A live job's in-flight byte truth beats idle disk truth:
            # the bytes have not landed the marker yet.
            state = "downloading"
            bytes_done = parsed_done
            bytes_total = parsed_total
            file = live.get("file") or "model.bin"
    if live is None:
        snap = board.snapshot() if board is not None else None
        # Board priority rule (binding): a snapshot participates ONLY
        # when its tag is the queried one; a downloading board beats
        # disk, a fail snapshot contributes only its error (post-failure
        # truth is "not-installed + why"), a succeed snapshot contributes
        # nothing (disk is the truth).
        if snap is not None and snap.get("tag") == tag:
            if snap.get("state") == "downloading":
                state = "downloading"
                bytes_done = snap.get("bytes_done")
                bytes_total = snap.get("bytes_total")
                file = snap.get("file") or None
            elif snap.get("state") == "not-installed" and state == "not-installed":
                error = snap.get("error") or None
    return {
        "tag": tag,
        "state": state,
        "cache_dir": str(cache_dir),
        "bytes_done": bytes_done,
        "bytes_total": bytes_total,
        "file": file,
        "error": error,
    }


# ---- device resolution --------------------------------------------------------


def resolve_transcription_device(requested: str) -> str:
    """auto -> cuda when CTranslate2 sees a CUDA device, else cpu.

    Honest fallback: the ctypes probe mirrors ctranslate2's own check;
    when neither applies the caller gets ``cpu`` (the ~1x realtime
    fallback documented in the design; the run log states which one).
    """
    requested = (requested or "auto").lower()
    if requested in ("cuda", "cpu"):
        return requested
    if requested != "auto":
        # unknown pin: honest refusal rather than a silent guess
        raise TranscriptionError(
            f"unknown transcription device '{requested}' "
            "(expected auto|cuda|cpu)"
        )
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except Exception:  # noqa: BLE001 - probe failure means no GPU
        pass
    return "cpu"


# ---- language (auto-detect + config pin; translation is a NON-GOAL) ----

LANGUAGE_CONFIDENCE_FLOOR = 0.5
# The detect-decode window bound lives beside the helper that enforces
# it (_decode_audio_prefix, defined after the WhisperEngine class): the
# engine detects on a bounded prefix, DETECT_AUDIO_SECONDS = 120.


def detect_language(
    engine: TranscribeEngine,
    audio_path: str,
    *,
    pin: str = "",
) -> tuple[str, LanguageGuess | None, list[str]]:
    """Resolve the transcription language for one file.

    Returns ``(language_for_call, guess_or_None, warnings)``. A config
    pin wins outright (the design's `ko` / `zh-cn` / `en` discipline).
    Otherwise the engine detects on a bounded prefix (first 120 s;
    DETECT_AUDIO_SECONDS); below
    the confidence floor the top guess PROCEEDS with a warning
    (fail-open discipline, never a refusal). ``guess`` is None when a
    pin made detection unnecessary.
    """
    pin = (pin or "").strip().lower()
    if pin:
        return pin, None, []
    guess = engine.detect_language(audio_path)
    warnings: list[str] = []
    if guess.confidence < LANGUAGE_CONFIDENCE_FLOOR:
        warnings.append(
            f"language auto-detect confidence low "
            f"({guess.confidence:.2f} < {LANGUAGE_CONFIDENCE_FLOOR:.2f}); "
            f"proceeding with '{guess.code or 'unknown'}'"
        )
    return (guess.code or "en"), guess, warnings


# ---- cue construction + alignment verification (the sync backstop) ----

# Design §Sync quality: a cue whose aligned midpoint lands >250 ms from
# the whisper midpoint is rebuilt on the aligned positions.
ALIGNED_MIDPOINT_REBUILD_MS = 250
# Cues below this aligned confidence are dropped (music-region garbage).
ALIGN_CONFIDENCE_DROP = 0.2
# Per-cue word-confidence floor (validation fix 4): a cue whose average
# WORD probability falls below this is dropped as music-region
# hallucination garbage. Measured on Eric's library with the real small
# model (Firefly/S01E01 en, Scarlet Heart/S01E01 ko; Karen's slices):
# real dialogue clusters at 0.75-0.995 average word probability while
# the music-edge garbled cues Karen flagged run 0.013-0.30 (the VAD
# passes them; the fixed-marker + no_speech>0.9 rules did not). 0.5 sits
# in the measured empty zone (0.30 garbage vs 0.75 real) — real
# mumbly-but-true speech stays, hallucinated noise goes.
WORD_PROB_FLOOR = 0.5
# Per-cue word-confidence floor for WORDLESS cues (validation fix 4):
# a segment whose words lack timestamps carries no word probability at
# all, so the segment's own no_speech_prob is the only honest signal.
# The silence/lo-fi signature Karen measured drops here too. Word-Stamped
# clusters use WORD_PROB_FLOOR instead (average word probability).
SEGMENT_NO_SPEECH_DROP = 0.9

# Readability lead-in/lead-out around the word timestamps. The measured
# lead-in is ZERO milliseconds ON PURPOSE: with the shipped VAD
# (silero speech_pad_ms=400 default, faster_whisper/vad.py) cue starts
# already lead the measured audio onsets (Karen's own onset metric:
# −80 ms median on the Firefly slice) — an explicit lead-in pushed the
# median to −96 ms on the same slices. The lead-out adds 120 ms of
# hold after the last word's end (subtitle minimum-dwell convention:
# cues that vanish at the last phoneme read as flicker), clamped
# against the next cue start so it can never overlap.
CUE_LEAD_IN_MS = 0
CUE_LEAD_OUT_MS = 120

# ---- v0.4.15: single-word hold-cap (Karen's stuck-cue fix) -----------------
#
# The rule: a SINGLE-word cue whose hold runs >2.5 s past its last word
# end is capped to word_end + the standard 120 ms readability hold —
# BUT ONLY when the audio immediately AFTER the word measures quiet
# (RMS < HOLD_QUIET_RMS over the (word_end+0.1 s .. word_end+1.5 s)
# window on the job's own 16 kHz scratch wav). Karen's 0.4.14 WARN:
# Whisper word-end times on trailing syllables smear when the VAD
# doesn't snap the segment shut; the cue then reads as a stuck line
# (Firefly cue 16 "can't": single word, 8.28 s hold, region RMS
# 0.0008-0.0089 — inside the quiet band with 10x headroom, while even
# quiet dialogue runs 0.02-0.15 RMS). The measured guard keeps real
# dramatic one-word holds (a shouted "No!" decaying over room
# tone/score) untouched, and without a wav the cap NEVER trims
# (guard-first: never trim on uncertainty).
SINGLE_WORD_HOLD_CAP_MS = 2500
HOLD_QUIET_RMS = 0.01
HOLD_MEASURE_WINDOW_S = (0.1, 1.5)
HOLD_MIN_KEEP_MS = 500

_HALLUCINATION_TEXTS = {
    "thank you.", "thanks for watching!", "subtitles by",
    " Amara.org", "subtitle", "please subscribe",
}


def segments_to_cues(
    segments: list[TranscriptSegment],
    *,
    word_prob_floor: float = WORD_PROB_FLOOR,
    no_speech_drop: float = SEGMENT_NO_SPEECH_DROP,
    hold_cap_wav: str | None = None,
    notes: list[str] | None = None,
) -> list[Cue]:
    """Cues from word timestamps (not segment midpoints).

    Each WORD-CLUSTER between big gaps (>=0.7 s within a segment)
    becomes its own cue bounded by word times: the start is the cluster's
    FIRST WORD start (the honest anchor for a segment whose start can
    precede its first word — the validation-fix fixture shape), the end
    the LAST WORD end plus CUE_LEAD_OUT_MS hold (clamped against the
    next cue, never overlapping); a segment whose words lack timestamps
    falls back to segment bounds. Cues whose average word probability
    is below ``word_prob_floor`` are dropped, and wordless segments with
    no_speech_prob at/above ``no_speech_drop`` are dropped (music-region
    hallucination garbage: measured real dialogue 0.75-0.995 vs garbled
    music-edge 0.013-0.30 on Eric's library). Whisper's own segmentation
    (~7 s / 80 chars) is respected by construction; the per-cue 500 ms
    minimum display floor rides with normalize_cues.
    """
    cues: list[Cue] = []
    hold_cap_trims = 0
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        words = [
            w for w in segment.words
            if w.start is not None and w.end is not None
            and w.word.strip()
        ]
        if not words:
            # the wordless fallback keeps segment bounds, but a segment
            # the engine itself flagged as mostly-noise at/above the
            # drop line is the hallucination signature — never a cue
            if segment.no_speech_prob >= no_speech_drop:
                continue
            cues.append(
                Cue(
                    start_ms=int(round(segment.start * 1000)),
                    end_ms=int(round(segment.end * 1000)),
                    text=text,
                )
            )
            continue
        clusters: list[list[WordStamp]] = []
        current: list[WordStamp] = [words[0]]
        for prev, nxt in zip(words, words[1:]):
            gap = (nxt.start or 0.0) - (prev.end or 0.0)
            if gap >= 0.7:
                clusters.append(current)
                current = [nxt]
            else:
                current.append(nxt)
        clusters.append(current)
        # a high-no_speech segment with real-looking words is exactly
        # Karen's music-edge signature (the VAD passes it, the words
        # hallucinate over the music bed): require the cluster's OWN
        # average word probability to clear the floor — which it does
        # for true speech — AND the segment's no_speech_prob to stay
        # below the drop line for any cluster whose text is short
        # single-word debris (the measured 'Oh'/'I' shapes).
        seg_no_speech = segment.no_speech_prob
        for cluster in clusters:
            cluster_text = " ".join(
                w.word.strip() for w in cluster if w.word.strip()
            )
            if not cluster_text:
                continue
            avg_prob = sum(w.probability for w in cluster) / len(cluster)
            if avg_prob < word_prob_floor:
                continue
            if seg_no_speech >= no_speech_drop and len(cluster) == 1:
                # single-word debris over a flagged no-speech window:
                # the measured music-edge shape ('Oh' p=.30 nsp=.82)
                continue
            end_word = cluster[-1].end
            end_ms = int(round(
                (end_word if end_word is not None else segment.end) * 1000
            ))
            if end_word is not None:
                end_ms += CUE_LEAD_OUT_MS
            cue = Cue(
                start_ms=int(round((cluster[0].start or 0.0) * 1000))
                + CUE_LEAD_IN_MS,
                end_ms=end_ms,
                text=cluster_text,
            )
            cues.append(cue)
            # v0.4.15: the single-word hold-cap rides the SAME loop so
            # the cluster shape (len == 1) and the LAST word's end
            # stamp are available per cue — the smear case is exactly
            # when the cue's padded end ran long past the word.
            if len(cluster) == 1 and end_word is not None:
                word_end_ms = int(round(end_word * 1000))
                word_start_s = float(cluster[0].start or 0.0)
                # the >2.5 s hold test lives INSIDE _trim_stuck_hold,
                # anchored at the audio-measured end — the smeared
                # stamp is exactly what cannot be trusted here.
                trim_to_ms = _trim_stuck_hold(
                    cue, word_start_s, word_end_ms, hold_cap_wav
                )
                if trim_to_ms is not None:
                    cues[-1] = Cue(
                        cue.start_ms,
                        max(trim_to_ms, cue.start_ms + HOLD_MIN_KEEP_MS),
                        cue.text,
                    )
                    hold_cap_trims += 1
    out = normalize_cues(cues)
    # the lead-out hold must never eat the next cue's start: clamp each
    # cue's end against its successor (zero-gap adjacency is allowed;
    # a >=500 ms display floor survives via normalize_cues for 0-length)
    clamped: list[Cue] = []
    for i, cue in enumerate(out):
        end = cue.end_ms
        if i + 1 < len(out):
            nxt = out[i + 1]
            if end > nxt.start_ms and cue.start_ms < nxt.start_ms:
                end = max(nxt.start_ms, cue.start_ms + 500)
        clamped.append(Cue(cue.start_ms, end, cue.text))
    if hold_cap_trims and notes is not None:
        notes.append(_trim_note(hold_cap_trims))
    return clamped


def _trim_note(count: int) -> str:
    """The Karen-visible notes line for stuck-hold trims."""
    return f"trimmed {count} stuck single-word cue hold(s)"


def _wav_rms(
    wav_path: str, start_s: float, end_s: float
) -> float | None:
    """RMS of a 16 kHz mono s16 wav slice; None when unreadable.

    s16 samples scale to -1.0..1.0 by /32768 (peak-normalized); the
    gate and Karen's measured band (0.0008-0.0089 near-silence vs
    0.02-0.15 quiet dialogue) live on that scale. stdlib wave+array
    only — the wav is s16le PCM mono by construction
    (build_audio_extract_command).
    """
    frames = _wav_readframes(wav_path, start_s, end_s)
    if frames is None:
        return None
    samples = array.array("h")
    samples.frombytes(frames)
    if not len(samples):
        return None
    # the wav contract is little-endian s16; byteswap on big-endian
    if sys.byteorder == "big":
        samples.byteswap()
    acc = 0.0
    scale = 1.0 / 32768.0
    for v in samples:
        acc += (v * scale) ** 2
    return (acc / len(samples)) ** 0.5


def _wav_readframes(wav_path: str, start_s: float, end_s: float):
    """The raw little-endian s16 frames for [start_s, end_s) clamped to
    the file; None when the wav is unusable (not mono s16, unreadable,
    or the window is empty). Reads only — never writes."""
    try:
        with wave.open(wav_path, "rb") as wav:
            if wav.getnchannels() != 1:
                return None
            rate = wav.getframerate()
            sampwidth = wav.getsampwidth()
            if rate <= 0 or sampwidth != 2:
                return None
            start = max(0, int(start_s * rate))
            end = min(wav.getnframes(), int(end_s * rate))
            if end <= start:
                return None
            wav.setpos(start)
            return wav.readframes(end - start)
    except (wave.Error, OSError, EOFError):
        return None


def _speech_offset_after(
    wav_path: str, search_start_s: float, search_end_s: float,
    *, window_s: float = 0.04, floor: float = HOLD_QUIET_RMS,
) -> float | None:
    """The word's audible offset: END of the FIRST voiced run inside
    [search_start, search_end).

    Walks non-overlapping ``window_s`` RMS steps forward and returns the
    END of the FIRST run of windows whose RMS clears the ``floor``
    (0.01: Karen's near-silence band sits below it with headroom; voiced
    speech clears it) — the measured moment the word actually stops.
    A single word is one voiced run; any LATER burst (a dramatic hold,
    a score swell) is separate audio the after-guard must see. None
    when no energy anywhere in the window (the anchor then falls back
    to the word stamp).
    """
    try:
        with wave.open(wav_path, "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                return None
            rate = wav.getframerate()
            if rate <= 0:
                return None
            total = wav.getnframes()
            start = max(0, int(search_start_s * rate))
            end = min(total, int(search_end_s * rate))
            step = max(1, int(window_s * rate))
            if end - start < step:
                return None
            wav.setpos(start)  # readframes seeks from CURRENT frame
            samples = array.array("h")
            samples.frombytes(
                wav.readframes(end - start)  # whole window once
            )
            if sys.byteorder == "big":
                samples.byteswap()
    except (wave.Error, OSError, EOFError):
        return None
    scale = 1.0 / 32768.0
    voiced_seen = False
    run_end_pos: int | None = None
    for pos in range(0, len(samples) - step + 1, step):
        chunk = samples[pos : pos + step]
        acc = 0.0
        for v in chunk:
            acc += (v * scale) ** 2
        if (acc / step) ** 0.5 >= floor:
            voiced_seen = True
            run_end_pos = pos + step
        elif voiced_seen:
            # first silence closes the run: a single word is ONE voiced
            # run; any later burst (a dramatic hold, a score swell) is
            # separate audio the after-guard must SEE.
            break
    if not voiced_seen or run_end_pos is None:
        return None
    # refine the run end to 4 ms grain inside the closing window: the
    # coarse 40 ms pass straddles the true edge (a half-voiced window
    # still reads voiced), and the cap's tested numbers live on exact
    # sample edges.
    sub = max(1, rate // 250)  # 4 ms
    refined = run_end_pos
    for spos in range(max(0, run_end_pos - step), run_end_pos, sub):
        chunk = samples[spos : min(spos + sub, run_end_pos)]
        if len(chunk) < sub:
            break
        acc = 0.0
        for v in chunk:
            acc += (v * scale) ** 2
        if (acc / len(chunk)) ** 0.5 >= floor:
            refined = spos + sub  # still voiced; push the end forward
        else:
            break  # sub-silence starts here — the true edge
    return (search_start_s * rate + refined) / rate


def _trim_stuck_hold(
    cue: Cue, word_start_s: float, word_end_ms: int,
    hold_cap_wav: str | None,
) -> float | None:
    """The smear-immune hold anchor + quiet-AFTER gate for one cue.

    Word-end stamps smear when the VAD doesn't snap the segment shut
    (Karen's 0.4.14 WARN: the word end stamp rode the segment end, so
    the cue held 8.28 s past the audible word). The stamp is trusted
    only where the audio agrees; the audio is the oracle:

    1. WITHOUT a wav: return None — the cap NEVER trims (guard-first:
       never trim on uncertainty; the pure path and every wav-less
       arm).
    2. The hold anchor is the AUDIO-MEASURED word end (the first
       voiced run's offset scanning word start -> cue end); an honest
       stamp measures identically, a smeared stamp reads its true end
       from the energy. No audible energy at all -> the stamp stands
       as the fallback anchor.
    2b. The >2.5 s hold test runs against the MEASURED anchor (cue
       end - true end), not the stamp: the design's rule-2 wording
       takes the stamp end, but its own smear note ("the segment/
       word end itself ran long, which is exactly the failure") and
       its tested numbers (word end 2.0 s, cue end 10.28 s, hold
       8.28 s) are only satisfiable against the true end — a
       stamp-based test cannot fire on the smear it exists to catch.
    3. The quiet guard measures (anchor+0.1 .. anchor+1.5): energy
       there means a held-on-purpose dramatic word — untouched.

    Returns the anchor ms to trim TO (anchor + the standard 120 ms
    readability hold) when the trim may fire; None otherwise.
    """
    if not hold_cap_wav:
        return None
    # The measured hold anchor: where the AUDIO says the word actually
    # ends. A behaved stamp matches the audio (identical numbers); a
    # stamped end that rode the segment end (the VAD never snapped
    # shut — Karen's shape) reads its true end straight out of the
    # energy. Pure silence across the whole cue (no audible word
    # anywhere) falls back to the stamp — the guard below still
    # decides.
    true_end_s = _speech_offset_after(
        hold_cap_wav, word_start_s, cue.end_ms / 1000.0
    )
    if true_end_s is not None:
        word_end_ms = int(round(true_end_s * 1000))
    # The hold test vs the MEASURED end: a cue end that outruns the
    # audible word by <=2.5 s is a normal readability hold, not a
    # stuck one.
    if cue.end_ms - word_end_ms <= SINGLE_WORD_HOLD_CAP_MS:
        return None
    guard_rms = _wav_rms(
        hold_cap_wav,
        word_end_ms / 1000.0 + HOLD_MEASURE_WINDOW_S[0],
        word_end_ms / 1000.0 + HOLD_MEASURE_WINDOW_S[1],
    )
    if guard_rms is None:
        return None
    if guard_rms >= HOLD_QUIET_RMS:
        # voiced/audible energy after the word: a dramatic hold reads
        # as held ON PURPOSE — untouched.
        return None
    return word_end_ms + CUE_LEAD_OUT_MS


def align_verify_cues(
    cues: list[Cue],
    segments: list[TranscriptSegment],
) -> tuple[list[Cue], list[str]]:
    """The forced-alignment verification pass (design §Sync quality 3).

    For each cue the expected aligned midpoint comes from the WORD
    timestamps inside the covering segment (the design's WhisperX-style
    window re-score, expressed against the engine's own word alignment —
    the honest implementation of "whisper proposes, forced alignment
    disposes" on this engine's surface). A cue whose word-aligned
    midpoint lands > ALIGNED_MIDPOINT_REBUILD_MS from its whisper
    midpoint is REBUILT on the aligned positions; missing word data
    leaves the cue untouched (never a fabricated rebuild).

    Zero-word segments drop cues below ALIGN_CONFIDENCE_DROP only when
    the engine reported real no-speech probability (music-region
    garbage signature); text-only segments keep everything.

    Returns (cues, notes) — notes go to the run log honestly.
    """
    notes: list[str] = []
    rebuilt = 0
    dropped = 0
    out: list[Cue] = []
    for cue in cues:
        aligned = _aligned_bounds(cue, segments)
        if aligned is None:
            out.append(cue)
            continue
        a_start, a_end, seg = aligned
        whisper_mid = (cue.start_ms + cue.end_ms) / 2000.0
        aligned_mid = (a_start + a_end) / 2.0
        if (
            abs(aligned_mid - whisper_mid) * 1000
            > ALIGNED_MIDPOINT_REBUILD_MS
        ):
            out.append(
                Cue(
                    start_ms=int(round(a_start * 1000)),
                    end_ms=int(round(a_end * 1000)),
                    text=cue.text,
                )
            )
            rebuilt += 1
            continue
        out.append(cue)
        # drop rule: a cue with words but an empty cluster over a
        # high-no_speech window is the music-garbage signature
        if (
            seg.no_speech_prob > 0.9
            and _is_hallucination_text(cue.text)
        ):
            out.pop()
            dropped += 1
    if rebuilt:
        notes.append(f"alignment pass rebuilt {rebuilt} cue(s) >250 ms off")
    if dropped:
        notes.append(f"alignment pass dropped {dropped} low-confidence cue(s)")
    return normalize_cues(out), notes


def _aligned_bounds(
    cue: Cue, segments: list[TranscriptSegment]
) -> tuple[float, float, TranscriptSegment] | None:
    """Word-aligned (start, end) for the cue's text within its segment.

    Finds the cue's FIRST and LAST word inside the covering segment by
    matching word tokens against the cue text; None when the words
    cannot be matched honestly (no fabricated alignment, ever).
    """
    cue_tokens = [t for t in re.split(r"\s+", cue.text.strip()) if t]
    if not cue_tokens:
        return None
    target = cue.start_ms / 1000
    best_seg: TranscriptSegment | None = None
    best_distance = float("inf")
    for seg in segments:
        if not seg.words:
            continue
        distance = abs(seg.start - target)
        if distance < best_distance and seg.start <= target + 5.0:
            best_distance = distance
            best_seg = seg
    if best_seg is None:
        return None
    words = [
        w
        for w in best_seg.words
        if w.start is not None and w.end is not None and w.word.strip()
    ]
    if not words:
        return None
    # match the first cue token forward and the last cue token backward
    # over the segment's word tokens (a pure string match; a mismatch
    # leaves the cue aligned=None — never a guess)
    lower_tokens = [w.word.strip().lower() for w in words]
    first_token = cue_tokens[0].strip(".,!?—–-\"'“”").lower()
    last_token = cue_tokens[-1].strip(".,!?—–-\"'“”").lower()
    if not first_token or not last_token:
        return None
    first_idx = _find_token(lower_tokens, first_token, 0, 1)
    if first_idx is None:
        return None
    last_idx = _find_token(lower_tokens, last_token, first_idx, -1)
    if last_idx is None or last_idx < first_idx:
        return None
    start = words[first_idx].start
    end = words[last_idx].end
    if start is None or end is None:
        return None
    return float(start), float(end), best_seg


def _find_token(
    tokens: list[str], needle: str, start: int, direction: int
) -> int | None:
    order = range(start, len(tokens)) if direction > 0 else range(
        len(tokens) - 1, start - 1, -1
    )
    for idx in order:
        if tokens[idx] == needle:
            return idx
    return None


def _is_hallucination_text(text: str) -> bool:
    lowered = text.strip().lower()
    return any(
        marker in lowered for marker in (m.strip().lower() for m in _HALLUCINATION_TEXTS)
    )


# ---- the KEEP contract + audio extraction discipline ------------------------


def transcribe_sidecar_srt_path(output_path: str) -> str:
    """``{output-stem}.srt`` next to the finished file (the KEEP
    contract, both modes; mirrors subtitles.sidecar_srt_path)."""
    return os.path.splitext(output_path)[0] + ".srt"


def build_audio_extract_command(
    ffmpeg: str, source_path: str, wav_path: str
) -> list[str]:
    """ffmpeg: source's FIRST audio stream -> 16 kHz mono s16le wav.

    THE SYNC ANCHOR (design §Music/current seams): the encode maps
    ``1:a:0`` from the ORIGINAL input with no tempo filter anywhere;
    transcribing this same stream yields SRT timestamps valid on the
    finished MP4 with ZERO offset math (PAL sped-up audio included —
    its timestamps are the sped timeline the viewer hears).
    """
    return [
        ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", source_path,
        "-map", "0:a:0",
        "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le",
        wav_path,
    ]


def transcribe_source_to_srt(
    source_path: str,
    *,
    output_path: str,
    workdir: str,
    ffmpeg: str,
    runner: Callable[[str, list[str]], "tuple[int, str, str]"],
    engine_factory: Callable[[str], TranscribeEngine] | None = None,
    model_pin: str | None = None,
    device_pin: str = "auto",
    language_pin: str = "",
    vad_on: bool = True,
    state_root: str | os.PathLike | None = None,
    on_step: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_download_progress: Callable[[int, int | None], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    download_log: Callable[[str], None] | None = None,
) -> "TranscriptionResult":
    """The core: source audio -> normalized SRT beside ``output_path``.

    Mirrors subtitles.py's resolve -> scratch -> raw -> normalize
    discipline: ffmpeg extracts the source's ``a:0`` to a 16 kHz mono
    wav in ``workdir`` (the sync anchor), the model resolves (first-run
    sha-pinned download through the seam), the engine transcribes with
    VAD + word timestamps, the alignment verification pass repairs
    rebuilds, and the normalized cues are written to the KEEP sidecar
    path. Raise TranscriptionError on any failure; callers decide the
    disposition (Mode A fail-open, Mode B job failure).
    """
    os.makedirs(workdir, exist_ok=True)
    wav_path = os.path.join(workdir, "source-audio-16k.wav")
    extract_cmd = build_audio_extract_command(
        ffmpeg, source_path, wav_path
    )
    code, _out, err = runner(extract_cmd[0], extract_cmd[1:])
    if code != 0 or not os.path.exists(wav_path):
        raise TranscriptionError(
            f"audio extraction for transcription failed: "
            f"{(err or '').strip()[:300]}"
        )
    try:
        # -- engine resolution (seam-first: the factory receives the
        # resolved model dir; a missing engine package raises
        # TranscriptionError BEFORE any long work happens) ----
        engine_kwargs: dict | None = None
        fallback_notes: list[str] = []
        # v0.4.15.3 (D4): the device the CORE ran — an honest fact the
        # result carries; "" when a foreign factory ran (the core never
        # saw the device decision).
        device_field = ""
        if engine_factory is None:
            # v0.4.15.2 (design §1): the default production factory is
            # the honest fallback wrapper — the device decision resolves
            # HERE (the caller's honest log line names the resolution
            # via its own transcribe_device; Mode B callers can pass
            # their own factory to keep the old verbatim behavior), and
            # the engine build falls back to CPU exactly once when the
            # CUDA runtime proves hostile (library rot, OOM at
            # construction, first-decode failure — the stderr WARNING
            # and the manifest note name it).
            resolved = resolve_transcription_device(device_pin)
            device_field = resolved

            def _default_on_fallback(note: str) -> None:
                # the note rides the core's result notes (Mode B honesty;
                # Mode A's run-side note slot is separate) + stderr is
                # already handled inside the wrapper factory chain.
                fallback_notes.append(note)

            engine_factory = make_whisper_engine_with_fallback
            engine_kwargs = dict(
                device=device_pin,
                resolved_device=resolved,
                cpu_threads=4,
                vad_on=vad_on,
                on_fallback=_default_on_fallback,
            )
        on_step and on_step("transcribe-resolve")
        # v0.4.16 (model management, ADR-3): the previously decorative
        # on_download_progress parameter becomes load-bearing — the
        # download's chunk beats now reach the caller (Mode B's
        # publisher / Mode A's run-side publisher) through the seam.
        model_dir = resolve_model_dir(
            model_pin,
            state_root=state_root,
            on_download_progress=on_download_progress,
        )
        engine = (
            engine_factory(model_dir, **engine_kwargs)
            if engine_kwargs is not None
            else engine_factory(model_dir)
        )
        if on_download_progress is not None and download_log is not None:
            download_log(
                f"transcription model resolved: {model_dir}"
            )
        # -- language ----
        on_step and on_step("transcribe-language")
        language, guess, warnings = detect_language(
            engine, wav_path, pin=language_pin
        )
        lang_confidence = guess.confidence if guess else None
        # -- transcribe (VAD + word timestamps inside the engine) ----
        on_step and on_step("transcribe-run")
        segments: list[TranscriptSegment] = []
        done = [0]

        def _on_segment(segment: TranscriptSegment) -> None:
            if should_cancel is not None and should_cancel():
                from .run import EngineCancelled  # lazy: cancels surface

                raise EngineCancelled("cancelled during transcription")
            segments.append(segment)
            done[0] += 1
            if on_progress is not None:
                on_progress(done[0], 0)  # segments_done; denominator unknown

        segments = engine.transcribe_segments(
            wav_path, language=language, on_segment=_on_segment
        )
        # -- cue build + alignment verification ----
        # v0.4.15: the scratch wav feeds the single-word hold-cap's
        # quiet-AFTER guard (reads only; the finally below still owns
        # deletion), and the trim note rides the SAME notes list the
        # alignment pass appends to.
        result_notes: list[str] = []
        on_step and on_step("transcribe-align")
        cues = segments_to_cues(
            segments, hold_cap_wav=wav_path, notes=result_notes
        )
        cues, notes = align_verify_cues(cues, segments)
        result_notes.extend(notes)
        # -- zero-cue rule (validation fix 3): an SRT with no cues is a
        # 3-byte BOM file every player chokes on and the mux fails with
        # an ffmpeg parse error instead of a reason. Name the condition
        # HERE; the callers decide the disposition (Mode A fail-open
        # note, Mode B failed job) — both honest, neither a parse error.
        if not cues:
            raise TranscriptionError(
                "no speech detected in audio — transcription produced "
                "no subtitle cues"
            )
        # -- write the KEEP sidecar ----
        on_step and on_step("transcribe-write")
        from .srt import write_srt_file

        srt_path = transcribe_sidecar_srt_path(output_path)
        write_srt_file(srt_path, cues)
        return TranscriptionResult(
            srt_path=srt_path,
            cue_count=len(cues),
            language=language,
            language_confidence=lang_confidence,
            notes=list(warnings) + list(result_notes) + list(fallback_notes),
            model=model_dir,
            # v0.4.15.3 (D4): the fallback wrapper ALWAYS lands on CPU,
            # so any fallback note means "cpu" regardless of what
            # resolved; an injected foreign factory reports "" — honest
            # unknown, never a guess.
            device=(
                "cpu"
                if (engine_kwargs is not None and fallback_notes)
                else device_field
            ),
        )
    finally:
        # the 16 kHz scratch wav is pure scratch the moment the engine
        # returns; keep the workdir itself for the caller's retention
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except OSError:
            pass


@dataclass
class TranscriptionResult:
    """What the core reports (the plan/manifest ``transcription`` block).

    v0.4.15.3 (D4): ``device`` states where the transcription actually
    ran ("cuda"/"cpu" from the core's own resolution; "cpu" whenever a
    fallback note fired — the wrapper always lands on CPU); "" for an
    injected foreign factory (honest unknown, never a fabrication).
    """

    srt_path: str
    cue_count: int
    language: str
    language_confidence: float | None
    notes: list[str] = field(default_factory=list)
    model: str = ""
    device: str = ""