"""Mode B — standalone "add subtitles" (design §Mode B, B1 soft-mux).

One pass over an EXISTING file at ANY resolution that lacks a usable
subtitle track: transcribe the source's own audio (the shared core,
transcribe_source_to_srt), then soft-mux (v0.4.15.3 allowlist map
`-map 0:v -map 0:a -map 1`, `-c copy`, mov_text + a language tag)
into a NEW file beside the source —
``{stem}.subs.mp4`` — never touching the source (source-blind
safety). The KEEP ``{stem}.subs.srt`` sidecar is written next to the
output by the core itself.

v0.4.17 (opt-in): ``replace_source`` makes the ONE post-success
exception to source-blindness — after a verified remux, the source is
deleted and the output lands at ``{stem}.mp4`` (see
:func:`replace_subs_output`). The default stays source-blind.

v0.4.18 (opt-in ``suppress_srt``, strictly opt-in default OFF): with
the flag set, the redundant ``{stem}.subs.srt`` sidecar is removed
AFTER a successful mux (a mux failure raises before suppression and
keeps it as the fallback deliverable) and BEFORE the replace block —
so ``SubsJobResult.srt_path`` becomes the deliberately-empty ``""``
sentinel (suppression never lies about a file that is gone; a removal
OSError keeps the sidecar AND the truthful path). The default keeps
the sidecar: pre-0.4.18 behavior is the absent-defaults rule.
v0.4.18 (WS3, Karen K-5): after a SUCCESSFUL job the default
``{output}.subs-work`` scratch directory is removed
(:func:`cleanup_subs_scratch`, the Mode B counterpart of run cleanup);
every failure/cancel path retains the scratch for diagnosis.

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
import shutil
import time
import tempfile
import math
from dataclasses import dataclass, field
from pathlib import Path

from .probe import ProbeError, classify, probe_file
from .subtitles import (
    SubtitleOcrError,
    mux_language,
    remove_srt_sidecar,  # v0.4.18 (opt-in suppress_srt; import ≠ export)
)
from .transcribe import (
    TRANSCRIPTION_ENABLED_DEFAULT,  # noqa: F401 (re-export convenience)
    TranscriptionError,
    transcribe_source_to_srt,
)

__all__ = [
    "SubsJobError",
    "SubsJobResult",
    "subs_output_path",
    "subs_replace_target",  # v0.4.17
    "replace_subs_output",  # v0.4.17
    "cleanup_subs_scratch",  # v0.4.18 (WS3, K-5)
    "run_subs_job",
    "subs_plan_verdict",  # v0.4.19 (WS2, plan-time skip)
    "SUBS_VERDICT_READY",
    "SUBS_VERDICT_SKIP_SUBTITLES",
    "SUBS_VERDICT_BLOCKED_OUTPUT",
    "SUBS_VERDICT_PROBE_ERROR",
]


class SubsJobError(RuntimeError):
    """Mode B job failure (the job's whole point could not be done)."""


@dataclass
class SubsJobResult:
    """What one subs job produced (mirrors TranscriptionResult + mux).

    v0.4.15.3 (D4): ``device`` mirrors the transcription's device fact
    ("" when a foreign factory ran — honest unknown).

    v0.4.18: ``srt_path`` is the honest post-run sidecar location;
    v0.4.18's ``suppress_srt`` may legitimately make it ``""`` — the
    deliberately-suppressed sentinel (never a stale claim of a file
    that was removed).
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


def subs_replace_target(source_path: str) -> str:
    """The v0.4.17 replace-source target name: ``{stem}.mp4`` beside the
    source — ALWAYS ``.mp4`` regardless of the source container
    (``.avi``, ``.mkv``, ``.ts``, …): mp4 is where mov_text lives, and
    the old extension is NEVER carried over (D6). The byte-identical
    stem-strip regex as :func:`subs_output_path` keeps one naming
    derivation for display, pre-flight, and the run guard (the
    v0.4.15.3 F-5 lesson)."""
    base = os.path.basename(source_path)
    stem = re.sub(r"\.[^.]+$", "", base)
    return os.path.join(os.path.dirname(source_path), f"{stem}.mp4")


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


# v0.4.19 (WS2): the plan-time skip verdict — already-done files are
# SKIPPED at the pre-flight surfaces (GUI pick, CLI subs, MCP
# transcribe), never enqueued to fail red at run time. The run-side
# guards above are UNCHANGED: they remain the ground-truth backstop
# for direct enqueues (older scripted callers, API bypasses).
SUBS_VERDICT_READY = "ready"
SUBS_VERDICT_SKIP_SUBTITLES = "skip-subtitles"
SUBS_VERDICT_BLOCKED_OUTPUT = "blocked-output-exists"
SUBS_VERDICT_PROBE_ERROR = "probe-error"


def subs_plan_verdict(
    path: str,
    *,
    ffprobe: str = "ffprobe",
    probe_runner=None,
    force: bool = False,
) -> str:
    """The plan-time verdict for one Mode B file (v0.4.19, WS2).

    Returns one of the ``SUBS_VERDICT_*`` constants:

    - ``blocked-output-exists`` — the ``{stem}.subs.mp4`` already
      exists; never overwritten. Checked FIRST and probe-free: the
      dominant re-add case costs no subprocess, and when both
      conditions hold the would-overwrite reason is the actionable one
      (a deliberate divergence from the run guard's order — spec §1
      WS2-D3). ``force`` does NOT bypass this (the run-side guard has
      no force exception either).
    - ``skip-subtitles`` — the source already carries a usable TEXT
      subtitle track (:func:`source_has_usable_subtitles`'s exact
      predicate: bitmap tracks and sidecars do NOT count).
      ``force=True`` reports ``ready`` — the genuine-retranscription
      override mirrors the run guard.
    - ``probe-error`` — the plan probe failed (ProbeError/OSError).
      Fail-OPEN: callers enqueue anyway and the run-side probe names
      the truth (a plan-time probe fault is not absence — the VAD
      preflight's "unknown proceeds" discipline).
    - ``ready`` — enqueue.
    """
    if os.path.exists(subs_output_path(path)):
        return SUBS_VERDICT_BLOCKED_OUTPUT
    if force:
        return SUBS_VERDICT_READY
    probe_kwargs = {}
    if probe_runner is not None:
        probe_kwargs["runner"] = probe_runner
    try:
        media = probe_file(path, binary=ffprobe, **probe_kwargs)
        report = classify(media)
    except (ProbeError, OSError):
        return SUBS_VERDICT_PROBE_ERROR
    if source_has_usable_subtitles(report):
        return SUBS_VERDICT_SKIP_SUBTITLES
    return SUBS_VERDICT_READY


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
    # v0.4.17 (opt-in replace-source): keyword-only, inserted after
    # `force` — ordering inside a kw-only block is source-compatible for
    # every caller (all seven call sites pass keywords).
    replace_source: bool = False,
    # v0.4.18 (opt-in suppress_srt, D6): strictly opt-in, default False
    # (the sidecar is the deliverable unless the caller opts in); the
    # empty-string srt_path sentinel is documented on SubsJobResult.
    suppress_srt: bool = False,
    workdir: str | None = None,
    state_root: str | os.PathLike | None = None,
    on_step=None,
    on_progress=None,
    on_download_progress=None,
    should_cancel=None,
    download_log=None,
    gpu_lock_factory=None,
    # v0.4.19 (WS3): forwarded verbatim to the core; absent = the
    # pre-0.4.19 silent-resolution behavior for direct/library callers.
    on_device=None,
    # v0.4.20 (ADR-1/S1): the caller's batch identity — scopes the CUDA
    # failure latch to this job/batch (one file's CUDA failure never
    # poisons another batch; a new batch re-probes). Absent/None → no
    # latching: every call re-probes, the honest behavior for direct use.
    batch_id=None,
    on_fallback=None,
) -> SubsJobResult:
    """One standalone subs job (B1 soft-mux; burn-in is permanently
    off the roadmap — the v0.4.15 strip removed the vestigial
    ``burn`` flag and its named not-implemented stub).

    Never overwrites the source (unless the opt-in ``replace_source``
    post-success path ships — then, v0.4.17, the source is deleted and
    the output lands at ``{stem}.mp4``; see :func:`replace_subs_output`):
    the output is ``{stem}.subs.mp4`` and a pre-existing output FAILS
    the job (not-ready, the caller decides; run-side guard mirrors the
    plan-time collision gate).

    v0.4.18 (``suppress_srt``, strictly opt-in — default False keeps the
    v0.4.17 byte behavior): after the mux SUCCEEDS the sidecar is
    removed best-effort; a mux failure raises before the removal, so
    ``{stem}.subs.srt`` stays exactly the fallback deliverable it is
    today (flag-independent); a removal OSError keeps the file and the
    truthful path. With suppression shipped, ``result.srt_path`` is
    ``""`` (the deliberately-none sentinel — see SubsJobResult).
    """

    def _step(name: str) -> None:
        if on_step is not None:
            on_step(name)

    started = time.time()
    from .media_transaction import prepare_media_operation, record_verified_output, commit_media_operation
    public_output = subs_output_path(source_path)
    try:
        operation = prepare_media_operation(
            source_path, subs_replace_target(source_path) if replace_source else public_output,
            replace_source=replace_source, suppress_srt=suppress_srt, cancel=should_cancel,
        )
    except OSError as exc:
        raise SubsJobError(str(exc)) from exc
    operation.fallback = public_output
    operation.sidecar_target = str(Path(subs_replace_target(source_path) if replace_source else public_output).with_suffix(".srt"))
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

    output_path = str(operation.directory / Path(public_output).name)
    if os.path.lexists(public_output):
        raise SubsJobError(
            f"output already exists (never overwritten): {output_path}"
        )
    srt_path = os.path.join(
        os.path.dirname(output_path),
        os.path.basename(output_path)[:-4] + ".srt",
    )
    # v0.4.18 (WS3, D10/K-5): the scratch directory is captured ONCE
    # here, PRE-rename, replacing the inline expression at the core
    # call — so the replace-source rename of the output can never
    # confuse the cleanup target (cleanup_subs_scratch's path guard
    # compares exactly this name).
    workdir_used = workdir or (output_path + ".subs-work")

    # ---- transcribe (the GPU-phase lock claim, Mode A discipline) --
    _step("transcribe")
    from .engine_ncnn import _gpu_phase_lock  # lazy: circular import

    gpu_lock = None if device_pin == "cpu" else (gpu_lock_factory or _gpu_phase_lock)()

    def _wait_label(holder_pid, held_seconds: float) -> None:
        _step(
            "transcribing audio (waiting for GPU — held by "
            f"pid {holder_pid if holder_pid else '?'} for "
            f"{held_seconds:.0f}s)"
        )

    if should_cancel is not None and should_cancel():
        from .run import EngineCancelled
        raise EngineCancelled("cancelled before transcription")
    lock_acquired = False
    if gpu_lock is not None:
        # Keep compatibility with injected/legacy lock adapters that expose
        # only on_wait; production GpuPhaseLock accepts cooperative cancel.
        import inspect
        acquire = gpu_lock.acquire
        if "should_cancel" in inspect.signature(acquire).parameters:
            lock_acquired = acquire(on_wait=_wait_label, should_cancel=should_cancel)
        else:
            lock_acquired = acquire(on_wait=_wait_label)
    if gpu_lock is not None and not lock_acquired:
        reason = getattr(gpu_lock, "last_unavailable", None) or "unknown"
        raise SubsJobError(
            f"GPU coordination unavailable ({reason}); refusing unsynchronized GPU work"
        )
    try:
        transcribed = transcribe_source_to_srt(
            source_path,
            output_path=output_path,
            workdir=workdir_used,
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
            on_device=on_device,
            batch_id=batch_id,
            on_fallback=on_fallback,
        )
    finally:
        if lock_acquired and gpu_lock is not None:
            gpu_lock.release()

    # ---- remux B1: `-c copy` + mov_text into the NEW file ----------
    _step("subs-mux")
    from .media_transaction import fingerprint, publish_sidecar
    operation.owned_srt = os.path.abspath(transcribed.srt_path)
    operation.srt_fingerprint = fingerprint(transcribed.srt_path)
    try:
        mplex(
            source_path,
            transcribed.srt_path,
            output_path=output_path,
            language=transcribed.language,
            ffmpeg=ffmpeg,
            runner=runner if runner is not None else _default_runner,
            ffprobe=ffprobe,
            probe_runner=probe_runner,
            expected_media=media,
        )
    except (SubsJobError, OSError) as exc:
        retained_srt, _ = publish_sidecar(operation, Path(public_output).with_suffix(".srt"))
        exc.srt_path = retained_srt
        raise

    final_output = output_path
    final_srt = transcribed.srt_path
    notes = list(transcribed.notes)
    if should_cancel is not None and should_cancel():
        from .run import EngineCancelled
        raise EngineCancelled("cancelled after remux, before post-success changes")
    # v0.4.18 (opt-in suppress_srt): the mux above SUCCEEDED, so the
    # sidecar is redundant — remove it BEFORE any replace, where the
    # helper's isfile guard (replace_subs_output step 4) then makes the
    # sidecar step a guaranteed no-op; on failure mplex already raised
    # ABOVE this line, so the sidecar survived as the fallback
    # deliverable. Removal is best-effort: an OSError keeps the file
    # and final_srt stays truthful (the helper logs the WARNING).
    operation.embedded = True
    record_verified_output(
        operation, output_path,
        verification=lambda path: verify_mux(path, ffprobe=ffprobe, probe_runner=probe_runner, expected_media=media),
        owned_srt=transcribed.srt_path,
    )
    if replace_source:
        # v0.4.17: cancellation never replaces — after the remux lands,
        # before any mutation (transcribe.py's should_cancel contract).
        if should_cancel is not None and should_cancel():
            from .run import EngineCancelled  # lazy: circular import

            raise EngineCancelled("cancelled after remux, before replace")
        _step("replace-source")
        try:
            final_output, maybe_srt = replace_subs_output(
                source_path,
                output_path,
                srt_path=transcribed.srt_path,
                log_line=notes.append,  # helper narration -> result notes
                operation=operation,
            )
            if maybe_srt is not None:
                final_srt = maybe_srt
        except (SubsJobError, OSError) as exc:
            # Mode A discipline: a replace failure is a warning that keeps
            # the verified success; both files stay on disk.
            notes.append(
                f"WARNING replace-source failed; keeping both files: {exc}"
            )
            operation.result = commit_media_operation(operation, cancel=should_cancel, retire_source=False)
    else:
        operation.result = commit_media_operation(operation, cancel=should_cancel)
    committed = operation.result
    final_output, final_srt = committed.output_path, committed.srt_path
    notes.extend(committed.warnings)
    if committed.sidecar_suppressed:
        notes.append("srt sidecar suppressed after a successful mux")

    # v0.4.18 (Karen K-5, success-only): the .subs-work scratch dir is
    # Mode A's cleanup_run_artifacts counterpart. This line is reached
    # only past a successful mux (and any requested replace); every
    # failure/cancel above retains the scratch for diagnosis.
    # workdir_used was captured PRE-rename; the helper's path guard
    # deletes only the default-derived directory.
    if workdir is None and not committed.needs_attention:
        cleanup_subs_scratch(output_path, workdir_used, log_line=notes.append)

    return SubsJobResult(
        source_path=source_path,
        output_path=final_output,
        srt_path=final_srt,
        cue_count=transcribed.cue_count,
        language=transcribed.language,
        language_confidence=transcribed.language_confidence,
        model=transcribed.model,
        device=transcribed.device,
        notes=notes,
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
    ffprobe: str = "ffprobe",
    probe_runner=None,
    expected_media=None,
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
    fd, tmp = tempfile.mkstemp(dir=Path(output_path).parent, prefix=".vidlift-mux-", suffix=".muxing.mp4")
    os.close(fd)
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
    try:
        code, _out, err = runner(cmd[0], cmd[1:])
    except BaseException:
        # The path is an exclusive temp created by this invocation only.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if code != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise SubsJobError(
            f"subs remux failed: {(err or '').strip()[:300]}"
        )
    try:
        verify_mux(tmp, ffprobe=ffprobe, probe_runner=probe_runner, expected_media=expected_media)
        from .safe_files import publish_new
        publish_new(tmp, output_path)
    except (OSError, SubsJobError) as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise SubsJobError(
            f"subs remux could not finalize the output: {exc}"
        ) from exc
    return output_path


def verify_mux(path, *, ffprobe="ffprobe", probe_runner=None, expected_media=None):
    """Independent final probe before publication, source removal or suppression.

    Stream/duration invariants detect a successful-but-invalid runner. They do
    not certify every decoded pixel or subtitle word and are not advertised so.
    """
    try:
        kwargs = {"runner": probe_runner} if probe_runner is not None else {}
        output = probe_file(path, binary=ffprobe, **kwargs)
        duration = output.duration_seconds
        if (output.video is None or not output.audio or
                not any(s.codec_name == "mov_text" for s in output.subtitles) or
                duration is None or not math.isfinite(duration) or duration <= 0 or
                "mp4" not in output.container.split(",")):
            raise SubsJobError("remux verification failed: missing video/audio/mov_text or invalid MP4 duration")
        if expected_media is not None:
            before = expected_media.video
            after = output.video
            if before is None or (before.codec_name, before.width, before.height) != (after.codec_name, after.width, after.height):
                raise SubsJobError("remux verification failed: video stream changed")
            if [(a.codec_name, a.channels) for a in expected_media.audio] != [(a.codec_name, a.channels) for a in output.audio]:
                raise SubsJobError("remux verification failed: audio streams changed")
            original_duration = expected_media.duration_seconds
            if original_duration is None or abs(duration - original_duration) > max(0.5, original_duration * 0.01):
                raise SubsJobError("remux verification failed: duration changed or unknown")
        from dataclasses import asdict
        return {"ok": True, "kind": "independent-remux-probe", "media": asdict(output)}
    except (ProbeError, OSError) as exc:
        raise SubsJobError("remux verification failed: output could not be independently probed") from exc


def replace_subs_output(
    source_path: str,
    output_path: str,
    *,
    srt_path: str | None = None,
    log_line=None,
    operation=None,
) -> tuple[str, str | None]:
    """Commit the bound verified remux, retaining backup, capture and journal."""
    if operation is None or str(operation.source) != os.path.abspath(source_path) or operation.candidate != os.path.abspath(output_path):
        raise SubsJobError("prepared verified operation required; source and output retained")
    if operation.target != operation.source and os.path.lexists(operation.target):
        raise SubsJobError(f"{operation.target.name} already exists as a different file; keeping both files")
    from .media_transaction import commit_media_operation
    operation.result = commit_media_operation(operation, cancel=getattr(operation, "cancel", None))
    if log_line is not None:
        for warning in operation.result.warnings:
            log_line(warning)
        log_line(f"replace-source: output now at {operation.result.output_path}")
    return operation.result.output_path, operation.result.srt_path or None


def cleanup_subs_scratch(
    output_path: str, workdir_used: str, *, log_line=None
) -> list[str]:
    """v0.4.18 (Karen K-5): remove the ``{output}.subs-work`` scratch
    directory after a SUCCESSFUL subs job — the Mode B counterpart of
    run.cleanup_run_artifacts (v0.4.7), with the same three guardrails
    held as structure, not convention:

    - success-only: the sole call site sits on the verified-success
      path (post-mux, post-replace); every raise path (probe, guard,
      transcription, mux, cancel) skips it, so the scratch survives for
      diagnosis, mirroring Mode A's failure/cancel retention;
    - best-effort: an OSError logs a WARNING and the success stands —
      cleanup never fails a job and never re-raises;
    - path-guarded: ONLY the exact default-derived
      ``output_path + ".subs-work"`` directory is ever deleted
      (normcase/abspath identity). A caller-supplied ``workdir`` is the
      caller's own directory and is NEVER touched here.

    ``output_path`` is the PRE-replace ``{stem}.subs.mp4`` path — the
    scratch name never follows a replace-source rename. Returns the
    removed paths ([] when nothing was removed).
    """
    default_scratch = output_path + ".subs-work"
    if os.path.normcase(os.path.abspath(workdir_used)) != os.path.normcase(
        os.path.abspath(default_scratch)
    ):
        return []  # caller-managed workdir — the path guard
    scratch = Path(default_scratch)
    if not scratch.is_dir():
        return []
    try:
        # The core removes its WAV. Never recursively delete unexpected files.
        scratch.rmdir()
    except OSError as exc:
        if log_line is not None:
            log_line(
                f"WARNING subs scratch cleanup could not remove "
                f"{scratch}: {exc}"
            )
        return []
    if log_line is not None:
        log_line(f"cleanup subs scratch: {scratch}")
    return [str(scratch)]