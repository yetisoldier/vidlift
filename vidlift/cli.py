"""VidLift CLI.

The CLI-first zero-touch rule from docs/design.md applies from day one:
every capability VidLift grows must be drivable headless, because a queue
of hours-long jobs cannot depend on a GUI babysitter.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .plan import PlanError, build_batch_plan
from .probe import ProbeError, probe_and_classify
from .run import MissingToolError, RunError, find_tools, run_file


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def _format_fps(value: float | None) -> str:
    if value is None:
        return "unknown"
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _render_report(report) -> str:
    lines = [report.path]
    if report.video is None:
        lines.append("  no video stream found")
    else:
        video = report.video
        if video.sar is not None:
            sar = f"{video.sar[0]}:{video.sar[1]}"
        else:
            sar = "unknown (assuming square pixels)"
        lines.append(
            f"  video: {video.codec_name} {video.width}x{video.height} "
            f"(SAR {sar}, displayed {report.displayed_width}x{report.displayed_height})"
        )
        fps = video.avg_frame_rate or video.r_frame_rate
        lines.append(
            f"  rate: {_format_fps(fps)} fps "
            f"| field order: {video.field_order or 'unknown'} "
            f"| duration: {_format_duration(report.duration_seconds)}"
        )
    if report.audio:
        parts = []
        for a in report.audio:
            channels = {1: "mono", 2: "stereo"}.get(a.channels, f"{a.channels}ch")
            lang = f" [{a.lang}]" if a.lang else ""
            parts.append(f"{a.codec_name} {channels}{lang}")
        lines.append("  audio: " + ", ".join(parts))
    else:
        lines.append("  audio: none")
    if report.subtitles:
        parts = [f"{s.codec_name} ({s.kind})" for s in report.subtitles]
        if report.sidecars:
            names = ", ".join(os.path.basename(sc.path) for sc in report.sidecars)
            parts.append(f"sidecar noted but not adopted ({names}; embedded subs win)")
        lines.append("  subtitles: " + ", ".join(parts))
    elif report.sidecars:
        names = ", ".join(os.path.basename(sc.path) for sc in report.sidecars)
        lines.append("  subtitles: none embedded; sidecar candidate: " + names)
    else:
        lines.append("  subtitles: none")
    lines.append(f"  source class: {report.source_class}")
    lines.append(f"  upscale: {report.upscale_verdict}")
    if report.pal_speedup_suspected:
        lines.append(
            "  PAL speedup: suspected (25 fps progressive; if this is a PAL "
            "transfer it plays ~4% fast; v0.2 will warn and process as-is)"
        )
    if report.interlaced_flagged:
        lines.append(
            "  interlace: flagged by the stream "
            "(v0.2 classifies progressive / IVTC / interlaced before filtering)"
        )
    return "\n".join(lines)


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    gb = value / 1_000_000_000
    return f"{gb:.2f} GB"


def _render_plan(item) -> str:
    report = item.report
    lines = [report.path]
    lines.append(f"  verdict: {item.process_verdict}")
    lines.append(f"  output: {item.output_path}")
    lines.append(
        f"  target: {item.output_width}x{item.output_height} HEVC MP4 "
        f"at {item.target_gb_per_hour:g} GB/hour"
    )
    lines.append(
        f"  bitrate plan: video {item.video_bitrate_kbps}k, "
        f"audio {item.audio.bitrate_kbps}k {item.audio.label}"
    )
    lines.append(f"  estimated size: {_format_bytes(item.estimated_output_bytes)}")
    lines.append(f"  subtitles: {item.subtitle_note}")
    # v0.4.1 (design §B.7): the plan names the disposition — which
    # track, how carried, OCR engine, and the honest style-loss note
    # for ASS sources (mov_text carries plain text + timing only).
    sp = item.subtitle_plan
    if sp is not None:
        if sp.source == "embedded":
            target = f"stream {sp.stream_index} ({sp.codec_name})"
        else:
            target = f"sidecar {os.path.basename(sp.sidecar_path)}"
        if sp.ocr_required:
            engine = "seconv" if os.name == "nt" else "tesseract"
            lines.append(f"  subtitle plan: {target} -> OCR ({engine}) -> mov_text + sidecar")
        else:
            lines.append(f"  subtitle plan: {target} -> mov_text + sidecar")
    if item.subtitle_style_loss:
        lines.append(
            "  warning: ASS/SSA styling does not survive mov_text "
            "conversion (plain text + timing only)"
        )
    lines.append(f"  expectation: {item.expectation}")
    if report.pal_speedup_suspected:
        lines.append("  warning: PAL speedup suspected; VidLift will process as-is")
    if report.interlaced_flagged:
        lines.append("  warning: interlace flagged; engine must deinterlace before upscale")
    if item.process_verdict == "output-exists":
        # v0.4.12 (item 3): plan-time collision gate — block-until-forced;
        # no force flag ships this cycle, so the action names the fix.
        lines.append(
            f"  warning: output exists: {os.path.basename(item.output_path)} "
            "(will not be overwritten)"
        )
        lines.append("  action: resolve the existing file, then re-plan")
    elif item.process_verdict != "ready":
        lines.append("  action: skip unless explicitly forced in a later pipeline version")
    return "\n".join(lines)


# v0.4.8 (design D6): the one additive anime expectation sentence,
# appended ONLY at the run-report surfaces that already hold the
# profile in scope (CLI run-report, GUI detail text) — plan.py stays
# content-agnostic (the profile is a run knob, not a plan knob) and
# PlannedFile.expectation is stable for every plan-level consumer.
ANIME_EXPECTATION_SUFFIX = (
    "Anime upscaling model: strong fit for flat fills and line art "
    "(common in cel animation); not for live-action content."
)


def _cmd_probe(files: list[str]) -> int:
    failures = 0
    for path in files:
        try:
            report = probe_and_classify(path)
        except ProbeError as exc:
            failures += 1
            print(f"{path}: error: {exc}", file=sys.stderr)
            continue
        print(_render_report(report))
        print()
    if failures:
        print(
            f"{failures} of {len(files)} file(s) failed to probe",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_plan(
    files: list[str],
    *,
    target: str,
    gbph: float,
    output_dir: str | None,
    no_transcribe: bool = False,
) -> int:
    reports = []
    failures = 0
    for path in files:
        try:
            reports.append(probe_and_classify(path))
        except ProbeError as exc:
            failures += 1
            print(f"{path}: error: {exc}", file=sys.stderr)
    if reports:
        try:
            batch = build_batch_plan(
                reports,
                target=target,
                gb_per_hour=gbph,
                output_dir=output_dir,
                # v0.4.15.4 (F-14a): the headless off switch threads
                # through the absent-defaults rule — None rides the
                # config default, never a hardcoded read.
                transcription_enabled=(False if no_transcribe else None),
            )
        except PlanError as exc:
            print(f"plan error: {exc}", file=sys.stderr)
            return 2
        for item in batch.files:
            print(_render_plan(item))
            print()
        print(
            f"summary: {batch.runnable_count}/{len(batch.files)} ready, "
            f"estimated total {_format_bytes(batch.total_estimated_bytes)}"
        )
    if failures:
        print(
            f"{failures} of {len(files)} file(s) failed to probe",
            file=sys.stderr,
        )
        return 1
    return 0


def _cmd_run(
    file_path: str,
    *,
    target: str,
    gbph: float,
    output_dir: str | None,
    ffmpeg: str | None,
    ffprobe: str | None,
    log_dir: str | None,
    engine: str,
    realesrgan: str | None,
    realesrgan_models: str | None,
    replace_source: bool = False,
    upscale_profile: str = "live-action",
    resume: bool = False,
    denoise: str = "off",
    no_transcribe: bool = False,
    # v0.4.18 (opt-in sidecar suppression): trailing kw-only param; the
    # flag rides into run_file after the transcription switch.
    suppress_srt: bool = False,
) -> int:
    """Drive one file through the runner and print its state.

    Exit codes: 0 verified run, 1 run/verification failure,
    2 missing tool (a setup problem, not a media problem).
    """
    try:
        tools = find_tools(ffmpeg=ffmpeg, ffprobe=ffprobe)
    except MissingToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        result = run_file(
            file_path,
            target=target,
            gb_per_hour=gbph,
            output_dir=output_dir,
            tools=tools,
            log_dir=log_dir,
            engine_request=engine,
            realesrgan=realesrgan,
            realesrgan_models=realesrgan_models,
            replace_source=replace_source,
            profile=upscale_profile,
            resume=resume,
            # v0.4.15.4 (F-14a): the headless off switch — None rides the
            # config default (run.py's absent-defaults rule); False is
            # the explicit CLI off.
            transcription_enabled=(False if no_transcribe else None),
            # v0.4.18 (opt-in suppress_srt): the explicit flag — absent
            # (never possible through argparse's boolean) still reads
            # False at run_file's seam (absent-defaults aligned).
            suppress_srt=suppress_srt,
        )
    except (ProbeError, PlanError, RunError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        state = getattr(exc, "state", None)
        if state is not None:
            if state.log is not None:
                print(f"run log: {state.log.path}", file=sys.stderr)
                for line in state.log.tail(6):
                    print(f"  {line}", file=sys.stderr)
            if state.manifest_path:
                print(f"run manifest: {state.manifest_path}", file=sys.stderr)
        return 1
    state = result.state
    print(f"input: {state.file_path}")
    print(f"output: {result.plan.output_path}")
    verdict = "verified" if result.verification.ok else "failed"
    print(f"verification: {verdict}")
    # v0.4.8 (D6): the anime expectation note renders ONLY at the
    # run-report surfaces that hold the profile (never in plan.py) —
    # appended to the plan's own expectation sentence when the user
    # chose the anime profile.
    if getattr(state, "upscale_profile", None) == "anime":
        print(f"expectation: {result.plan.expectation}")
        print(f"  {ANIME_EXPECTATION_SUFFIX}")
    if state.fail_open:
        # The final report states exactly what shipped and why (clause d):
        # a fail-open run is a completed, verified re-encode with NO AI
        # pass, and the summary never lets that pass unmentioned.
        print(
            "engine: FAILED OPEN - no AI pass shipped; output is a real "
            "re-encode of the prepared source"
        )
        print(f"fail-open reason: {state.fail_open_reason}")
    elif state.engine_simulated:
        print(
            "engine: STUB (no Real-ESRGAN pass; output is a real re-encode "
            "of the prepared source, not an AI upscale)"
        )
    if state.engine_note:
        print(f"engine note: {state.engine_note}")
    # v0.4.7: when delete-and-replace shipped, the printed output path is
    # the SOURCE's old location, not the plan's output path.
    # v0.4.7 r2 (Karen D1): line reports the OUTCOME, not the request --
    # state.file_path == state.output_path holds only after
    # replace_source_with_output moved the output (run.py sets
    # state.output_path then); a refused replace leaves output_path at
    # the plan path and gets an honest kept-both notice instead.
    if replace_source and result.verification.ok:
        normalized_file = os.path.normcase(os.path.abspath(state.file_path))
        normalized_output = os.path.normcase(
            os.path.abspath(state.output_path)
        )
        if normalized_file == normalized_output:
            print(f"source replaced: now {state.file_path}")
        elif normalized_file != os.path.normcase(
            os.path.abspath(result.plan.output_path)
        ):
            print(
                "source NOT replaced: both files kept "
                "(see run log) 'WARNING: replace-source failed'"
            )
    # v0.4.1: the subtitle outcome, honestly — carried/ocr/none/failed-open
    subs = state.subtitles
    if subs is not None:
        status = subs.get("status", "none")
        if status == "failed-open":
            print(
                "subtitles: FAILED OPEN - shipped without subtitles; "
                f"{subs.get('reason', 'unknown reason')}"
            )
            for warning in subs.get("warnings", []):
                print(f"  subtitle warning: {warning}")
        elif status == "ocr":
            ocr = subs.get("ocr", {})
            print(
                f"subtitles: OCR carried ({ocr.get('engine', '?')}, "
                f"{ocr.get('cue_count', '?')} cues); spot-check spelling"
            )
            # v0.4.18 (opt-in suppress_srt, S-14): a suppressed success
            # has no sidecar to name — the print never claims one.
            if subs.get("sidecar_suppressed"):
                print("  sidecar: suppressed after a successful embed")
            elif subs.get("sidecar_path"):
                print(f"  sidecar: {subs.get('sidecar_path')}")
        elif status == "carried":
            # v0.4.18 (opt-in suppress_srt, S-13): the suppressed arm
            # replaces the "+ .srt sidecar" claim; the default arm
            # prints the unchanged v0.4.17 line.
            if subs.get("sidecar_suppressed"):
                print(
                    "subtitles: carried (mov_text embedded; "
                    ".srt sidecar suppressed)"
                )
            else:
                print("subtitles: carried (mov_text embedded + .srt sidecar)")
        else:
            print("subtitles: none")
    print(f"log: {state.log.path if state.log else 'none'}")
    print(f"manifest: {result.manifest_path}")
    print("phases:")
    for phase in state.phases:
        duration = phase.duration_seconds
        duration_s = f"{duration:.2f}s" if duration is not None else "running"
        print(f"  {phase.name}: {duration_s}")
    return 0 if result.ok else 1


def _cmd_subs(
    files: list[str],
    *,
    lang: str,
    model: str | None,
    device: str,
    force: bool,
    ffmpeg: str | None,
    ffprobe: str | None,
    # v0.4.17 (opt-in replace-source): the kw-only param rides the
    # argparse flag; exit codes unchanged — a refused/failed replace is
    # a warning note inside a succeeded job (keep-both discipline).
    replace_source: bool = False,
    # v0.4.18 (opt-in sidecar suppression): trailing kw, same pattern.
    suppress_srt: bool = False,
) -> int:
    """Mode B: transcribe + soft-mux; exit 0 ok, 1 job failure, 2 tool."""
    from .subs_job import (
        SUBS_VERDICT_BLOCKED_OUTPUT,
        SUBS_VERDICT_SKIP_SUBTITLES,
        SubsJobError,
        run_subs_job,
        subs_output_path,
        subs_plan_verdict,
    )
    from .transcribe import TranscriptionError

    try:
        tools = find_tools(ffmpeg=ffmpeg, ffprobe=ffprobe)
    except MissingToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    failures = 0
    for path in files:
        print(f"== {path}")
        # v0.4.19 (WS2): the plan-time verdict — already-done files are
        # SKIPPED, not run-side failures (the end-state already holds;
        # WS2-D9: neither counts toward `failures`). --force remains the
        # subtitle override; a probe fault enqueues to the run-side truth.
        verdict = subs_plan_verdict(path, ffprobe=tools.ffprobe, force=force)
        if verdict == SUBS_VERDICT_SKIP_SUBTITLES:
            print("  skip — already has subtitles")
            continue
        if verdict == SUBS_VERDICT_BLOCKED_OUTPUT:
            print(
                "  blocked — output already exists (never overwritten): "
                f"{subs_output_path(path)}"
            )
            continue
        try:
            result = run_subs_job(
                path,
                ffmpeg=tools.ffmpeg,
                ffprobe=tools.ffprobe,
                model_pin=model,
                device_pin=device,
                language_pin=lang,
                force=force,
                replace_source=replace_source,
                suppress_srt=suppress_srt,
            )
        except SubsJobError as exc:
            print(f"error: {exc}", file=sys.stderr)
            failures += 1
            continue
        except TranscriptionError as exc:
            print(f"error: transcription unavailable: {exc}", file=sys.stderr)
            failures += 1
            continue
        print(
            f"  output: {result.output_path}\n"
            f"  language: {result.language} "
            f"(confidence {result.language_confidence})\n"
            f"  cues: {result.cue_count}  model: {result.model}"
            f"\n  device: {result.device or 'unknown'}"
        )
        for note in result.notes:
            print(f"  note: {note}")
    return 1 if failures else 0


def _cmd_benchmark(
    file_path: str,
    *,
    duration: float,
    engine_request: str,
    ffmpeg: str | None,
    ffprobe: str | None,
    realesrgan: str | None,
    realesrgan_models: str | None,
    log_dir: str | None,
) -> int:
    """Measure one benchmark slice and print its provenance (roadmap 4).

    Exit codes: 0 measured, 1 measurement failure, 2 missing tool.
    """
    try:
        tools = find_tools(ffmpeg=ffmpeg, ffprobe=ffprobe)
    except MissingToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        from .engine_ncnn import select_engine

        engine, _note = select_engine(
            engine_request, binary=realesrgan, models_dir=realesrgan_models
        )
    except MissingToolError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    from .benchmark import BenchmarkCacheError, run_benchmark

    try:
        result = run_benchmark(
            file_path,
            tools=tools,
            engine=engine,
            slice_seconds=duration,
            workdir=log_dir,
        )
    except (BenchmarkCacheError, ProbeError, PlanError, RunError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"sample: {file_path}")
    print(f"engine: {result.engine}")
    if result.model:
        print(
            f"model: {result.model} (native scale {result.native_scale})"
        )
    print(f"geometry bucket: {result.geometry_bucket}")
    print(f"slice: {result.duration_sliced_s:g} s, {result.frames} frames")
    print(f"engine phase wall time: {result.wall_seconds:.3f} s")
    print(f"seconds per frame: {result.seconds_per_frame:.6f}")
    # provenance FIRST-CLASS: a stub benchmark is machinery timing and
    # the summary says so, never letting the number pose as an AI pass
    if result.engine_simulated:
        print("engine_simulated: true")
        print(f"note: stub numbers; not an AI-pass measurement.")
    else:
        print("engine_simulated: false")
    for note in result.notes:
        print(f"note: {note}")
    print(f"measured at: {result.measured_at:.0f}")
    print("cache: benchmark constant recorded for this engine+geometry bucket")
    return 0


def _cmd_selftest() -> int:
    """v0.4.16.2 (ADR-4): the frozen binary reports its own transcription
    capability — the release gate's authoritative voice (faster-whisper's
    OWN loader proves the payload, not our derivation). Lazy import keeps
    cli.py import-light; the suite drives main() with this seam
    monkeypatched."""
    import json
    from .transcribe import selftest_report

    report = selftest_report()
    print("vidlift-selftest " + json.dumps(report, sort_keys=True), flush=True)
    return 0 if report.get("ok") is True else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vidlift",
        description=(
            "Batch AI upscaler for SD video files to 720p HEVC MP4"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"vidlift {__version__}"
    )
    # v0.4.16.2 (ADR-4): the frozen binary's self-reporting capability gate —
    # dispatched FIRST in main(), before any subcommand chain.
    parser.add_argument(
        "--selftest",
        action="store_true",
        help=(
            "check this install's transcription capability (engine package "
            "+ bundled VAD asset); prints 'vidlift-selftest {json}', "
            "exit 0 pass / 1 fail"
        ),
    )
    sub = parser.add_subparsers(dest="command")
    probe_parser = sub.add_parser(
        "probe", help="probe and classify video files"
    )
    probe_parser.add_argument(
        "files", nargs="+", metavar="FILE", help="video files to inspect"
    )
    plan_parser = sub.add_parser(
        "plan", help="pre-flight a batch before an upscale run"
    )
    plan_parser.add_argument(
        "files", nargs="+", metavar="FILE", help="video files to pre-flight"
    )
    plan_parser.add_argument(
        "--target",
        choices=["720p"],
        default="720p",
        help="output frame target (default: 720p)",
    )
    plan_parser.add_argument(
        "--gbph",
        type=float,
        default=1.5,
        help="target gigabytes per hour (default: 1.5)",
    )
    plan_parser.add_argument(
        "--output-dir",
        help="directory for planned outputs (default: beside each input)",
    )
    plan_parser.add_argument(
        "--no-transcribe",
        action="store_true",
        help=(
            "disable Mode A fallback transcription for this run "
            "(default: enabled — config TRANSCRIPTION_ENABLED_DEFAULT)"
        ),
    )
    run_parser = sub.add_parser(
        "run", help="run one file end-to-end through the runner pipeline"
    )
    run_parser.add_argument(
        "file", metavar="FILE", help="the single video file to run"
    )
    run_parser.add_argument(
        "--target",
        choices=["720p"],
        default="720p",
        help="output frame target (default: 720p)",
    )
    run_parser.add_argument(
        "--gbph",
        type=float,
        default=1.5,
        help="target gigabytes per hour (default: 1.5)",
    )
    run_parser.add_argument(
        "--output-dir",
        help="directory for the output file (default: beside the input)",
    )
    run_parser.add_argument(
        "--ffmpeg",
        help="explicit ffmpeg path (else VIDLIFT_FFMPEG, then PATH)",
    )
    run_parser.add_argument(
        "--ffprobe",
        help="explicit ffprobe path (else VIDLIFT_FFPROBE, then PATH)",
    )
    run_parser.add_argument(
        "--log-dir",
        help="directory for run logs/manifests (default: INPUT_DIR/.vidlift-runs)",
    )
    run_parser.add_argument(
        "--replace-source",
        action="store_true",
        help=(
            "after a verified upscale, delete the source video and move the "
            "MP4 into its place (IRREVERSIBLE: the original SD encode is "
            "destroyed; default: keep both files)"
        ),
    )
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "claim retained scratch from an earlier failed/cancelled run of "
            "this same file when source, plan, and prepared content all "
            "prove equal; otherwise runs clean (default: prepare fresh)"
        ),
    )
    # v0.4.18 (opt-in sidecar suppression, D6): strictly opt-in; the
    # help names the fail-open fallback promise (S-11).
    run_parser.add_argument(
        "--suppress-srt",
        action="store_true",
        help=(
            "after subtitles are embedded in the verified output, delete "
            "the redundant .srt sidecar; if the embed fails open the "
            "sidecar is kept as the fallback (default: keep the sidecar)"
        ),
    )
    run_parser.add_argument(
        "--engine",
        choices=["auto", "stub", "ncnn"],
        default="auto",
        help=(
            "upscale engine: auto = real ncnn engine when it resolves and a "
            "GPU probe frame succeeds, else the stub with a note; "
            "stub = force the honest no-AI stub; ncnn = force the real "
            "engine (missing tools become named setup errors)"
        ),
    )
    run_parser.add_argument(
        "--upscale-profile",
        choices=["live-action", "anime"],
        default="live-action",
        help=(
            'default "live-action"; choose "anime" for cel-animation / '
            "flat-fill sources (DVD cartoons, anime), where the "
            "animevideov3-x2 model is faster and avoids false texture in "
            "fills. Manual switch only — VidLift does not auto-detect "
            "content type."
        ),
    )
    run_parser.add_argument(
        "--denoise",
        choices=["off", "light", "strong"],
        default="off",
        help=(
            "denoise the source before upscaling: 'off' (default), 'light', or "
            "'strong'. Use for noisy sources (VHS rips, old tape transfers, grainy "
            "analog captures); leave off for clean DVD/Blu-ray rips — the upscale "
            "already softens grain, and denoising a clean source costs fine detail "
            "for no gain. Adds almost no time compared to the upscale itself."
        ),
    )
    run_parser.add_argument(
        "--no-transcribe",
        action="store_true",
        help=(
            "disable Mode A fallback transcription for this run "
            "(default: enabled — config TRANSCRIPTION_ENABLED_DEFAULT)"
        ),
    )
    run_parser.add_argument(
        "--realesrgan",
        help="explicit realesrgan-ncnn-vulkan path (else VIDLIFT_REALESRGAN_NCNN, then bundled)",
    )
    run_parser.add_argument(
        "--realesrgan-models",
        help="explicit model dir (else VIDLIFT_REALESRGAN_MODELS, then models/ beside the binary)",
    )
    subs_parser = sub.add_parser(
        "subs",
        help=(
            "transcribe a file's own audio and soft-mux subtitles into a NEW "
            "{stem}.subs.mp4 beside it (Mode B; no upscale, source untouched)"
        ),
    )
    subs_parser.add_argument(
        "files", metavar="FILE", nargs="+", help="video file(s) to subtitle"
    )
    subs_parser.add_argument(
        "--lang", default="", help="pin the transcription language (ISO 639-1, e.g. ko); default auto-detect"
    )
    subs_parser.add_argument(
        "--model", default=None, help="whisper model tag or path (default: medium; first use downloads it)"
    )
    subs_parser.add_argument(
        "--device", choices=["auto", "cuda", "cpu"], default="auto",
        help="transcription device (default auto)"
    )
    subs_parser.add_argument(
        "--force", action="store_true",
        help="transcribe even when a usable text subtitle track exists"
    )
    # v0.4.17 (opt-in Mode B replace-source): the Mode A --replace-source
    # pattern (cli.py's run-parser wording) with the Mode B naming rule
    # spelled out — the target is always {stem}.mp4.
    subs_parser.add_argument(
        "--replace-source",
        action="store_true",
        help=(
            "after a verified subtitles pass, delete the source video and "
            "rename the output to the source's name with an .mp4 extension "
            "(e.g. …35 Hours.avi → …35 Hours.mp4); if a file of that name "
            "already exists, both files are kept (default: off)"
        ),
    )
    # v0.4.18 (opt-in sidecar suppression, D6): strictly opt-in; the
    # help names the mux-failure fallback (S-12).
    subs_parser.add_argument(
        "--suppress-srt",
        action="store_true",
        help=(
            "after the subtitle track is muxed into the MP4, delete the "
            "redundant .srt sidecar (on a mux failure the sidecar is kept "
            "as the fallback; default: keep the sidecar)"
        ),
    )
    subs_parser.add_argument("--ffmpeg", help="explicit ffmpeg path")
    subs_parser.add_argument("--ffprobe", help="explicit ffprobe path")
    benchmark_parser = sub.add_parser(
        "benchmark",
        help="measure one sample slice to pin an ETA constant for this machine",
    )
    benchmark_parser.add_argument(
        "file", metavar="FILE", help="a real sample video file on this machine"
    )
    benchmark_parser.add_argument(
        "--duration",
        type=float,
        default=30,
        help="slice length in seconds, clamped to [10, 120] (default: 30)",
    )
    benchmark_parser.add_argument(
        "--engine",
        choices=["auto", "stub", "ncnn"],
        default="auto",
        help="engine to benchmark through (default: auto)",
    )
    benchmark_parser.add_argument(
        "--realesrgan",
        help="explicit realesrgan-ncnn-vulkan path",
    )
    benchmark_parser.add_argument(
        "--realesrgan-models",
        help="explicit model dir",
    )
    benchmark_parser.add_argument(
        "--ffmpeg",
        help="explicit ffmpeg path (else VIDLIFT_FFMPEG, then PATH)",
    )
    benchmark_parser.add_argument(
        "--ffprobe",
        help="explicit ffprobe path (else VIDLIFT_FFPROBE, then PATH)",
    )
    benchmark_parser.add_argument(
        "--work-dir",
        help="scratch directory for the benchmark slice (default: a temp dir)",
    )
    # v0.4.9 (design §1.6): the prune surface over the scratch GC.
    runs_parser = sub.add_parser(
        "runs",
        help="scratch-run maintenance (prune retained scratch sets)",
    )
    runs_sub = runs_parser.add_subparsers(dest="runs_command")
    prune_parser = runs_sub.add_parser(
        "prune",
        help="remove stale retained scratch sets (newest-2 + 7-day floor)",
    )
    prune_parser.add_argument(
        "file",
        nargs="?",
        default=None,
        help="a media file whose SOURCE_DIR/.vidlift-runs is pruned",
    )
    prune_parser.add_argument(
        "--log-dir",
        default=None,
        help="the runs directory to prune (overrides the FILE form)",
    )
    prune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be removed; delete nothing",
    )
    mcp_parser = sub.add_parser(
        "mcp-server",
        help="run the VidLift MCP bridge as a stdio server (for MCP clients)",
    )
    mcp_parser.add_argument(
        "--state-dir",
        default=None,
        help="state directory for the job registry and benchmark cache",
    )
    sub.add_parser("gui", help="open the VidLift planning workbench")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # v0.4.16.2 (ADR-4): the selftest flag outranks every subcommand —
    # an argparse flag (not a subcommand), dispatched first so a frozen
    # binary that cannot transcribe still answers honestly.
    if getattr(args, "selftest", False):
        return _cmd_selftest()
    if args.command == "probe":
        return _cmd_probe(args.files)
    if args.command == "plan":
        return _cmd_plan(
            args.files,
            target=args.target,
            gbph=args.gbph,
            output_dir=args.output_dir,
            no_transcribe=args.no_transcribe,
        )
    if args.command == "run":
        return _cmd_run(
            args.file,
            target=args.target,
            gbph=args.gbph,
            output_dir=args.output_dir,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            log_dir=args.log_dir,
            engine=args.engine,
            realesrgan=args.realesrgan,
            realesrgan_models=args.realesrgan_models,
            replace_source=args.replace_source,
            upscale_profile=args.upscale_profile,
            resume=args.resume,
            denoise=args.denoise,
            no_transcribe=args.no_transcribe,
            # v0.4.18 (opt-in sidecar suppression)
            suppress_srt=args.suppress_srt,
        )
    if args.command == "subs":
        return _cmd_subs(
            args.files,
            lang=args.lang,
            model=args.model,
            device=args.device,
            force=args.force,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            replace_source=args.replace_source,
            # v0.4.18 (opt-in sidecar suppression)
            suppress_srt=args.suppress_srt,
        )
    if args.command == "runs":
        # v0.4.9 (design §1.6): the prune surface over the scratch GC.
        from .run import prune_plan, gc_stale_scratch

        if getattr(args, "runs_command", None) != "prune":
            print("usage: vidlift runs prune [--dry-run]", file=sys.stderr)
            return 2
        file_argv = Path(args.file) if getattr(args, "file", None) else None
        root = (
            Path(args.log_dir)
            if getattr(args, "log_dir", None)
            else (
                (file_argv.parent / ".vidlift-runs")
                if file_argv is not None
                else None
            )
        )
        if root is None:
            print(
                "error: runs prune needs --log-dir (no FILE in this form)",
                file=sys.stderr,
            )
            return 2
        if not root.is_dir():
            print(
                f"error: not found: {root}",
                file=sys.stderr,
            )
            return 2
        decisions = (
            prune_plan(root) if args.dry_run else gc_stale_scratch(root)
        )
        for decision in decisions:
            paths = ", ".join(decision["paths"])
            print(
                f"{'would remove' if args.dry_run else 'removed'}: "
                f"{decision['run_id']} ({decision['reason']}): {paths}"
            )
        if not decisions:
            print("nothing to prune")
        return 0
    if args.command == "benchmark":
        return _cmd_benchmark(
            args.file,
            duration=args.duration,
            engine_request=args.engine,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
            realesrgan=args.realesrgan,
            realesrgan_models=args.realesrgan_models,
            log_dir=args.work_dir,
        )
    if args.command == "gui":
        from .gui import main as gui_main

        return gui_main([])
    if args.command == "mcp-server":
        from .mcp_server import main as mcp_main

        return mcp_main(["--state-dir", args.state_dir] if args.state_dir else [])
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())