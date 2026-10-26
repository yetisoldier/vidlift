"""Pre-flight planning for VidLift batch jobs.

The planner is the v0.2 front door: it turns the probe/classify report
into a plain, testable job description before any hours-long upscale or
encode starts. It does not run Real-ESRGAN or ffmpeg; later pipeline code
consumes this plan.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from pathlib import Path

from .probe import (
    FileReport,
    SOURCE_DVD_MPEG2_RIP,
    SOURCE_H264_SD,
    SOURCE_OTHER,
    SOURCE_XVID_DIVX_AVI,
)
from .subtitles import (
    HANDLED_BITMAP_CODECS,
    SubtitlePlan,
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
    "select_subtitle_track",
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
    # v0.4.1 additive (design §B.7): the structured subtitle plan —
    # which stream/sidecar was selected, whether OCR is required, and
    # the engine lane. None = no subtitle track was selected (the
    # honest 'none'; a fact, not a warning). free-text subtitle_note
    # stays for back-compat.
    subtitle_plan: SubtitlePlan | None = None
    # v0.4.1 additive (design §B.3): True when the selected embedded
    # text track is ASS/SSA — styling does not survive mov_text
    # conversion; stated, never hidden.
    subtitle_style_loss: bool = False


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
    return str(directory / f"{source.stem}.720p.hevc.mp4")


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


def select_subtitle_track(report: FileReport) -> SubtitlePlan | None:
    """The deterministic probe-side selection rule (design §B.2).

    The file-first replacement for b2m's scan-side recommend.py, porting
    the Eric-approved philosophy verbatim in rule (English preferred,
    players can toggle off; one track; commentary-descriptive skipped is
    a §B.9 follow-up — v0.4.1 selects purely on kind/lang):

    1. Any embedded TEXT sub -> the first text stream, English-tagged
       first, lowest stream index as tie-break.
    2. Else any handled-bitmap sub (PGS/VobSub) -> the first such
       stream, same preference — the OCR candidate. One stream only.
    3. Else the adopted sidecar (§B.1 discovery), if any.
    4. Else None — recorded as the honest 'none', a fact, not a
       warning.

    Plan-time decision, run-time execution (the repo's established
    split): the run phase consumes the plan with zero decision logic.
    """
    text_subs = [s for s in report.subtitles if s.kind == "text"]
    if text_subs:
        chosen = min(
            text_subs,
            key=lambda s: (0 if s.lang in ("eng", "en") else 1, s.index),
        )
        return SubtitlePlan(
            source="embedded",
            stream_index=chosen.index,
            sidecar_path="",
            kind="text",
            codec_name=chosen.codec_name,
            lang=chosen.lang,
            ocr_required=False,
        )
    bitmap_subs = [
        s
        for s in report.subtitles
        if s.kind == "image" and s.codec_name in HANDLED_BITMAP_CODECS
    ]
    if bitmap_subs:
        chosen = min(
            bitmap_subs,
            key=lambda s: (0 if s.lang in ("eng", "en") else 1, s.index),
        )
        return SubtitlePlan(
            source="embedded",
            stream_index=chosen.index,
            sidecar_path="",
            kind="image",
            codec_name=chosen.codec_name,
            lang=chosen.lang,
            ocr_required=True,
        )
    for sidecar in report.sidecars:
        if sidecar.kind == "text":
            return SubtitlePlan(
                source="sidecar",
                stream_index=-1,
                sidecar_path=sidecar.path,
                kind="text",
                codec_name="srt",
                lang="",  # a sidecar has no tag until muxed; honesty rule -> und
                ocr_required=False,
            )
        if sidecar.kind == "image":
            return SubtitlePlan(
                source="sidecar",
                stream_index=-1,
                sidecar_path=sidecar.path,
                kind="image",
                codec_name="vobsub",
                lang="",
                ocr_required=True,
            )
    return None


def _subtitle_note(report: FileReport) -> str:
    """Describe the PLAN, not just the inventory (design §B.7).

    v0.4.1 rewrite: today's 'listed only; no OCR in VidLift' verdict is
    replaced by the disposition — which track carries, how, and what
    is skipped. A mixed text+bitmap file now reports carrying the
    text track (the old all-or-nothing silence is the gap v0.4.1
    closes).
    """
    selected = select_subtitle_track(report)
    if selected is None:
        if report.sidecars:
            # Discovery found a sidecar but selection did not adopt it
            # (embedded truth wins). Name it so the disk file never
            # surprises anyone when a player auto-loads it.
            names = ", ".join(os.path.basename(s.path) for s in report.sidecars)
            return f"none carried; sidecar noted beside source ({names})"
        return "none"
    parts: list[str] = []
    if selected.kind == "text" and selected.source == "embedded":
        parts.append(
            f"text track {selected.stream_index} ({selected.codec_name}) "
            "carried as mov_text + sidecar"
        )
    elif selected.kind == "text" and selected.source == "sidecar":
        parts.append(
            f"sidecar {os.path.basename(selected.sidecar_path)} adopted "
            "as the text track (muxed mov_text + copied sidecar)"
        )
    elif selected.kind == "image" and selected.source == "embedded":
        engine = "seconv" if os.name == "nt" else "tesseract"
        parts.append(
            f"bitmap track {selected.stream_index} ({selected.codec_name}) "
            f"-> OCR ({engine}) -> mov_text + sidecar"
        )
    elif selected.kind == "image" and selected.source == "sidecar":
        engine = "seconv" if os.name == "nt" else "tesseract"
        parts.append(
            f"sidecar {os.path.basename(selected.sidecar_path)} adopted as "
            f"OCR candidate -> OCR ({engine}) -> mov_text + sidecar"
        )
    # A sidecar that was NOT adopted (embedded truth wins) is still
    # named — the disk file never surprises anyone (§B.1).
    if selected.source == "embedded" and report.sidecars:
        names = ", ".join(os.path.basename(s.path) for s in report.sidecars)
        parts.append(f"sidecar noted but not adopted ({names}; embedded subs win)")
    # The unselected remainder, honestly named.
    text = sum(1 for s in report.subtitles if s.kind == "text")
    image = sum(1 for s in report.subtitles if s.kind == "image")
    other = len(report.subtitles) - text - image
    if selected.source == "embedded":
        if selected.kind == "text":
            skipped_image = image
            skipped_other = other
            skipped_text = text - 1
        else:
            skipped_image = image - 1
            skipped_other = other
            skipped_text = text
    else:
        skipped_image = image
        skipped_other = other
        skipped_text = text
    skipped: list[str] = []
    if skipped_text:
        skipped.append(f"{skipped_text} text stream(s) not selected")
    if skipped_image:
        skipped.append(f"{skipped_image} bitmap stream(s) listed only")
    if skipped_other:
        skipped.append(f"{skipped_other} unknown stream(s) listed only")
    if skipped:
        parts.append("; " + ", ".join(skipped))
    return ". ".join(parts) if False else "; ".join(parts)


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
    subtitle_plan = select_subtitle_track(report)
    style_loss = bool(
        subtitle_plan is not None
        and subtitle_plan.source == "embedded"
        and subtitle_plan.kind == "text"
        and subtitle_plan.codec_name in ("ass", "ssa")
    )
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
        subtitle_plan=subtitle_plan,
        subtitle_style_loss=style_loss,
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
