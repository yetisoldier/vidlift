"""Subtitle extraction, OCR, and MP4 embedding for VidLift (v0.4.1).

Port of bluray-to-mp4's twice-field-proven subtitle machinery
(v0.2.27 unattended two-pass seconv+Tesseract OCR, v0.2.29 stale-file
and truncated-write hardening, v0.2.30 quality reporting + ``.ocr``
diagnosis kit; read-only reference at bluray-to-mp4 e62a80e, cited
per section) into VidLift's seams: every external call goes through
the injectable ``Runner`` (binary, args) -> (code, stdout, stderr)
seam, never a hidden subprocess; plan-time decisions live in
plan.py; this module executes.

Engine lanes (design §A.3):
- Windows (packaged app): the seconv lane, ported exactly from b2m --
  settings seed, pinned /outputfilename, /ocrlanguage:eng, stale-SRT
  purge before each pass, GUI-binary refusal, pass-2 FixCommonOcrErrors
  auto-resolve with timing-paired counting, truncated-write raw
  fallback.
- Linux (dev box): a direct-Tesseract lane. seconv is a Windows .NET
  binary; the design assigns this box pipeline correctness, so the
  lane renders the bitmap track to PNGs via ffmpeg's image2 muxer
  (one single-pass decode of the extracted track, ``-fps_mode
  passthrough``, one sub_%08d.png per decoded frame in container time
  order) and OCRs each image with ``tesseract <png> stdout --psm 6``,
  assembling cues from the container's own per-frame timestamps (a
  showinfo timeline probe of the same filtergraph). Validated on real
  ffmpeg 8.0.1 + tesseract 5.3.4 against a real PGS sample: per-packet
  seeking lands on each packet's FIRST segment, which for PGS is the
  clear/blank wipe (zero cues), and default vsync duplicates frames by
  hundreds — both rejected; the image2 sequence is the design's §B.4
  mechanism as written. Both lanes share everything downstream of "SRT
  text in memory" (§B.4).
- No usable OCR tool anywhere: FAIL OPEN with an honest warning naming
  the tool and the fix; the run ships video+audio, never refuses
  (§B.8). The engine resolution is explicit like ffmpeg's (env ->
  PATH -> named missing-tool error surfaced as fail-open, not a
  silent guess).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .srt import Cue, SrtError, normalize_cues, parse_srt, write_srt_file

__all__ = [
    "OCR_LANGUAGE",
    "OcrOutcome",
    "SubtitleOcrError",
    "SubtitlePlan",
    "SeconvOcr",
    "TesseractOcr",
    "extract_subtitles",
    "intermediate_ext",
    "mux_language",
    "mux_srt_into_mp4",
    "ocr_selected_track",
    "resolve_tesseract",
    "resolve_seconv",
    "run_subtitle_ocr",
    "sidecar_srt_path",
    "write_ocr_settings",
    "count_auto_resolved_cues",
    "convert_microdvd_to_cues",
    "convert_vobsub_sidecar_pair",
    "ocr_cue_quality",
    "ocr_quality_note",
    "write_ocr_diagnostic_kit",
]

# Runner seam, same shape as run.Runer/probe.Runer: run(binary, args)
# -> (returncode, stdout, stderr). Tests replay canned effects.
Runner = Callable[[str, list[str]], "tuple[int, str, str]"]


class SubtitleOcrError(RuntimeError):
    """Subtitle extraction, OCR, or muxing failed.

    The runner catches this and fails OPEN (design §B.8): video+audio
    ship normally, the warning names the failure, the manifest records
    ``failed-open``. Never a run refusal.
    """


# The OCR language, ISO 639-2 (what Tesseract wants). English per the
# b2m default Eric approved (design §B.9: no translation; non-English
# tracks OCR through eng with an honest und tag + warning).
OCR_LANGUAGE = "eng"


_SHOWINFO_FRAME_RE = re.compile(
    r"n:\s*(\d+)\s+pts:\s*(-?\d+|N/A)\s+pts_time:\s*(-?[\d.]+|N/A)"
)


def _frame_timeline(track_path: str, *, ffmpeg: str, runner: Runner) -> list[float]:
    """The decoded subtitle frames' presentation timestamps, in decode order.

    The design's §B.4 contract ("frame timestamps come from the container")
    needs a timeline that matches the rendered PNG sequence 1:1 — and only
    the decode itself provides one: ffprobe's packet list splits PGS packets
    differently than the decoder does (validated on real media: 8 packets vs
    5 decoded frames), so the lane runs the same sub2video filtergraph into a
    null sink with showinfo logging each frame's container pts. The lines are
    AV_LOG_INFO, so this pass runs at ffmpeg's default log level and parses
    stderr; the render pass stays quiet (-loglevel error).
    """
    code, _out, err = runner(
        ffmpeg,
        [
            "-hide_banner", "-nostdin",
            "-i", track_path,
            "-filter_complex", "[0:s:0]null,showinfo[s]",
            "-map", "[s]",
            "-f", "null", "-",
        ],
    )
    if code != 0:
        raise SubtitleOcrError(
            f"subtitle timeline probe failed: {(err or '').strip()[:200]}"
        )
    pts: list[float] = []
    for line in err.splitlines():
        m = _SHOWINFO_FRAME_RE.search(line)
        if not m:
            continue
        try:
            value = float(m.group(3))
        except ValueError:
            continue  # pts_time N/A (no container timestamp on this frame)
        if value >= 0:
            pts.append(value)
    return pts


def _render_subtitle_frames(
    track_path: str, png_dir: str, *, ffmpeg: str, runner: Runner
) -> int:
    """Render every decoded subtitle frame to its own PNG (design §B.4).

    The single-pass image2 sequence against the extracted track: one bounded
    ffmpeg call decodes the whole bitmap stream and writes sub_00000001.png,
    sub_00000002.png, ... in container time order — no per-packet seeks (a
    seek at a packet's pts lands on that packet's FIRST segment, which for
    PGS is the clear/blank wipe: validated wrong on real media), and no
    frame-rate resampling (-fps_mode passthrough keeps exactly one file per
    decoded frame; default vsync duplicated frames by hundreds). The image2
    muxer warns about non-monotonic dts when a packet carries several
    same-pts segments (a PGS display set); the warning is cosmetic — every
    frame still lands in its own file, in order. Returns the number of PNGs
    written; the caller cross-checks it against the timeline length.
    """
    os.makedirs(png_dir, exist_ok=True)
    for stale in os.listdir(png_dir):  # a killed run's partial sequence
        if re.match(r"^sub_\d{8}\.png$", stale):
            try:
                os.remove(os.path.join(png_dir, stale))
            except OSError:
                pass
    pattern = os.path.join(png_dir, "sub_%08d.png")
    code, _out, err = runner(
        ffmpeg,
        [
            "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", track_path,
            "-filter_complex", "[0:s:0]null[s]",
            "-map", "[s]",
            "-fps_mode", "passthrough",
            "-c:v", "png",
            pattern,
        ],
    )
    if code != 0:
        raise SubtitleOcrError(
            f"subtitle frame render failed: {(err or '').strip()[:200]}"
        )
    return len([f for f in os.listdir(png_dir) if re.match(r"^sub_\d{8}\.png$", f)])


def _assemble_cues(frames: list[tuple[float, str]]) -> list[Cue]:
    """Cues from (pts_seconds, ocr_text) pairs in decode order.

    Empty text is a clear/silence segment and closes the open cue at that
    frame's pts; identical consecutive text continues the display (a PGS
    packet may carry several same-pts segments); different text without an
    intervening clear closes the old cue where the new one starts; past the
    last frame a 3s display hold bounds the tail honestly. Every cue gets
    the 500ms minimum-display floor the SRT contract expects.
    """
    cues: list[Cue] = []
    open_text: str | None = None
    open_start = 0.0

    def close(end_pts: float) -> None:
        nonlocal open_text
        assert open_text is not None
        start_ms = int(round(open_start * 1000))
        end_ms = max(start_ms + 500, int(round(end_pts * 1000)))
        cues.append(Cue(start_ms, end_ms, open_text))
        open_text = None

    last_pts = frames[-1][0] if frames else 0.0
    for pts, text in frames:
        if not text:
            if open_text is not None:
                close(pts)
        elif open_text == text:
            continue
        else:
            if open_text is not None:
                close(pts)
            open_text = text
            open_start = pts
    if open_text is not None:
        close(last_pts + 3.0)
    return cues


# seconv settings seed name (b2m subtitles.py SE_CONV_SETTINGS_NAME).
SE_CONV_SETTINGS_NAME = "vidlift-seconv-settings.json"
# Pass-1 raw OCR output name (pinned via /outputfilename:, so a stale
# file can never be mistaken for this run's output).
RAW_SRT_NAME = "vidlift_ocr_raw.srt"
FIXED_SRT_NAME = "vidlift_ocr_fixed.srt"

# The SE 5.x GUI cannot run unattended OCR (no /convert mode) --
# invoking it is exactly b2m run 12's wizard interaction, so the
# wrapper refuses it (b2m subtitles.py _refuse_gui_binary).
_GUI_BINARY_NAMES = {"subtitleedit.exe", "subtitleedit"}

# ISO 639-2 (Tesseract) -> ISO 639-1 for seconv's OCR-fix gate.
_FCE_LANGUAGES = {
    "eng": "en", "deu": "de", "fra": "fr", "spa": "es", "ita": "it",
    "nld": "nl", "por": "pt", "swe": "sv", "dan": "da", "nor": "no",
    "fin": "fi", "pol": "pl",
}

# The bitmap codec set this milestone OCRs (design §B.1): PGS and
# DVD VobSub. dvb/xsub/other are detected and reported as uncarried
# (§B.9 follow-up), never silently attempted.
HANDLED_BITMAP_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "vobsub"}


def _bundled_ocr_tool(relative: Path) -> str | None:
    """Return a packaged OCR tool, preferring PyInstaller's data root."""
    if not getattr(sys, "frozen", False):
        return None
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    roots.append(Path(sys.executable).resolve().parent)
    return next(
        (str(root / relative) for root in roots if (root / relative).is_file()),
        None,
    )


def resolve_tesseract(explicit: str | None = None) -> str:
    """Explicit tesseract resolution: VIDLIFT_TESSERACT, then PATH.

    The same standing discipline as run.find_tools (no PATH guessing
    mid-run): the caller passes the resolved path or catches the named
    error. Raises MissingTool-style ValueError naming the tool.
    """
    binary = (
        explicit
        or os.environ.get("VIDLIFT_TESSERACT")
        or _bundled_ocr_tool(Path("tools/SubtitleEdit/Tesseract/tesseract.exe"))
        or "tesseract"
    )
    if not shutil.which(binary):
        raise SubtitleOcrError(
            f"tesseract not found at '{binary}': install Tesseract "
            "(e.g. 'apt install tesseract-ocr' or the UB Mannheim build "
            "on Windows), pass the explicit path, or set VIDLIFT_TESSERACT"
        )
    return binary


def resolve_seconv(explicit: str | None = None) -> str:
    """Explicit seconv resolution with the GUI-binary refusal inline.

    The packaged Windows app passes its bundled tools/SubtitleEdit
    seconv.exe here; resolution honors VIDLIFT_SECONV then PATH. The
    SE GUI binary is refused outright (b2m's run-12 lesson: the 5.x GUI
    has no unattended mode at all).
    """
    binary = (
        explicit
        or os.environ.get("VIDLIFT_SECONV")
        or _bundled_ocr_tool(Path("tools/SubtitleEdit/seconv.exe"))
        or "seconv"
    )
    base = os.path.basename((binary or "").replace("\\", "/")).lower()
    if base in _GUI_BINARY_NAMES:
        raise SubtitleOcrError(
            "Subtitle Edit's GUI cannot run unattended OCR (its 5.x "
            "/convert mode is gone); seconv - the headless converter - "
            "is required. Reinstall the latest release zip to get the "
            "bundled seconv."
        )
    if not shutil.which(binary):
        raise SubtitleOcrError(
            f"seconv not found at '{binary}': install Subtitle Edit's "
            "seconv (the headless converter, bundled in the release zip), "
            "pass the explicit path, or set VIDLIFT_SECONV"
        )
    return binary


@dataclass
class SubtitlePlan:
    """What the planner selected (plan.py builds it; this module runs it)."""
    source: str              # "embedded" | "sidecar"
    stream_index: int        # embedded: the ffprobe stream index
    sidecar_path: str        # sidecar: the adopted file's path
    kind: str                # "text" | "image"
    codec_name: str
    lang: str
    ocr_required: bool


def convert_microdvd_to_cues(content: str, fps: float) -> list[Cue]:
    """Convert MicroDVD frame-number cues to wall-clock Cues (§B.6.1).

    MicroDVD indexes cues by FRAME NUMBER ({start}{{end}}text per
    line), so conversion needs a rate. The ONLY re-timing edge in
    v0.4.1 (the design forbids any other re-timing machinery): the
    caller passes the v0.4.0 DERIVED stamp rate — the same canonical
    token the mezzanine was stamped with — so subtitles and video
    agree by construction. When the stamp refused at derive time
    (unsnappable → fail-open at original resolution), the caller
    passes the prepared container's DECLARED rate with an honest
    container-derived note (the v0.4.0 demotion rule applies to
    pixels, not this one-time conversion; stated so nobody
    re-litigates it).
    """
    if fps <= 0:
        raise SubtitleOcrError(
            f"MicroDVD conversion needs a positive frame rate, got {fps}"
        )
    cues: list[Cue] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        # {start}{end}text — regex is the honest parser (frame numbers
        # are integers; the text may itself contain braces)
        match = re.match(r"^\{(\d+)\}\{(\d+)\}(.*)$", line)
        if not match:
            continue
        start_f = int(match.group(1))
        end_f = int(match.group(2))
        text = match.group(3).strip()
        if not text:
            continue
        start_ms = int(round(start_f * 1000 / fps))
        end_ms = int(round(end_f * 1000 / fps))
        cues.append(Cue(start_ms, end_ms, text))
    return cues


def convert_vobsub_sidecar_pair(
    sub_path: str,
    idx_path: str,
    *,
    ffmpeg: str,
    runner: Runner,
    workdir: str,
    on_step: Callable[[str], None] | None = None,
) -> str:
    """Adopt a .sub/.idx VobSub pair as the OCR candidate of last resort.

    ffmpeg demuxes the pair directly (the vobsub demuxer reads the .idx
    index + the .sub MPEG packets); the pair is muxed into a single-track
    MKV through the runner seam and handed to the OCR stage exactly like
    an embedded VobSub stream (§B.1/B.4). The wall-clock truth comes from
    the .idx timestamps (wall-clock hh:mm:ss:ms lines), so the OCR lane
    needs no re-timing at all — the §B.6.1 rate-aware conversion is the
    MicroDVD text format's need, not the bitmap pair's.
    """
    if on_step is not None:
        on_step("subtitle-sidecar-adopt")
    os.makedirs(workdir, exist_ok=True)
    track_path = os.path.join(
        workdir,
        os.path.splitext(os.path.basename(sub_path))[0] + ".vobsub.mkv",
    )
    code, _out, err = runner(
        ffmpeg,
        [
            "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", idx_path,
            "-map", "0:s:0",
            "-c", "copy",
            track_path,
        ],
    )
    if code != 0 or not os.path.exists(track_path):
        raise SubtitleOcrError(
            f"VobSub sidecar adoption failed: {(err or '').strip()[:300]}"
        )
    return track_path


def intermediate_ext(codec_name: str) -> str:
    """Container extension for a losslessly extracted bitmap track.

    Port of b2m's _intermediate_ext (v0.2.16-vobsub-ocr §2): PGS ->
    .sup (raw stream, ffmpeg's sup muxer); VobSub/unknown -> .mkv (the
    only ffmpeg muxer that accepts a dvd_subtitle copy). Subtitle Edit
    reads VobSub from MKV directly; the tesseract lane renders either
    through the same overlay route.
    """
    return ".sup" if codec_name == "hdmv_pgs_subtitle" else ".mkv"


def extract_subtitles(
    source_path: str,
    stream_index: int,
    out_path: str,
    *,
    ffmpeg: str,
    runner: Runner,
    on_step: Callable[[str], None] | None = None,
) -> str:
    """Extract one embedded subtitle stream losslessly to its own file.

    ``ffmpeg -map 0:<idx> -c copy`` through the runner seam (the same
    extraction b2m subtitles.py:96-135 performs via subprocess; here
    it is seam-runnable so tests replay it). A nonzero exit or a
    missing output file raises SubtitleOcrError (fail-open upstream).
    """
    if on_step is not None:
        on_step("subtitle-extract")
    code, _out, err = runner(
        ffmpeg,
        [
            "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-i", source_path,
            "-map", f"0:{stream_index}",
            "-c", "copy",
            out_path,
        ],
    )
    if code != 0 or not os.path.exists(out_path):
        raise SubtitleOcrError(
            f"subtitle extraction failed: {(err or '').strip()[:300]}"
        )
    return out_path


def sidecar_srt_path(output_path: str) -> str:
    """<output-stem>.srt next to the finished MP4 (the KEEP contract)."""
    return os.path.splitext(output_path)[0] + ".srt"


# ---- seconv lane (Windows; port of b2m's two-pass flow) --------------------


def write_ocr_settings(path: str) -> None:
    """Seed seconv's settings JSON before OCR runs (v0.2.27).

    seconv starts from libse's built-in defaults -- never a user's GUI
    config -- so this seed is the explicit, version-pinned statement of
    the OCR text contract (conservative libse line/duration limits)
    every run hands to the engine via /settings:. Every key is a
    GeneralSection member of seconv 5.2.0's settings schema, so the
    invocation never triggers seconv's unknown-key warning. The
    frame-rate keys matter only for frame-number-invocable formats
    (MicroDVD); wall-clock formats ignore them (design §B.6.1).
    """
    seed = {
        "general": {
            "currentFrameRate": 23.976,
            "defaultFrameRate": 23.976,
            "subtitleLineMaximumLength": 43,
            "maxNumberOfLines": 2,
            "mergeLinesShorterThan": 33,
            "minimumMillisecondsBetweenLines": 24,
            "subtitleMinimumDisplayMilliseconds": 1000,
            "subtitleMaximumDisplayMilliseconds": 8000,
            "subtitleMaximumCharactersPerSeconds": 25.0,
            "subtitleOptimalCharactersPerSeconds": 15.0,
        }
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(seed, fh, indent=2)
        fh.write("\n")


def _refuse_gui_binary(binary: str) -> None:
    """Never drive the SE 5.x GUI (b2m run 12's wizard)."""
    base = os.path.basename((binary or "").replace("\\", "/")).lower()
    if base in _GUI_BINARY_NAMES:
        raise SubtitleOcrError(
            "Subtitle Edit's GUI cannot run unattended OCR (its 5.x "
            "/convert mode is gone); seconv - the headless converter - "
            "is required. Reinstall the latest release zip to get the "
            "bundled seconv."
        )


@dataclass
class SeconvOcr:
    """OCR via Subtitle Edit's headless seconv converter (the b2m lane).

    Two fully unattended passes (v0.2.27): pass 1 raw Tesseract OCR,
    pass 2 FixCommonOcrErrors auto-resolve. Neither pass ever interacts
    with a human; the caller reports the honest auto-resolved count.
    """

    binary: str = "seconv"
    language: str = OCR_LANGUAGE

    def __post_init__(self) -> None:
        # Construction-time refusal (b2m's rule: a GUI-only install must
        # never reach any invocation path).
        _refuse_gui_binary(self.binary)

    def command(self, in_path: str, out_dir: str) -> list[str]:
        """Pass 1 (raw OCR). /outputfilename pins the exact output name:
        seconv's derived name for MKV image tracks carries a language
        suffix, which is not the name the sidecar contract expects."""
        return [
            self.binary,
            "/convert", in_path,
            "subrip",
            "/ocrengine:tesseract",
            f"/ocrlanguage:{self.language}",
            "/settings:" + os.path.join(out_dir, SE_CONV_SETTINGS_NAME),
            f"/outputfilename:{RAW_SRT_NAME}",
            "/outputfolder:" + out_dir,
            "/overwrite",
        ]

    def fix_command(self, raw_srt_path: str, out_dir: str) -> list[str]:
        """Pass 2: auto-resolve unknown glyphs (headless OCR-fix pass).

        FixCommonOcrErrors applies the English OCR replace list and
        Hunspell guessing using dictionaries bundled inside seconv, so
        it needs no user training and never prompts. /fce-language pins
        the gate to the OCR language (the ISO 639-1 form).
        """
        return [
            self.binary,
            "/convert", raw_srt_path,
            "subrip",
            "/fixcommonerrorsrules:FixCommonOcrErrors",
            f"/fce-language:{_FCE_LANGUAGES.get(self.language, 'en')}",
            "/settings:" + os.path.join(out_dir, SE_CONV_SETTINGS_NAME),
            f"/outputfilename:{FIXED_SRT_NAME}",
            "/outputfolder:" + out_dir,
            "/overwrite",
        ]

    def run(
        self,
        in_path: str,
        out_dir: str,
        *,
        runner: Runner,
    ) -> "OcrOutcome":
        """Run both unattended passes through the runner seam.

        Pass 1 failing raises SubtitleOcrError (no SRT is produced;
        fail-open upstream). Pass 2 failing is NOT a hard failure: the
        raw OCR is still usable, so the raw SRT ships with
        fix_applied=False and the reason (b2m's honest fallback).
        The v0.2.29 stale-file purge runs before EACH pass: a killed
        prior OCR in this workdir leaves files behind, and a pass that
        exits 0 without writing would otherwise ship a different run's
        output under a success path.
        """
        _refuse_gui_binary(self.binary)
        settings_path = os.path.join(out_dir, SE_CONV_SETTINGS_NAME)
        if not os.path.exists(settings_path):
            write_ocr_settings(settings_path)

        raw_srt = os.path.join(out_dir, RAW_SRT_NAME)
        if os.path.exists(raw_srt):
            try:
                os.remove(raw_srt)  # stale from a killed run (v0.2.29)
            except OSError:
                pass
        cmd = self.command(in_path, out_dir)
        code, _out, err = runner(cmd[0], cmd[1:])
        if code != 0:
            raise SubtitleOcrError(
                f"seconv OCR pass 1 failed (exit {code}): "
                f"{(err or '').strip()[:300]}"
            )
        if not os.path.exists(raw_srt):
            raise SubtitleOcrError(
                f"seconv produced no SRT (expected {raw_srt}); check the "
                "Tesseract installation"
            )

        fixed_srt = os.path.join(out_dir, FIXED_SRT_NAME)
        if os.path.exists(fixed_srt):
            try:
                os.remove(fixed_srt)  # stale from a killed run (v0.2.29)
            except OSError:
                pass
        fix_cmd = self.fix_command(raw_srt, out_dir)
        fix_code, _f_out, fix_err = runner(fix_cmd[0], fix_cmd[1:])
        if fix_code != 0 and not os.path.exists(fixed_srt):
            # exit nonzero AND no file: pass 2 genuinely failed -> raw ships
            return OcrOutcome(
                raw_srt_path=raw_srt,
                srt_path=raw_srt,
                fix_applied=False,
                fix_error=(fix_err or f"exit {fix_code}").strip()[:200],
            )
        if not os.path.exists(fixed_srt):
            return OcrOutcome(
                raw_srt_path=raw_srt,
                srt_path=raw_srt,
                fix_applied=False,
                fix_error="auto-fix pass produced no SRT",
            )
        return OcrOutcome(
            raw_srt_path=raw_srt,
            srt_path=fixed_srt,
            fix_applied=True,
            fix_error="",
        )


# ---- tesseract lane (Linux; new orchestration, shared downstream) ----------


@dataclass
class TesseractOcr:
    """OCR via the system tesseract binary, one PNG per decoded frame.

    The Linux lane (design §A.3/§B.4 as validated on real media): a
    showinfo timeline probe decodes the extracted track once and logs
    each frame's container pts; a single-pass image2 render writes
    exactly one sub_%08d.png per decoded frame in container time
    order (``-fps_mode passthrough`` — default vsync duplicated frames
    by hundreds; per-packet ``-ss`` seeks land on each PGS packet's
    FIRST segment, which is the clear/blank wipe — both validated
    wrong and rejected); tesseract OCRs each PNG with ``--psm 6``;
    ``_assemble_cues`` builds cues from the (pts, text) pairs.
    """

    binary: str = "tesseract"
    language: str = OCR_LANGUAGE
    psm: int = 6

    def ocr_png(self, png_path: str, *, runner: Runner) -> str:
        """OCR one PNG to text (stdout) through the runner seam."""
        code, out, err = runner(
            self.binary,
            [png_path, "stdout", "-l", self.language, "--psm", str(self.psm)],
        )
        if code != 0:
            raise SubtitleOcrError(
                f"tesseract failed on {os.path.basename(png_path)} "
                f"(exit {code}): {(err or '').strip()[:200]}"
            )
        return out

    def run(
        self,
        track_path: str,
        workdir: str,
        *,
        ffmpeg: str,
        runner: Runner,
        on_step: Callable[[str], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> "OcrTextResult":
        """showinfo timeline -> single-pass image2 render -> per-PNG OCR."""
        if on_step is not None:
            on_step("subtitle-timeline")
        pts_list = _frame_timeline(track_path, ffmpeg=ffmpeg, runner=runner)
        if not pts_list:
            raise SubtitleOcrError(
                "no subtitle frames decoded; the track is likely empty "
                f"({track_path})"
            )
        if on_step is not None:
            on_step("subtitle-render")
        png_dir = os.path.join(workdir, "frames")
        rendered = _render_subtitle_frames(
            track_path, png_dir, ffmpeg=ffmpeg, runner=runner
        )
        if rendered != len(pts_list):
            raise SubtitleOcrError(
                f"subtitle render produced {rendered} PNGs for "
                f"{len(pts_list)} timeline frames; aborting rather "
                "than mis-timing cues"
            )
        frames: list[tuple[float, str]] = []
        total = len(pts_list)
        for i, pts in enumerate(pts_list):
            if on_step is not None:
                on_step(f"ocr-image-{i + 1}/{total}")
            if on_progress is not None:
                on_progress(i, total)
            png_path = os.path.join(png_dir, f"sub_{i + 1:08d}.png")
            text = self.ocr_png(png_path, runner=runner).strip()
            frames.append((pts, text))
        return OcrTextResult(raw_text="", assembled_cues=_assemble_cues(frames))


@dataclass
class OcrTextResult:
    """The tesseract lane's in-memory result (no files written)."""
    raw_text: str
    assembled_cues: list[Cue]


@dataclass
class OcrOutcome:
    """The seconv lane's file-based result."""
    raw_srt_path: str
    srt_path: str
    fix_applied: bool
    fix_error: str = ""


def count_auto_resolved_cues(raw_text: str, fixed_text: str) -> int:
    """Cues the auto-fix pass changed, paired by TIMING (v0.2.30 port).

    Port of b2m's _count_auto_resolved_cues verbatim in rule: pass 2
    merges and splits cues, so positional zip() pairing drifts and
    undercounts exactly when the fix pass did the most work. Each
    fixed cue counts as changed when its start_ms has no raw cue
    carrying the identical text (an absent start_ms is a split cue the
    pass created, hence changed). Defensive: any parse problem yields
    0 -- the count is honest reporting, never a gate.
    """
    try:
        raw = normalize_cues(parse_srt(raw_text))
        fixed = normalize_cues(parse_srt(fixed_text))
    except SrtError:
        return 0
    raw_by_start: dict[int, set[str]] = {}
    for cue in raw:
        raw_by_start.setdefault(cue.start_ms, set()).add(cue.text)
    return sum(
        1 for cue in fixed
        if cue.text not in raw_by_start.get(cue.start_ms, frozenset())
    )


# ---- v0.2.30 quality reporting (reporting only, never a gate) --------------

# The documented "clean" contract (b2m subtitles.py): ASCII letters,
# digits, spaces, basic punctuation, and the common accented Latin-1
# letters (U+00C0..U+00FF minus x/ division signs) plus U+0178.
# Deliberately conservative: curly quotes, cent signs, pipes count as
# garbage -- exactly the shape Tesseract's misreads take on stylized
# subtitle fonts.
_ACCENTED_LATIN1 = frozenset(
    chr(c) for c in range(0x00C0, 0x0100)
    if c not in (0x00D7, 0x00F7)
) | {"\u0178"}
_OCR_CLEAN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " \n"
    ".,!?'\n\"-:;()/&%$"
    "\u2013\u2014\u2026\u20ac\u00a3\u00b0"
) | _ACCENTED_LATIN1


def ocr_cue_quality(cues: list[Cue]) -> tuple[int, int, int]:
    """(total, garbage, empty) over the parsed cue list, before
    normalize_cues drops empties -- so clean + garbage + empty == total."""
    garbage = empty = 0
    for cue in cues:
        text = cue.text.strip()
        if not text:
            empty += 1
        elif any(ch not in _OCR_CLEAN_CHARS for ch in text):
            garbage += 1
    return len(cues), garbage, empty


def ocr_quality_note(parsed_cues: list[Cue]) -> str:
    """The honest, data-driven quality line (v0.2.30 port)."""
    total, garbage, empty = ocr_cue_quality(parsed_cues)
    clean = total - garbage - empty
    pct = round(clean * 100 / total) if total else 100
    return (
        f"OCR quality: {pct}% of {total} cues clean "
        f"({garbage} cues contain non-English characters, {empty} empty)"
    )


# ---- v0.2.30 diagnosis kit --------------------------------------------------

# The extracted track (a full-movie PGS .sup can run large) is retained
# up to this size; above it the copy is skipped with an honest note.
_OCR_TRACK_CAP_MB = 150
_OCR_TRACK_CAP_BYTES = _OCR_TRACK_CAP_MB * 1024 * 1024


def write_ocr_diagnostic_kit(
    video_out_path: str,
    *,
    track_path: str | None,
    raw_srt_path: str | None,
) -> list[str]:
    """Retain OCR inputs in <output-stem>.ocr/ next to the MP4.

    Port of b2m's _write_ocr_diagnostic_kit: the extracted track fed to
    OCR and the raw pass-1 output copied as <stem>.raw.srt, enough to
    re-run OCR offline without re-ripping/re-encoding. A subfolder,
    deliberately: a bare raw .srt next to the MP4 would make every
    player auto-load the uncorrected pass-1 text over the shipped
    sidecar. Size-capped at 150 MB with an honest skip note; any
    OSError degrades to a note, never an error (fail-open like
    everything here). Written on SUCCESS AND on fail-open (the caller
    passes whatever inputs exist).
    """
    notes: list[str] = []
    out_dir = os.path.dirname(os.path.abspath(video_out_path))
    stem = os.path.splitext(os.path.basename(video_out_path))[0]
    kit_dir = os.path.join(out_dir, stem + ".ocr")
    try:
        os.makedirs(kit_dir, exist_ok=True)
        if raw_srt_path and os.path.exists(raw_srt_path):
            shutil.copy2(
                raw_srt_path, os.path.join(kit_dir, stem + ".raw.srt")
            )
        if track_path and os.path.exists(track_path):
            size = os.path.getsize(track_path)
            if size > _OCR_TRACK_CAP_BYTES:
                notes.append(
                    f"OCR input retained except the subtitle track "
                    f"({size / (1024 * 124):.1f} MB > "
                    f"{_OCR_TRACK_CAP_MB} MB cap)"
                )
            else:
                shutil.copy2(
                    track_path,
                    os.path.join(kit_dir, os.path.basename(track_path)),
                )
    except OSError as exc:
        notes.append(f"OCR diagnosis kit incomplete ({exc})")
    return notes


# ---- the shared full path ----------------------------------------------------


def run_subtitle_ocr(
    subtitle_plan: "SubtitlePlan",
    source_path: str,
    output_path: str,
    *,
    ffmpeg: str,
    runner: Runner,
    workdir: str,
    seconv_binary: str | None = None,
    tesseract_binary: str | None = None,
    on_step: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    force_engine: str | None = None,
) -> "OcrResult":
    """Extract -> OCR -> normalize -> <output-stem>.srt sidecar.

    The disposition contract (design §B.3/B.4): embed mov_text in the
    MP4 AND keep the editable .srt sidecar; the .ocr/ kit retains the
    inputs for offline tuning. Fail-open semantics live one level up
    (run.py catches SubtitleOcrError and fails open); this function
    raises on hard OCR failure so the kit can still be written by the
    caller from whatever inputs exist (b2m's rule: a bad-OCR source
    must be re-tunable without re-work -- the kit writes on fail-open
    too, subtitles.py:616-630).
    """
    os.makedirs(workdir, exist_ok=True)
    # Extract the selected bitmap stream losslessly from the SOURCE
    # (subs never enter the prepared/mezzanine path; design §B.4 step 1).
    img_path = os.path.join(
        workdir,
        os.path.splitext(os.path.basename(source_path))[0]
        + ".vobsub-extract"
        + intermediate_ext(subtitle_plan.codec_name),
    )
    # The same-directory collision guard (b2m subtitles.py:526-543):
    # when workdir IS the source's directory the derived intermediate
    # can equal the source itself; the suffix above keeps them apart.
    if os.path.normcase(os.path.abspath(img_path)) == os.path.normcase(
        os.path.abspath(source_path)
    ):
        img_path = os.path.join(
            workdir,
            os.path.splitext(os.path.basename(source_path))[0]
            + ".vobsub-extract2" + intermediate_ext(subtitle_plan.codec_name),
        )
    extract_subtitles(
        source_path,
        subtitle_plan.stream_index,
        img_path,
        ffmpeg=ffmpeg,
        runner=runner,
        on_step=on_step,
    )

    notes: list[str] = []
    engine = "unknown"
    # The lane selector: platform default (§A.3) with an explicit test
    # override — forcing os.name is FORBIDDEN as a test technique
    # (patching os.name poisons pathlib.Path process-wide: WindowsPath
    # on Linux mangles every path; the seam, not the platform, is the
    # honest injection point).
    lane = force_engine or ("seconv" if os.name == "nt" else "tesseract")
    try:
        if lane == "seconv":
            engine = "seconv"
            # The Windows lane: b2m payload verbatim (settings seed,
            # two passes, stale purges). Binary already resolved by the
            # caller (packaged app bundles; dev uses env/PATH).
            se = SeconvOcr(binary=seconv_binary or "seconv")
            outcome = se.run(img_path, workdir, runner=runner)
            engine = "seconv"
            with open(outcome.raw_srt_path, encoding="utf-8-sig") as fh:
                raw_text = fh.read()
            with open(outcome.srt_path, encoding="utf-8-sig") as fh:
                final_text = fh.read()
            raw_srt_for_kit = outcome.raw_srt_path
            fixed_parse_failed = False
            try:
                parsed_cues = parse_srt(final_text)
            except SrtError as exc:
                if outcome.srt_path == outcome.raw_srt_path:
                    raise SubtitleOcrError(
                        f"OCR output was not valid SRT: {exc}"
                    ) from exc
                # v0.2.29's second fix: pass 2 exited 0 but its file does
                # not parse (truncated partial write); raw is still good.
                try:
                    parsed_cues = parse_srt(raw_text)
                except SrtError:
                    raise SubtitleOcrError(
                        f"OCR output was not valid SRT: {exc}"
                    ) from exc
                final_text = raw_text
                fixed_parse_failed = True
            cues = normalize_cues(parsed_cues)
            if not cues:
                raise SubtitleOcrError(
                    "OCR completed but produced zero cues; the subtitle "
                    "track is likely unusable (or Tesseract needs its "
                    "English data)."
                )
            if fixed_parse_failed:
                notes.append(
                    "Unattended OCR auto-fix output was not valid SRT "
                    "(likely a truncated write); the raw OCR shipped "
                    "without glyph corrections - spot-check spelling."
                )
            elif outcome.fix_applied:
                auto_resolved = count_auto_resolved_cues(raw_text, final_text)
                notes.append(
                    f"Unattended OCR auto-resolved {auto_resolved} of "
                    f"{len(cues)} cues with best-guess replacements"
                    + ("; spot-check those for glyph errors."
                       if auto_resolved else ".")
                )
            else:
                notes.append(
                    "Unattended OCR auto-fix pass failed ("
                    f"{outcome.fix_error[:200]}); the raw OCR shipped "
                    "without glyph corrections - spot-check spelling."
                )
            auto_resolved_note = notes[0] if notes else ""
        elif lane == "tesseract":
            engine = "tesseract"
            tes = TesseractOcr(binary=tesseract_binary or "tesseract")
            result = tes.run(
                img_path, workdir, ffmpeg=ffmpeg, runner=runner,
                on_step=on_step, on_progress=on_progress,
            )
            parsed_cues = result.assembled_cues
            cues = normalize_cues(parsed_cues)
            if not cues:
                raise SubtitleOcrError(
                    "OCR completed but produced zero cues; the subtitle "
                    "track is likely unusable (or Tesseract needs its "
                    "English data)."
                )
            # The tesseract lane has no pass-2 machinery; the assembled
            # cues ARE the raw. Write one for the kit.
            raw_srt_for_kit = os.path.join(workdir, RAW_SRT_NAME)
            write_srt_file(raw_srt_for_kit, cues)
            raw_text = ""
            auto_resolved_note = ""
        else:
            raise SubtitleOcrError(
                f"unknown OCR engine lane {lane!r}"
            )
    except SubtitleOcrError:
        # v0.2.30 fail-open retention: write the kit from whatever OCR
        # inputs exist (the extracted track always does) BEFORE raising,
        # so a bad-OCR source is re-tunable without re-ripping.
        write_ocr_diagnostic_kit(
            output_path,
            track_path=img_path,
            raw_srt_path=os.path.join(workdir, RAW_SRT_NAME),
        )
        raise

    # Quality line (v0.2.30): always present, pure reporting.
    notes.append(ocr_quality_note(parsed_cues))
    if len(cues) < 50:
        notes.append(
            f"Only {len(cues)} cues detected; check a few against the film."
        )
    notes.append("Tesseract OCR is ~95-99% accurate; spot-check spelling.")

    sidecar = sidecar_srt_path(output_path)
    write_srt_file(sidecar, cues)
    notes.extend(
        write_ocr_diagnostic_kit(
            output_path, track_path=img_path, raw_srt_path=raw_srt_for_kit
        )
    )
    # Clean intermediates (the kit holds the retained copies).
    for tmp in (img_path,):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
    shutil.rmtree(os.path.join(workdir, "frames"), ignore_errors=True)
    return OcrResult(
        srt_path=sidecar,
        cue_count=len(cues),
        notes=notes,
        engine=engine,
        language=OCR_LANGUAGE,
        auto_resolved_note=auto_resolved_note,
    )


@dataclass
class OcrResult:
    """What the OCR stage reports back to the runner."""
    srt_path: str
    cue_count: int
    notes: list[str] = field(default_factory=list)
    engine: str = "unknown"
    language: str = OCR_LANGUAGE
    auto_resolved_note: str = ""


def mux_language(lang: str) -> str:
    """ffprobe subtitle language -> MP4 track tag (the honesty rule).

    'eng'/'en' -> 'eng'; anything else (including '', the unconfirmed
    case) -> 'und'. Never stamp 'eng' on an unconfirmed stream
    (b2m subtitles.py:458-466; the v0.2.16-embedded-subs §3 rule).
    """
    return "eng" if lang in ("eng", "en") else "und"


def mux_srt_into_mp4(
    video_path: str,
    srt_path: str,
    *,
    language: str = "und",
    ffmpeg: str,
    runner: Runner,
) -> str:
    """Embed srt_path as a soft mov_text track in video_path.

    Port of b2m's mux_srt_into_mp4 (v0.2.16-embedded-subs §2): muxes to
    a temp sibling then os.replace()s it over video_path, so a failed
    or interrupted mux can never destroy the finished encode -- on any
    failure the temp is deleted and video_path stays bit-identical.
    mov_text is the only subtitle codec MP4 supports and the one every
    player reads from .mp4. Through the runner seam here.
    """
    tmp = video_path + ".muxing.mp4"  # sibling: same fs -> atomic replace
    if os.path.exists(tmp):
        try:
            os.remove(tmp)  # stale temp from a killed run
        except OSError:
            pass
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", video_path, "-i", srt_path,
        "-map", "0", "-map", "1",
        "-c", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", f"language={mux_language(language)}",
        tmp,
    ]
    code, _out, err = runner(cmd[0], cmd[1:])
    if code != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise SubtitleOcrError(
            f"subtitle muxing failed: {(err or '').strip()[:300]}"
        )
    try:
        os.replace(tmp, video_path)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise SubtitleOcrError(
            f"subtitle muxing could not replace the MP4: {exc}"
        ) from exc
    return video_path


def ocr_selected_track(
    subtitle_plan: "SubtitlePlan",
    source_path: str,
    output_path: str,
    *,
    ffmpeg: str,
    runner: Runner,
    workdir: str,
    on_step: Callable[[str], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> "OcrResult":
    """Thin alias: the runner's entry point for the OCR stage."""
    return run_subtitle_ocr(
        subtitle_plan,
        source_path,
        output_path,
        ffmpeg=ffmpeg,
        runner=runner,
        workdir=workdir,
        on_step=on_step,
        on_progress=on_progress,
    )