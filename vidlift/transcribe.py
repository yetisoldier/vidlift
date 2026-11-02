"""Speech transcription to SRT subtitles for VidLift (v0.4.14).

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
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
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
    "TRANSCRIPTION_ENABLED_DEFAULT",
    "TranscribeEngine",
    "TranscriptionError",
    "TranscriptionResult",
    "WhisperEngine",
    "align_verify_cues",
    "CUE_LEAD_IN_MS",
    "CUE_LEAD_OUT_MS",
    "detect_language",
    "make_whisper_engine",
    "model_cache_dir",
    "model_download_url_for",
    "resolve_transcription_device",
    "resolve_model_dir",
    "segments_to_cues",
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
        from faster_whisper.audio import decode_audio

        audio = decode_audio(
            audio_path, sampling_rate=16000, split_stereo=False
        )
        if offset_seconds > 0:
            audio = audio[int(offset_seconds * 16000) :]
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
        return WhisperEngine(model_path, device=device, vad_on=vad_on)
    except ImportError as exc:
        raise TranscriptionError(
            "faster-whisper is not installed (vidlift's transcription "
            f"engine package): {exc}"
        ) from exc


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


def resolve_model_dir(
    pin: str | None = None,
    *,
    state_root: str | os.PathLike | None = None,
    fetch=None,
) -> str:
    """Resolve the transcription engine model: explicit pin (a user
    model PATH or a known tag) -> the machine-shared cache.

    Known tag: download on first use into
    ``<state_root>/whisper/models/<tag>/`` with the sha256-pinned
    model.bin (``.part`` resume; MIN_DOWNLOAD_SIZE guard; final size +
    digest checked before rename). Honest progress through
    ``on_download_progress(done_bytes, total_bytes_or_None)``.
    A known tag whose cache exists just returns the path (no fetch).

    An unknown pin is treated as a PATH the user supplied (the config
    pin override large-v3/user-path discipline); a readable directory
    wins verbatim, a missing one raises TranscriptionError naming it.
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
    if marker.is_file():
        return str(target)
    expected_sha = MODEL_SHA256.get(tag)
    if expected_sha is None:
        raise TranscriptionError(
            f"no sha256 pin for model '{tag}'; refusing an unpinned "
            "transcription model download"
        )
    if fetch is None:
        fetch = _default_fetcher()
    for filename, expected_size in MODEL_SUPPORT_FILES.get(tag, ()):
        path = target / filename
        if not path.is_file() or path.stat().st_size != expected_size:
            _download_file(
                target,
                filename,
                model_download_url_for(tag, filename),
                expected_size=expected_size,
                fetch=fetch,
                on_progress=None,
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
            on_progress=None,
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
    double suffixes — the caller's ``.part`` IS the staging name)."""

    def fetch(url: str, target_path: Path) -> None:
        tmp = target_path.with_name(target_path.name + ".tmp")
        size_hint: int | None = None
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                length = response.headers.get("Content-Length")
                if length:
                    size_hint = int(length)
                tmp.parent.mkdir(parents=True, exist_ok=True)
                with open(tmp, "wb") as fh:
                    shutil.copyfileobj(response, fh, 1024 * 1024)
            if size_hint is not None and tmp.stat().st_size != size_hint:
                tmp.unlink(missing_ok=True)
                raise TranscriptionError(
                    f"download of {target_path.name} was truncated "
                    f"({size_hint} bytes expected)"
                )
        finally:
            pass
        os.replace(tmp, target_path)

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


def detect_language(
    engine: TranscribeEngine,
    audio_path: str,
    *,
    pin: str = "",
) -> tuple[str, LanguageGuess | None, list[str]]:
    """Resolve the transcription language for one file.

    Returns ``(language_for_call, guess_or_None, warnings)``. A config
    pin wins outright (the design's `ko` / `zh-cn` / `en` discipline).
    Otherwise the engine detects on the first 60-120 s of speech; below
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

_HALLUCINATION_TEXTS = {
    "thank you.", "thanks for watching!", "subtitles by",
    " Amara.org", "subtitle", "please subscribe",
}


def segments_to_cues(
    segments: list[TranscriptSegment],
    *,
    word_prob_floor: float = WORD_PROB_FLOOR,
    no_speech_drop: float = SEGMENT_NO_SPEECH_DROP,
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
            cues.append(
                Cue(
                    start_ms=int(round((cluster[0].start or 0.0) * 1000))
                    + CUE_LEAD_IN_MS,
                    end_ms=end_ms,
                    text=cluster_text,
                )
            )
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
    return clamped


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
        covering = [
            seg for seg in segments
            if seg.words and seg.start <= cue.start_ms / 1000 + 0.01
        ]
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


MAX_EXTRACT_SECONDS = 4 * 3600  # sanity bound: a 4h wav is ~460 MB


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
        if engine_factory is None:
            def engine_factory(model_path: str) -> TranscribeEngine:
                return make_whisper_engine(
                    model_path, device=device_pin, vad_on=vad_on
                )
        on_step and on_step("transcribe-resolve")
        model_dir = resolve_model_dir(
            model_pin,
            state_root=state_root,
        )
        engine = engine_factory(model_dir)
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
        on_step and on_step("transcribe-align")
        cues = segments_to_cues(segments)
        cues, notes = align_verify_cues(cues, segments)
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
            notes=list(warnings) + list(notes),
            model=model_dir,
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
    """What the core reports (the plan/manifest ``transcription`` block)."""

    srt_path: str
    cue_count: int
    language: str
    language_confidence: float | None
    notes: list[str] = field(default_factory=list)
    model: str = ""