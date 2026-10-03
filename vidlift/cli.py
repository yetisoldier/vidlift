"""VidLift CLI.

v0.1 ships `vidlift probe FILE...` and `vidlift --version`. The encode
and batch commands arrive with v0.2 (engine gated on bluray-to-mp4
v0.2.19). The CLI-first zero-touch rule from docs/design.md applies
from day one: every capability VidLift ever grows must be drivable
headless, because a queue of hours-long jobs cannot depend on a GUI
babysitter.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .probe import ProbeError, probe_and_classify


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vidlift",
        description=(
            "Batch AI upscaler for SD video files to 720p HEVC MP4 "
            "(v0.1: probe and classify only; the engine arrives with v0.2)"
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "probe":
        return _cmd_probe(args.files)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())