"""Mode B — standalone "add subtitles" (design §Mode B, B1 soft-mux).

One pass over an EXISTING file at ANY resolution that lacks a usable
subtitle track: transcribe the source's own audio (the shared core,
transcribe_source_to_srt), then soft-mux (v0.4.15.3 allowlist map
`-map 0:v -map 0:a -map 1`, `-c copy`, mov_text + a language tag)
into a NEW file beside the source —
``{stem}.subs.mp4`` — never touching the source (source-blind
safety). The KEEP ``{stem}.subs.srt`` sidecar is written next to the
output by the core itself.

The pipeline has no upscale, no mezzanine, no plan object: it is
probe → guard → transcribe (GPU-phase lock claim) → remux. The MCP
registry's ``subs`` job kind and the CLI ``vidlift subs`` subcommand
are both thin callers of :func:`run_subs_job`.

Failure discipline (Mode B differs from Mode A): transcription IS the
job's whole point, so its failures fail the JOB
(:class:`SubsJobError`) — but the source file is never touched, and
already-written artifacts (the sidecar) are kept.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from .probe import classify, probe_file
from .subtitles import SubtitleOcrError, mux_language
from .transcribe import (
    TRANSCRIPTION_ENABLED_DEFAULT,  # noqa: F401 (re-export convenience)
    TranscriptionError,
    transcribe_source_to_srt,
)

__all__ = ["SubsJobError", "SubsJobResult", "subs_output_path", "run_subs_job"]


class SubsJobError(RuntimeError):
    """Mode B job failure (the job's whole point could not be done)."""


@dataclass
class SubsJobResult:
    """What one subs job produced (mirrors TranscriptionResult + mux).

    v0.4.15.3 (D4): ``device`` mirrors the transcription's device fact
    ("" when a foreign factory ran — honest unknown).
    """

    source_path: str
    output_path: str
    srt_path: str
    cue_count: int
    language: str
    language_confidence: float | None
    model: str
    device: str = ""
    notes: list[str] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration_seconds(self) -> float:
        return max(self.finished_at - self.started_at, 0.0)


def subs_output_path(source_path: str) -> str:
    """The Mode B output name: ``{stem}.subs.mp4`` beside the source —
    a distinct infix family, it can never collide with the upscale
    ``.{target}.mp4`` names. A .mkv source STILL remuxes to .mp4: mp4
    is where mov_text lives."""
    base = os.path.basename(source_path)
    stem = re.sub(r"\.[^.]+$", "", base)
    return os.path.join(os.path.dirname(source_path), f"{stem}.subs.mp4")


_USABLE_KINDS = {"text"}


def source_has_usable_subtitles(report) -> bool:
    """Mode B's guard: embedded TEXT subs count as usable; bitmap tracks
    (PGS/VobSub) and sidecars do NOT stop the job — a file with only
    bitmap subs has no legible subs in most players, which is exactly
    Mode B's reason to exist.

    Contract with ``mplex``: when this returns False (or ``--force``
    overrides it), the remux allowlist-maps only ``0:v`` + ``0:a`` + the
    new SRT — bitmap tracks are left behind in the untouched source,
    never transcoded.
    """
    return any(s.kind in _USABLE_KINDS for s in report.subtitles)


def run_subs_job(
    source_path: str,
    *,
    ffmpeg: str,
    ffprobe: str = "ffprobe",
    probe_runner=None,
    runner=None,
    engine_factory=None,
    model_pin: str | None = None,
    device_pin: str = "auto",
    language_pin: str = "",
    force: bool = False,
    workdir: str | None = None,
    state_root: str | os.PathLike | None = None,
    on_step=None,
    on_progress=None,
    on_download_progress=None,
    should_cancel=None,
    download_log=None,
    gpu_lock_factory=None,
) -> SubsJobResult:
    """One standalone subs job (B1 soft-mux; burn-in is permanently
    off the roadmap — the v0.4.15 strip removed the vestigial
    ``burn`` flag and its named not-implemented stub).

    Never overwrites the source: the output is ``{stem}.subs.mp4`` and
    a pre-existing output FAILS the job (not-ready, the caller
    decides; run-side guard mirrors the plan-time collision gate).
    """
    from .run import find_tools  # lazy: run.py is heavy

    del find_tools  # (resolved ffmpeg/ffprobe come from the caller)

    def _step(name: str) -> None:
        if on_step is not None:
            on_step(name)

    started = time.time()
    # ---- probe (verbatim reuse) ------------------------------------
    _step("probe")
    probe_kwargs = {}
    if probe_runner is not None:
        probe_kwargs["runner"] = probe_runner
    media = probe_file(source_path, binary=ffprobe, **probe_kwargs)
    report = classify(media)

    if report.video is None:
        raise SubsJobError(f"no video stream: {source_path}")
    if not report.audio:
        raise SubsJobError(
            f"no audio stream to transcribe: {source_path}"
        )
    if source_has_usable_subtitles(report) and not force:
        raise SubsJobError(
            f"already has a usable text subtitle track (use --force "
            f"to transcribe anyway): {source_path}"
        )

    output_path = subs_output_path(source_path)
    if os.path.exists(output_path):
        raise SubsJobError(
            f"output already exists (never overwritten): {output_path}"
        )
    srt_path = os.path.join(
        os.path.dirname(output_path),
        os.path.basename(output_path)[:-4] + ".srt",
    )

    # ---- transcribe (the GPU-phase lock claim, Mode A discipline) --
    _step("transcribe")
    from .engine_ncnn import _gpu_phase_lock  # lazy: circular import

    gpu_lock = (gpu_lock_factory or _gpu_phase_lock)()

    def _wait_label(holder_pid, held_seconds: float) -> None:
        _step(
            "transcribing audio (waiting for GPU — held by "
            f"pid {holder_pid if holder_pid else '?'} for "
            f"{held_seconds:.0f}s)"
        )

    lock_acquired = gpu_lock.acquire(on_wait=_wait_label)
    try:
        transcribed = transcribe_source_to_srt(
            source_path,
            output_path=output_path,
            workdir=workdir or (output_path + ".subs-work"),
            ffmpeg=ffmpeg,
            runner=runner if runner is not None else _default_runner,
            engine_factory=engine_factory,
            model_pin=model_pin,
            device_pin=device_pin,
            language_pin=language_pin,
            state_root=state_root,
            on_step=on_step,
            on_progress=on_progress,
            on_download_progress=on_download_progress,
            should_cancel=should_cancel,
            download_log=download_log,
        )
    finally:
        if lock_acquired:
            gpu_lock.release()

    # ---- remux B1: `-c copy` + mov_text into the NEW file ----------
    _step("subs-mux")
    mplex(
        source_path,
        transcribed.srt_path,
        output_path=output_path,
        language=transcribed.language,
        ffmpeg=ffmpeg,
        runner=runner if runner is not None else _default_runner,
    )

    return SubsJobResult(
        source_path=source_path,
        output_path=output_path,
        srt_path=transcribed.srt_path,
        cue_count=transcribed.cue_count,
        language=transcribed.language,
        language_confidence=transcribed.language_confidence,
        model=transcribed.model,
        device=transcribed.device,
        notes=list(transcribed.notes),
        started_at=started,
        finished_at=time.time(),
    )


def _default_runner(binary: str, args: list[str]) -> tuple[int, str, str]:
    from .run import _subprocess_runner  # lazy

    return _subprocess_runner(binary, args)


def mplex(
    source_path: str,
    srt_path: str,
    *,
    output_path: str,
    language: str,
    ffmpeg: str,
    runner,
) -> str:
    """B1 remux: `ffmpeg -i source -i subs -map 0:v -map 0:a -map 1
    -c copy -c:s mov_text -metadata:s:s:0 language=<lang> -movflags
    +faststart <output>` — temp-sibling + os.replace atomicity (the
    mux_srt_into_mp4 pattern), but building a NEW file beside the
    source instead of replacing the finished encode in place.

    The map is an allowlist by contract: the probe→guard gate upstream
    guarantees ≥1 video stream, ≥1 audio stream, and no *text*
    subtitle track (unless `--force`), so `0:a` is non-optional. Bitmap
    subtitles (PGS/VobSub — the files Mode B exists for), attachments,
    and data streams are deliberately NOT carried: they cannot be
    transcoded to mov_text and several cannot live in mp4 at all. The
    output's subtitle track IS the transcription; the untouched source
    keeps its bitmap tracks.

    Residual boundary: a source whose *video* codec has no mp4 tag
    (e.g. some VPx/theora builds) still fails the remux honestly with
    :class:`SubsJobError` — unchanged, out of the v0.4.15.3 fix's
    scope.

    Source-blind: the job touches nothing else.
    """
    tmp = output_path + ".muxing.mp4"
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", source_path, "-i", srt_path,
        # v0.4.15.3 (F-1): allowlist mapping — the old blanket `-map 0`
        # routed bitmap subtitle tracks (PGS/VobSub) and mkv
        # attachment/data streams into the mp4, where the blanket
        # `-c:s mov_text` forces a bitmap→text transcode ffmpeg
        # categorically refuses. The probe→guard gate already
        # guarantees a video and an audio stream (see the
        # source_has_usable_subtitles contract).
        "-map", "0:v", "-map", "0:a", "-map", "1",
        "-c", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", f"language={mux_language(language)}",
        "-movflags", "+faststart",
        tmp,
    ]
    code, _out, err = runner(cmd[0], cmd[1:])
    if code != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise SubsJobError(
            f"subs remux failed: {(err or '').strip()[:300]}"
        )
    try:
        os.replace(tmp, output_path)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise SubsJobError(
            f"subs remux could not finalize the output: {exc}"
        ) from exc
    return output_path