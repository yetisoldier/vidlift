"""VidLift CLI.

The CLI-first zero-touch rule from docs/design.md applies from day one:
every capability VidLift grows must be drivable headless, because a queue
of hours-long jobs cannot depend on a GUI babysitter.
"""

from __future__ import annotations

import argparse
import sys

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
        lines.append("  subtitles: " + ", ".join(parts))
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
    lines.append(f"  expectation: {item.expectation}")
    if report.pal_speedup_suspected:
        lines.append("  warning: PAL speedup suspected; VidLift will process as-is")
    if report.interlaced_flagged:
        lines.append("  warning: interlace flagged; engine must deinterlace before upscale")
    if item.process_verdict != "ready":
        lines.append("  action: skip unless explicitly forced in a later pipeline version")
    return "\n".join(lines)


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


def _cmd_plan(files: list[str], *, target: str, gbph: float, output_dir: str | None) -> int:
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
                reports, target=target, gb_per_hour=gbph, output_dir=output_dir
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
    if state.engine_simulated:
        print(
            "engine: STUB (no Real-ESRGAN pass; output is a real re-encode "
            "of the prepared source, not an AI upscale)"
        )
    print(f"log: {state.log.path if state.log else 'none'}")
    print(f"manifest: {result.manifest_path}")
    print("phases:")
    for phase in state.phases:
        duration = phase.duration_seconds
        duration_s = f"{duration:.2f}s" if duration is not None else "running"
        print(f"  {phase.name}: {duration_s}")
    return 0 if result.ok else 1


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
    sub.add_parser("gui", help="open the VidLift planning workbench")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "probe":
        return _cmd_probe(args.files)
    if args.command == "plan":
        return _cmd_plan(
            args.files,
            target=args.target,
            gbph=args.gbph,
            output_dir=args.output_dir,
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
        )
    if args.command == "gui":
        from .gui import main as gui_main

        return gui_main([])
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())