"""Pre-flight planning for VidLift batch jobs.

The planner is the v0.2 front door: it turns the probe/classify report
into a plain, testable job description before any hours-long upscale or
encode starts. It does not run Real-ESRGAN or ffmpeg; later pipeline code
consumes this plan.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path

from .probe import (
    FileReport,
    SOURCE_DVD_MPEG2_RIP,
    SOURCE_H264_SD,
    SOURCE_OTHER,
    SOURCE_XVID_DIVX_AVI,
)

__all__ = [
    "AudioPlan",
    "BatchPlan",
    "PlanError",
    "PlannedFile",
    "attach_estimate",
    "build_batch_plan",
    "build_file_plan",
    "estimate_video_bitrate_kbps",
    "output_geometry",
]

GB = 1_000_000_000
CONTAINER_SLACK = 0.03
DEFAULT_GB_PER_HOUR = 1.5
AUDIO_KBPS = {8: 640, 6: 448, 2: 192, 1: 128}


class PlanError(ValueError):
    """The requested plan cannot be built from the probed file."""


@dataclass(frozen=True)
class AudioPlan:
    channels: int
    bitrate_kbps: int
    label: str


@dataclass(frozen=True)
class PlannedFile:
    report: FileReport
    target: str
    output_path: str
    output_width: int
    output_height: int
    target_gb_per_hour: float
    audio: AudioPlan
    video_bitrate_kbps: int
    estimated_output_bytes: int | None
    expectation: str
    subtitle_note: str
    process_verdict: str
    # P3 additive (attach_estimate): measured ETA mapping from the
    # benchmark cache, None when no constant exists for this geometry
    # bucket. Additive with a default, so every existing constructor
    # call and consumer is unaffected.
    eta_estimate: dict | None = None


@dataclass(frozen=True)
class BatchPlan:
    files: list[PlannedFile]

    @property
    def runnable_count(self) -> int:
        return sum(1 for item in self.files if item.process_verdict == "ready")

    @property
    def total_estimated_bytes(self) -> int | None:
        total = 0
        for item in self.files:
            if item.estimated_output_bytes is None:
                return None
            total += item.estimated_output_bytes
        return total


def estimate_video_bitrate_kbps(
    *,
    target_gb_per_hour: float = DEFAULT_GB_PER_HOUR,
    audio_channels: int = 2,
    audio_kbps: int | None = None,
) -> int:
    """Video bitrate that fits the requested GB/hour target."""
    audio = audio_kbps or AUDIO_KBPS.get(min(audio_channels, 8), 192)
    avg_total_kbps = target_gb_per_hour * GB * 8 / 1000 / 3600
    avg_total_kbps *= 1 - CONTAINER_SLACK
    return max(int(avg_total_kbps - audio), 500)


def output_geometry(report: FileReport, target: str = "720p") -> tuple[int, int]:
    """Return the 720p-frame output geometry for a classified file."""
    if target != "720p":
        raise PlanError(f"unsupported target: {target}")
    if report.video is None:
        raise PlanError("cannot plan a file with no video stream")
    if report.displayed_width <= 0 or report.displayed_height <= 0:
        raise PlanError("cannot plan a file with unusable geometry")

    ratio = report.displayed_width / report.displayed_height
    width = int(math.ceil(720 * ratio))
    if width % 2:
        width += 1
    width = min(width, 1280)
    return width, 720


def _audio_plan(report: FileReport) -> AudioPlan:
    if not report.audio:
        return AudioPlan(channels=0, bitrate_kbps=0, label="none")
    channels = max((a.channels for a in report.audio), default=2)
    bitrate = AUDIO_KBPS.get(min(channels, 8), 192)
    label = {1: "mono", 2: "stereo"}.get(channels, f"{channels}ch")
    return AudioPlan(channels=channels, bitrate_kbps=bitrate, label=label)


def _expected_output_path(input_path: str, output_dir: str | None) -> str:
    source = Path(input_path)
    directory = Path(output_dir) if output_dir else source.parent
    return str(directory / f"{source.stem}.vidlift.720p.hevc.mp4")


def _expectation(report: FileReport) -> str:
    if report.source_class == SOURCE_DVD_MPEG2_RIP:
        return "best 720p that source can give; good, not transformative"
    if report.source_class == SOURCE_XVID_DIVX_AVI:
        return "cleaner on a TV, but detail missing from the rip cannot be recovered"
    if report.source_class == SOURCE_H264_SD:
        return "likely modest improvement; inspect the sample before a full run"
    if report.source_class == SOURCE_OTHER:
        return "unknown source type; inspect the sample before a full run"
    return "inspect the sample before a full run"


def _subtitle_note(report: FileReport) -> str:
    if not report.subtitles:
        return "none"
    text = sum(1 for s in report.subtitles if s.kind == "text")
    image = sum(1 for s in report.subtitles if s.kind == "image")
    other = len(report.subtitles) - text - image
    parts: list[str] = []
    if text:
        parts.append(f"{text} text stream(s) can be carried as mov_text")
    if image:
        parts.append(f"{image} image stream(s) listed only; no OCR in VidLift")
    if other:
        parts.append(f"{other} unknown subtitle stream(s) listed only")
    return "; ".join(parts)


def _process_verdict(report: FileReport) -> str:
    if report.upscale_verdict == "candidate":
        return "ready"
    return report.upscale_verdict


def build_file_plan(
    report: FileReport,
    *,
    target: str = "720p",
    gb_per_hour: float = DEFAULT_GB_PER_HOUR,
    output_dir: str | None = None,
) -> PlannedFile:
    """Build a pre-flight plan for one classified file."""
    if gb_per_hour <= 0:
        raise PlanError("gb_per_hour must be greater than zero")
    if report.video is None:
        raise PlanError("cannot plan a file with no video stream")
    width, height = output_geometry(report, target)
    audio = _audio_plan(report)
    video_kbps = estimate_video_bitrate_kbps(
        target_gb_per_hour=gb_per_hour,
        audio_channels=max(audio.channels, 1),
        audio_kbps=audio.bitrate_kbps or None,
    )
    estimated_bytes = None
    if report.duration_seconds is not None:
        estimated_bytes = int(gb_per_hour * GB * report.duration_seconds / 3600)
    return PlannedFile(
        report=report,
        target=target,
        output_path=_expected_output_path(report.path, output_dir),
        output_width=width,
        output_height=height,
        target_gb_per_hour=gb_per_hour,
        audio=audio,
        video_bitrate_kbps=video_kbps,
        estimated_output_bytes=estimated_bytes,
        expectation=_expectation(report),
        subtitle_note=_subtitle_note(report),
        process_verdict=_process_verdict(report),
    )


def build_batch_plan(
    reports: list[FileReport],
    *,
    target: str = "720p",
    gb_per_hour: float = DEFAULT_GB_PER_HOUR,
    output_dir: str | None = None,
) -> BatchPlan:
    """Build a batch pre-flight plan from classified probe reports."""
    return BatchPlan(
        [
            build_file_plan(
                report,
                target=target,
                gb_per_hour=gb_per_hour,
                output_dir=output_dir,
            )
            for report in reports
        ]
    )


def attach_estimate(
    plan: PlannedFile,
    estimates: dict,
    *,
    estimate_fn=None,
) -> PlannedFile:
    """Return ``plan`` with a measured wall-clock estimate attached (P3).

    The one additive seam the design pins (real-engine-adapter.md
    Section 2): UI/CLI/MCP render measured ETAs from the benchmark
    cache without the planner ever growing ETA math. ``estimates`` is
    the dict ``load_estimates`` returns; ``estimate_fn`` defaults to
    :func:`vidlift.benchmark.estimate_eta` (injected to keep plan.py
    import-free of the benchmark/runner layers). The estimate mapping
    rides in ``PlannedFile.eta_estimate`` (None when no measured
    constant exists for this geometry bucket — no ETA rather than a
    projected one, clause c). No existing plan math changes.
    """
    if estimate_fn is None:
        from .benchmark import estimate_eta as estimate_fn
    eta = estimate_fn(plan, estimates=estimates)
    return replace(plan, eta_estimate=eta)
