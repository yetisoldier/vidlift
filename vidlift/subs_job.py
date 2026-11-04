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
from dataclasses import dataclass, field
from pathlib import Path

from .probe import classify, probe_file
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
    # v0.4.18 (WS3, D10/K-5): the scratch directory is captured ONCE
    # here, PRE-rename, replacing the inline expression at the core
    # call — so the replace-source rename of the output can never
    # confuse the cleanup target (cleanup_subs_scratch's path guard
    # compares exactly this name).
    workdir_used = workdir or (output_path + ".subs-work")

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

    final_output = output_path
    final_srt = transcribed.srt_path
    notes = list(transcribed.notes)
    # v0.4.18 (opt-in suppress_srt): the mux above SUCCEEDED, so the
    # sidecar is redundant — remove it BEFORE any replace, where the
    # helper's isfile guard (replace_subs_output step 4) then makes the
    # sidecar step a guaranteed no-op; on failure mplex already raised
    # ABOVE this line, so the sidecar survived as the fallback
    # deliverable. Removal is best-effort: an OSError keeps the file
    # and final_srt stays truthful (the helper logs the WARNING).
    if suppress_srt:
        if remove_srt_sidecar(transcribed.srt_path, log_line=notes.append):
            final_srt = ""
            notes.append("srt sidecar suppressed after a successful mux")
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
            )
            if maybe_srt is not None:
                final_srt = maybe_srt
        except (SubsJobError, OSError) as exc:
            # Mode A discipline: a replace failure is a warning that keeps
            # the verified success; both files stay on disk.
            notes.append(
                f"WARNING replace-source failed; keeping both files: {exc}"
            )

    # v0.4.18 (Karen K-5, success-only): the .subs-work scratch dir is
    # Mode A's cleanup_run_artifacts counterpart. This line is reached
    # only past a successful mux (and any requested replace); every
    # failure/cancel above retains the scratch for diagnosis.
    # workdir_used was captured PRE-rename; the helper's path guard
    # deletes only the default-derived directory.
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


def replace_subs_output(
    source_path: str,
    output_path: str,
    *,
    srt_path: str | None = None,
    log_line=None,
) -> tuple[str, str | None]:
    """Mode B replace (v0.4.17, opt-in): delete the source and land the
    verified subs output at the source's stem with an .mp4 extension.

    Discipline (mirrors run.replace_source_with_output's invariants, with
    rename-to-stem-.mp4 semantics):
    - rename-before-delete: the output moves onto {stem}.mp4 FIRST
      (atomic os.replace), the source is deleted SECOND; every failure
      mode keeps BOTH files (spec §2 deviation 1);
    - collision guard: a pre-existing {stem}.mp4 that is NOT the source
      (normcase/abspath identity) refuses the whole replace — an
      unrelated file is never overwritten;
    - source-is-.mp4 (target == source path): safe and intended — the
      single os.replace IS the delete-and-move;
    - cross-device (unreachable by construction — output and target share
      the source's directory — but defended): stage at a temp SIBLING of
      the target, shutil.copy2, SHA-256-verify against the verified
      output (from .run import _sha256_file — lazy, the _default_runner
      pattern), then os.replace; the temp never outlives the call;
    - sidecar: {stem}.subs.srt is renamed to {stem}.srt when (and only
      when) no unrelated {stem}.srt exists; on refusal or OSError the
      sidecar keeps its .subs.srt name — best-effort, never blocks the
      mp4 outcome;
    - raises SubsJobError on refusal/failure; the job-level caller keeps
      the verified success either way.

    Returns (final_output_path, final_srt_path_or_None): the second
    element is the sidecar's HONEST post-attempt location — the renamed
    path, the original path when the rename was refused/failed, or None
    when no sidecar exists. result.srt_path must never lie.
    """
    source = Path(source_path)
    output = Path(output_path)
    target = Path(subs_replace_target(source_path))
    target_srt = target.with_suffix(".srt")

    def _log(message: str) -> None:
        # The narration channel; None stays inert (the job-level caller
        # always passes a collector, the bare library call stays safe).
        if log_line is not None:
            log_line(message)

    # 1. guards (no mutation before this line)
    if not output.is_file():
        raise SubsJobError(
            f"replace-source refused: verified output is missing: {output}"
        )
    if not source.is_file():
        raise SubsJobError(
            f"replace-source refused: source is gone: {source}"
        )
    same_path = os.path.normcase(os.path.abspath(str(source))) == (
        os.path.normcase(os.path.abspath(str(target)))
    )
    if not same_path and target.exists():
        raise SubsJobError(
            f"replace-source refused: {target.name} already exists as a "
            "different file; keeping both files"
        )
    # 2. land the output at the target (atomic)
    if os.stat(str(source)).st_dev == os.stat(str(output)).st_dev:
        try:
            os.replace(str(output), str(target))
        except OSError as exc:
            raise SubsJobError(
                f"replace-source failed to move the output to {target}: {exc}"
            ) from exc
    else:
        # Defensive arm (v0.4.15.3 F-2 discipline, D3): unreachable by
        # construction today (output and target share the source's
        # directory), specced so the helper never silently relies on
        # caller geometry. Stage at a temp SIBLING of the target (same
        # directory -> same filesystem -> the final os.replace is an
        # atomic rename(2)); hash-verify the staged copy against the
        # verified output BEFORE anything else moves.
        tmp = str(target) + ".replace-tmp"
        if os.path.exists(tmp):
            try:
                os.remove(tmp)  # stale temp from a killed process
            except OSError:
                pass
        try:
            try:
                shutil.copy2(str(output), tmp)
                from .run import _sha256_file  # lazy: circular import

                if _sha256_file(str(output)) != _sha256_file(tmp):
                    raise SubsJobError(
                        "replace-source: the staged copy for "
                        f"{target} does not hash-match the verified "
                        "output; keeping everything (source, verified "
                        "output) untouched"
                    )
                os.replace(tmp, str(target))
            except OSError as exc:
                raise SubsJobError(
                    f"replace-source cross-device copy into {target} "
                    f"failed: {exc}"
                ) from exc
        finally:
            # The temp must never outlive the call (the run.py:1774-1783
            # pattern): the success path consumed it via os.replace;
            # every error path strands only an unproven copy.
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        # The copy hash-verified: the file at the target is now the good
        # data, so only the former output copy gets removed.
        try:
            output.unlink()
        except OSError as exc:
            _log(
                f"WARNING replace-source: the verified output copy at "
                f"{output} could not be removed: {exc}"
            )
    # 3. delete the source copy (skipped when same_path — the rename
    #    consumed the name)
    if not same_path:
        try:
            os.remove(str(source))
        except OSError as exc:
            _log(
                f"WARNING replace-source: output shipped to {target} "
                f"but the source copy could not be removed: {exc}"
            )
    # 4. sidecar (best-effort, guarded — D1)
    final_srt: str | None = None
    if srt_path is not None and os.path.isfile(srt_path):
        if os.path.normcase(os.path.abspath(srt_path)) == os.path.normcase(
            os.path.abspath(str(target_srt))
        ):
            final_srt = srt_path  # already matches (defensive)
        elif target_srt.exists():
            _log(
                f"WARNING replace-source: {target_srt.name} already "
                "exists; sidecar kept as " + os.path.basename(srt_path)
            )
            final_srt = srt_path
        else:
            try:
                os.replace(srt_path, str(target_srt))
                final_srt = str(target_srt)
            except OSError as exc:
                _log(f"WARNING replace-source: sidecar kept at {srt_path}: {exc}")
                final_srt = srt_path
    _log(f"replace-source: output now at {target}")
    return str(target), final_srt


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
        shutil.rmtree(scratch)
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