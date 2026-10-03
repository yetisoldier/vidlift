"""ffprobe wrapper and source classification for SD video files.

v0.1 core. Reads what a file actually declares (resolution, SAR/DAR,
field order, frame rates, codec, container, duration, audio channels,
subtitle types) and turns it into an honest classification: source
class, upscale candidacy after square-pixel normalization, and a
PAL-speedup suspicion. The tool never invents an "original
resolution"; it reports what the file says.

Zero GUI imports: this module is plain Python plus ffprobe so the
core stays testable headless. Tests inject a fake runner through the
``runner`` seam (same pattern as bluray-to-mp4's CLI seams); no real
ffprobe is needed for the unit tests.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AudioInfo",
    "FileReport",
    "MediaProbe",
    "ProbeError",
    "SubtitleInfo",
    "VideoInfo",
    "classify",
    "displayed_size",
    "probe_and_classify",
    "probe_file",
    "parse_probe",
    "real_runner",
]

# Field orders that mean stored interlace (top or bottom field first,
# either polarity of field order in the stream).
INTERLACED_FIELD_ORDERS = {"tt", "bb", "tb", "bt"}

# PAL detection window: frame rates within this of exactly 25.000.
PAL_FPS_TOLERANCE = 0.05

# Upscale candidacy ceiling: SD is anything up to 576 displayed lines
# (PAL) after square-pixel normalization. Taller files are already HD.
SD_MAX_DISPLAYED_HEIGHT = 576

# Source classes (v0.1 heuristics; the pre-flight report in v0.2
# carries them to the user with the honest quality expectations).
SOURCE_XVID_DIVX_AVI = "xvid-divx-avi"
SOURCE_DVD_MPEG2_RIP = "dvd-mpeg2-rip"
SOURCE_H264_SD = "h264-sd"
SOURCE_OTHER = "other"

# MPEG-4 Part 2 (Advanced Simple Profile) codec names ffprobe reports
# for the ~700 MB XviD/DivX era rips.
MPEG4_ASP_CODECS = {"mpeg4", "xvid", "divx", "div3", "div4", "div5", "dx50"}

# MPEG-1/2 video: proper DVD rips and captures.
MPEG12_CODECS = {"mpeg1video", "mpeg2video", "mpeg1", "mpeg2"}

# Subtitle typing: text subs can pass through to MP4 (mov_text);
# image subs are detected and reported as "not carried" in v0.2.
TEXT_SUBTITLE_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text",
}
IMAGE_SUBTITLE_CODECS = {
    "dvd_subtitle", "vobsub", "pgssub", "hdmv_pgs_subtitle",
    "dvb_subtitle", "xsub",
}


class ProbeError(RuntimeError):
    """The file is missing, ffprobe failed, or its output was unusable."""


# Runner seam: run(binary, args) -> (returncode, stdout, stderr).
# The real runner shells out; tests replay canned ffprobe JSON.
Runner = Callable[[str, list[str]], "tuple[int, str, str]"]


def real_runner(binary: str, args: list[str]) -> tuple[int, str, str]:
    """Run a real ffprobe command and return (code, stdout, stderr)."""
    result = subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, result.stdout, result.stderr


@dataclass
class VideoInfo:
    """What the video stream declares about itself."""

    codec_name: str
    width: int
    height: int
    sample_aspect_ratio: str  # raw ffprobe value, e.g. "32:27" or "0:1"
    display_aspect_ratio: str  # raw ffprobe value, e.g. "16:9"
    sar: tuple[int, int] | None  # parsed SAR; None = unknown/missing
    field_order: str
    r_frame_rate: float | None  # base/field rate, parsed
    avg_frame_rate: float | None  # average rate over the file, parsed
    r_frame_rate_raw: str
    avg_frame_rate_raw: str

    @property
    def interlaced_flagged(self) -> bool:
        """True when the stream itself declares an interlaced field order."""
        return self.field_order.lower() in INTERLACED_FIELD_ORDERS


@dataclass
class AudioInfo:
    codec_name: str
    channels: int
    lang: str


@dataclass
class SubtitleInfo:
    codec_name: str
    kind: str  # "text" | "image" | "other"
    lang: str


@dataclass
class MediaProbe:
    """Raw probe of one media file: container plus its streams."""

    path: str
    container: str  # ffprobe format_name, e.g. "avi", "mov,mp4,..."
    duration_seconds: float | None
    video: VideoInfo | None  # first video stream, or None
    audio: list[AudioInfo]
    subtitles: list[SubtitleInfo]


@dataclass
class FileReport:
    """Classified probe: what v0.2's pre-flight report and the engine
    consume. Everything here is read or derived from the file itself;
    nothing is guessed."""

    path: str
    container: str
    duration_seconds: float | None
    video: VideoInfo | None
    audio: list[AudioInfo]
    subtitles: list[SubtitleInfo]
    source_class: str
    displayed_width: int
    displayed_height: int
    upscale_verdict: str  # "candidate" | "already-hd" | "no-video"
    interlaced_flagged: bool
    pal_speedup_suspected: bool


def _parse_ratio(text: object) -> tuple[int, int] | None:
    """Parse an ffprobe ratio like "32:27" into (32, 27).

    Returns None for anything unusable: missing, "N/A", "0:1"
    (undetermined SAR), or garbage. Callers treat None as square
    pixels, the honest default when the file does not declare one.
    """
    if not isinstance(text, str) or ":" not in text:
        return None
    num, _, den = text.partition(":")
    try:
        n, d = int(num), int(den)
    except ValueError:
        return None
    if n <= 0 or d <= 0:
        return None
    return n, d


def _parse_rate(text: object) -> float | None:
    """Parse an ffprobe rate like "30000/1001" or "25" into fps.

    Returns None for missing, "0/0", or unparseable values.
    """
    if not isinstance(text, str) or not text:
        return None
    num, sep, den = text.partition("/")
    try:
        if sep:
            n, d = float(num), float(den)
        else:
            return float(num)
    except ValueError:
        return None
    if d == 0:
        return None
    return n / d


def _subtitle_kind(codec_name: str) -> str:
    codec = (codec_name or "").lower()
    if codec in TEXT_SUBTITLE_CODECS:
        return "text"
    if codec in IMAGE_SUBTITLE_CODECS:
        return "image"
    return "other"


def parse_probe(path: str, text: str) -> MediaProbe:
    """Parse ffprobe -show_format -show_streams JSON into a MediaProbe."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"unparsable ffprobe output for {path}: {exc}") from exc

    video: VideoInfo | None = None
    audio: list[AudioInfo] = []
    subtitles: list[SubtitleInfo] = []
    for stream in data.get("streams") or []:
        codec_type = stream.get("codec_type", "")
        codec_name = stream.get("codec_name", "") or ""
        lang = (stream.get("tags") or {}).get("language", "") or ""
        if codec_type == "video" and video is None:
            video = VideoInfo(
                codec_name=codec_name,
                width=int(stream.get("width") or 0),
                height=int(stream.get("height") or 0),
                sample_aspect_ratio=stream.get("sample_aspect_ratio") or "",
                display_aspect_ratio=stream.get("display_aspect_ratio") or "",
                sar=_parse_ratio(stream.get("sample_aspect_ratio")),
                field_order=stream.get("field_order") or "",
                r_frame_rate=_parse_rate(stream.get("r_frame_rate")),
                avg_frame_rate=_parse_rate(stream.get("avg_frame_rate")),
                r_frame_rate_raw=stream.get("r_frame_rate") or "",
                avg_frame_rate_raw=stream.get("avg_frame_rate") or "",
            )
        elif codec_type == "audio":
            audio.append(
                AudioInfo(
                    codec_name=codec_name,
                    channels=int(stream.get("channels") or 0),
                    lang=lang,
                )
            )
        elif codec_type == "subtitle":
            subtitles.append(
                SubtitleInfo(
                    codec_name=codec_name,
                    kind=_subtitle_kind(codec_name),
                    lang=lang,
                )
            )

    fmt = data.get("format") or {}
    try:
        duration: float | None = float(fmt.get("duration"))
    except (TypeError, ValueError):
        duration = None
    return MediaProbe(
        path=path,
        container=fmt.get("format_name", "") or "",
        duration_seconds=duration,
        video=video,
        audio=audio,
        subtitles=subtitles,
    )


def probe_file(
    path: str,
    *,
    binary: str = "ffprobe",
    runner: Runner = real_runner,
) -> MediaProbe:
    """Probe one media file with ffprobe and return its raw layout.

    Raises ProbeError when the file is missing, ffprobe fails, or the
    JSON cannot be parsed.
    """
    if not os.path.isfile(path):
        raise ProbeError(f"no such file: {path}")
    args = [
        "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json",
        path,
    ]
    code, stdout, stderr = runner(binary, args)
    if code != 0:
        detail = stderr.strip()[:300] or "no diagnostic output"
        raise ProbeError(f"ffprobe failed on {path}: {detail}")
    return parse_probe(path, stdout)


def displayed_size(
    width: int, height: int, sar: tuple[int, int] | None
) -> tuple[int, int]:
    """Square-pixel displayed size: stored geometry scaled by SAR.

    Anamorphic sources (DVD rips, anamorphic DivX) store non-square
    pixels; the displayed size is what the viewer actually sees and
    what the v0.2 engine normalizes to with setsar=1 before upscaling.
    Unknown SAR means square pixels as the honest default. Width
    rounds up to even to stay encode-friendly and to match the
    geometry table in the design doc (720x480 at SAR 32:27 displays
    as 854x480).
    """
    if sar is None:
        return width, height
    num, den = sar
    displayed = math.ceil(width * num / den)
    if displayed % 2:
        displayed += 1
    return displayed, height


def _classify_source(probe: MediaProbe, displayed_height: int) -> str:
    """Best-guess source class from codec, container, and extension.

    These are heuristics for the pre-flight report, not certainties;
    the point is honest expectation-setting (a 700 MB XviD rip and a
    proper DVD MPEG-2 rip deserve different promises).
    """
    video = probe.video
    if video is None:
        return SOURCE_OTHER
    codec = video.codec_name.lower()
    ext = Path(probe.path).suffix.lower()
    container = probe.container.lower()
    if codec in MPEG4_ASP_CODECS and (ext == ".avi" or "avi" in container):
        return SOURCE_XVID_DIVX_AVI
    if codec in MPEG12_CODECS:
        return SOURCE_DVD_MPEG2_RIP
    if codec == "h264" and 0 < displayed_height <= SD_MAX_DISPLAYED_HEIGHT:
        return SOURCE_H264_SD
    return SOURCE_OTHER


def _pal_speedup_suspected(video: VideoInfo) -> bool:
    """Heuristic for the classic PAL transfer of a film source.

    Many European rips of US films run 25.000 fps progressive and play
    about 4% fast with raised pitch. Without a reference runtime we
    cannot prove speedup versus native PAL content, so this is a
    suspicion for the report, never an automatic retime. v0.2's
    pre-flight report can add the runtime-vs-known-cut check.
    """
    if video.interlaced_flagged:
        return False
    fps = video.avg_frame_rate or video.r_frame_rate
    return fps is not None and abs(fps - 25.0) <= PAL_FPS_TOLERANCE


def classify(probe: MediaProbe) -> FileReport:
    """Turn a raw probe into the classification the product consumes."""
    video = probe.video
    if video is None:
        return FileReport(
            path=probe.path,
            container=probe.container,
            duration_seconds=probe.duration_seconds,
            video=None,
            audio=probe.audio,
            subtitles=probe.subtitles,
            source_class=SOURCE_OTHER,
            displayed_width=0,
            displayed_height=0,
            upscale_verdict="no-video",
            interlaced_flagged=False,
            pal_speedup_suspected=False,
        )
    dw, dh = displayed_size(video.width, video.height, video.sar)
    if 0 < dh <= SD_MAX_DISPLAYED_HEIGHT:
        verdict = "candidate"
    else:
        verdict = "already-hd"
    return FileReport(
        path=probe.path,
        container=probe.container,
        duration_seconds=probe.duration_seconds,
        video=video,
        audio=probe.audio,
        subtitles=probe.subtitles,
        source_class=_classify_source(probe, dh),
        displayed_width=dw,
        displayed_height=dh,
        upscale_verdict=verdict,
        interlaced_flagged=video.interlaced_flagged,
        pal_speedup_suspected=_pal_speedup_suspected(video),
    )


def probe_and_classify(
    path: str,
    *,
    binary: str = "ffprobe",
    runner: Runner = real_runner,
) -> FileReport:
    """Probe one file and classify it in a single call."""
    return classify(probe_file(path, binary=binary, runner=runner))