"""PyQt desktop interface for planning and running VidLift upscales.

v0.4.4 (design: docs/designs/v0.4.4-gui-status-mcp-unified.md): the
GUI is a VIEW over the MCP bridge's persistent job registry — Start
enqueues JobRecords through :mod:`vidlift.mcp_bridge.client_local`,
Cancel flips the same claim-time CancelFlag an MCP agent's cancel
flips, and the per-second tick reads the bridge's own
``_status_payload`` shapes. The old in-thread serial UpscaleWorker is
gone; there is exactly one worker (the bridge's JobExecutor).

Proven ports from the sibling bluray-to-mp4 UI (behavior contracts,
read-only reference): the EtaProgressBar busy/ETA discipline, the
ActivityDots liveness row, the 1 Hz single-writer elapsed/forecast
timer, restart-tolerant cancellation, and the honest "unknown ETA"
never lying.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
import threading
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QTimer, QThreadPool, QUrl, QByteArray, QEvent
from PyQt6.QtGui import QAction, QDragEnterEvent, QDropEvent, QFont, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .config import (
    MONITOR_ENABLED_DEFAULT,
    MONITOR_HOST_DEFAULT,
    MONITOR_PORT_DEFAULT,
    MONITOR_TOKEN_DEFAULT,
    MONITOR_DISCOVERY_FILENAME,
    MONITOR_DISCOVERY_SCHEMA,
    QSETTINGS_MONITOR_KEYS,
    DENOISE_DEFAULT,
    QSETTINGS_DENOISE_KEYS,
)
from .mcp_bridge.client_local import (
    BatchTooLarge,
    LocalBridgeClient,
    default_state_dir,
)
from .mcp_bridge.tools import BridgeError
from .monitor import (
    lan_ip,
    start_server_with_fallback,
    stop_server,
)
from .run import UPSCALE_STEP_PREPARED_COUNT as PREPARED_COUNT_PROBE_STEP
from .plan import BatchPlan, PlannedFile, build_batch_plan
from .probe import ProbeError, probe_and_classify
# v0.4.16 (model management, ADR-4): the GUI-owned install worker — the
# GPU-meter pattern (QThreadPool/QRunnable + signals), never the serial
# JobExecutor, never a remote surface.
from .ui.model_install import ModelInstallRunnable, ModelInstallSignals
from .transcribe import DEFAULT_MODEL, model_install_state
# v0.4.15.3 (F-5): the Mode B output name has ONE derivation (regex
# stem-strip in subs_job) — the GUI's display, pre-flight, and run
# guard reuse it so they can never diverge on non-4-char extensions
# (.ts/.divx/.mpeg): the old path[:-4] slice mangled those stems.
# v0.4.19 (WS2): the plan-time verdict + verdict constants ride the
# same import — the pre-flight decides WHO gets enqueued before the
# conflict guard and the rows render.
from .subs_job import (
    SUBS_VERDICT_BLOCKED_OUTPUT,
    SUBS_VERDICT_PROBE_ERROR,
    SUBS_VERDICT_READY,
    SUBS_VERDICT_SKIP_SUBTITLES,
    subs_output_path,
    subs_plan_verdict,
)
from .run import MissingToolError, find_tools
from .run import summarize_batch, BatchSummary
from .ui.gpu_meter import (
    GPU_METER_HEIGHT,
    GPU_METER_WIDTH,
    GpuMeterWidget,
)
from .ui.widgets import ActivityDots, EtaProgressBar
from .ui.view_state import PlanInputs, SubtitleOptions, positive_gbph
from .ui.drop_area import SubtitleDropGroup
from .ui.preflight import PreparationRunnable, PreflightRunnable, PreflightSignals, probe_runner
from .ui.review import BatchReviewDialog


# v0.4.22 (scope restore): the one reviewed row against the .19 bridge
# contract. The HEAD-era durable submission receipts (registry
# transactions) are deferred (BRIDGE-1); validation failures stay
# provably pre-mutation and everything else fails closed via
# "unknown" (see _submit_gui_job).
@dataclass(frozen=True)
class SubmissionOutcome:
    state: str  # "accepted" | "rejected" | "unknown"
    job_ids: tuple[str, ...] = ()
    error: Exception | None = None


VIDEO_EXTENSIONS = {
    ".avi", ".divx", ".flv", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".ts", ".vob", ".wmv",
}

# v0.4.4 GUI safety gate (design §5): folder-add refuses absurd
# single-shot selections before planning; per-click Start enqueues at
# most MAX_GUI_BATCH job records (client_local also enforces).
MAX_FILES = 500


def collect_video_files(folder: str | Path, *, cancel: threading.Event | None = None) -> list[str]:
    """v0.4.18 (recursive folder-add, Eric's whole-show case): walk a
    folder TREE and return every recognized video (VIDEO_EXTENSIONS),
    deduplicated and sorted by path. Shared by BOTH Add Folder buttons
    (Mode A upscale, Mode B subtitles) and the drop handler — one
    ingestion traversal, one count policy. Recursion is unconditional
    (no toggle): the file pickers remain the shallow-pick surface.

    Symlink discipline: ``os.walk(followlinks=False)`` never descends
    into a symlinked directory, so a symlink loop CANNOT form (no
    visited-inode bookkeeping needed); file-level symlink duplicates
    collapse via normcase(realpath) dedupe, with the lexicographically
    first name surviving (dedupe runs over the sorted list, never on
    walk order). Unreadable subtrees are skipped silently (os.walk's
    documented default). The result is fully sorted — the pre-0.4.18
    ``sorted(iterdir())`` determinism generalized to the tree.

    v0.4.19 (WS2): never yields VidLift's own ``*.subs.mp4`` /
    ``*.subs.mkv`` outputs (any case).
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        if cancel is not None and cancel.is_set():
            return []
        dirnames.sort()  # deterministic descent, independent of os.walk order
        for name in filenames:
            if cancel is not None and cancel.is_set():
                return []
            candidate = Path(dirpath) / name
            if candidate.suffix.lower() in VIDEO_EXTENSIONS:
                # v0.4.19 (WS2): VidLift's OWN keep-original outputs are
                # never re-ingested — re-adding a finished folder must
                # not re-queue its own results. v0.4.24: BOTH families,
                # the legacy ``{stem}.subs.mp4`` and the current
                # ``{stem}.subs.mkv`` (container ADR). Name-based fast
                # path only: the replace-source-renamed form is
                # name-indistinguishable and is caught by the WS2
                # plan-time subtitle probe instead (belt + suspenders).
                if name.lower().endswith((".subs.mp4", ".subs.mkv")):
                    continue
                found.append(str(candidate))
    seen: set[str] = set()
    collected: list[str] = []
    for path in sorted(found):
        key = os.path.normcase(os.path.realpath(path))
        if key in seen:
            continue
        seen.add(key)
        collected.append(path)
    return collected


def _collect_inputs(paths: tuple[str, ...], cancel: threading.Event) -> list[str]:
    """Shared worker ingestion, preserving tree order and canonical deduplication."""
    collected, seen = [], set()
    for raw in paths:
        if cancel.is_set():
            return []
        candidate = Path(raw)
        expanded = collect_video_files(candidate, cancel=cancel) if candidate.is_dir() else [raw]
        for path in expanded:
            if cancel.is_set():
                return []
            candidate = Path(path)
            # v0.4.24: exclude both subs-output families (legacy .subs.mp4
            # + current .subs.mkv) — mirrors collect_video_files' guard.
            if candidate.suffix.lower() not in VIDEO_EXTENSIONS or candidate.name.lower().endswith(
                (".subs.mp4", ".subs.mkv")
            ):
                continue
            key = os.path.normcase(os.path.realpath(candidate))
            if key not in seen:
                seen.add(key)
                collected.append(str(candidate))
    return collected

# v0.4.5 (progress-honesty design §2, D-tail): pin-to-tail tolerance
# for the run log widget. Within this many pixels of the scrollbar's
# maximum the log is "at the bottom" and stays pinned to the tail;
# farther up counts as user-inspecting and disables the pin.
_LOG_PIN_TOLERANCE_PX = 8

# v0.4.5 (§3c, D-eta): the honest busy label while the count pass is
# still producing the denominator — it names what is being measured,
# never a fabricated percent.
_BUSY_MEASURING_LABEL = "measuring — counting frames…"
# v0.4.15.3 (F-4): the busy label keys on the PHASE — only the upscale
# count probe "measures"; every other unmeasured phase names ITSELF.
# (run.py merges progress dicts, so stale frame counters ride every
# later phase's payload; the label must never say "measuring" — or a
# bare "estimating" — for work that has its own name.)
# v0.4.15.4 (N-2): key parity with the emitted phases — "plan"
# (run.py's PHASE_PLAN, emitted for real) pulses its own label; the
# dead "finalize" key is dropped (no writer emits it). "done" stays
# out deliberately: terminal snapshots never busy-pulse. The parity is
# pinned structurally (tests: the emitted-phase set asserts key-set
# equality), so a future phase addition fails the suite instead of
# silently falling back.
_PHASE_BUSY_LABELS = {
    "upscale": _BUSY_MEASURING_LABEL,
    "probe": "probing ...",
    "plan": "planning ...",
    "prepare": "preparing ...",
    "encode": "encoding ...",
    "verify": "verifying ...",
    "subtitles": "working subtitles ...",
    "transcribe": "transcribing audio ...",
    "subs-mux": "muxing subtitles ...",
}

# closeEvent budget (ms): the run worker gets the same 30 s ffmpeg
# terminate window the sibling's validation established; terminate is
# the documented last resort, never the first move.
_CLOSE_WAIT_RUN_MS = 30000

# The engine combo rows (design §7): internal values match
# select_engine's request strings byte-for-byte; labels name the honest
# consequence of each choice. Stub is exposed deliberately — it is the
# honest fail-open destination and users occasionally want it.
ENGINE_CHOICES: list[tuple[str, str]] = [
    ("Auto (recommended)", "auto"),
    ("Real-ESRGAN ncnn/Vulkan (GPU)", "ncnn"),
    ("Stub (no AI; encode-only fallback)", "stub"),
]

# v0.4.8 (design D5): the model-profile rows — which MODEL_TABLE weights
# a run upscales with. Manual switch only (D3); the value maps
# 1:1 into run_file(profile=...) through the bridge's
# options["upscale_profile"].
PROFILE_CHOICES: list[tuple[str, str]] = [
    ("Live action (general x2plus)", "live-action"),
    ("Anime / cartoons (animevideov3-x2)", "anime"),
]

PROFILE_TOOLTIP = (
    "Live action uses the general Real-ESRGAN x2plus net (default). "
    "Anime/cartoons uses the animevideov3-x2 net — faster on flat fills "
    "and line art. Manual switch; VidLift does not auto-detect content "
    "type. Persists across sessions."
)

# v0.4.15 (opt-in denoise): the three-state combo (a checkbox cannot
# name two strengths) beside the transcription checkbox row, default
# Off. The tooltip is the ERIC-LOCKED guidance copy — character-for-
# character per the design (Karen diffs it against §5's block); the
# CLI help and docs/usage.md carry the same contract.
DENOISE_COMBO_LABEL = "Denoise source before upscaling"
DENOISE_TOOLTIP = (
    "Removes tape noise and grain before the upscale pass.\n\n"
    "Use it for noisy sources: VHS rips, old tape transfers, grainy or "
    "snowy analog captures. Leave it Off for clean DVD or Blu-ray rips — "
    "upscaling already softens grain, and denoising a clean source only "
    "costs fine detail. Light is the right strength for most noisy "
    "tapes; try Strong only if noise is still visible. Adds almost no "
    "time compared to the upscale itself."
)
DENOISE_CHOICES = (("Off", "off"), ("Light", "light"), ("Strong", "strong"))

PLAN_HELPER_TEXT = ("Add files → Plan Batch → Review → Start Upscale. "
                    "Adding and planning never start processing.")


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value / 1_000_000_000:.2f} GB"


# v0.4.10 (R2): the honest batch-bar format strings. Truth depends on
# which non-zero outcomes exist (a cancelled job may NEVER ride a
# "completed" count); mixed outcomes join with ', '. A still-running
# batch keeps its pending tail ("x/y succeeded" while anything is
# unresolved) so the never-lying bar reads the same mid-run and done.
def _batch_format(summary: BatchSummary) -> str:
    total = summary.total
    if summary.cancelled and summary.failed:
        return f"{summary.succeeded}/{total} succeeded, {summary.cancelled} cancelled, {summary.failed} failed"
    if summary.cancelled:
        return f"{summary.succeeded}/{total} succeeded, {summary.cancelled} cancelled"
    if summary.failed:
        return f"{summary.succeeded}/{total} succeeded, {summary.failed} failed"
    return f"{summary.succeeded}/{total} succeeded"


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def warnings_for(item: PlannedFile) -> list[str]:
    warnings: list[str] = []
    report = item.report
    if report.pal_speedup_suspected:
        warnings.append("PAL speedup suspected; VidLift will process as-is")
    if report.interlaced_flagged:
        warnings.append("Interlace flagged; engine must deinterlace before upscale")
    if item.process_verdict == "output-exists":
        # v0.4.12 (item 3): the plan-time collision gate. The planned
        # output name sits on disk already; the encoder's -y would erase
        # it silently. The warning NAMES the file (the acceptance
        # wording); resolution is a user decision — no force flag ships
        # this cycle.
        warnings.append(
            f"output exists: {Path(item.output_path).name} (will not be overwritten)"
        )
    elif item.process_verdict != "ready":
        warnings.append("Skipped unless explicitly forced in a later pipeline version")
    return warnings


# v0.4.9 (design §2.3, Eric D4): the stale-activity warning threshold —
# the GUI surfaces "running (no activity N min)" when the snapshot's
# activity age exceeds this many seconds. Surface-only: never an
# auto-cancel, never a kill (a wedged GPU process and a slow-but-healthy
# chunk look identical from outside; the label says exactly what is
# known). Slower GPUs may prefer 1800; the constant is the shipped
# default.
STALE_ACTIVITY_SECONDS = 900


def detail_text(
    item: PlannedFile,
    *,
    subtitle_ocr_enabled: bool = True,
    upscale_profile: str | None = None,
) -> str:
    report = item.report
    source = report.video.codec_name if report.video else "no video"
    subtitle_text = item.subtitle_note
    if (
        not subtitle_ocr_enabled
        and item.subtitle_plan is not None
        and item.subtitle_plan.ocr_required
    ):
        subtitle_text = "bitmap subtitle OCR disabled; no subtitles will be carried"
    lines = [
        str(report.path),
        "",
        f"Verdict: {item.process_verdict}",
        f"Source: {report.source_class} ({source})",
        f"Duration: {format_duration(report.duration_seconds)}",
        f"Displayed geometry: {report.displayed_width}x{report.displayed_height}",
        "",
        "OUTPUT PREVIEW",
        f"Destination: {item.output_path}",
        f"Target geometry: {item.output_width}x{item.output_height}",
        f"Video: HEVC MKV, {item.output_width}x{item.output_height}, {item.video_bitrate_kbps}k",
        f"Audio: AAC, {item.audio.bitrate_kbps}k {item.audio.label}",
        f"Subtitles: {subtitle_text}",
        f"Estimated size: {format_bytes(item.estimated_output_bytes)}",
        "",
        f"Expectation: {item.expectation}",
    ]
    # v0.4.8 (design D6): the one additive anime sentence at the run
    # surfaces that hold the profile — never in plan.py, so
    # PlannedFile.expectation stays stable for every plan consumer.
    if upscale_profile == "anime":
        from .cli import ANIME_EXPECTATION_SUFFIX

        lines.append(ANIME_EXPECTATION_SUFFIX)
    warnings = warnings_for(item)
    if warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


def _stage_label_text(stage: str) -> str:
    """The stage-change comparison text: the FIRST SEGMENT of the
    label with any parenthetical detail stripped (v0.4.5 §3b: heartbeat
    seconds tick inside one and the same stage; they must not reset
    the busy/ETA state every second)."""
    return stage.split(" (", 1)[0]


def _subs_enqueue_summary(queued: int, skipped: int, blocked: int) -> str:
    """v0.4.19 (WS2): the enqueue-time count line — enqueued jobs plus
    the pre-flight outcome counts, so a folder re-add answers 'what
    happened to the other 195 files' without red failures."""
    text = f"{queued} queued for subtitles"
    extras: list[str] = []
    if skipped:
        extras.append(f"{skipped} skipped (already subtitled)")
    if blocked:
        extras.append(f"{blocked} blocked (output exists)")
    if extras:
        text += " — " + ", ".join(extras)
    return text


def _device_display(snapshot: dict | None) -> str | None:
    """v0.4.19 (WS3): the ONE device-wording rule. Reads the additive
    ``transcription`` echo (``device`` / ``device_requested``) that rides
    every status payload, and renders the honest state:

      device "cuda"                     -> "GPU (CUDA)"
      device "cpu" + requested "cpu"    -> "CPU"
      device "cpu" + requested unknown  -> "CPU"        (legacy records)
      device "cpu" + requested auto/cuda-> "CPU (GPU unavailable)"
      device "cpu (cuda unavailable)"   -> "CPU (GPU unavailable)"
      other truthy device               -> verbatim (never massaged)
      device null + requested, queued   -> "<requested> (requested)"
      device null + requested, running  -> "<requested> — resolving…"
      both null                         -> None (no display at all)

    Honest-null discipline: an absent fact renders NOTHING (older
    bridges, non-transcribing jobs, run jobs before the plan resolves)
    — never a fabricated "CPU" before the fallback actually happened.
    """
    if not snapshot:
        return None
    tr = snapshot.get("transcription") or {}
    device = tr.get("device")
    requested = tr.get("device_requested")
    if device:
        if device == "cuda":
            return "GPU (CUDA)"
        if device == "cpu (cuda unavailable)":
            return "CPU (GPU unavailable)"
        if device == "cpu":
            return (
                "CPU"
                if (requested == "cpu" or not requested)
                else "CPU (GPU unavailable)"
            )
        return str(device)
    if requested:
        if snapshot.get("state") == "queued":
            return f"{requested} (requested)"
        return f"{requested} — resolving…"
    return None


def _with_device_suffix(label: str, snapshot: dict) -> str:
    """v0.4.19 (WS3): the transcription-family stage labels (Mode B
    ``transcribe``/``subs-mux``, Mode A ``subtitles``) carry the
    RESOLVED device once known — the at-a-glance answer. Pre-resolution
    and requested-only states never ride the stage label (no ETA/busy
    key churn on the resolve edge for them; the detail overlay carries
    the resolving wording).
    """
    if (snapshot or {}).get("current_phase") not in (
        "transcribe",
        "subtitles",
        "subs-mux",
    ):
        return label
    tr = (snapshot or {}).get("transcription") or {}
    if not tr.get("device"):
        return label
    display = _device_display(snapshot)
    if display is None:
        return label
    return f"{label} — {display}"


def run_stage_label(snapshot: dict | None) -> str:
    """Stage/step classification from the bridge status payload (the
    sibling's _run_stage classifier, ported to dict facts). Names WHAT
    is running from the runner's own ``current_phase`` plus the
    ``progress["step"]``/``progress["phase_note"]`` labels; never
    guesses a percent.

    v0.4.5 (progress-honesty §3, D-count/D-eta): the count pass says
    so and warns that no per-frame ETA exists yet; the promoted
    chunk_index/chunk_total render as ``upscaling chunk 3/12``; the
    long phases surface their heartbeat seconds; a
    ``progress["phase_note"]`` is appended as a parenthetical suffix.
    """
    if not snapshot:
        return ""
    phase = snapshot.get("current_phase") or ""
    if snapshot.get("state") == "queued":
        return "queued — waiting for a worker"
    if snapshot.get("state") in ("succeeded", "failed", "cancelled"):
        return snapshot.get("state") or phase
    progress = snapshot.get("progress") or {}
    step = progress.get("step") or ""
    chunk_text = ""
    progress_state = progress or {}
    frames_done = progress_state.get("frames_done")
    frames_total = progress_state.get("frames_total")
    # v0.4.15.3 (F-4): the frame suffix renders only in the phase that
    # OWNS the frame denominators (upscale). run.py's record_step
    # merges (never replaces) progress dicts, so upscale's final
    # counters ride every later phase's payload — rendering them there
    # printed "encoding frames 3600/3600" through a whole verify.
    if (
        frames_done is not None
        and frames_total
        and phase == "upscale"
    ):
        chunk_text = f" frames {frames_done}/{frames_total}"
    # v0.4.16 (model management, F2): an in-flight model download renders
    # its byte truth BEFORE the phase dispatch — the download rides the
    # existing transcribe/subtitles phases (no new phase, no new busy
    # label; the N-2 parity pins stay intact), and the honest
    # total-unknown shape never fabricates a percent.
    dl = progress.get("model_download")
    if isinstance(dl, dict) and phase in ("transcribe", "subtitles"):
        done = dl.get("bytes_done")
        total = dl.get("bytes_total")
        if isinstance(done, (int, float)) and done >= 0:
            if isinstance(total, (int, float)) and total > 0:
                pct = min(100.0, done * 100.0 / total)
                return _with_device_suffix(
                    f"downloading transcription model — "
                    f"{format_bytes(int(done))} / {format_bytes(int(total))} "
                    f"({pct:.0f}%)",
                    snapshot,
                )
            return _with_device_suffix(
                f"downloading transcription model — "
                f"{format_bytes(int(done))} (total size unknown)",
                snapshot,
            )
    if phase == "probe":
        base = "probe — reading source metadata"
    elif phase == "prepare":
        base = "prepare — normalizing source"
    elif phase == "upscale":
        if step == PREPARED_COUNT_PROBE_STEP:
            # v0.4.5 §3b (D-eta): the count pass has no measurable
            # denominator — it IS the denominator being measured. The
            # honest label says so; no fabricated percentage.
            base = (
                "upscaling — counting frames (this can take 10–40 min "
                "on SD sources; no per-frame ETA exists yet)"
            )
        elif progress_state.get("chunk_index") and progress_state.get("chunk_total"):
            # §3b: the promoted chunk-index/total label; the raw frame
            # counters stay out of the sub-stage label (a chunk is a
            # known unit: chunk 3/12; "frames 300/3600" remains on the
            # stage bar via frames_done/frames_total).
            chunk_index = progress_state.get("chunk_index")
            chunk_total = progress_state.get("chunk_total")
            if isinstance(chunk_index, int) and chunk_index > 0 and isinstance(chunk_total, int) and chunk_total > 0:
                base = f"upscaling chunk {chunk_index}/{chunk_total}"
            else:
                base = f"upscaling{chunk_text}"
        else:
            base = f"upscaling{chunk_text}"
    elif phase == "encode":
        base = f"encoding{chunk_text}"
    elif phase == "verify":
        base = "verify — full decode"
    elif phase == "subtitles":
        base = "subtitles — OCR/carry"
    elif phase == "transcribe":
        # v0.4.14 (Mode B, the count-pass honest-label pattern §3b
        # D-eta): the denominator is unknown until whisper's stream
        # ENDS (segments_done counts up with no total), so the base
        # label names the work and shows the count so far; no
        # percentage can exist yet. Elapsed seconds ride the existing
        # liveness clock; the ETA machinery contributes no digits.
        base = "transcribing audio"
        segments_done = progress_state.get("segments_done")
        if isinstance(segments_done, (int, float)) and segments_done > 0:
            unit = "segment" if int(segments_done) == 1 else "segments"
            base = f"transcribing audio — {int(segments_done)} {unit} done"
    elif phase == "subs-mux":
        # v0.4.14 (Mode B): the soft-mux is a stream copy — seconds.
        # Honest by construction; no ETA digits (elapsed only).
        base = "remuxing subtitles (stream copy — no re-encode)"
    elif phase == "done":
        base = "done"
    else:
        base = phase or "starting"
    # v0.4.5 §3b: the two long phases surface the heartbeat's elapsed
    # seconds honestly (the runner already measures them via on_touch).
    beat_seconds = progress_state.get(
        "decode_seconds" if phase == "verify" else "encode_seconds"
    )
    if phase in ("encode", "verify") and isinstance(beat_seconds, (int, float)):
        base = f"{base} (heartbeat t={beat_seconds:.0f}s)"
    # §3b: the phase_note suffix — a short human string the engine sets
    # on entering a sub-stage with no measurable denominator.
    note = progress_state.get("phase_note")
    label: str
    if note:
        if isinstance(note, str) and note.startswith(base):
            # the note already carries the base render (the engine's
            # chunk notes embed the chunk label); render it once.
            label = note
        else:
            label = f"{base} ({note})"
    elif step and not (
        isinstance(step, str)
        and (
            step.startswith("subtitle")
            # a token that is already rendered inside base (the bare
            # phase word or a chunk label carrying chunk_index) adds
            # no information — never double-render it
            or step == phase
            or (isinstance(step, str) and step in base)
        )
    ):
        label = f"{base} ({step})"
    else:
        label = base
    # v0.4.19 (WS3): the resolved-device suffix for the transcription
    # family (suffix helper owns the phase/fact gates).
    return _with_device_suffix(label, snapshot)


class MainWindow(QMainWindow):
    def __init__(self, *, client: LocalBridgeClient | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"VidLift {__version__}")
        self.resize(1180, 760)
        self.setAcceptDrops(True)
        self.files: list[str] = []
        self.batch: BatchPlan | None = None
        self._planned_inputs: PlanInputs | None = None
        self._plan_dirty = False
        self._preflight_generation = 0
        self._preflights: dict[int, tuple] = {}
        self._plan_checking = False
        self._preparation_kind: str | None = None
        self._upscale_submitting = False
        self._ui_closed = False
        self._uncertain_upscale_paths: set[str] = set()
        self._uncertain_subtitle_paths: set[str] = set()
        self._degraded_jobs: set[str] = set()

        # ---- v0.4.4 unified-run state (design §4.1) ----
        # The bridge client owns the registry + JobExecutor (one worker,
        # serial). The GUI is a projection: every tick re-reads status.
        self.client = client or LocalBridgeClient()
        self.job_ids: list[str] = []
        # v0.4.10 (R2): the batch bar's chunk-color cache — changed
        # ONLY through _apply_batch_summary so a QSS restyle fires
        # once per color change, never per tick.
        self._progress_chunk_color: str | None = None

        # v0.4.12 (item 1): the stable job_id->table-row map for the
        # batch's cosmetic cell writes; empty until a batch starts
        # (seeded in start_upscale beside the slot index, cleared with
        # the table on clear_files/_render_pending_files).
        self._batch_row_for_job: dict[str, int] = {}

        # v0.4.13 addendum (selection-follow, design §3.2): the follow
        # state machine. _follow_mode True = the selection tracks the
        # RUNNING job (auto-select on claim edges); a user selection
        # (mouse or keyboard) disengages it until the next claim edge
        # ("stay on the selected item until the running item switches
        # then adjust it" — Eric's rule). _follow_job_id is the running
        # identity last followed/observed; None resets at batch start so
        # the FIRST claim is always an identity EDGE. _programmatic_select
        # distinguishes the follow's own selectRow writes from user
        # selection in itemSelectionChanged (any NEW programmatic
        # selection writer MUST use _follow_select_row or replicate this
        # guard — the fail-safe direction is follow-disengage, T3-class
        # tests catch violators).
        self._follow_mode: bool = True
        self._follow_job_id: str | None = None
        self._programmatic_select: bool = False

        # v0.4.12 (item 2): per-current-job ETA state — EWMA of the
        # measured phase rate, keyed on CHUNK boundaries (a long
        # disk-bound lull must not bleed into the GPU rate), reset at
        # every phase transition and at every batch start. Seeded empty:
        # ETA digits appear only after >=2 chunk samples, never
        # fabricated.
        self._eta_state: dict = {}
        # v0.4.12 (item 2): the phase the ETA window belongs to (the
        # stage's first word); a change wipes the window.
        self._eta_phase_now: str | None = None
        # Display damping for the current-job ETA: when the newly
        # computed value is within 25% of the last rendered one (and the
        # rate did not truly change), render the previous value — the
        # number must not visibly jitter at 1 Hz.
        self._eta_rendered: float | None = None
        # Batch label damping mirrors the same rule across ticks.
        self._batch_eta_rendered: float | None = None

        # v0.4.5 (progress-honesty §2, D-tail): pin-to-tail state for
        # the run log widget (Rule 1-5 contract lives in these flags;
        # the helpers read/reset them). Started disconnected; the
        # scroll/rangec hooks wire up in _build_shell.
        self._log_user_scrolled_up = False
        self._log_force_pin_next = False
        self._log_rendered_key: str | None = None
        self._log_scroll_max = 0  # cached maximum (rangeChanged writer)

        # v0.4.6 (status-API design §4): monitor widgets + state.
        # Default-on per settings; actual startup waits for the tail
        # of __init__ (the pill must exist before a fallback-port
        # report reaches it). Startup failure is non-fatal — the pill
        # says so and the app continues.
        self.monitor_check = QCheckBox("LAN monitoring")
        self.monitor_check.setToolTip("Allow agents to check video/subtitle job progress from other machines.")
        self.monitor_pill = QLabel("")
        self.monitor_pill.setObjectName("VersionBadge")
        self.monitor_port_reported: int | None = None

        # ---- v0.4.16 (model management, ADR-4): the subs-model pill
        # + Install button, beside the monitor pill (the proven chrome
        # for a machine-state fact with an action affordance; visible
        # at launch, pre-plan — model presence is global machine
        # state, not per-plan input).
        self.subs_model_pill = QLabel("")
        self.subs_model_pill.setObjectName("VersionBadge")
        self.subs_model_button = QPushButton("Install subtitle model")
        self.subs_model_button.setObjectName("ModelInstallButton")
        self.subs_model_button.setToolTip(
            "Download the transcription model (~1.5 GB, one time, "
            "sha256-verified) used by Add subtitles. Downloads in the "
            "background; jobs keep running."
        )
        self.subs_model_button.clicked.connect(self._install_subs_model)
        # The in-flight click guard (G-5): exactly one install runnable
        # at a time; reads False until the first Install click.
        self._subs_model_installing = False

        # Sibling port: the 1 Hz single-writer liveness timer.
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._run_started_at: float | None = None
        self._stage_now = "queued"
        self._stage_eta: float | None = None
        self._stage_fraction: float | None = None
        # v0.4.15.3 (F-4): the last MEASURED stage fraction (0.0 until a
        # measurable tick). The overall queue bar renders this while the
        # current phase owns no fraction, so the bar is monotonic — it
        # never visibly RECEEDS at a phase change.
        self._stage_fraction_measured: float = 0.0
        # v0.4.15.4 (F-13c): the ACTUALLY-running job id, tracked from
        # the 1 Hz drain's running snapshots. "Cancel Current" keys on
        # it — a queued-only batch has NO current job, and the old
        # active_job_count() rule enabled the button over queued work
        # its click would permanently kill.
        self._running_job_id: str | None = None

        self.file_table = QTableWidget(0, 9)
        self.file_table.setHorizontalHeaderLabels(
            ["Status", "File", "Source", "Target / source policy", "Output", "Size", "Subtitles", "Warnings", "Mode"]
        )
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.verticalHeader().hide()
        header = self.file_table.horizontalHeader()
        # v0.4.23.0: user-draggable column edges. Interactive is the
        # only resize mode that permits dragging a section edge
        # (ResizeToContents and Stretch both lock it); the last section
        # still stretches so the table keeps filling the window width.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.file_table.itemSelectionChanged.connect(self._sync_details)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Select a video to review its plan, staged options or job results.")

        self.gbph = QLineEdit("1.5")
        self.gbph.setFixedWidth(90)
        self.gbph.setToolTip("Target output size in gigabytes per hour")
        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("Beside each input")
        self.subtitle_ocr = QCheckBox("Enable subtitle OCR")
        self.subtitle_ocr.setChecked(True)
        self.subtitle_ocr.setToolTip(
            "Convert image-based PGS/VobSub subtitles to text. Ordinary text subtitles are still carried when this is off."
        )
        self.subtitle_ocr.toggled.connect(self._refresh_details)
        # v0.4.7 (delete-and-replace): strictly opt-in, default OFF;
        # persisted via QSettings like every other GUI preference.
        self.replace_source = QCheckBox(
            "Replace original on success (deletes the source video)"
        )
        self.replace_source.setChecked(
            self._settings().value("replace_source", False, type=bool)
        )
        self.replace_source.setToolTip(
            "After a verified upscale, delete the source video and move the "
            "finished 720p MKV into its original path.\n\n"
            "Replaces the original file after a successful upscale "
            "(irreversible). If the replace cannot be done, the upscale "
            "output is kept in its planned location and nothing is deleted."
        )
        self.replace_source.toggled.connect(self._replace_source_changed)

        # v0.4.9 (resume claim, design §1.4): opt-in claim of retained
        # scratch from an earlier failed/cancelled run of the same file.
        # Default OFF (the replace-source pattern); QSettings-persisted
        # like every other GUI preference.
        self.resume_claim = QCheckBox("Resume retained work")
        self.resume_claim.setChecked(
            self._settings().value("resume_claim", False, type=bool)
        )
        self.resume_claim.setToolTip(
            "When a previous run of this same file was cancelled or failed, "
            "claim its retained partial work (prepared mezzanine + completed "
            "chunks) instead of starting over.\n\n"
            "The retained work is used only when the source file, the "
            "conversion plan, and the prepared content all prove identical "
            "(a decode-count pass proves the content); otherwise the run "
            "prepares fresh and the old scratch is left for cleanup."
        )
        self.resume_claim.toggled.connect(self._resume_claim_changed)

        # v0.4.14 (Mode A, Eric's locked default ON): transcribe the
        # source's own audio when no subtitle track exists. The honest
        # tooltip says the cost; QSettings-persisted like every other
        # GUI preference.
        # v0.4.17 workstream-2 (Hermes Finding 1): this checkbox was
        # created v0.4.14 but never added to a layout — invisible and
        # unclickable ever since (tests drove it only via setChecked).
        # It is RENDERED by v0.4.17 workstream-2 at the upscale grid
        # (3,3,1,3); the label is shortened to the verb-first sibling
        # pattern ("Enable subtitle OCR") because the full semantics
        # live verbatim in the tooltip below (no-track condition,
        # 1.5 GB model, minutes-per-file cost, sync-from-own-audio).
        self.transcribe_subs = QCheckBox("Transcribe missing subtitles")
        self.transcribe_subs.setChecked(
            self._settings().value(
                "transcribe_subs", True, type=bool
            )
        )
        self.transcribe_subs.setToolTip(
            "When a file has no subtitle track, transcribe its own audio "
            "and embed the result (first use downloads a ~1.5 GB model "
            "once; adds minutes per file). Sync comes from the source's "
            "own audio stream, exactly the stream the encode keeps."
        )
        self.transcribe_subs.toggled.connect(self._transcribe_subs_changed)

        # v0.4.18 (opt-in suppress_srt, Mode A, D5/D6): strictly opt-in,
        # default OFF — the side-by-side KEEP deliverable is the
        # documented v0.4.15 contract, so deselected/absent means the
        # pre-0.4.18 behavior. The checkbox state IS the submitted
        # option (the v0.4.17 rule); QSettings persists under Mode A's
        # own key ("suppress_srt"), never Mode B's ("subs_suppress_srt").
        self.suppress_srt = QCheckBox("Suppress .srt sidecar on success")
        self.suppress_srt.setChecked(
            self._settings().value("suppress_srt", False, type=bool)
        )
        self.suppress_srt.setToolTip(
            "After subtitles are safely embedded in the finished MKV, do "
            "not keep the .srt sidecar beside the output — the toggleable "
            "track inside the MKV is the deliverable.\n\n"
            "Whenever subtitles fail open (no embed), the sidecar that "
            "was written is kept as the fallback, no matter how this "
            "option is set."
        )
        self.suppress_srt.toggled.connect(self._suppress_srt_changed)

        # v0.4.15 (opt-in denoise): three states in a combo (Off default,
        # Light, Strong) — a checkbox cannot name two strengths. The
        # Mode A upscale flow is the ONLY surface: Mode B (subs) has no
        # prepare chain, so the control says "before upscaling".
        self.denoise_combo = QComboBox()
        for label, data in DENOISE_CHOICES:
            self.denoise_combo.addItem(label, data)
        self.denoise_combo.setToolTip(DENOISE_TOOLTIP)
        self.denoise_mode = self._load_denoise_mode()
        index = self.denoise_combo.findData(self.denoise_mode)
        self.denoise_combo.setCurrentIndex(index if index >= 0 else 0)
        self.denoise_combo.currentIndexChanged.connect(
            self._denoise_changed
        )

        # v0.4.10 (R1): the GPU meter rides the top bar — a fixed-size
        # Task-Manager-style utilization chart (see vidlift/ui/
        # gpu_meter.py). Sampling is QThreadPool-off-thread, ~2 Hz,
        # hard 2 s child timeout; with nvidia-smi absent it renders
        # "GPU n/a" and never raises. It is read-only instrumentation:
        # the meter touches no run state and can never cancel or stall
        # an active upscale.
        self.gpu_meter = GpuMeterWidget()

        # ---- engine combo (design §7): EXACT three adapters in tree ----
        self.engine_combo = QComboBox()
        for label, data in ENGINE_CHOICES:
            self.engine_combo.addItem(label, data)
        self.engine_combo.setToolTip(
            "Auto tries the Real-ESRGAN GPU engine first and falls back to the stub, recording exactly why. "
            "Real-ESRGAN requires the bundled ncnn/Vulkan binary and models. "
            "Stub ships a verified re-encode with no AI pass."
        )
        self.engine_choice = self._load_engine_choice()
        index = self.engine_combo.findData(self.engine_choice)
        self.engine_combo.setCurrentIndex(index if index >= 0 else 0)
        self.engine_combo.currentIndexChanged.connect(self._engine_changed)

        # ---- profile combo (v0.4.8 design D5): which MODEL_TABLE lane ----
        self.profile_combo = QComboBox()
        for label, data in PROFILE_CHOICES:
            self.profile_combo.addItem(label, data)
        self.profile_combo.setToolTip(PROFILE_TOOLTIP)
        self.profile_choice = self._load_profile_choice()
        index = self.profile_combo.findData(self.profile_choice)
        self.profile_combo.setCurrentIndex(index if index >= 0 else 0)
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)

        self.plan_button = QPushButton("Plan Batch")
        self.plan_button.setObjectName("PrimaryButton")
        self.plan_button.setToolTip("Probe and classify every listed file. Nothing is written to disk.")
        self.plan_button.clicked.connect(self.request_plan)
        self.start_button = QPushButton("Start Upscale")
        self.start_button.setObjectName("StartButton")
        self.start_button.setEnabled(False)
        self.start_button.setToolTip(
            "Enqueue every ready file as a tracked job. A running engine choice cannot change; cancel to change it."
        )
        self.start_button.clicked.connect(self.start_upscale)
        self.cancel_button = QPushButton("Cancel Current")
        self.cancel_button.setEnabled(False)
        # v0.4.15.4 (F-13c): the tooltip is now true by construction —
        # the button cancels ONLY the tracked running job; queued jobs
        # are Cancel Batch's documented surface.
        self.cancel_button.setToolTip(
            "Stop the running job safely; queued jobs stay queued "
            "(use Cancel Batch to clear the queue)."
        )
        self.cancel_button.clicked.connect(self.cancel_upscale)
        # v0.4.13 (design §4.1): the batch-wide cancel — one action stops
        # the running job AND cancels every queued job in this batch
        # (registry-side atomicity; the GUI makes ONE client call).
        self.cancel_batch_button = QPushButton("Cancel Batch")
        self.cancel_batch_button.setEnabled(False)
        self.cancel_batch_button.setToolTip(
            "Stop the running job and cancel every queued job in this batch."
        )
        self.cancel_batch_button.clicked.connect(self.cancel_batch_upscale)
        # v0.4.14 (Mode B): standalone subtitles — transcribe a file's own
        # audio and soft-mux the result WITHOUT re-encoding or upscaling
        # the video. The queue table and the batch-accounting bootstrap
        # (_begin_job_slate) are shared with the upscale flow; the slate
        # machinery is job-kind-blind so row rendering/Cancel Batch ride
        # the exact v0.4.13 paths.
        self._staged_subs: list[dict] = []
        self._subs_submitting = False
        self.add_subs_button = QPushButton("Add Subtitles…")
        self.add_subs_button.setEnabled(True)
        # v0.4.17: the "never modified" sentence names the per-run opt-in
        # checkbox co-located beside this button (the Eric-confusion fix:
        # the scope of the replace control is visually unambiguous).
        self.add_subs_button.setToolTip(
            "Add subtitles by transcribing each file's own audio — without "
            "re-encoding the video.\n\n"
            "The output lands beside each source as <name>.subs.mkv plus a "
            ".srt sidecar; the source is kept (a per-run opt-in can replace "
            "it — see the checkbox beside this button). Existing videos at "
            "any resolution are welcome. First use downloads a ~1.5 GB model "
            "once."
        )
        self.add_subs_button.clicked.connect(self.choose_subs_files)
        # v0.4.18 (WS1 recursive folder-add, D1): the Mode B twin of
        # Mode A's "Add Folder" — an ATTRIBUTE (not a layout-local like
        # Mode A's button) so the T1 layout fence and the F-1
        # membership pin can hold it accountable. It feeds the SAME
        # shared collector (collect_video_files) through
        # choose_subs_folder: one traversal, one count policy across
        # both modes and the drop handler. Structure preservation is by
        # derivation, never by code: every output lands beside its own
        # source (subs_output_path), so nested shows never relocate.
        self.subs_add_folder_button = QPushButton("Add Folder")
        self.subs_add_folder_button.setToolTip(
            "Add recognized videos from a folder, including every "
            "subfolder — a whole show (all seasons) queues at once.\n\n"
            "Outputs land beside each source file, so the folder "
            "structure is preserved; the same per-run options (replace "
            "original, suppress .srt) apply as for picked files."
        )
        self.subs_add_folder_button.clicked.connect(self.choose_subs_folder)
        self.start_subtitles_button = QPushButton("Start Subtitles")
        self.start_subtitles_button.setEnabled(False)
        self.start_subtitles_button.clicked.connect(self.request_start_subtitles)
        self.start_subtitles_button.setObjectName("StartButton")
        self.subs_device_combo = QComboBox()
        for label, value in (("Auto", "auto"), ("GPU (CUDA)", "cuda"), ("CPU", "cpu")):
            self.subs_device_combo.addItem(label, value)
        stored_device = self._settings().value("subs_device", "auto", type=str)
        self.subs_device_combo.setCurrentIndex(max(0, self.subs_device_combo.findData(stored_device)))
        self.subs_device_combo.setToolTip(
            "Requested transcription device for files added next. GPU may fall back "
            "to CPU; the live Device line reports the actual resolved device."
        )
        self.subs_device_combo.currentIndexChanged.connect(
            lambda: self._settings().setValue("subs_device", self.subs_device_combo.currentData())
        )
        self.subs_remove_button = QPushButton("Remove selected staged")
        self.subs_remove_button.clicked.connect(self.remove_staged_subtitles)
        self.subs_clear_button = QPushButton("Clear staged subtitles")
        self.subs_clear_button.clicked.connect(self.clear_staged_subtitles)
        # v0.4.17 workstream-2 (opt-in Mode B replace-source, D4-1):
        # the checkbox lives IN the Mode B group's own grid at (0, 1),
        # immediately right of "Add Subtitles…" (0, 0) — co-located so
        # the scope is visually unambiguous (D4-1) — QSettings-
        # persisted under its own key ("subs_replace_source", no
        # collision with Mode A's "replace_source"), default OFF,
        # persisted from its toggled signal exactly like
        # _replace_source_changed. Workstream-2 (Karen K-1) shortens
        # the label from the 352px original — at (2, 8) in the shared
        # grid its hint taxed column 8 and pushed the window minimum
        # width 1290 -> 1532 — to the Mode A-parity verb-first label;
        # the tooltip (workstream-1 §6.3) stays verbatim.
        self.subs_replace_source = QCheckBox(
            "Replace original on success (deletes source)"
        )
        self.subs_replace_source.setChecked(
            self._settings().value("subs_replace_source", False, type=bool)
        )
        self.subs_replace_source.setToolTip(
            "After a successful subtitles pass, delete the original file "
            "and rename the output to the original name with an .mkv "
            "extension (e.g. …35 Hours.avi → …35 Hours.mkv).\n\n"
            "The .srt sidecar is renamed to match. If a file of that name "
            "already exists, or the replace cannot be done, both files are "
            "kept and the job still succeeds. Applies to Add Subtitles "
            "only — the upscale panel has its own replace option."
        )
        self.subs_replace_source.toggled.connect(
            self._subs_replace_source_changed
        )
        # v0.4.18 (opt-in suppress_srt, Mode B, D5/D6): strictly opt-in,
        # default OFF — the .srt sidecar side-by-side KEEP disposition is
        # the documented v0.4.15 contract, so absent/deselected means the
        # pre-0.4.18 deliverable set. The checkbox state IS the submitted
        # option (the v0.4.17 rule); QSettings persists it under Mode B's
        # own key ("subs_suppress_srt" — the "subs_" prefix precedent
        # from "subs_replace_source"), isolated from Mode A's
        # "suppress_srt".
        self.subs_suppress_srt = QCheckBox(
            "Suppress .srt sidecar on success"
        )
        self.subs_suppress_srt.setChecked(
            self._settings().value(
                "subs_suppress_srt", False, type=bool
            )
        )
        self.subs_suppress_srt.setToolTip(
            "After the subtitle track is safely muxed into the MKV, delete "
            "the redundant .srt sidecar — the toggleable track inside the "
            "MKV is the deliverable.\n\n"
            "If the mux fails, the .srt is kept as the fallback deliverable "
            "no matter how this option is set. Applies to Add Subtitles "
            "only — the upscale panel has its own sidecar option."
        )
        self.subs_suppress_srt.toggled.connect(
            self._subs_suppress_srt_changed
        )
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.hide()

        self.setCentralWidget(self._build_shell())
        self._apply_style()
        self._configure_accessibility()
        for field in (self.gbph, self.output_dir):
            field.textChanged.connect(self._invalidate_plan)
        self.subtitle_ocr.toggled.connect(self._invalidate_plan)
        self.transcribe_subs.toggled.connect(self._invalidate_plan)
        for widget in (self.replace_source, self.suppress_srt, self.resume_claim):
            widget.toggled.connect(lambda _checked: self._render_detail_for_row())
        for combo in (self.engine_combo, self.profile_combo, self.denoise_combo):
            combo.currentIndexChanged.connect(lambda _index: self._render_detail_for_row())
        self._set_summary("No files planned yet", "Drop videos into the selected workflow or use its Add button.")
        self._render_recent_jobs()
        # v0.4.6 (§4.1): default-on start AFTER the shell exists — the
        # pill is built, so a fallback-port report can reach it.
        self._start_monitor()
        # v0.4.10 (R1): the meter starts after the shell exists too —
        # its first sample lands on a fully built top bar. With
        # nvidia-smi absent it simply renders "GPU n/a" every 0.5 s.
        self.gpu_meter.start()
        # v0.4.16 (model management, ADR-4): the subs-model pill's first
        # truth lands with the window (construction-time disk check).
        self._refresh_subs_model_pill()
        geometry = self._settings().value("ui/window_geometry")
        if isinstance(geometry, (bytes, QByteArray)):
            self.restoreGeometry(geometry)
        screen = self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            self.resize(min(self.width(), area.width()), min(self.height(), area.height()))
            self.move(max(area.left(), min(self.x(), area.right() - self.width() + 1)),
                      max(area.top(), min(self.y(), area.bottom() - self.height() + 1)))
        splitter = self._settings().value("ui/details_splitter")
        if isinstance(splitter, (bytes, QByteArray)):
            self.details_splitter.restoreState(splitter)
        # External serial work can finish without this window owning a run
        # timer. Refresh only staging eligibility, never enqueue automatically.
        self._staged_controls_timer = QTimer(self)
        self._staged_controls_timer.setInterval(1000)
        self._staged_controls_timer.timeout.connect(self._refresh_staged_controls)
        self._staged_controls_timer.start()

    # -- engine persistence (design §7) ---------------------------------

    def _current_plan_inputs(self) -> PlanInputs:
        return PlanInputs(tuple(self.files), positive_gbph(self.gbph.text()),
                          self.output_dir.text().strip(), self.subtitle_ocr.isChecked(),
                          self.transcribe_subs.isChecked())

    def _invalidate_plan(self, *_args) -> None:
        if self._plan_checking:
            self.cancel_checking()
        if self.batch is not None:
            self._plan_dirty = True
            self.start_button.setEnabled(False)
            self._show_message("Settings changed. Plan again before starting an upscale.")

    def request_plan(self) -> None:
        """User-facing Plan action; subprocess probing never runs in the Qt thread."""
        if self._preflights or self.job_ids or self.client.active_job_count() > 0:
            self._show_message("Finish current checking/submitted work before planning again.")
            return
        if not self.files or self._valid_size() is None:
            self._show_message("Add files first and enter a finite positive GB/hour value.")
            return
        try:
            tools = find_tools()
        except MissingToolError as exc:
            self._show_message(f"Cannot check files: {exc}")
            return
        inputs = self._current_plan_inputs()
        self._preflight_generation += 1
        generation = self._preflight_generation
        cancel = threading.Event()
        signals = PreflightSignals()
        signals.progress.connect(self._preflight_progress)
        signals.finished.connect(self._preflight_finished)
        self._preflights[generation] = (inputs, cancel, signals)
        self._plan_checking = True
        self._plan_dirty = True
        self.plan_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.cancel_check_button.setEnabled(True)
        self._show_message("Checking files in the background. No jobs are being submitted.")
        runner = probe_runner(cancel)
        worker = PreflightRunnable(generation, inputs.paths,
            lambda path: probe_and_classify(path, binary=tools.ffprobe, runner=runner), cancel, signals)
        QThreadPool.globalInstance().start(worker)

    def cancel_checking(self) -> None:
        pending = self._preflights.get(self._preflight_generation)
        if pending:
            pending[1].set()
        self._preflight_generation += 1
        self._plan_checking = False
        self._preparation_kind = None
        self.cancel_check_button.setEnabled(False)
        self.plan_button.setEnabled(not self.job_ids)
        self._sync_staged_controls()
        self._show_message("Checking cancelled. Submitted jobs are unchanged; Plan again to review files.")

    def _preflight_progress(self, generation: int, done: int, total: int) -> None:
        if generation == self._preflight_generation and not self._ui_closed:
            self._show_message(f"Checking file {done} of {total}. No execution jobs created." if total else
                               "Scanning folders; total file count is not yet known. No jobs created.")

    def _preflight_finished(self, generation: int, reports, failures) -> None:
        pending = self._preflights.pop(generation, None)
        if pending is None or generation != self._preflight_generation or self._ui_closed:
            if not self._ui_closed:
                self._sync_staged_controls()
            return
        self._plan_checking = False
        self._preparation_kind = None
        self.cancel_check_button.setEnabled(False)
        self.plan_button.setEnabled(not self.job_ids)
        self._sync_staged_controls()
        if isinstance(pending[0], dict):
            self._accept_preparation(pending[0], reports, failures)
            return
        try:
            same_inputs = pending[0] == self._current_plan_inputs()
        except ValueError:
            same_inputs = False
        if not same_inputs or self.job_ids or self.client.active_job_count() > 0:
            self._show_message("Files/settings changed during checking. Plan again before starting.")
            return
        self._accept_plan_reports(reports, failures, pending[0])

    def _request_preparation(self, context: dict, operation) -> bool:
        if self._ui_closed or self._preflights or self._subs_submitting or self._upscale_submitting:
            self._show_message("Another check or submission is still stopping/finishing. Try again when it settles.")
            return False
        self._preflight_generation += 1
        generation = self._preflight_generation
        cancel = threading.Event()
        signals = PreflightSignals()
        signals.progress.connect(self._preflight_progress)
        signals.finished.connect(self._preflight_finished)
        self._preflights[generation] = (context, cancel, signals)
        self._preparation_kind = context["kind"]
        self.cancel_check_button.setEnabled(True)
        self.plan_button.setEnabled(False)
        self._sync_staged_controls()
        self._show_message("Scanning/checking in the background. No execution jobs created.")
        QThreadPool.globalInstance().start(PreparationRunnable(generation, operation, cancel, signals))
        return True

    def _request_ingestion(self, paths: list[str], *, subtitles: bool) -> None:
        if not paths:
            return
        inputs = tuple(paths)
        options = SubtitleOptions(self.subs_replace_source.isChecked(), self.subs_suppress_srt.isChecked(),
                                  self.subs_device_combo.currentData())
        try:
            ffprobe = find_tools().ffprobe if subtitles else ""
        except MissingToolError:
            ffprobe = "ffprobe"

        def operation(cancel, progress):
            progress(0, 0)
            found = _collect_inputs(inputs, cancel)
            if subtitles:
                found = [str(Path(path).expanduser().resolve()) for path in found]
            if len(found) > MAX_FILES:
                raise ValueError(f"That selection holds {len(found)} videos (subfolders included); add at most {MAX_FILES} at once.")
            verdicts = {}
            runner = probe_runner(cancel)
            if subtitles:
                for index, path in enumerate(found):
                    if cancel.is_set():
                        return None
                    verdicts[path] = subs_plan_verdict(path, ffprobe=ffprobe, probe_runner=runner)
                    progress(index + 1, len(found))
            return found, verdicts

        self._request_preparation({"kind": "add-subs" if subtitles else "add-upscale", "options": options}, operation)

    def request_start_subtitles(self) -> None:
        if self._subtitle_start_blocked():
            return
        eligible = [r for r in self._staged_subs if r["verdict"] in
                    (SUBS_VERDICT_READY, SUBS_VERDICT_PROBE_ERROR) and not r.get("uncertain")]
        if not eligible:
            return
        ids = tuple(r["id"] for r in eligible)
        paths = tuple(r["path"] for r in eligible)
        try:
            ffprobe = find_tools().ffprobe
        except MissingToolError:
            ffprobe = "ffprobe"

        def operation(cancel, progress):
            verdicts = {}
            runner = probe_runner(cancel)
            for index, path in enumerate(paths):
                if cancel.is_set():
                    return None
                verdicts[path] = subs_plan_verdict(path, ffprobe=ffprobe, probe_runner=runner)
                progress(index + 1, len(paths))
            return verdicts

        self._request_preparation({"kind": "start-subs", "ids": ids}, operation)

    def _accept_preparation(self, context: dict, result, failures: list[str]) -> None:
        if failures:
            self._show_message("Checking failed. Nothing was submitted/added. Retry after resolving:\n" + "\n".join(failures))
            return
        if result is None:
            return
        if context["kind"] == "start-subs":
            expected = set(context["ids"])
            current = {r["id"] for r in self._staged_subs}
            if not expected <= current:
                self._show_message("Staging changed during checking. Review and Start again.")
                return
            for staged in self._staged_subs:
                if staged["id"] not in expected:
                    continue
                staged["verdict"] = result[staged["path"]]
                for row in range(self.file_table.rowCount()):
                    if self._row_meta(row).get("staged_id") == staged["id"]:
                        self._write_staged_row(row, staged)
                        break
            self._sync_staged_controls()
            self.start_subtitles(checked_verdicts=result, staged_ids=expected)
        elif context["kind"] == "add-subs":
            paths, verdicts = result
            self._enqueue_subs_paths(paths, verdicts=verdicts, options=context["options"])
        else:
            self._add_paths(result[0])

    def _show_message(self, text: str) -> None:
        self._message_text = text
        lines = text.splitlines()
        summary = "\n".join(lines[:3])[:400]
        if summary != text:
            summary += "\nFull message available with Copy message."
        self.message_label.setText(summary)
        self.message_label.setToolTip(text)
        self.message_label.setVisible(bool(text))
        self.copy_message_button.setEnabled(bool(text))

    def _copy_message(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(getattr(self, "_message_text", ""))

    def _valid_size(self) -> float | None:
        try:
            value = positive_gbph(self.gbph.text())
        except ValueError as exc:
            self._show_message(str(exc))
            self.gbph.setFocus()
            self.gbph.setAccessibleDescription(str(exc))
            return None
        self.gbph.setAccessibleDescription("Approximate final size per hour of video.")
        self._show_message("")
        return value

    def _row_meta(self, row: int) -> dict:
        item = self.file_table.item(row, 0)
        return (item.data(Qt.ItemDataRole.UserRole) or {}) if item else {}

    def _tag_row(self, row: int, **data) -> None:
        item = self.file_table.item(row, 0)
        if item is not None:
            meta = self._row_meta(row)
            meta.setdefault("row_id", uuid.uuid4().hex)
            meta.update(data)
            kind = meta.get("kind", "video")
            mode = "Subtitles only" if kind in ("staged-subtitles", "subtitles") else "Upscale"
            self.file_table.setItem(row, 8, QTableWidgetItem(mode))
            for col in range(self.file_table.columnCount()):
                cell = self.file_table.item(row, col)
                if cell is not None:
                    cell.setData(Qt.ItemDataRole.UserRole, meta)
                    cell.setData(Qt.ItemDataRole.AccessibleDescriptionRole, "Mode: " + str(meta.get("kind", "video")))
                    cell.setToolTip(cell.text())

    def _reindex_job_rows(self) -> None:
        tracked = set(self._batch_row_for_job)
        self._batch_row_for_job = {
            meta["job_id"]: row for row in range(self.file_table.rowCount())
            if (meta := self._row_meta(row)).get("job_id") in tracked
        }

    def _remove_display_rows(self, rows: list[int]) -> None:
        # One structural boundary: selection callbacks never see half-updated maps.
        blocked = self.file_table.blockSignals(True)
        try:
            for job, row in self._batch_row_for_job.items():
                self._tag_row(row, job_id=job)
            for row in sorted(set(rows), reverse=True):
                self.file_table.removeRow(row)
            self._reindex_job_rows()
        finally:
            self.file_table.blockSignals(blocked)

    def _append_staged_row(self, staged: dict) -> None:
        row = self.file_table.rowCount()
        self.file_table.insertRow(row)
        self._write_staged_row(row, staged)

    def _write_staged_row(self, row: int, staged: dict) -> None:
        cells = self._subs_file_rows([staged["path"]], {staged["path"]: staged["verdict"]})[0]
        if staged["verdict"] != SUBS_VERDICT_SKIP_SUBTITLES:
            cells[3] = "replace original → .mkv" if staged["replace_source"] else "keep original"
            cells[6] = "Will transcribe own audio if eligible"
        if staged.get("error"):
            cells[0] = "submission unknown" if staged.get("uncertain") else "not submitted"
            cells[7] = staged["error"]
        for col, text in enumerate(cells):
            self.file_table.setItem(row, col, QTableWidgetItem(text))
        self._tag_row(row, kind="staged-subtitles", staged_id=staged["id"], row_id=staged["id"],
                      options=staged["options"])

    def _restore_staged_rows(self) -> None:
        for staged in self._staged_subs:
            self._append_staged_row(staged)
        self._sync_staged_controls()

    def _refresh_staged_controls(self) -> None:
        # A QObject-bound slot disconnects on window destruction. Do not use
        # a closure retaining a deleted window across a queued timer event.
        if self._staged_subs and not self._ui_closed:
            self._sync_staged_controls()

    def _sync_staged_controls(self) -> None:
        reason = self._subtitle_start_reason()
        eligible = any(r["verdict"] in (SUBS_VERDICT_READY, SUBS_VERDICT_PROBE_ERROR)
                       and not r.get("uncertain") for r in self._staged_subs)
        self.start_subtitles_button.setEnabled(eligible and not reason and not self._subs_submitting and not self._preflights)
        self.start_subtitles_button.setToolTip(
            reason or
            "Submit all eligible staged subtitle files in order, not just the selected row."
        )
        self.subs_start_help.setText(
            "Drop subtitle videos here. Options apply to files added next; outputs stay beside each source. " +
            (reason or "Start submits all eligible staged subtitle files in order."))
        self.subs_clear_button.setEnabled(bool(self._staged_subs) and not self._subs_submitting)
        self.subs_remove_button.setEnabled(bool(self._staged_subs) and not self._subs_submitting)

    def _subtitle_start_reason(self) -> str:
        if self._upscale_submitting:
            return "Wait for the upscale submission review to finish. New subtitle files stay staged."
        try:
            live = self.client.active_job_count() > 0 or bool(self.job_ids)
        except Exception:
            return "Cannot check active jobs. Start is disabled until the queue is readable."
        return ("Wait for the active batch to finish or cancellation to settle. New files stay staged."
                if live else "")

    def _subtitle_start_blocked(self) -> bool:
        reason = self._subtitle_start_reason()
        if reason:
            self._sync_staged_controls()
            self._show_message(reason)
        return bool(reason)

    def remove_staged_subtitles(self) -> None:
        if self._subs_submitting:
            return
        rows = [index.row() for index in self.file_table.selectionModel().selectedRows()
                if self._row_meta(index.row()).get("kind") == "staged-subtitles"]
        ids = {self._row_meta(row).get("staged_id") for row in rows}
        self._staged_subs = [r for r in self._staged_subs if r["id"] not in ids]
        self._remove_display_rows(rows)
        self._sync_staged_controls()
        self._show_message(f"Removed {len(rows)} staged subtitle file(s). No media was deleted.")
        self._render_detail_for_row()

    def clear_staged_subtitles(self) -> None:
        if self._subs_submitting:
            return
        if self._preparation_kind in ("add-subs", "start-subs"):
            self.cancel_checking()
        rows = [r for r in range(self.file_table.rowCount())
                if self._row_meta(r).get("kind") == "staged-subtitles"]
        self._staged_subs.clear()
        self._remove_display_rows(rows)
        self._sync_staged_controls()
        self._show_message("Staged subtitles cleared. Submitted jobs and media are unchanged.")
        self._render_detail_for_row()

    def clear_upscale_files(self) -> None:
        """Discard only unsent upscale choices; live rows belong to the executor."""
        if self._plan_checking or self._preparation_kind == "add-upscale":
            self.cancel_checking()
        live_rows = set(self._batch_row_for_job.values())
        removable = [row for row in range(self.file_table.rowCount())
                     if row not in live_rows and self._row_meta(row).get("kind") in
                     ("pending-upscale", "planned-upscale")]
        self.files = []
        self._planned_inputs = None
        self._plan_dirty = True
        if not self.job_ids and self.client.active_job_count() == 0:
            self.batch = None
        self._remove_display_rows(removable)
        self.start_button.setEnabled(False)
        self._show_message("Unsent upscale files cleared. Staged subtitles, submitted jobs and media are unchanged.")
        self._render_detail_for_row()

    def _confirm_options(self, title: str, details: str) -> bool:
        dialog = BatchReviewDialog(title, details, self)
        return dialog.exec() == dialog.DialogCode.Accepted

    def _configure_accessibility(self) -> None:
        names = {
            "gbph": "Upscale output size, GB per hour", "output_dir": "Upscale output folder",
            "engine_combo": "Upscale engine", "profile_combo": "Upscale profile",
            "denoise_combo": "Denoise source before upscaling",
            "subs_device_combo": "Subtitle transcription device requested",
            "file_table": "Video plan, staged subtitles and submitted jobs",
            "details": "Selected video details", "run_log_tail": "Current job log tail",
            "add_subs_button": "Add files for subtitles only",
            "subs_add_folder_button": "Add folder for subtitles only",
        }
        for attr, name in names.items():
            getattr(self, attr).setAccessibleName(name)
        self.stage_bar.bar.setAccessibleName("Current file progress")
        self.overall_bar.bar.setAccessibleName("Queue resolution progress")
        self.progress.setAccessibleName("Batch outcomes: resolved, succeeded, failed and cancelled")
        self.subs_model_button.setAccessibleName("Install subtitle model")
        menu_bar = self.menuBar()
        assert menu_bar is not None
        menu = menu_bar.addMenu("&File")
        assert menu is not None
        self._command_actions = {}
        for title, shortcut, slot, button in (
            ("Add files to upscale", "Ctrl+O", self.add_files, self.upscale_add_button),
            ("Add files for subtitles only", "Ctrl+Shift+O", self.choose_subs_files, self.add_subs_button),
            ("Plan upscale batch", "Ctrl+P", self.request_plan, self.plan_button),
        ):
            action = QAction(title, self)
            action.setShortcut(shortcut)
            action.triggered.connect(slot)
            button.clicked.disconnect(slot)
            button.clicked.connect(action.trigger)
            self._command_actions[button] = action
            button.installEventFilter(self)
            action.changed.connect(lambda button=button, action=action: button.setEnabled(action.isEnabled()))
            action.setEnabled(button.isEnabled())
            menu.addAction(action)
        view = menu_bar.addMenu("&View")
        assert view is not None
        system_colours = QAction("Use &system colours (including contrast themes)", self)
        system_colours.setCheckable(True)
        system_colours.setChecked(self._settings().value("ui/system_palette", False, type=bool))
        system_colours.toggled.connect(self._set_system_colours)
        view.addAction(system_colours)
        remove = QAction("Remove selected staged subtitles", self.file_table)
        remove.setShortcut("Delete")
        remove.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        remove.triggered.connect(self.remove_staged_subtitles)
        self.file_table.addAction(remove)
        for table in (self.file_table, self.history_table):
            copy = QAction("Copy selected table cells", table)
            copy.setShortcut("Ctrl+C")
            copy.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            copy.triggered.connect(lambda _checked=False, table=table: self._copy_table_cells(table))
            table.addAction(copy)
        order = [self.subs_model_button, self.workflow_tabs.tabBar(), self.upscale_add_button, self.upscale_folder_button,
                 self.upscale_clear_button, self.gbph, self.upscale_output_button,
                 self.output_dir, self.engine_combo, self.profile_combo, self.denoise_combo,
                 self.subtitle_ocr, self.transcribe_subs, self.replace_source, self.suppress_srt,
                 self.resume_claim, self.monitor_check, self.plan_button, self.start_button, self.add_subs_button,
                 self.subs_add_folder_button, self.subs_device_combo, self.subs_replace_source,
                 self.subs_suppress_srt, self.subs_remove_button, self.subs_clear_button,
                 self.start_subtitles_button, self.cancel_check_button, self.copy_message_button,
                 self.cancel_button, self.cancel_batch_button, self.log_toggle, self.run_log_tail,
                 self.queue_tabs.tabBar(), self.file_table, self.history_table, self.details,
                 *self.output_action_buttons]
        for before, after in zip(order, order[1:]):
            self.setTabOrder(before, after)

    def eventFilter(self, watched, event):
        actions = getattr(self, "_command_actions", {})
        if watched in actions and event.type() == QEvent.Type.EnabledChange:
            actions[watched].setEnabled(watched.isEnabled())
        return super().eventFilter(watched, event)

    def _submit_gui_job(self, kind: str, path: str, options: dict,
                        engine: str = "auto") -> SubmissionOutcome:
        """One reviewed row against the .19 bridge contract.

        Validation failures are provably pre-mutation (path/batch checks run
        before any registry write), so they map to "rejected" and retry is
        allowed. Anything else maps to "unknown": mark the row uncertain and
        disable retry for that batch rather than risk a duplicate job.
        """
        try:
            ids = (self.client.enqueue_subs([path], **options) if kind == "subs"
                   else self.client.enqueue_run([path], options=options, engine=engine))
        except (ValueError, FileNotFoundError, BatchTooLarge) as exc:
            return SubmissionOutcome("rejected", error=exc)
        except Exception as exc:
            return SubmissionOutcome("unknown", error=exc)
        if len(ids) == 1 and ids[0]:
            return SubmissionOutcome("accepted", tuple(ids))
        return SubmissionOutcome("unknown")

    def _set_system_colours(self, checked: bool) -> None:
        self._settings().setValue("ui/system_palette", checked)
        self._apply_style()


    def _settings(self) -> QSettings:
        # CI uses a distinct organisation so Windows native settings remain
        # isolated too (XDG_CONFIG_HOME only isolates Linux settings).
        return QSettings(os.environ.get("VIDLIFT_SETTINGS_ORG") or "YetiWurks", "VidLift")

    def _pending_submission_ids(self, ids: list[str]) -> list[str]:
        """Do not drop a just-finished receipt before the poll records its outcome."""
        live = set(self._live_window_ids(ids, self.client))
        return [job for job in ids if job in live or
                (job in self._batch_slot_index and
                 self._batch_slot_index[job] < len(self.run_counts) and
                 self.run_counts[self._batch_slot_index[job]] is None)]

    def _load_engine_choice(self) -> str:
        """The persisted engine choice; a stale/unknown value reads as
        ``auto`` — never silently applied (design §7)."""
        value = self._settings().value("engine", "auto")
        if value not in ("auto", "stub", "ncnn"):
            return "auto"
        return str(value)

    def _load_profile_choice(self) -> str:
        """The persisted upscale profile (v0.4.8 D5); a stale/unknown
        value reads as ``live-action`` — never silently applied."""
        value = self._settings().value("upscale_profile", "live-action")
        if value not in ("live-action", "anime"):
            return "live-action"
        return str(value)

    def _load_denoise_mode(self) -> str:
        """The persisted denoise mode (v0.4.15); a stale/unknown value
        reads as ``off`` — never silently applied."""
        value = self._settings().value(
            QSETTINGS_DENOISE_KEYS["mode"], DENOISE_DEFAULT
        )
        if value not in ("off", "light", "strong"):
            return "off"
        return str(value)

    # -- LAN monitor (v0.4.6 design §4) -------------------------------------

    def _monitor_settings(self) -> dict:
        """The persisted monitor settings (QSettings, §4.4); a missing
        value reads as the module default — ON by default at
        0.0.0.0:8765 with the baked token (vidlift.config). A stored
        empty token means local-only open.

        ``token_source`` (visibility ADR-2) names WHERE the effective
        token came from, resolved HERE because ``contains`` is the only
        honest presence test and this is the one method reading
        QSettings: absent → ``default`` (the baked module constant is
        in force — the zero-action pairing fast path); present-and-
        nonempty → ``qsettings`` (an override a watcher must re-pair
        for); present-empty → ``empty-open`` (open mode; the served
        semantics are UNCHANGED — this is a label, a canary for the
        paired watcher, never a behavior switch)."""
        settings = self._settings()
        try:
            port = int(
                settings.value(QSETTINGS_MONITOR_KEYS["port"], MONITOR_PORT_DEFAULT)
            )
        except (TypeError, ValueError):
            port = MONITOR_PORT_DEFAULT
        enabled = settings.value(
            QSETTINGS_MONITOR_KEYS["enabled"], MONITOR_ENABLED_DEFAULT
        )
        if isinstance(enabled, str):
            enabled = enabled.lower() not in ("false", "0", "no", "off", "")
        host = settings.value(QSETTINGS_MONITOR_KEYS["host"], MONITOR_HOST_DEFAULT)
        token = settings.value(QSETTINGS_MONITOR_KEYS["token"], MONITOR_TOKEN_DEFAULT)
        if not settings.contains(QSETTINGS_MONITOR_KEYS["token"]):
            token_source = "default"
        elif str(token or ""):
            token_source = "qsettings"
        else:
            token_source = "empty-open"
        return {
            "enabled": bool(enabled),
            "host": str(host or MONITOR_HOST_DEFAULT),
            "port": port,
            "token": str(token or ""),
            "token_source": token_source,
        }

    def _write_monitor_discovery(
        self, *, enabled: bool, port: int | None, token: str, token_source: str
    ) -> None:
        """ADR-2 discovery card: ``<state_dir>/monitor.json`` (atomic
        temp+replace, pid-unique staging; owner-only 0o600 — the token
        is a secret, D-5/D-6). Carries the EFFECTIVE port/token the monitor
        serves so a paired watcher (Hermes) learns the CURRENT pair
        without reading QSettings — the port-fallback walk makes the
        real port unknowable a priori, and a QSettings token override
        is invisible to the network. The token is a SECRET: never
        logged, never in window chrome, never committed — the card
        lives in the user-profile state dir (same trust zone as the
        QSettings store). Best-effort by design (the BridgeLog swallow
        discipline): a failed card write must never take the monitor
        down — the truth surface (/status) is unaffected.

        The card root is the window's OWN client registry root —
        production resolves identically to
        ``client_local.default_state_dir()`` (both honor
        ``$VIDLIFT_STATE_DIR`` first: ``build_services`` resolves None
        through ``benchmark.state_dir()``, and run_gui.py exports the
        env var before construction when frozen), and ADR-2's
        provisioning artifact then sits beside the registry the
        monitor actually reads — by identity, not by coincidence.
        Bridge-less harness instances (suite fakes with no services)
        write nothing: there is no registry root to provision beside,
        and the suite must never touch real user state."""
        services = getattr(self.client, "services", None)
        if services is None:
            return
        import json
        import os

        card = {
            "schema": MONITOR_DISCOVERY_SCHEMA,
            "enabled": enabled,
            "bind_host": str(self._monitor_settings()["host"]),
            "port": port,
            "lan_base_url": f"http://{lan_ip()}:{port}" if port is not None else None,
            "token": token,
            "token_source": token_source,
            "version": __version__,
            "written_at": time.time(),
        }
        path = Path(services.state_dir) / MONITOR_DISCOVERY_FILENAME
        # v0.4.16 (D-6, Karen remediation): the staging name is
        # pid-unique — the registry explicitly supports multi-writer
        # convergence, so two GUI instances sharing one state dir must
        # never interleave into one fixed `monitor.json.tmp`; os.replace
        # moves ONLY this writer's own completed card (the old fixed
        # name let writer A's replace publish writer B's mid-write file).
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            # v0.4.16 (D-5, Karen remediation): the card carries the
            # EFFECTIVE monitor token — a secret — so it is created
            # owner-only: 0o600 at file creation (umask can only
            # tighten further) and 0o700 when WE are the ones creating
            # the state dir. The old 0o664 write made the token
            # group/world-readable on shared machines; os.replace then
            # carries the tightened mode onto the final artifact.
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(
                str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
            )
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(json.dumps(card, indent=2))
            os.replace(tmp, path)
        except OSError:
            # A provisioning artifact is the one thing here allowed to
            # fail silently-ish: no monitor behavior depends on it.
            pass

    def _start_monitor(self) -> None:
        """Construction-time start (default-on, §4.1): a daemon thread
        through start_server_with_fallback; a fallback port is
        persisted so the next launch is clean. Startup failure is
        NON-FATAL — the pill names the problem and the app continues."""
        monitor = self._monitor_settings()
        was_blocked = self.monitor_check.blockSignals(True)
        self.monitor_check.setChecked(bool(monitor["enabled"]))
        self.monitor_check.blockSignals(was_blocked)
        if not monitor["enabled"]:
            # ADR-2: the serving state resolved to OFF — the card
            # records it so a paired watcher never pairs from a stale
            # "on" card.
            self._write_monitor_discovery(
                enabled=False,
                port=None,
                token=monitor["token"],
                token_source=monitor["token_source"],
            )
            self._refresh_monitor_pill()
            return
        import os

        if os.environ.get("VIDLIFT_MONITOR_AUTOSTART", "") in (
            "0",
            "false",
            "off",
        ):
            # Explicit opt-out (test/CI runs, headless containers):
            # the setting stays ON; only THIS launch skips the bind.
            self._write_monitor_discovery(
                enabled=False,
                port=None,
                token=monitor["token"],
                token_source=monitor["token_source"],
            )
            self._refresh_monitor_pill()
            return
        try:
            _server, _base, actual_port = start_server_with_fallback(
                client=self.client,
                host=monitor["host"],
                port=monitor["port"],
                token=monitor["token"],
            )
        except OSError as exc:
            self._write_monitor_discovery(
                enabled=False,
                port=None,
                token=monitor["token"],
                token_source=monitor["token_source"],
            )
            self.monitor_pill.setText(f"Monitor off (port busy: {exc})")
            return
        if actual_port != monitor["port"]:
            self._settings().setValue(QSETTINGS_MONITOR_KEYS["port"], actual_port)
        self.monitor_port_reported = actual_port
        # ADR-2: the card carries the ACTUAL port (fallback-shifted
        # bindings are exactly the case a watcher cannot learn
        # otherwise) and the effective token, rewritten on every
        # resolution.
        self._write_monitor_discovery(
            enabled=True,
            port=actual_port,
            token=monitor["token"],
            token_source=monitor["token_source"],
        )
        self._refresh_monitor_pill()

    def _toggle_monitor(self, checked: bool) -> None:
        """The checkbox handler (sibling _toggle_monitor, §4.2): on →
        start_server_with_fallback; off → stop_server; either way
        persist the setting and refresh the pill."""
        self._settings().setValue(QSETTINGS_MONITOR_KEYS["enabled"], bool(checked))
        try:
            if checked:
                monitor = self._monitor_settings()
                _server, _base, actual_port = start_server_with_fallback(
                    client=self.client,
                    host=monitor["host"],
                    port=monitor["port"],
                    token=monitor["token"],
                )
                if actual_port != monitor["port"]:
                    self._settings().setValue(
                        QSETTINGS_MONITOR_KEYS["port"], actual_port
                    )
                self.monitor_port_reported = actual_port
                # ADR-2: the card carries the ACTUAL bound port and the
                # effective token, rewritten at every resolution.
                self._write_monitor_discovery(
                    enabled=True,
                    port=actual_port,
                    token=monitor["token"],
                    token_source=monitor["token_source"],
                )
            else:
                stop_server()
                self.monitor_port_reported = None
                resolved = self._monitor_settings()
                self._write_monitor_discovery(
                    enabled=False,
                    port=None,
                    token=resolved["token"],
                    token_source=resolved["token_source"],
                )
        except OSError as exc:
            self._settings().setValue(QSETTINGS_MONITOR_KEYS["enabled"], False)
            self.monitor_port_reported = None
            resolved = self._monitor_settings()
            self._write_monitor_discovery(
                enabled=False,
                port=None,
                token=resolved["token"],
                token_source=resolved["token_source"],
            )
            self.monitor_pill.setText(f"Monitor off ({exc})")
            return
        self._refresh_monitor_pill()

    def _refresh_monitor_pill(self) -> None:
        """The pill (sibling _refresh_monitor_pill, §4.3): on with the
        best-effort LAN IP when serving; off otherwise. State comes
        from the GUI's own start/stop bookkeeping — never a stale
        from-import of the monitor's module global (from-imports bind
        values, not live names). The token is a secret-ish value and
        stays OUT of window chrome (§4.4)."""
        if self.monitor_port_reported is not None:
            self.monitor_pill.setText(f"Monitor on: {lan_ip()}:{self.monitor_port_reported}")
        else:
            self.monitor_pill.setText("Monitor off")

    # -- v0.4.16 subs-model pill + install (model management, ADR-4) ----

    def _refresh_subs_model_pill(self, live_dl: dict | None = None) -> None:
        """ADR-1/4: the disk predicate, re-read on every call (1 Hz while a
        slate lives; on construction; after an install settles). The pill
        NAMES its tag — a config-pinned different model is the job's own pin,
        never this indicator's lie.

        ``live_dl`` rides the tick path (_consume_running_snapshot): the
        polled snapshot's ``progress["model_download"]`` so the pill counts
        bytes during IN-RUN downloads too (the board covers idle installs;
        in-run truth arrives via the existing 1 Hz poll — no second timer).
        """
        services = getattr(self.client, "services", None)
        board = getattr(services, "model_install_board", None)
        block = model_install_state(
            DEFAULT_MODEL,
            state_root=default_state_dir(),
            board=board,
            progress_download=live_dl if isinstance(live_dl, dict) else None,
        )
        state = block["state"]
        tag = block["tag"]
        if state == "installed":
            self.subs_model_pill.setText(f"Subs model ({tag}): installed")
            self.subs_model_button.setVisible(False)
        elif state == "downloading":
            done = block["bytes_done"]
            total = block["bytes_total"]
            if total is not None and total > 0 and isinstance(done, (int, float)) and done >= 0:
                pct = min(100.0, done * 100.0 / total)
                self.subs_model_pill.setText(
                    f"Subs model ({tag}): downloading — "
                    f"{format_bytes(int(done))} / {format_bytes(int(total))} ({pct:.0f}%)"
                )
            elif isinstance(done, (int, float)) and done >= 0:
                self.subs_model_pill.setText(
                    f"Subs model ({tag}): downloading — "
                    f"{format_bytes(int(done))} (total size unknown)"
                )
            else:
                self.subs_model_pill.setText(
                    f"Subs model ({tag}): downloading…"
                )
            self.subs_model_button.setVisible(True)
            self.subs_model_button.setEnabled(False)
            self.subs_model_button.setText("Downloading…")
        elif state == "unknown":
            self.subs_model_pill.setText(
                f"Subs model ({tag}): state unknown (cache unreadable)"
            )
            self.subs_model_button.setVisible(True)
            self.subs_model_button.setEnabled(True)
            self.subs_model_button.setText("Install subtitle model")
        else:  # not-installed — the install affordance is the point
            self.subs_model_pill.setText(
                f"Subs model ({tag}): not installed — first subtitles "
                "use downloads ~1.5 GB"
            )
            self.subs_model_button.setVisible(True)
            self.subs_model_button.setEnabled(True)
            self.subs_model_button.setText("Install subtitle model")

    def _install_subs_model(self) -> None:
        """ADR-4: GUI-owned install on a QThreadPool runnable (the GPU-meter
        pattern — the GUI's only background idiom), NEVER the serial
        JobExecutor (a 1.5 GB fetch must not starve the queue) and NEVER any
        remote surface (N4). resolve_model_dir is the whole implementation:
        sha256-pinned, lock-serialized, .part-resuming. In-flight click guard
        via self._subs_model_installing."""
        if getattr(self, "_subs_model_installing", False):
            return
        services = getattr(self.client, "services", None)
        board = getattr(services, "model_install_board", None)
        if board is None:
            # No board to report into (foreign/no services): the install
            # would still work, but nothing could observe it — refuse
            # honestly instead of silently running an invisible download.
            QMessageBox.warning(
                self,
                "Transcription model",
                "Install is unavailable: no services object carries the "
                "install board.",
            )
            return
        self._subs_model_installing = True
        board.begin(DEFAULT_MODEL)
        signals = ModelInstallSignals()
        # v0.4.16 (lifetime): the shim must outlive the install — the
        # window holds the reference until the settle slot runs (a
        # QThreadPool runnable may emit long after this method's local
        # frame is gone; an unparented shim would otherwise be
        # collected mid-install and its emits would raise).
        self._subs_model_signals = signals
        signals.progress.connect(self._on_subs_model_progress)
        signals.finished.connect(self._on_subs_model_finished)
        runnable = ModelInstallRunnable(
            DEFAULT_MODEL,
            state_dir=default_state_dir(),
            board=board,
            signals=signals,
            fetch=None,  # tests inject a fake here via the constructor
        )
        QThreadPool.globalInstance().start(runnable)
        self._refresh_subs_model_pill()

    def _on_subs_model_progress(self, info: dict) -> None:
        self._refresh_subs_model_pill()  # the board now carries the truth; one render path

    def _on_subs_model_finished(self, ok: bool, message: str) -> None:
        self._subs_model_installing = False
        self._refresh_subs_model_pill()
        # the shim's lifetime anchor (see _install_subs_model) is only
        # cleared when the install settled
        self._subs_model_signals = None
        if not ok:
            QMessageBox.warning(
                self,
                "Transcription model install failed",
                f"{message}\n\nThe model was NOT installed. Check the network "
                "connection and try again — the button stays available.",
            )

    def _replace_source_changed(self, checked: bool) -> None:
        """Persist the replace-source preference (QSettings, house
        rule). The checkbox itself warns the user in its label; only
        the stored value changes here."""
        self._settings().setValue("replace_source", bool(checked))

    def _subs_replace_source_changed(self, checked: bool) -> None:
        """v0.4.17: persist the Mode B replace-source preference
        (QSettings house rule, verbatim _replace_source_changed
        pattern); only the stored value changes here. The key
        ("subs_replace_source") is Mode B's own — Mode A's
        ("replace_source") key is never touched by it."""
        self._settings().setValue("subs_replace_source", bool(checked))

    def _subs_suppress_srt_changed(self, checked: bool) -> None:
        """v0.4.18: persist the Mode B suppress-sidecar preference
        (QSettings house rule, verbatim _subs_replace_source_changed
        pattern). "subs_suppress_srt" is Mode B's own key — Mode A's
        ("suppress_srt") is never touched by it."""
        self._settings().setValue("subs_suppress_srt", bool(checked))

    def _suppress_srt_changed(self, checked: bool) -> None:
        """v0.4.18: persist the Mode A suppress-sidecar preference
        ("suppress_srt"); Mode B's "subs_suppress_srt" is never
        touched."""
        self._settings().setValue("suppress_srt", bool(checked))

    def _resume_claim_changed(self, checked: bool) -> None:
        """Persist the resume-claim preference (v0.4.9 §1.4, QSettings
        house rule); only the stored value changes."""
        self._settings().setValue("resume_claim", bool(checked))

    def _transcribe_subs_changed(self, checked: bool) -> None:
        """Persist the Mode A preference (v0.4.14, QSettings house
        rule); only the stored value changes."""
        self._settings().setValue("transcribe_subs", bool(checked))

    def _denoise_changed(self, index: int) -> None:
        """Persist the denoise-mode choice (v0.4.15, QSettings house
        rule); only the stored value and the live choice change."""
        data = self.denoise_combo.itemData(index)
        if data in ("off", "light", "strong"):
            self.denoise_mode = data
            self._settings().setValue(QSETTINGS_DENOISE_KEYS["mode"], data)

    def _engine_changed(self, index: int) -> None:
        data = self.engine_combo.itemData(index)
        if data in ("auto", "stub", "ncnn"):
            self.engine_choice = data
            self._settings().setValue("engine", data)

    def _profile_changed(self, index: int) -> None:
        """Persist the upscale-profile choice (v0.4.8 D5, QSettings
        house rule); only the stored value and the live choice change."""
        data = self.profile_combo.itemData(index)
        if data in ("live-action", "anime"):
            self.profile_choice = data
            self._settings().setValue("upscale_profile", data)

    # -- layout -----------------------------------------------------------

    def _build_shell(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("VidLift")
        self.title_label = title
        title.setObjectName("Title")
        title_stack = QVBoxLayout()
        title_stack.addWidget(title)
        title_stack.addWidget(self._version_pill(), alignment=Qt.AlignmentFlag.AlignLeft)
        title_stack.addWidget(self.monitor_pill, alignment=Qt.AlignmentFlag.AlignLeft)
        # v0.4.16 (model management, ADR-4): the subs-model pill and its
        # Install button share one inline row (spacing 6, no margins —
        # it reads as one control cluster) directly under the monitor
        # pill; the row rides the existing left-aligned title stack.
        subs_model_row = QWidget()
        subs_model_row_layout = QHBoxLayout(subs_model_row)
        subs_model_row_layout.setContentsMargins(0, 0, 0, 0)
        subs_model_row_layout.setSpacing(6)
        subs_model_row_layout.addWidget(self.subs_model_pill, 1)
        subs_model_row_layout.addWidget(self.subs_model_button)
        title_stack.addWidget(subs_model_row)
        title_stack.addWidget(QLabel("Upscale video or add subtitles"))
        self.subs_model_pill.setWordWrap(True)
        self.subs_model_pill.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # Give wrapped status text real width instead of an empty header
        # stretch squeezing it into a tall column above the workflow tabs.
        title_row.addLayout(title_stack, 3)
        self.summary_count = QLabel()
        self.summary_count.setObjectName("SummaryCount")
        self.summary_note = QLabel()
        self.summary_note.setObjectName("SummaryNote")
        self.summary_note.setWordWrap(True)
        self.summary_count.setWordWrap(True)
        self.total_pill = QLabel()
        self.total_pill.setObjectName("TotalPill")
        summary = QVBoxLayout()
        summary.addWidget(self.total_pill, alignment=Qt.AlignmentFlag.AlignRight)
        summary.addWidget(self.summary_count, alignment=Qt.AlignmentFlag.AlignRight)
        summary.addWidget(self.summary_note, alignment=Qt.AlignmentFlag.AlignRight)
        title_row.addLayout(summary, 2)
        # v0.4.10 (R1): the GPU meter sits at the top bar's far right,
        # vertically centered against the stacked title/pill column.
        # Fixed size → the bar's geometry is decided once; samples
        # repaint the meter only.
        title_row.addWidget(
            self.gpu_meter, 0, Qt.AlignmentFlag.AlignVCenter
        )
        layout.addLayout(title_row)

        helper = QLabel(PLAN_HELPER_TEXT)
        helper.setObjectName("Muted")
        helper.setWordWrap(True)

        # ---- v0.4.17 workstream-2 (Hermes findings 1/2, Karen K-1/K-2):
        # the settings surface splits into TWO titled groups so each mode
        # shows only its own controls. The separation is LAYOUT-STRUCTURAL —
        # two independent grids, two independent column budgets — so no
        # Mode B control can ever inflate the upscale grid's width again
        # (K-1: the 352px checkbox at shared-grid (2,8) pushed the window
        # minimum width 1290 -> 1532).
        controls_upscale = QGroupBox("Upscale")
        controls_upscale.setObjectName("Controls")
        grid = QGridLayout(controls_upscale)
        grid.setContentsMargins(18, 8, 18, 16)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        add = QPushButton("Add Files")
        self.upscale_add_button = add
        add.setAccessibleName("Add files to upscale")
        add.setToolTip("Choose one or more video files")
        add.clicked.connect(self.add_files)
        folder = QPushButton("Add Folder")
        self.upscale_folder_button = folder
        folder.setAccessibleName("Add folder to upscale")
        # v0.4.18 (WS1): the tooltip advertises recursion truthfully
        # (S-3) — the button's reach grew from the folder's top level to
        # the whole tree.
        folder.setToolTip(
            "Add recognized videos from a folder, including every "
            "subfolder (recursion is always on — a whole show at once)."
        )
        folder.clicked.connect(self.add_folder)
        clear = QPushButton("Clear unsent upscale")
        self.upscale_clear_button = clear
        clear.clicked.connect(self.clear_upscale_files)
        browse_output = QPushButton("Output Folder")
        self.upscale_output_button = browse_output
        browse_output.clicked.connect(self.choose_output_dir)
        # Canonical verb order left-to-right: Add → Plan → Start (§12).
        grid.addWidget(add, 0, 0)
        grid.addWidget(folder, 0, 1)
        grid.addWidget(clear, 0, 2)
        grid.addWidget(QLabel("GB/hour"), 0, 3)
        grid.addWidget(self.gbph, 0, 4)
        grid.addWidget(browse_output, 0, 5)
        grid.addWidget(self.output_dir, 0, 6)
        grid.addWidget(self.plan_button, 0, 7)
        grid.addWidget(self.start_button, 0, 8)
        grid.addWidget(QLabel("Upscale engine"), 1, 0)
        grid.addWidget(self.engine_combo, 1, 1)
        grid.addWidget(self.subtitle_ocr, 1, 2, 1, 3)
        # Karen r2 D2: span 2 columns, not 3 -- a 3-column span reached
        # column 7, where cancel_button sits; the checkbox label clipped
        # under the Cancel button.
        grid.addWidget(self.replace_source, 1, 5, 1, 2)
        grid.addWidget(self.cancel_button, 1, 7)
        # v0.4.13 (design §4.1): Cancel Batch lands immediately right of
        # Cancel Current.
        grid.addWidget(self.cancel_batch_button, 1, 8)
        # v0.4.17 workstream-2: row 2 de-overlapped. The monitor span
        # narrows 5 -> 4 (its 419px hint still fits the 477px four-column
        # budget), and resume_claim moves from the invisible overlap at
        # (2,1,1,2) — painted over by the later-created monitor (Qt stacks
        # by CREATION order; measured 0/7314 painted pixels, the Finding-1
        # twin) — to the row's free tail the Mode B controls vacated.
        grid.addWidget(self.monitor_check, 2, 0, 1, 4)
        self.monitor_check.toggled.connect(self._toggle_monitor)
        # v0.4.8 (design D5): the profile combo rides row 2.
        grid.addWidget(QLabel("Upscale profile"), 2, 5)
        grid.addWidget(self.profile_combo, 2, 6)
        grid.addWidget(self.resume_claim, 2, 7, 1, 2)
        # v0.4.15 (opt-in denoise) row 3 — the Mode A processing-options
        # row. v0.4.17 (Hermes Finding 1): the transcription toggle is
        # RENDERED for the first time since v0.4.14 (created at :643 but
        # never added to a layout); the (3,3,1,3) span's 271px column
        # budget takes its 184px label at zero width cost.
        grid.addWidget(QLabel(DENOISE_COMBO_LABEL), 3, 1)
        grid.addWidget(self.denoise_combo, 3, 2)
        grid.addWidget(self.transcribe_subs, 3, 3, 1, 3)
        # v0.4.18 (opt-in suppress_srt, row 5-E4): row 3 IS the Mode A
        # subtitle-carry-policy row. The (3, 6, 1, 3) span is verified
        # free (transcribe_subs spans (3,3,1,3); column 6 is the stretch
        # column via setColumnStretch(6, 1) below) and shares the
        # existing stretch, so the checkbox's label adds no new
        # minimum-width contribution (T5's width gate re-proves it).
        grid.addWidget(self.suppress_srt, 3, 6, 1, 3)
        grid.setColumnStretch(6, 1)
        layout.addWidget(controls_upscale)

        # ---- Mode B surface (v0.4.17 workstream-2): its own group, its
        # own column budget, only its own controls (Hermes Finding 2). The
        # explainer states SCOPE only; the naming rule, keep-both promise,
        # and replace semantics live in the two tooltips (one derivation,
        # no drift surface).
        controls_subs = SubtitleDropGroup("Add subtitles (no upscale)")
        self.controls_subs = controls_subs
        controls_subs.pathsDropped.connect(self._stage_subtitle_drop)
        controls_subs.setObjectName("Controls")
        subs_grid = QGridLayout(controls_subs)
        subs_grid.setContentsMargins(18, 8, 18, 12)
        subs_grid.setHorizontalSpacing(12)
        subs_grid.setVerticalSpacing(10)
        # v0.4.18 (WS1/WS2 re-tile, row 4-A9): the picker PAIR first —
        # "Add Subtitles…" (0,0) + "Add Folder" (0,1), mirroring the
        # upscale group's Add Files → Add Folder order — then the
        # options pair ADJACENT: subs_replace_source (0,2, moved from
        # (0,1)) and subs_suppress_srt (0,3, both are the run-shaping
        # checkboxes); the scope explainer keeps the trailing stretch
        # (col 4, moved from (0,2)).
        subs_grid.addWidget(self.add_subs_button, 0, 0)
        subs_grid.addWidget(self.subs_add_folder_button, 0, 1)
        subs_grid.addWidget(self.start_subtitles_button, 0, 2)
        subs_grid.addWidget(self.subs_replace_source, 0, 3)
        subs_grid.addWidget(self.subs_suppress_srt, 0, 5)
        explainer = QLabel(
            "Adds subtitles from the file's own audio — video streams "
            "are copied (no upscale, no re-encode)."
        )
        explainer.setObjectName("Muted")
        explainer.setWordWrap(True)
        subs_grid.addWidget(explainer, 0, 4)
        subs_grid.setColumnStretch(4, 1)
        layout.addWidget(controls_subs)
        # Reflow the settings into two independent, font-driven columns.
        # No shared nine-column budget, and no hidden/overlapping controls.
        for old_grid in (grid, subs_grid):
            while old_grid.count():
                child = old_grid.takeAt(0).widget()
                if isinstance(child, QLabel) and child is not explainer:
                    child.hide()
                    child.deleteLater()
            for column in range(9):
                old_grid.setColumnStretch(column, 0)

        def pair(*widgets):
            row_layout = QHBoxLayout()
            for widget in widgets:
                row_layout.addWidget(widget)
            return row_layout

        def field(label_text, widget):
            label = QLabel(label_text)
            label.setBuddy(widget)
            return pair(label, widget)

        grid.addLayout(pair(add, folder, clear), 0, 0)
        grid.addLayout(pair(self.plan_button, self.start_button), 0, 1)
        grid.addLayout(field("&GB/hour", self.gbph), 1, 0)
        grid.addLayout(pair(browse_output, self.output_dir), 1, 1)
        grid.addLayout(field("Upscale &engine", self.engine_combo), 2, 0)
        grid.addLayout(field("Upscale &profile", self.profile_combo), 2, 1)
        grid.addLayout(field(DENOISE_COMBO_LABEL, self.denoise_combo), 3, 0)
        grid.addWidget(self.resume_claim, 3, 1)
        grid.addWidget(self.subtitle_ocr, 4, 0)
        grid.addWidget(self.transcribe_subs, 4, 1)
        grid.addWidget(self.replace_source, 5, 0)
        grid.addWidget(self.suppress_srt, 5, 1)
        for combo in (self.engine_combo, self.profile_combo, self.denoise_combo):
            combo.setMinimumContentsLength(12)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.gbph.setFixedWidth(90)
        subs_grid.addLayout(pair(self.add_subs_button, self.subs_add_folder_button), 0, 0)
        subs_grid.addWidget(self.start_subtitles_button, 0, 1)
        subs_grid.addLayout(field("Transcription &device", self.subs_device_combo), 1, 0)
        subs_grid.addLayout(pair(self.subs_remove_button, self.subs_clear_button), 1, 1)
        subs_grid.addWidget(self.subs_replace_source, 2, 0)
        subs_grid.addWidget(self.subs_suppress_srt, 2, 1)
        subs_grid.addWidget(explainer, 3, 0, 1, 2)
        subs_help = QLabel("Drop subtitle videos here. Options apply to files added next; "
                           "Start submits all eligible staged files. Outputs stay beside each source.")
        subs_help.setWordWrap(True)
        self.subs_start_help = subs_help
        subs_grid.addWidget(subs_help, 4, 0, 1, 2)
        for mode_grid in (grid, subs_grid):
            mode_grid.setColumnStretch(0, 1)
            mode_grid.setColumnStretch(1, 1)
        layout.removeWidget(controls_upscale)
        layout.removeWidget(controls_subs)
        # Keep the same settings widgets alive on independent workflow pages.
        # Tab selection is presentation only: no planning, staging or submission
        # signals are connected to currentChanged. Queue/results stay outside.
        self.workflow_tabs = QTabWidget()
        self.workflow_tabs.setObjectName("WorkflowTabs")
        self.workflow_tabs.setAccessibleName("Video workflow")
        workflow_bar = self.workflow_tabs.tabBar()
        assert workflow_bar is not None
        workflow_bar.setObjectName("WorkflowTabBar")
        workflow_bar.setAccessibleName("Choose AI Upscale or Subtitles Only")
        self.workflow_tabs.setMinimumHeight(150)
        self.workflow_tabs.setMaximumHeight(330)
        self.setup_scroll = QScrollArea()
        self.subs_setup_scroll = QScrollArea()
        for scroll, name, controls, help_label in (
            (self.setup_scroll, "AI Upscale options", controls_upscale, helper),
            (self.subs_setup_scroll, "Subtitles Only options", controls_subs, None),
        ):
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            if help_label is not None:
                page_layout.addWidget(help_label)
            page_layout.addWidget(controls)
            page_layout.addStretch(1)
            scroll.setAccessibleName(name)
            scroll.setWidgetResizable(True)
            scroll.setWidget(page)
        self.workflow_tabs.addTab(self.setup_scroll, "AI Upscale")
        self.workflow_tabs.addTab(self.subs_setup_scroll, "Subtitles Only")
        layout.addWidget(self.workflow_tabs, 2)
        self.message_label = QLabel("")
        self.message_label.setWordWrap(True)
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.message_label.setAccessibleName("Action guidance or error")
        self.message_label.hide()
        layout.addWidget(self.message_label)
        self.cancel_check_button = QPushButton("Cancel checking (not jobs)")
        self.cancel_check_button.setEnabled(False)
        self.cancel_check_button.clicked.connect(self.cancel_checking)
        self.copy_message_button = QPushButton("Copy message")
        self.copy_message_button.setEnabled(False)
        self.copy_message_button.clicked.connect(self._copy_message)
        guidance_actions = QHBoxLayout()
        guidance_actions.addWidget(self.cancel_check_button)
        guidance_actions.addWidget(self.copy_message_button)
        guidance_actions.addStretch(1)
        guidance_actions.addWidget(self.monitor_check)
        layout.addLayout(guidance_actions)
        layout.addWidget(self.progress)

        # ---- run panel (hidden until the first Start; design §4.2) ----
        self.run_panel = QFrame()
        self.run_panel.setObjectName("RunPanel")
        run_layout = QGridLayout(self.run_panel)
        run_layout.setContentsMargins(18, 14, 18, 14)
        run_layout.setHorizontalSpacing(14)
        run_layout.setVerticalSpacing(8)
        self.stage_label = QLabel("")
        self.stage_label.setObjectName("StageLabel")
        self.stage_label.setWordWrap(True)
        self.activity_dots = ActivityDots()
        self.time_elapsed = QLabel("")
        self.time_elapsed.setObjectName("Muted")
        self.total_remaining = QLabel("")
        self.total_remaining.setObjectName("Muted")
        self.run_log_tail = QPlainTextEdit()
        self.run_log_tail.setReadOnly(True)
        self.run_log_tail.setMaximumHeight(140)
        self.run_log_tail.setPlaceholderText("Runner log tail appears here while a job runs.")
        self.stage_bar = EtaProgressBar()
        self.overall_bar = EtaProgressBar()
        run_layout.addWidget(self.stage_label, 0, 0, 1, 3)
        liveness = QHBoxLayout()
        liveness.addWidget(self.activity_dots)
        liveness.addWidget(self.time_elapsed)
        liveness.addWidget(self.total_remaining, 1)
        run_layout.addLayout(liveness, 1, 0, 1, 3)
        run_layout.addWidget(QLabel("Current file"), 2, 0)
        run_layout.addWidget(self.stage_bar, 2, 1)
        run_layout.addWidget(QLabel("Overall queue"), 3, 0)
        run_layout.addWidget(self.overall_bar, 3, 1)
        self.log_toggle = QCheckBox("Show runner log tail")
        self.log_toggle.toggled.connect(self.run_log_tail.setVisible)
        run_layout.addWidget(self.log_toggle, 4, 0, 1, 3)
        run_layout.addWidget(self.run_log_tail, 5, 0, 1, 3)
        self.run_log_tail.hide()
        run_layout.addWidget(self.cancel_button, 0, 3)
        run_layout.addWidget(self.cancel_batch_button, 1, 3)
        # v0.4.5 (§2, D-tail): the scroll hooks. valueChanged tracks the
        # user-scroll override; rangeChanged keeps the cached maximum
        # fresh (setPlainText rebuilds the document, so the range
        # changes on every re-render). Qt ordering: on setPlainText the
        # document is REPLACED — rangeChanged fires before any settling
        # valueChanged, so "was at bottom" is read BEFORE the swap.
        scrollbar = self.run_log_tail.verticalScrollBar()
        scrollbar.valueChanged.connect(self._log_scroll_value_changed)
        scrollbar.rangeChanged.connect(self._log_scroll_range_changed)
        self.run_panel.hide()
        layout.addWidget(self.run_panel)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.details_splitter = splitter
        self.queue_tabs = QTabWidget()
        self.queue_tabs.addTab(self.file_table, "Current batch")
        self.history_table = QTableWidget(0, 3)
        self.history_table.setHorizontalHeaderLabels(["State", "File", "Error"])
        self.history_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_table.setAccessibleName("Recent jobs, read only")
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.history_table.itemSelectionChanged.connect(self._select_history)
        self.queue_tabs.addTab(self.history_table, "Recent jobs (read only)")
        self.queue_tabs.currentChanged.connect(self._queue_tab_changed)
        splitter.addWidget(self.queue_tabs)
        splitter.addWidget(self.details)
        splitter.setSizes([760, 360])
        layout.addWidget(splitter, 1)
        output_actions = QGridLayout()
        self.output_action_buttons = []
        for index, (title, callback) in enumerate((("Copy details", lambda: QApplication.clipboard().setText(self.details.toPlainText())),
                                ("Copy source path", self._copy_source_path),
                                ("Copy output path", self._copy_output_path),
                                ("Reveal output", lambda: self._open_recorded_path("output_path")),
                                ("Open manifest", lambda: self._open_recorded_path("manifest_path")),
                                ("Open log", lambda: self._open_recorded_path("log_path")))):
            button = QPushButton(title)
            button.clicked.connect(callback)
            self.output_action_buttons.append(button)
            output_actions.addWidget(button, index // 3, index % 3)
        layout.addLayout(output_actions)
        return root

    def _version_pill(self) -> QLabel:
        """The second, persistent version surface (design §1.6/§4.2): a
        header pill beside the title, in addition to the title text."""
        pill = QLabel(f"v{__version__}")
        pill.setObjectName("VersionBadge")
        return pill

    def _apply_style(self) -> None:
        self._system_colours = self._settings().value("ui/system_palette", False, type=bool)
        self._progress_chunk_color = None
        if self._system_colours:
            self.setStyleSheet("")
            self.progress.setStyleSheet("")
            return
        # v0.4.18 (WS4, D12): the QGroupBox title floats visibly off its
        # border line. MECHANICS: with `subcontrol-origin: margin` +
        # `subcontrol-position: top left` the title anchors to the TOP
        # of the margin box while the border sits at the margin's inner
        # edge — so every pixel of added margin-top moves the border
        # DOWN relative to the fixed-anchored title (the title floats up
        # off the line by exactly that delta). REJECTED: the negative
        # `top:` on ::title — paints above the widget's own rect; QStyle
        # may clip that platform-dependently, and it risks the row
        # above. TUNING (Windows Segoe UI build only): acceptance band
        # is 4-7 px of title-bottom-to-border clearance at 100 % scale;
        # if outside the band adjust ONLY the margin-top below in ±2 px
        # steps and re-verify BOTH groups. Linux offscreen cannot
        # reproduce the metric; the committed value is 20 px.
        # Styling the indicator replaces native painting, including the tick.
        # Ship that mark explicitly; native unchecked outlines can disappear
        # against our dark palette. System colours retain native rendering.
        checkbox_tick = (
            Path(__file__).resolve().parent / "ui" / "assets" / "checkbox-checked.png"
        ).as_posix()
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #101214; color: #edf0ec; }
            QLabel { color: #d9ded7; }
            #Title { font-weight: 700; letter-spacing: 0; }
            #VersionBadge { background: #263036; border: 1px solid #3b4a52; border-radius: 9px; color: #b9c7ce; padding: 2px 8px; }
            #TotalPill { color: #8fd0a8; font-weight: 600; }
            #Muted { color: #98a196; }
            #StageLabel { color: #e8ddc0; font-weight: 600; }
            #RunPanel { background: #191d1f; border: 1px solid #2b3134; border-radius: 8px; }
            QGroupBox#Controls { background: #191d1f; border: 1px solid #2b3134; border-radius: 8px; margin-top: 20px; padding-top: 4px; font-weight: 600; }
            QGroupBox#Controls::title { subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 4px; color: #98a196; }
            QPushButton { background: #242a2d; border: 2px solid #657178; border-radius: 6px; padding: 6px 10px; color: #edf0ec; }
            QPushButton:focus, QComboBox:focus, QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QTableWidget:focus { border: 2px solid #8fd0e8; }
            QCheckBox { spacing: 6px; }
            QCheckBox:disabled { color: #89969e; }
            QCheckBox::indicator { width: 16px; height: 16px; border: 2px solid #a7b5bd; border-radius: 0px; background: #15191b; }
            QCheckBox::indicator:unchecked { image: none; }
            QCheckBox::indicator:checked { image: url("CHECKBOX_TICK_URL"); background: #8fd0e8; border-color: #b6e5f5; }
            QCheckBox::indicator:enabled:hover { border-color: #ffffff; }
            QCheckBox::indicator:enabled:focus { border-color: #f0b45c; }
            QCheckBox::indicator:disabled { background: #191d1f; border-color: #75828b; }
            QCheckBox::indicator:disabled:checked { background: #75828b; }
            QPushButton:hover { background: #30383c; border-color: #657178; }
            QPushButton:pressed { background: #1b2022; }
            QPushButton:disabled { color: #758079; border-color: #353d41; }
            #PrimaryButton { background: #d8943a; border-color: #f0b45c; color: #15110a; font-weight: 700; }
            #ModelInstallButton { padding: 3px 10px; font-size: 11px; }
            #StartButton { background: #3d9865; border-color: #65bd88; color: #07150d; font-weight: 700; }
            #StartButton:disabled { background: #26302a; border-color: #35443a; color: #758079; }
            QProgressBar { background: #15191b; border: 1px solid #30383c; border-radius: 6px; color: #edf0ec; text-align: center; min-height: 22px; }
            QProgressBar::chunk { background: #3d9865; border-radius: 5px; }
            QComboBox { background: #242a2d; border: 1px solid #3a4246; border-radius: 6px; padding: 7px 10px; color: #edf0ec; }
            QComboBox:disabled { color: #758079; }
            QLineEdit, QTextEdit, QPlainTextEdit { background: #15191b; border: 1px solid #30383c; border-radius: 6px; padding: 8px; color: #eef2ea; selection-background-color: #326b78; }
            QTableWidget { background: #111517; alternate-background-color: #171c1e; gridline-color: #252c2f; border: 1px solid #30383c; border-radius: 8px; }
            QHeaderView::section { background: #202629; color: #bfc8c0; border: 0; border-right: 1px solid #30383c; padding: 8px; font-weight: 700; }
            QTableWidget::item { padding: 8px; }
            QTableWidget::item:selected { background: #244750; color: #ffffff; }
            QSplitter::handle { background: #657178; width: 6px; }
            QTabBar#WorkflowTabBar::tab { background: #242a2d; color: #bfc8c0; border: 2px solid #657178; padding: 9px 22px; font-weight: 700; }
            QTabBar#WorkflowTabBar::tab:selected { background: #244750; color: #ffffff; border-color: #8fd0e8; }
            QTabBar#WorkflowTabBar::tab:focus { border-color: #8fd0e8; }
            #PrimaryButton:focus, #StartButton:focus { border: 2px solid #8fd0e8; }
            """.replace("CHECKBOX_TICK_URL", checkbox_tick)
        )
        self.file_table.setAlternatingRowColors(True)
        title_font = QFont(QApplication.font())
        title_font.setPointSizeF(max(12.0, title_font.pointSizeF() * 1.8))
        self.title_label.setFont(title_font)
        fixed = QFont("JetBrains Mono")
        fixed.setStyleHint(QFont.StyleHint.Monospace)
        self.details.setFont(fixed)
        self.run_log_tail.setFont(fixed)

    def _set_summary(self, count: str, note: str) -> None:
        self.summary_count.setText(count)
        self.summary_note.setText(note)

    def _add_paths(self, paths: list[str]) -> None:
        if len(set(self.files) | set(paths)) > MAX_FILES:
            self._show_message(f"Add at most {MAX_FILES} upscale files at once. Nothing was added.")
            return
        added = 0
        known = set(self.files)
        for path in paths:
            clean = str(Path(path))
            if clean not in known:
                self.files.append(clean)
                known.add(clean)
                added += 1
        if added:
            self._invalidate_plan()
            if self.job_ids or self.client.active_job_count() > 0:
                for path in self.files[-added:]:
                    row = self.file_table.rowCount()
                    self.file_table.insertRow(row)
                    for column, text in enumerate(["pending", path, "", "", "", "", "", ""]):
                        self.file_table.setItem(row, column, QTableWidgetItem(text))
                    self._tag_row(row, kind="pending-upscale")
                self._show_message("Files added for the next upscale plan. Submitted jobs are unchanged.")
            else:
                self._render_pending_files()

    def _render_pending_files(self) -> None:
        self.batch = None
        # v0.4.12 (item 1): the table just became pending rows — clear the
        # previous batch's job_id->row map with it (back-to-back-batches
        # hygiene; see the v0.4.12 boundary test).
        self._batch_row_for_job = {}
        # v0.4.13 addendum (§3.3-g): the follow state resets with the row
        # map — a NEW batch's first claim must always be an identity edge.
        self._follow_mode = True
        self._follow_job_id = None
        self.start_button.setEnabled(False)
        self.file_table.setRowCount(len(self.files))
        for row, path in enumerate(self.files):
            for column, text in enumerate(["pending", path, "", "", "", "", "", ""]):
                self.file_table.setItem(row, column, QTableWidgetItem(text))
            self._tag_row(row, kind="pending-upscale")
        self._restore_staged_rows()
        self._set_summary(f"{len(self.files)} pending upscale", "Run Plan Batch to inspect them.")
        self.details.setPlainText("\n".join(self.files))

    def add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose video files",
            str(Path.home()),
            "Video files (*.avi *.divx *.flv *.m4v *.mkv *.mov *.mp4 *.mpeg *.mpg *.ts *.vob *.wmv);;All files (*)",
        )
        self._add_paths(paths)

    def add_folder(self) -> None:
        # v0.4.18 (WS1): the traversal is the SHARED recursive collector
        # (D1: one collector, one count policy). The MAX_FILES guard now
        # evaluates the recursive TOTAL and the warning names the count
        # and the tree (S-4); `_add_paths` is unchanged.
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", str(Path.home()))
        if not folder:
            return
        self._request_ingestion([folder], subtitles=False)

    def choose_output_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose an output folder", str(Path.home()))
        if folder:
            self.output_dir.setText(folder)

    # -- Add Subtitles (v0.4.14 Mode B, no upscale) ---------------------

    def _subs_file_rows(
        self, paths: list[str], verdicts: dict[str, str]
    ) -> list[list[str]]:
        """The subs table rows (same 8-column schema as the plan view;
        the Status cell carries the subs verdict instead of the upscale
        one) — the shared-table render contract for subs batches.

        v0.4.15.3 (F-5): the output path comes straight from
        :func:`subs_output_path` — one naming derivation for display,
        pre-flight, and the run guard. The old ``path[:-4]`` slice
        mangled non-4-char extensions and silently disabled the
        collision pre-flight for those files.

        v0.4.19 (WS2): the caller pre-flights every pick and hands the
        verdict map in (structural ``SUBS_VERDICT_*`` → display string
        mapping lives HERE, the render layer). Skip and blocked rows
        are STATIC — they carry no job and the 1 Hz poll never rewrites
        them. ``verdicts`` is a REQUIRED argument: a row must never be
        rendered from a verdict the pre-flight didn't make.
        """
        rows: list[list[str]] = []
        for path in paths:
            out_path = subs_output_path(path)
            existing = Path(out_path)
            verdict = verdicts.get(path, SUBS_VERDICT_READY)
            action = (
                "replace original → .mkv"
                if self.subs_replace_source.isChecked()
                else "keep original"
            )
            if verdict == SUBS_VERDICT_BLOCKED_OUTPUT:
                rows.append(
                    [
                        "blocked — subtitles output already exists (will not be overwritten)",
                        path,
                        "transcribe",
                        action,
                        out_path,
                        "",
                        "transcribed from the file's own audio",
                        "output exists: " + existing.name
                        + " (will not be overwritten)",
                    ]
                )
            elif verdict == SUBS_VERDICT_SKIP_SUBTITLES:
                rows.append(
                    [
                        "skip — already has subtitles",
                        path,
                        "skip (no transcription)",
                        "no action",
                        out_path,
                        "",
                        "already subtitled — nothing to do",
                        "",
                    ]
                )
            else:
                # ready (or probe-error: fail-open — enqueue; the
                # run-side check names the truth, and the warning cell
                # discloses the inconclusive plan probe honestly).
                rows.append(
                    [
                        "subs ready",
                        path,
                        "transcribe",
                        action,
                        out_path,
                        "",
                        "transcribed from the file's own audio",
                        (
                            "plan probe inconclusive — re-checked at run time"
                            if verdict == SUBS_VERDICT_PROBE_ERROR
                            else ""
                        ),
                    ]
                )
        return rows

    @staticmethod
    def _active_subs_paths(running: list, queued: list) -> frozenset[str]:
        """v0.4.15.1: the LIVE standalone-subs conflict set — the source
        paths of every queued/running ``subs`` job in the registry's
        snapshot views (client.queue_views()). ONLY live subs jobs
        conflict: an upscale job's paths are irrelevant to the subs
        picker (Bob's v0.4.15.1 decision — Mode B is independent of the
        upscale batch slate; the serial executor serializes everything
        anyway), and a window-owned stale id list is not live work (the
        Eric false-positive class this release fixes)."""
        records = [r for r in [*running, *queued] if getattr(r, "kind", None) == "subs"]
        return frozenset(
            str(path)
            for record in records
            for path in (getattr(record, "paths", None) or [])
        )

    @staticmethod
    def _live_window_ids(job_ids: list[str], client) -> list[str]:
        """v0.4.15.1: window-owned ids still worth tracking — snapshot-
        provable LIVE work or an unknown-state id (conservative keep:
        the pre-0.4.15.1 poll drain treats unknown ids as active). An
        id the registry reports TERMINAL leaves the window list (the
        stale-Eric false-positive class; the poll drain also drops
        these every tick)."""
        live: list[str] = []
        for job_id in job_ids:
            try:
                snapshot = client.status_snapshot(job_id)
            except Exception:
                snapshot = None
            if snapshot is None or snapshot.get("state") not in (
                "succeeded", "failed", "cancelled",
            ):
                live.append(str(job_id))
        return live

    def choose_subs_files(self) -> None:
        """v0.4.14 (Mode B, Eric's reach): the GUI entry for standalone
        subtitles — pick EXISTING videos at ANY resolution, enqueue one
        ``subs`` job per file through the SAME registry/executor the MCP
        transcribe tool uses (one enqueue surface, never a fork), and
        render through the shared slate + table machinery (rows keyed
        by _batch_row_for_job; Cancel Current/Cancel Batch ride the
        v0.4.13 cancel paths unchanged). No plan, no upscale pass, no
        engine/profile lock: the video streams are copied verbatim.

        v0.4.15.1 (the Eric guard fix): standalone subs mode is
        INDEPENDENT of the upscale batch slate (Bob's decision) — no
        blanket guard, and the window's own id list is NOT live work.
        The picker always opens; a block dialog fires ONLY for a real
        conflict, a live (queued/running) subs job over the SAME source
        file (the run-side never-overwritten gate, subs_job.py, made
        visible pre-flight). Picked files append into the live batch
        slate when one is running; the executor serializes all job
        kinds, so a subs enqueue can never contend with the running
        encode's worker."""
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose videos to add subtitles to",
            str(Path.home()),
            "Video files (*.avi *.divx *.flv *.m4v *.mkv *.mov *.mp4 *.mpeg *.mpg *.ts *.vob *.wmv);;All files (*)",
        )
        paths = [str(path) for path in paths if str(path)]
        if not paths:
            return
        self._request_ingestion(paths, subtitles=True)

    def choose_subs_folder(self) -> None:
        """v0.4.18 (WS1): the Mode B folder entry — a directory picker
        feeding the SHARED recursive collector (collect_video_files):
        the whole tree queues at once, symlink-loop-proof, deduplicated,
        deterministically ordered. Outputs land beside each source
        (subs_output_path derives from the source's own directory), so
        the nested structure is preserved by construction — the add
        never relocates or flattens. The MAX_FILES guard evaluates the
        recursive TOTAL and names the count; an empty folder is a
        silent no-op (the picker's already-cancelled discipline). The
        handoff (_enqueue_subs_paths) is choose_subs_files' exact
        post-pick tail: same conflict guard, same enqueue surface, same
        slate."""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose a folder of videos to add subtitles to",
            str(Path.home()),
        )
        if not folder:
            return
        self._request_ingestion([folder], subtitles=True)

    def _enqueue_subs_paths(self, paths: list[str], *, verdicts: dict[str, str] | None = None,
                            options: SubtitleOptions | None = None) -> None:
        """Stage Mode B paths without creating registry work."""
        options = options or SubtitleOptions(self.subs_replace_source.isChecked(),
                                            self.subs_suppress_srt.isChecked(),
                                            self.subs_device_combo.currentData())
        known = {row["path"] for row in self._staged_subs}
        incoming = {str(Path(raw).expanduser().resolve()) for raw in paths}
        if len(known | incoming) > MAX_FILES:
            QMessageBox.warning(self, "VidLift", f"Stage at most {MAX_FILES} subtitle files at once.")
            return
        try:
            ffprobe = find_tools().ffprobe
        except MissingToolError:
            ffprobe = "ffprobe"
        added = []
        for raw in paths:
            path = str(Path(raw).expanduser().resolve())
            if path in known:
                continue
            known.add(path)
            verdict = subs_plan_verdict(path, ffprobe=ffprobe) if verdicts is None else verdicts[path]
            self._staged_subs.append({
                "id": uuid.uuid4().hex,
                "path": path, "verdict": verdict,
                "replace_source": options.replace_source,
                "suppress_srt": options.suppress_srt,
                "options": options,
                "uncertain": path in self._uncertain_subtitle_paths,
                "error": ("Submission status remains unknown; retry is disabled. Inspect Recent jobs."
                          if path in self._uncertain_subtitle_paths else ""),
            })
            added.append(path)
        if added:
            for staged in self._staged_subs:
                if staged["path"] in added:
                    self._append_staged_row(staged)
        self._sync_staged_controls()
        self._set_summary(f"{len(self._staged_subs)} subtitle file(s) staged. Click Start Subtitles to begin.", "No transcription is started while adding files.")

    def start_subtitles(self, *, checked_verdicts: dict[str, str] | None = None,
                        staged_ids: set[str] | None = None) -> None:
        """Submit the current eligible staged rows once, in table order."""
        if self._subs_submitting or self._ui_closed or self._preflights:
            return
        if self._subtitle_start_blocked():
            return
        eligible = [r for r in self._staged_subs
                    if r["verdict"] in (SUBS_VERDICT_READY, SUBS_VERDICT_PROBE_ERROR)
                    and not r.get("uncertain") and (staged_ids is None or r["id"] in staged_ids)]
        if not eligible:
            return
        # Reserve before the modal review starts its nested event loop.
        self._subs_submitting = True
        self.start_subtitles_button.setEnabled(False)
        self._subs_submission_notices = []
        self._subs_submission_failed = False
        try:
            destructive = [r for r in eligible if r["replace_source"] or r["suppress_srt"]]
            if destructive:
                details = f"{len(eligible)} reviewed subtitle job(s); {len(destructive)} with deletion options.\n" + "\n".join(
                    f"{r['path']} -> {subs_output_path(r['path'])}: " + ", ".join(x for x, enabled in (
                        ("Replace original file on success (only after verified backend success)", r["replace_source"]),
                        ("Delete .srt sidecar after embed (only on successful embed; keep on embed failure)", r["suppress_srt"]),
                    ) if enabled) for r in eligible
                )
                if not self._confirm_options("Confirm subtitle options", details):
                    return
            # A modal review/preflight can outlive another submission.
            if self._subtitle_start_blocked():
                return
            # One path per call prevents partial registry writes from
            # masquerading as an entirely failed multi-file submission.
            successful: set[str] = set()
            for row in eligible:
                for index in range(self.file_table.rowCount() - 1, -1, -1):
                    if self._row_meta(index).get("staged_id") == row["id"]:
                        self._remove_display_rows([index])
                        break
                self._subs_submit_error = ""
                self._subs_submit_uncertain = False
                submitted = self._submit_subs_paths(
                    [row["path"]], replace_source=row["replace_source"],
                    suppress_srt=row["suppress_srt"], device=row["options"].device,
                    checked_verdicts=checked_verdicts,
                    row_id=row["id"],
                )
                successful.update(submitted)
                if not submitted:
                    row["error"] = self._subs_submit_error or "Not submitted; check eligibility and retry."
                    row["uncertain"] = self._subs_submit_uncertain
                    if row["uncertain"]:
                        self._uncertain_subtitle_paths.add(row["path"])
                    row["verdict"] = getattr(self, "_last_subs_verdicts", {}).get(row["path"], row["verdict"])
                    self._append_staged_row(row)
            self._staged_subs = [r for r in self._staged_subs if r["path"] not in successful]
            if self._subs_submission_notices:
                text = "\n".join(self._subs_submission_notices)
                self._show_message(text)
                notify = QMessageBox.warning if self._subs_submission_failed else QMessageBox.information
                notify(self, "Subtitle submission review", text[:2000])
        finally:
            self._subs_submitting = False
            self._sync_staged_controls()

    def _submit_subs_paths(
        self, paths: list[str], *, replace_source: bool | None = None,
        suppress_srt: bool | None = None, device: str = "auto",
        checked_verdicts: dict[str, str] | None = None,
        row_id: str | None = None,
    ) -> set[str]:
        """Create registry jobs; return only paths actually enqueued."""
        # v0.4.19 (WS2): plan-time pre-flight BEFORE the conflict guard
        # — one probe per picked path (output-exists is probe-free;
        # find_tools failure degrades to the fail-open probe-error
        # verdict, enqueueing to the run-side truth). The verdict map
        # decides WHO gets enqueued; skip/block rows are rendered but
        # never become jobs (deviation 3: the conflict dialog now
        # evaluates the ready subset only).
        try:
            ffprobe = find_tools().ffprobe
        except MissingToolError:
            ffprobe = "ffprobe"  # probe fails honestly → probe-error → ready
        verdicts: dict[str, str] = {}
        for path in paths:
            verdicts[path] = (subs_plan_verdict(path, ffprobe=ffprobe) if checked_verdicts is None
                              else checked_verdicts[path])
        self._last_subs_verdicts = verdicts
        ready = [
            path for path in paths
            if verdicts[path] in (SUBS_VERDICT_READY, SUBS_VERDICT_PROBE_ERROR)
        ]
        skipped = [
            path for path in paths
            if verdicts[path] == SUBS_VERDICT_SKIP_SUBTITLES
        ]
        blocked = [
            path for path in paths
            if verdicts[path] == SUBS_VERDICT_BLOCKED_OUTPUT
        ]
        if not ready:
            self._subs_submit_error = (
                "Output exists; it will not be overwritten." if blocked
                else "Already has usable subtitles; not submitted."
            )
            return set()
        # v0.4.15.1: the ONLY blocking condition — a live subs job over
        # the same (READY) source. Pure read over queue_views(); a client
        # without that surface (older scripted harnesses) yields no
        # conflict info and never blocks — independence-first, the
        # fail-open direction (the registry-side never-overwrite gate
        # at subs_job.py still holds the ground truth).
        live_subs_paths: frozenset[str] = frozenset()
        try:
            running, queued = self.client.queue_views()
        except AttributeError:
            running, queued = [], []
        except (OSError, BridgeError) as exc:
            self._subs_submit_error = f"Could not check the queue: {exc}. Retry when the queue is readable."
            self._show_message(self._subs_submit_error)
            return set()
        live_subs_paths = self._active_subs_paths(running or [], queued or [])
        conflicts = [path for path in ready if path in live_subs_paths]
        if conflicts:
            shown = ", ".join(Path(path).name for path in conflicts[:5])
            more = f" (+{len(conflicts) - 5} more)" if len(conflicts) > 5 else ""
            self._subs_submit_error = "Already queued for subtitles: " + shown + more
            text = "Wait for the current batch to finish or cancel it first.\n" + self._subs_submit_error
            if self._subs_submitting:
                self._subs_submission_notices.append(text)
            else:
                QMessageBox.information(self, "Subtitle conflict", text)
            return set()
        pending_ids: list[str] | None = None
        if self.job_ids:
            pending_ids = list(self.job_ids)
        fresh_ids: list[str] = []
        # Callers submit one reviewed row. The bridge distinguishes proven
        # pre-write rejection, exact accepted identity, and unknown outcome.
        if len(ready) != 1:
            raise ValueError("Submit subtitle rows individually")
        result = self._submit_gui_job("subs", ready[0], {
            "replace_source": self.subs_replace_source.isChecked() if replace_source is None else replace_source,
            "suppress_srt": self.subs_suppress_srt.isChecked() if suppress_srt is None else suppress_srt,
            "device": device,
        })
        fresh_ids = list(result.job_ids)
        if result.state != "accepted":
            self._subs_submit_uncertain = result.state != "rejected"
            self._subs_submit_error = f"could not record the batch: {result.error or 'no exact job receipt returned'}"
            if self._subs_submit_uncertain:
                self._subs_submit_error += "; submission status unknown. Inspect Recent jobs; retry is disabled."
            text = self._subs_submit_error
            if self._subs_submitting:
                self._subs_submission_failed = True
                self._subs_submission_notices.append(text)
            else:
                QMessageBox.warning(self, "Subtitle submission failed", text)
            if pending_ids is not None:
                # the live batch's ids survive untouched — only stale
                # (unknown/terminal) window ids are dropped
                self.job_ids = self._pending_submission_ids(pending_ids)
            else:
                self.job_ids = []
            if not fresh_ids:
                return set()
        elif result.error is not None:
            notice = (
                f"Job {fresh_ids[0]} was recorded, but executor notification failed: {result.error}. "
                "Tracking that exact job; do not resubmit.")
            if self._subs_submitting:
                self._subs_submission_notices.append(notice)
            else:
                self._show_message(notice)
        if not fresh_ids:
            self._subs_submit_uncertain = True
            self._subs_submit_error = "Submission status unknown: no job receipt returned. Automatic retry is disabled."
            return set()
        # v0.4.19 (WS2): rows ALWAYS render (the partition: ready first,
        # then skip, then blocked — _begin_job_slate's job↔row mapping
        # is row_offset+index over the READY list; skip/blocked rows
        # carry no job and sit BELOW the last job row, never aliased).
        seed = self.file_table.rowCount()
        ordered = ready + skipped + blocked
        self.file_table.setRowCount(seed + len(ordered))
        for offset, cells in enumerate(self._subs_file_rows(ordered, verdicts)):
            if replace_source is not None and verdicts[ordered[offset]] != SUBS_VERDICT_SKIP_SUBTITLES:
                cells[3] = "replace original → .mkv" if replace_source else "keep original"
            for column, text in enumerate(cells):
                self.file_table.setItem(
                    seed + offset, column, QTableWidgetItem(text)
                )
            self._tag_row(seed + offset, kind="subtitles", row_id=row_id or uuid.uuid4().hex,
                          options=SubtitleOptions(
                self.subs_replace_source.isChecked() if replace_source is None else replace_source,
                self.subs_suppress_srt.isChecked() if suppress_srt is None else suppress_srt,
                device))
        if not fresh_ids:
            # nothing enqueued — the static skip/block rows still render
            # (and the summary below names why); no slate, no timer.
            if pending_ids is not None:
                self.job_ids = pending_ids
            self._set_summary(
                _subs_enqueue_summary(0, len(skipped), len(blocked)),
                "Skipped and blocked files are not enqueued; the "
                "run-side guards remain the backstop.",
            )
            return set()
        # v0.4.15.1: the window tracks ENQUEUED-BY-THIS-WINDOW ids with
        # a LIVE (non-terminal, snapshot-provable) state — stale ids
        # (the Eric false-positive class) are dropped from tracking, a
        # concurrent submission EXTENDS the live list, and the poll
        # loop keeps draining terminal ids per tick.
        live_pending = self._pending_submission_ids(pending_ids or [])
        self.job_ids = live_pending + [str(job_id) for job_id in fresh_ids]
        # v0.4.14 (Mode B): a subs batch reuses the SHARED table — its
        # rows append after any prior batch's rows (the table is the
        # window's batch view; an old batch's terminal rows stay
        # readable above — clear_files empties the whole table).
        # v0.4.14 (Mode B): a fresh submission reseeds the slate — the
        # shared bootstrap gives these rows the same _batch_row_for_job
        # identity map, run_counts slots, bar, and follow engagement
        # the upscale flow gets; a prior batch's structures can never
        # alias into this one (_begin_job_slate rewrites ALL of them).
        # Subs rows APPEND: the row map shifts by the existing row count.
        # v0.4.15.1: when an upscale slate is LIVE the bootstrap extends
        # it (below) — a reset here would orphan the running batch.
        if pending_ids is not None and self._batch_slots:
            live = self._batch_slots
            fresh = [job_id for job_id in self.job_ids if job_id not in live]
            self._batch_slots = live + fresh
            self._batch_slot_index = {
                job_id: index for index, job_id in enumerate(self._batch_slots)
            }
            self.run_total = len(self._batch_slots)
            self.run_counts = (self.run_counts or [None] * len(live)) + [
                None
            ] * len(fresh)
            for index, job_id in enumerate(fresh):
                self._batch_row_for_job[job_id] = seed + index
                self._tag_row(seed + index, job_id=job_id)
            self.run_summary = summarize_batch(self.run_counts)
            self.progress.setFormat(_batch_format(self.run_summary))
        else:
            self._reset_batch_progress()
            self._begin_job_slate(self.job_ids, row_offset=seed)
        self._set_summary(
            _subs_enqueue_summary(
                len(self._batch_slots) if pending_ids is not None
                else len(self.job_ids),
                len(skipped),
                len(blocked),
            ),
            "Transcribing audio and soft-muxing subtitles (no upscale).",
        )
        return set(ready)

    def clear_files(self) -> None:
        if self._preflights:
            self.cancel_checking()
        if self.client.active_job_count() > 0:
            self.clear_staged_subtitles()
            self.files = []
            self._invalidate_plan()
            self._show_message("Unsent files cleared. Submitted jobs and source media are unchanged.")
            return
        if any(r["replace_source"] or r["suppress_srt"] for r in self._staged_subs):
            if QMessageBox.question(self, "Clear staged subtitles", "Discard staged subtitle rows with destructive options?") != QMessageBox.StandardButton.Yes:
                return
        self._staged_subs.clear()
        self.start_subtitles_button.setEnabled(False)
        self.files = []
        self.batch = None
        self._planned_inputs = None
        self._plan_dirty = False
        # v0.4.12 (item 1): the table is gone — a stale job_id->row map from
        # the previous batch must never map into a fresh one.
        self._batch_row_for_job = {}
        # v0.4.13 addendum (§3.3-g): the follow state resets with the row
        # map — a NEW batch's first claim must always be an identity edge.
        self._follow_mode = True
        self._follow_job_id = None
        self.start_button.setEnabled(False)
        self.file_table.setRowCount(0)
        self.details.clear()
        self._set_summary("No files planned yet", "Drop videos into the selected workflow or use its Add button.")

    def plan_batch(self) -> None:
        if self.job_ids or self.client.active_job_count() > 0:
            self._show_message("Wait for submitted work to finish before planning the next upscale.")
            return
        if not self.files:
            QMessageBox.information(self, "VidLift", "Add one or more video files first.")
            return
        gbph = self._valid_size()
        if gbph is None:
            return
        self._plan_dirty = True
        self.start_button.setEnabled(False)
        try:
            tools = find_tools()
        except MissingToolError as exc:
            QMessageBox.critical(self, "VidLift setup problem", str(exc))
            return
        reports = []
        failures: list[str] = []
        for path in self.files:
            try:
                reports.append(probe_and_classify(path, binary=tools.ffprobe))
            except (ProbeError, OSError) as exc:
                failures.append(f"{path}: {exc}")
        self._accept_plan_reports(reports, failures, self._current_plan_inputs())

    def _accept_plan_reports(self, reports, failures: list[str], inputs: PlanInputs) -> None:
        if not reports:
            self._render_pending_files()
            self._show_failed_plan_rows(inputs, failures)
            self._show_message("No files could be checked. " + "\n".join(failures))
            return
        try:
            # v0.4.15.3 (F-6): the checkbox rides the PLAN call — the
            # plan table must never promise transcription the run
            # won't do when the user has disabled it at plan time.
            self.batch = build_batch_plan(
                reports,
                target="720p",
                gb_per_hour=inputs.gbph,
                output_dir=inputs.output_dir or None,
                transcription_enabled=inputs.transcribe,
            )
        except Exception as exc:
            QMessageBox.critical(self, "VidLift planning failed", str(exc))
            return
        self._planned_inputs = inputs
        self._plan_dirty = False
        self.render_batch(self.batch)
        if failures:
            self._show_failed_plan_rows(inputs, failures)
            self._show_message("Some files failed to check:\n" + "\n".join(failures))
        else:
            self._show_message("")

    def _show_failed_plan_rows(self, inputs: PlanInputs, failures: list[str]) -> None:
        for failure in failures:
            path = next((path for path in inputs.paths if failure.startswith(path + ": ")), None)
            if path is None:
                continue
            row = next((row for row in range(self.file_table.rowCount())
                        if self.file_table.item(row, 1) is not None and
                        self.file_table.item(row, 1).text() == path and
                        self._row_meta(row).get("kind") == "pending-upscale"), None)
            if row is None:
                row = self.file_table.rowCount()
                self.file_table.insertRow(row)
                for col, value in enumerate(["check failed", path, "", "", "", "", "", failure]):
                    self.file_table.setItem(row, col, QTableWidgetItem(value))
            else:
                self.file_table.item(row, 0).setText("check failed")
                self.file_table.item(row, 7).setText(failure)
            self._tag_row(row, kind="pending-upscale", plan_error=failure)

    def render_batch(self, batch: BatchPlan) -> None:
        if self.job_ids or self.client.active_job_count() > 0:
            self._render_detail_for_row()
            return
        # v0.4.14 (Mode B): a plan render REPLACES the table (a plan is
        # its own batch view; subs history rows may only survive a
        # plan render on the NEXT subs append, never across a new plan —
        # the upscale batch's row map seeds from 0 and any leftover
        # foreign row above it would alias poll writes).
        self.file_table.setRowCount(len(batch.files))
        for row, item in enumerate(batch.files):
            report = item.report
            warnings = "; ".join(warnings_for(item))
            subtitle_note = item.subtitle_note
            if (
                not self.subtitle_ocr.isChecked()
                and item.subtitle_plan is not None
                and item.subtitle_plan.ocr_required
            ):
                subtitle_note = "OCR disabled; no bitmap subtitles"
            # v0.4.15.3 (F-6): the transcription mirror of the OCR
            # override — a plan built (or planned earlier) with
            # transcription on renders the honest disabled note when
            # the checkbox is now off (covers "user unchecks after
            # pressing Plan" without a replan).
            if (
                not self.transcribe_subs.isChecked()
                and item.subtitle_plan is not None
                and item.subtitle_plan.source == "transcribed"
            ):
                subtitle_note = "transcription disabled; no subtitles transcribed"
            values = [
                item.process_verdict,
                report.path,
                report.source_class,
                f"{item.output_width}x{item.output_height}",
                item.output_path,
                format_bytes(item.estimated_output_bytes),
                subtitle_note,
                warnings,
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column == 0 and item.process_verdict == "ready":
                    cell.setForeground(Qt.GlobalColor.green)
                elif column == 0:
                    cell.setForeground(Qt.GlobalColor.yellow)
                self.file_table.setItem(row, column, cell)
            self._tag_row(row, kind="planned-upscale", plan=item)
        self._restore_staged_rows()
        self.file_table.selectRow(0)
        self._set_summary(
            f"{batch.runnable_count}/{len(batch.files)} ready",
            f"Estimated total {format_bytes(batch.total_estimated_bytes)}. Click Start Upscale to begin.",
        )
        self._update_start_enabled()

    def _update_start_enabled(self) -> None:
        """Start stays available only with a plan, ready rows, and no
        in-flight jobs (design §5: re-enable at the LAST terminal).

        v0.4.14 (Mode B): a subs slate carries NO plan — the Start
        button is untouched (still disabled from construction) so a
        stray click across a running/subs-only queue can never ship the
        current batch as an upscale re-plan."""
        if self.client.active_job_count() > 0:
            # a run is live; Start is the cancel affordance via disable–
            # enable symmetry handled in _sync_run_controls
            return
        if not self.batch:
            self.start_button.setEnabled(False)
            return
        allowed = (
            self.batch.runnable_count > 0
            and not self.job_ids
            and not self._plan_dirty
            and not self._plan_checking
            and not self._preflights
            and not self._upscale_submitting
        )
        self.start_button.setEnabled(allowed)

    def _sync_details(self) -> None:
        # v0.4.13 addendum (ITEM 1, design §2.3 D-selection-2): the slot
        # boundary. In the frozen --windowed Windows build any exception
        # escaping a Qt slot has no console and kills the process
        # silently (the Eric crash family §1.1). This slot is
        # user-triggered, rare, idempotent — the ONE place a logged
        # swallow is justified; the poll heartbeat keeps its explicit-
        # guard style (§2.3: no wholesale wrapper there). The boundary
        # LOGS AND SWALLOWS, never silently — and also carries the
        # v0.4.13 §3.4 user-selection edge (follow disengage; ITEM 2).
        if getattr(self, "_programmatic_select", False):
            # The follow-write's own selectRow delivers this signal
            # synchronously — a programmatic write, never a user
            # selection: the follow path renders detail for that row
            # itself. Any NEW programmatic selection writer MUST use
            # _follow_select_row or replicate this guard (R4), else the
            # machine misreads it as a user override (fail-safe
            # direction: follow disengages; T3-class tests catch it).
            return
        # §3.4: anything reaching here IS a user selection (mouse or
        # keyboard — arrows fire the same itemSelectionChanged), the
        # USER_SELECT edge: the user stays on their row until the next
        # claim edge re-engages follow (Eric: "stay on the selected item
        # until the running item switches then adjust it").
        self._follow_mode = False
        try:
            self._render_detail_for_row()
        except Exception:
            import sys as _sys
            import traceback

            print("VidLift: detail render survived an exception:", file=_sys.stderr)
            traceback.print_exc()

    # -- v0.4.13 addendum ITEM 1 (design §2.1 D-selection-1): the ONE detail renderer
    # every selection detail write goes through. Plan body from detail_text (already
    # fully None-guarded, v0.4.12 §1.3-verified), optional scratch note, plus a LIVE
    # overlay when the row maps to a job in `_batch_row_for_job` (identity-keyed —
    # never the draining index). Tolerates the FULL state matrix (§2.2): only `state`
    # and `job_id` keys are assumed on a non-None snapshot; everything else uses the
    # poll path's tolerant `.get(...) or default` getters; a None snapshot renders an
    # honest "(no data)" line instead of raising. Safe for queued (no snapshot data),
    # running mid-transition, cancelled, succeeded, failed, and lookup-failed rows.
    def _render_detail_for_row(self, row: int | None = None) -> None:
        if self.queue_tabs.currentIndex() == 1:
            return
        if row is None:
            rows = self.file_table.selectionModel().selectedRows()
            if not rows:
                return
            row = rows[0].row()
        meta = self._row_meta(row)
        if meta.get("kind") == "pending-upscale":
            self._set_details_text("Pending upscale: " + self.file_table.item(row, 1).text()
                                   + "\nRun Plan Batch before starting."
                                   + ("\nCheck failed: " + meta["plan_error"] if meta.get("plan_error") else ""))
            return
        item = meta.get("plan")
        if meta.get("kind") in ("staged-subtitles", "subtitles") or (self.batch is None and item is None):
            # v0.4.14 (Mode B): a subs slate has NO BatchPlan — the
            # detail body renders from the shared table row and then
            # FALLS THROUGH to the SAME LIVE overlay tail the plan
            # path uses (one overlay contract, kind-blind).
            if not (0 <= row < self.file_table.rowCount()):
                return
            text = self._subs_detail_text(row)
        else:
            # Keep the v0.4.12 bounds guard: a row outside the plan renders
            # nothing rather than inventing data (§2.1 step 1).
            if item is None:
                if not (0 <= row < len(self.batch.files)):
                    return
                item = self.batch.files[row]
            text = detail_text(
                item,
                subtitle_ocr_enabled=meta.get("submitted_options", {}).get("subtitle_ocr", self.subtitle_ocr.isChecked()),
                upscale_profile=meta.get("submitted_options", {}).get("upscale_profile", self.profile_choice),
            )
            submitted = meta.get("submitted_options")
            effective = submitted or {
                "replace_source": self.replace_source.isChecked(),
                "suppress_srt": self.suppress_srt.isChecked(),
                "denoise": self.denoise_mode,
                "resume": self.resume_claim.isChecked(),
            }
            if meta.get("submission_error"):
                text += "\nSubmission: " + meta["submission_error"]
            if effective:
                text += "\n\n" + ("Submitted options (frozen):" if submitted else "Run options (reviewed at Start):")
                text += "\nSource file policy: " + ("replace only after verified success" if effective["replace_source"] else "keep original")
                text += "\n" + SubtitleOptions(suppress_srt=effective["suppress_srt"]).sidecar_note
                text += "\nRequested upscale engine: " + str(meta.get("submitted_engine", self.engine_choice))
                text += "\nDenoise: " + str(effective.get("denoise", "off"))
                text += "\nResume retained work: " + ("on" if effective.get("resume") else "off")
            if self._plan_dirty and row not in self._batch_row_for_job.values():
                text = "Settings changed. Plan again before starting.\n\n" + text
            # v0.4.9 (§1.4 D1), unchanged call — but now inside the _sync_details
            # slot boundary (belt-and-braces over scratch_note_for_source's own
            # OSError/JSONDecodeError guards: a Windows locked-file transient
            # must never escape a slot in the frozen build, §2.1 step 3).
            if not self.resume_claim.isChecked():
                from .run import scratch_note_for_source

                note = scratch_note_for_source(Path(item.report.path))
                if note:
                    text = f"{text}\n\n{note}"
        # LIVE overlay (§2.1 step 4): additive, tolerant, identity-keyed.
        job_id = next(
            (
                jid
                for jid, mapped_row in self._batch_row_for_job.items()
                if mapped_row == row
            ),
            None,
        )
        if job_id is None:
            self._set_details_text(text)
            return
        try:
            snapshot = self.client.status_snapshot(job_id)
        except Exception:
            # §2.3: bridge reads stay inside the boundary too — degrade to
            # plan body, never raise out of a render.
            snapshot = None
        if snapshot is None:
            self._set_details_text(
                f"{text}\n\n── LIVE ─────────────────────\nState: (no data — job lookup failed)"
            )
            return
        state = snapshot.get("state") or "queued"
        stage = run_stage_label(snapshot)
        rendered = f"{stage}" if stage else state
        # v0.4.17 (fail-path): a terminal stage renders its own state
        # word (run_stage_label's terminal arm returns the state), so
        # the "state — stage" tail would stutter ("failed — failed").
        # A live stage still reads "running — <what is running>".
        state_part = state if rendered == state else f"{state} — {rendered}"
        eta = snapshot.get("eta_seconds")
        eta_suffix = ""
        if eta is not None:
            try:
                eta_val = float(eta)
                eta_suffix = f" — ~{self._fmt_hm(eta_val)} remaining"
            except (TypeError, ValueError):
                eta_suffix = ""
        lines = [
            "── LIVE ─────────────────────",
            f"Job: {job_id}   State: {state_part}{eta_suffix}",
        ]
        # v0.4.19 (WS3): the device fact line — the resolved GPU/CPU
        # truth, the requested pin while resolving/queued, absent when
        # neither fact exists (older bridges, non-transcribing jobs).
        device_display = _device_display(snapshot)
        if device_display is not None:
            lines.append(f"Device: {device_display}")
        fallback_reason = (snapshot.get("transcription") or {}).get("fallback_reason")
        if fallback_reason:
            lines.append("Transcription fallback: " + str(fallback_reason)[:200])
        # v0.4.17 (fail-path, the failure-reason surfacing): the
        # payload's own `error` rides the overlay verbatim — the exact
        # string the record/registry carries (e.g. a probe
        # VideoToolError message); never paraphrased, never guessed. A
        # subs job that fails at probe writes no run log, so THIS line
        # is where Eric first sees why the .avi died. Honest rule: any
        # present reason displays; an absent one adds nothing.
        error = snapshot.get("error")
        if error:
            lines.append(f"Error: {error}")
        progress = snapshot.get("progress") or {}
        if isinstance(progress, dict) and progress:
            frames_done = progress.get("frames_done")
            frames_total = progress.get("frames_total")
            if frames_done is not None:
                total = frames_total if frames_total else "?"
                lines.append(f"Frames: {frames_done}/{total}")
        tail = snapshot.get("log_tail") or []
        if tail:
            last = tail[-1]
            lines.append(f"Log: {last}")
        self._set_details_text("\n".join([text, "", *lines]))

    def _subs_detail_text(self, row: int) -> str:
        """v0.4.14 (Mode B): the subs row's detail body — the file, the
        output contract, and the row's own verdict — rendered from the
        shared table's cells (no BatchPlan exists for a subs slate).
        The LIVE overlay (_render_detail_for_row's tail) appends below
        exactly as it does for plan batches."""
        cells = (
            self.file_table.item(row, column)
            for column in range(self.file_table.columnCount())
        )
        values = [cell.text() if cell is not None else "" for cell in cells]
        status, path, source, target, output, _size, subtitle_note, warning = (
            (values + [""] * 8)[:8]
        )
        lines = [
            path,
            "",
            "Mode: subtitles only (no upscale; the video streams are copied)",
            f"Verdict: {status}",
            f"Subtitles: {subtitle_note or 'transcribed from the file' + chr(39) + 's own audio'}",
            f"Output: {output} (+ .srt sidecar)",
        ]
        options = self._row_meta(row).get("options")
        if isinstance(options, SubtitleOptions):
            lines[-1] = f"Output: {output}"
            lines.extend([f"Sidecar policy: {options.sidecar_note}",
                          f"Requested device: {options.device_note}"])
            if self._row_meta(row).get("kind") == "staged-subtitles":
                lines.append("Staged only: no transcription job has been submitted.")
        # v0.4.17 workstream-2 (Karen K-2): column 3 of a subs row carries an
        # ACTION phrase ("replace original → .mkv" / "keep original"), not a
        # resolution — the "Resolution handling" heading was a v0.4.14
        # upscale-schema holdover and read incoherently under the subs
        # schema. The heading now names the source's fate, rendered in BOTH
        # postures (silence used to imply "kept" — the invisibility class
        # this workstream exists to kill). The literals mirror
        # _subs_file_rows (:1574-1577) — pinned by test 28 and §8-T8
        # (v0.4.24: the literal follows the output's container, .mkv).
        if target == "replace original → .mkv":
            lines.append(
                "Source file: replaced on success — output renamed to <name>.mkv"
            )
        elif target == "keep original":
            lines.append(
                "Source file: kept — output beside it as <name>.subs.mkv"
            )
        elif target:
            lines.append(f"Source file: {target}")
        if warning:
            lines.extend(["", "Warnings:", f"- {warning}"])
        return "\n".join(lines)

    def _set_details_text(self, text: str) -> None:
        """§3.3-f: the details QTextEdit has no change-guard — setPlainText
        resets the user's scroll. Compare-before-write so a paused/idle
        running job does not repaint-kick the panel every second."""
        if self.details.toPlainText() != text:
            previous = self.details.toPlainText()
            cursor = self.details.textCursor()
            anchor, position = cursor.anchor(), cursor.position()
            scrollbar = self.details.verticalScrollBar()
            scroll = scrollbar.value() if scrollbar is not None else 0
            self.details.setPlainText(text)
            if previous.split("\n", 1)[0] == text.split("\n", 1)[0]:
                if previous[min(anchor, position):max(anchor, position)] == text[min(anchor, position):max(anchor, position)]:
                    from PyQt6.QtGui import QTextCursor
                    cursor = self.details.textCursor()
                    cursor.setPosition(min(anchor, len(text)))
                    cursor.setPosition(min(position, len(text)), QTextCursor.MoveMode.KeepAnchor)
                    self.details.setTextCursor(cursor)
                if scrollbar is not None:
                    scrollbar.setValue(scroll)

    def _refresh_details(self) -> None:
        if self.batch is None:
            return
        # v0.4.13 addendum ITEM 1 (design §2.4 D-selection-3): the C1
        # re-entrancy class dies structurally — while a batch is LIVE with
        # in-flight jobs, render_batch (full setRowCount/setItem rebuild)
        # must never run: it can disturb a tracked selection mid
        # itemSelectionChanged delivery and segfault in the Qt binary
        # (§1.4 C1). The subtitle_ocr toggle's refresh degrades to a
        # DETAIL-ONLY re-render of the current row; the table's plan
        # columns already carry the same subtitle strings render_batch
        # would write, so nothing is lost. Contract pinned by T5.
        if self.job_ids:
            self._render_detail_for_row()
            return
        self.render_batch(self.batch)

    # ---- Start: enqueue through the unified bridge path (design §5) ----

    # v0.4.14 (Mode B): the batch slate bootstrap every enqueue flow
    # shares (upscale Start and Add Subtitles alike). One batch = one
    # submission = one slate; back-to-back hygiene rides the same
    # structures — _reset_batch_progress() reseeds slot/row maps at
    # submission time, so a second submission never aliases the first
    # batch's rows or slots.
    def _begin_job_slate(
        self, job_ids: list[str], *, row_offset: int = 0, row_map: dict[str, int] | None = None
    ) -> None:
        if not job_ids:
            return
        # v0.4.14 (Mode B): the fresh slate begins with a CLEAN poll
        # context — the stage/ETA facts of a PREVIOUS batch must never
        # leak into a fresh submission's rows (the render path consults
        # _batch_row_for_job before writing cells).
        self._batch_row_for_job = {}
        self.run_panel.show()
        self.stage_bar.reset()
        self.overall_bar.reset()
        self.overall_bar.set_busy(True, "queue active ...")
        self.stage_bar.set_busy(True, "starting ...")
        self.run_total = len(job_ids)
        # v0.4.10 (R2): stable per-batch slot registry (see start_upscale
        # for the full contract); a subs batch uses the IDENTICAL slate.
        self._batch_slots: list[str] = list(job_ids)
        self._batch_slot_index: dict[str, int] = {
            job_id: index for index, job_id in enumerate(self._batch_slots)
        }
        # v0.4.12 (item 1): the stable ROW map, seeded ONCE in submission
        # order, immutable for the batch's lifetime (the full truth
        # contract lives at its original seeding site in start_upscale).
        # v0.4.14 (Mode B): subs rows APPEND below any previous batch's
        # rows (the shared table is the window's batch view), so the
        # identity map carries the row_offset — slots/run_counts stay
        # 0-based slate-parallel arrays (submission order), while cells
        # write at physical rows offset+index.
        self._batch_row_for_job: dict[str, int] = {
            job_id: row_map[job_id] if row_map is not None else row_offset + index
            for index, job_id in enumerate(self._batch_slots)
        }
        for job, row in self._batch_row_for_job.items():
            self._tag_row(row, job_id=job)
        if len(self._batch_row_for_job) != len(self._batch_slots):
            raise RuntimeError(
                "batch row map seeding mismatch: "
                f"{len(self._batch_row_for_job)} rows for "
                f"{len(self._batch_slots)} job ids"
            )
        self._degraded_jobs.clear()
        self.run_counts: list[str | None] = [None] * self.run_total
        self.run_summary = summarize_batch(self.run_counts)
        # v0.4.13 addendum (§3.2 BATCH_START): follow engages with
        # _follow_job_id=None so the FIRST claim is an identity edge.
        self._follow_mode = True
        self._follow_job_id = None
        # v0.4.15.4 (F-13c): a new slate has no running job yet — the
        # tracked id expires with the previous batch's truth (before
        # this method's _sync_run_controls reads the rule).
        self._running_job_id = None
        # v0.4.10 (R2): the batch bar restarts from a KNOWN color.
        self._progress_chunk_color = None
        self.progress.setRange(0, self.run_total)
        self.progress.setValue(0)
        self.progress.setFormat(_batch_format(self.run_summary))
        self.progress.show()
        self._sync_run_controls()
        # Liveness: dots animate, clock ticks until the last job lands.
        self._run_started_at = time.time()
        self._elapsed_timer.start()
        self.activity_dots.start()
        self._stage_now = "queued"
        self._stage_eta = None
        self._stage_fraction = None
        # v0.4.15.3 (F-4): the measured-truth floor resets with the
        # slate (and ONLY at a slate boundary — never on stage change).
        self._stage_fraction_measured = 0.0
        # v0.4.12 (item 2): ETA state starts fresh with every batch.
        self._eta_state = {}
        self._eta_phase_now = None
        self._eta_rendered = None
        self._batch_eta_rendered = None
        self.time_elapsed.setText("Elapsed 0:00")
        # Seed the forecast immediately instead of waiting one tick.
        self._forecast_total()
        self._render_recent_jobs()

    def _reset_batch_progress(self) -> None:
        """v0.4.14 (Mode B): a subs batch reuses the SAME run panel,
        bars, and slate structures as an upscale batch; the fresh-sub
        path clears them to a new submission's baseline (no QSettings —
        an ephemeral in-window reset, distinct from the batch-boundary
        reset that _batch_finished gives every ENDED batch)."""
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.hide()
        self.stage_bar.reset()
        self.overall_bar.reset()
        self._set_text(self.stage_label, "")
        self._batch_slots = []
        self._batch_slot_index = {}
        self._batch_row_for_job = {}
        self.run_counts = []
        self.run_total = 0
        self.run_summary = None
        # v0.4.15.4 (F-13c): a fresh submission reseeds the run state —
        # no running job belongs to the new slate yet.
        self._running_job_id = None

    def start_upscale(self) -> None:
        if self._preflights or self._upscale_submitting or self._subs_submitting or self._ui_closed:
            return
        if self.client.active_job_count() > 0 or self.job_ids:
            return
        if self.batch is None:
            return
        items = [item for item in self.batch.files if item.process_verdict == "ready"]
        if any(item.report.path in self._uncertain_upscale_paths for item in items):
            self._show_message("A previous submission is unresolved. Inspect Recent jobs before retrying these sources.")
            return
        if not items:
            QMessageBox.information(self, "VidLift", "No files in this plan are ready to upscale.")
            return
        if len(items) > MAX_FILES:
            self._show_message(f"Submit at most {MAX_FILES} upscale files at once. Nothing was submitted.")
            return
        gbph = self._valid_size()
        if gbph is None:
            return
        if self._plan_dirty or (self._planned_inputs is not None
                                 and self._planned_inputs != self._current_plan_inputs()):
            self._invalidate_plan()
            return
        options = {
            "target": "720p",
            "gb_per_hour": gbph,
            "output_dir": self.output_dir.text().strip() or None,
            "subtitle_ocr": self.subtitle_ocr.isChecked(),
            "replace_source": self.replace_source.isChecked(),
            # v0.4.8 (D5): the profile rides the options into
            # run_file(profile=...) via the bridge's absent-defaults
            # rule (absent would read as "live-action" anyway).
            "upscale_profile": self.profile_choice,
            # v0.4.9 (§1.4): rides into run_file(resume=...) through the
            # bridge's absent-defaults rule (absent would read False).
            "resume": self.resume_claim.isChecked(),
            # v0.4.14 (Mode A): the checkbox is the load-bearing kill
            # switch; False strips an already-selected transcribed plan.
            "transcription_enabled": self.transcribe_subs.isChecked(),
            # v0.4.15 (opt-in denoise): rides into run_file(denoise=...)
            # through the bridge's absent-defaults rule the same way the
            # profile rides (off would read the config default anyway).
            "denoise": self.denoise_mode,
            # v0.4.18 (opt-in suppress_srt): rides into
            # run_file(suppress_srt=...) through the bridge's
            # absent-defaults rule — absent reads False (the KEEP
            # sidecar v0.4.17 behavior), so a stale caller is safe.
            "suppress_srt": self.suppress_srt.isChecked(),
        }
        engine = self.engine_choice
        self._upscale_submitting = True
        try:
            if options["replace_source"] or options["suppress_srt"]:
                effects = ", ".join(label for label, enabled in (
                    ("Replace original file on success (only after verified backend success)", options["replace_source"]),
                    ("Delete .srt sidecar after embed (only on successful embed; keep on embed failure)", options["suppress_srt"]),
                ) if enabled)
                if not self._confirm_options("Confirm upscale options", f"{len(items)} reviewed upscale job(s).\n" + "\n".join(
                        f"{item.report.path} -> {item.output_path}: {effects}" for item in items)):
                    return
            # A review is a nested event loop: file/settings changes invalidate it.
            if self._plan_dirty or self.batch is None or any(item not in self.batch.files for item in items):
                self._show_message("The reviewed plan changed. Plan again before starting.")
                return
            self._submit_upscale_reviewed(items, options, engine)
        finally:
            self._upscale_submitting = False

    def _submit_upscale_reviewed(self, items, options: dict, engine: str) -> None:
        """Submit a frozen review, preserving accepted receipts on later errors."""
        options = dict(options)
        accepted = []
        self.job_ids = []
        errors = []
        for item in items:
            result = self._submit_gui_job("run", item.report.path, options, engine)
            ids = list(result.job_ids)
            if result.state != "accepted" or result.error is not None:
                cause = str(result.error or "no exact job receipt returned")
                errors.append(f"{item.report.path}: {cause}" +
                              ("; exact job recorded and tracked, do not resubmit" if ids else ""))
                if result.state != "rejected" and not ids:
                    self._uncertain_upscale_paths.add(item.report.path)
                if not ids:
                    for row in range(self.file_table.rowCount()):
                        if self._row_meta(row).get("plan") == item:
                            status = "submission unknown" if item.report.path in self._uncertain_upscale_paths else "not submitted"
                            status_cell = self.file_table.item(row, 0)
                            warning_cell = self.file_table.item(row, 7)
                            if status_cell is not None:
                                status_cell.setText(status)
                            if warning_cell is not None:
                                warning_cell.setText(cause)
                            self._tag_row(row, submission_error=cause)
            if ids:
                self.job_ids.extend(ids)
                accepted.extend([item] * len(ids))
        if errors:
            self._show_message("Some submissions failed or are unresolved:\n" + "\n".join(errors))
            QMessageBox.warning(self, "Could not record all jobs", "\n".join(errors)[:2000])
        if not self.job_ids:
            return
        self.engine_combo.setEnabled(False)
        self.engine_combo.setToolTip("Engine is fixed once a batch starts; cancel to change it.")
        # v0.4.8 (D5): the profile is fixed for the batch the same way.
        self.profile_combo.setEnabled(False)
        self.profile_combo.setToolTip(PROFILE_TOOLTIP)
        # v0.4.15: denoise is fixed for the batch the same way.
        self.denoise_combo.setEnabled(False)
        # v0.4.14 (Mode B): the slate bootstrap lives in ONE place for
        # both flows; the upscale tail keeps its engine/profile locks
        # between enqueue and the shared slate build. Plan rows start at
        # physical row 0 (render_batch rebuilt the table).
        row_map = {
            job: next((row for row in range(self.file_table.rowCount())
                       if self._row_meta(row).get("plan") == item), index)
            for index, (job, item) in enumerate(zip(self.job_ids, accepted))
        }
        self._begin_job_slate(self.job_ids, row_map=row_map)
        for job, row in self._batch_row_for_job.items():
            self._tag_row(row, submitted_options=dict(options), submitted_engine=engine, job_id=job)

    def cancel_upscale(self) -> None:
        """Cancel the RUNNING job only (v0.4.15.4, F-13c): 'Current'
        means current — queued jobs are Cancel Batch's documented
        surface; this button must never convert a queued job to
        cancelled."""
        job_id = self._running_job_id
        if job_id is None:
            return
        result = self.client.cancel(job_id)
        if result in ("cancelling", "cancelled"):
            self.cancel_button.setEnabled(False)
            self.stage_label.setText("Cancelling...")
            self._set_summary("Cancelling", "Stopping the current file safely.")

    def cancel_batch_upscale(self) -> None:
        """The v0.4.13 batch-wide cancel (design §4.1): ONE client call —
        the atomicity lives registry-side (§3.2), the GUI never touches
        the registry. Every job in this batch whose registry state is
        queued lands cancelled; the running job's claim-time CancelFlag
        flips and dies through the proven terminate path. The drain,
        per-row truth rendering, and batch cleanup then ride the
        EXISTING _poll_jobs -> _consume_terminal_snapshot ->
        _batch_finished pipeline — no new render path; both per-id
        verdicts (cancelling/cancelled) mean "lands cancelled"."""
        ids = list(self.job_ids)  # snapshot the batch set
        if not ids:
            return
        if (
            QMessageBox.question(
                self,
                "Cancel Batch",
                "Cancel the entire batch? The running file stops and "
                f"{len(ids)} queued file(s) will not run.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,  # ship default: Yes (§4.3)
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.client.cancel_batch(ids)
        # Belt-and-suspenders (§4.4): both cancel buttons disable on the
        # first click; a second can only find already-finished work.
        self.cancel_batch_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.stage_label.setText("Cancelling batch...")
        self._set_summary(
            "Cancelling batch",
            "Stopping the current file and clearing the queue.",
        )

    def _sync_run_controls(self) -> None:
        self._sync_staged_controls()
        # v0.4.15.4 (F-13c): "Cancel Current" is enabled iff THIS window
        # owns a RUNNING job (the tracked running id). The old
        # active_job_count() rule counted QUEUED work — a queued-only
        # batch enabled the button whose click permanently converted a
        # queued job to cancelled: every word of the tooltip was false
        # for exactly the case it described. Queued clearing is Cancel
        # Batch's documented surface (below, §4.2, with its confirm
        # dialog).
        self.cancel_button.setEnabled(
            self._running_job_id is not None and bool(self.job_ids)
        )
        # v0.4.13 (design §4.2): Cancel Batch enabled iff this window
        # owns a live batch set — INCLUDING the idle-queue case (nothing
        # running right now, queued jobs remain): cancel_batch marks all
        # queued and the batch drains.
        self.cancel_batch_button.setEnabled(bool(self.job_ids))
        # v0.4.13: Start/Plan stay locked while the client still has
        # live work (the enqueue-armed liveness truth); F-13c changed
        # ONLY the cancel button's rule — this arm is the old active
        # rule's surviving half, byte-identical.
        if self.client.active_job_count() > 0 and bool(self.job_ids):
            self.start_button.setEnabled(False)
            self.plan_button.setEnabled(False)
        elif not self.job_ids:
            self.plan_button.setEnabled(not self._preflights)
            self._update_start_enabled()

    # ---- 1 Hz tick: single writer for time labels (sibling port) ----

    def _tick_elapsed(self) -> None:
        if self._run_started_at is None:
            return
        s = int(time.time() - self._run_started_at)
        m, sec = divmod(s, 60)
        self._set_text(self.time_elapsed, f"Elapsed {m}:{sec:02d}")
        self._poll_jobs()
        if not self.job_ids:
            self._stop_liveness()
            return
        self._forecast_total()

    def _poll_jobs(self) -> None:
        """Read the shared registry once a second and re-render every
        row/stage widget from its snapshot (the GUI is a view)."""
        try:
            snapshots = list(self.client.enqueue_snapshots(self.job_ids) or [])
        except Exception as exc:
            self._show_message(f"Job status unavailable: {exc}. Jobs remain tracked; checking again next tick.")
            return
        still_active: list[str] = []
        terminal_notes: list[str] = []
        # v0.4.13 addendum ITEM 2 (§3.3-g): the RUNNING identity observed
        # on THIS tick's pre-drain pairing (at most one under the serial
        # executor); None on idle/terminal-only ticks.
        running_now: str | None = None
        # v0.4.5 (§2 Rule 4): the rendered job-set key — the leading
        # job id — force-pins the log tail once when it changes (new
        # batch start; a claim rebase rides the leading id since the
        # head slot never changes for the batch's lifetime).
        # v0.4.10-fix (D2): the rendered job-set key rides the STABLE
        # slot order (a drained terminal job must never rebase the
        # log tail mid-batch).
        rendered_key = self._batch_slots[0] if self._batch_slots else None
        if rendered_key != self._log_rendered_key:
            self._log_rendered_key = rendered_key
            if rendered_key is not None:
                self._log_force_pin_next = True
        for index, job_id in enumerate(self.job_ids):
            snapshot = snapshots[index] if index < len(snapshots) else None
            if snapshot is not None and snapshot.get("job_id", job_id) != job_id:
                snapshot = None
            # v0.4.12 (item 1): the cell row comes from the STABLE
            # job_id->row map seeded at start, never from this loop index —
            # terminal jobs drain from self.job_ids every tick, so surviving
            # jobs shift down and an index-keyed write lands on the wrong
            # physical row (the v0.4.11 escape). A snapshot whose id is
            # missing from the map is dropped from cell writes; the BAR
            # accounting still keys it via _batch_slot_for (which may
            # legitimately claim a late slate slot), so nothing is invented
            # and nothing is lost there.
            row = self._batch_row_for_job.get(job_id) if isinstance(job_id, str) else None
            if snapshot is None:
                still_active.append(job_id)
                continue
            state = snapshot.get("state") or "queued"
            stage = run_stage_label(snapshot)
            if row is not None and row < self.file_table.rowCount():
                status_cell = self.file_table.item(row, 0)
                if status_cell is not None:
                    # v0.4.9 (§2.3): a RUNNING row whose last activity is
                    # older than the threshold renders the honest warning
                    # (the same yellow family as `cancelled`; data, never
                    # an action). BELOW the threshold the plain state —
                    # byte-identical to v0.4.8.
                    if state == "running":
                        age = snapshot.get("activity_age_seconds")
                        if (
                            isinstance(age, (int, float))
                            and age > STALE_ACTIVITY_SECONDS
                        ):
                            minutes = int(age // 60)
                            status_cell.setText(
                                f"running (no activity {minutes} min)"
                            )
                            status_cell.setForeground(
                                Qt.GlobalColor.yellow
                            )
                        else:
                            status_cell.setText(state)
                    else:
                        status_cell.setText(state)
            terminal = state in ("succeeded", "failed", "cancelled")
            if not terminal:
                still_active.append(job_id)
                if state == "running":
                    # v0.4.13 §3.3-g: running_now keys on the PRE-drain
                    # pairing (this loop IS the pre-drain pairing), since
                    # the just-claimed job is present here and the drain
                    # assignment below would misalign ids to snapshots.
                    if isinstance(job_id, str):
                        running_now = job_id  # at most one (serial executor)
                    self._consume_running_snapshot(row, snapshot, stage)
            else:
                self._consume_terminal_snapshot(row, snapshot)
                terminal_notes.append(
                    f"{Path(snapshot.get('file_path') or '?').name}: {state}"
                )
        self.job_ids = still_active if still_active else []
        # v0.4.15.4 (F-13c): "Cancel Current" re-syncs on the
        # running-truth EDGE only (a claim enabled it / a terminal land
        # disabled it) — never every tick. Reading the button's state
        # keeps the bare consume methods resync-free (they are
        # seam-bound in tests without the widget set).
        running_truth = self._running_job_id is not None and bool(
            self.job_ids
        )
        if self.cancel_button.isEnabled() != running_truth:
            self._sync_run_controls()
        # v0.4.13 addendum ITEM 2 (§3.3-g): the follow logic lives HERE
        # (evented on poll data — a selection-side hook could never
        # distinguish a fresh claim from a stale read without this tick's
        # snapshots). running_now was captured on the PRE-drain pairing;
        # the edge check runs against _follow_job_id now.
        self._follow_claim_edge(running_now)
        if self._run_started_at is not None and not self.job_ids:
            # The batch actually finished (every job reached a terminal
            # state and was drained above). Running this on every tick
            # would stop the liveness timer on the FIRST tick while
            # jobs are still queued/running — the run panel would
            # freeze at "queued" forever (the v0.4.4 validation D1
            # repro: timer dead, rows never update).
            self._batch_finished(terminal_notes)
            # v0.4.17 (fail-path, Eric's probe-failure repro): the
            # selected row's details panel must land on the terminal
            # truth too — the old code skipped the per-tick detail
            # render on the batch-end tick, claiming the terminal state
            # was "just written" by _batch_finished, but that path
            # writes the summary/batch-bar/table only (NEVER the
            # details panel). A single-job batch that failed at probe
            # therefore kept the last running live overlay
            # ("State: running — probe ...") over a finished batch,
            # with no later tick to repair it (the elapsed timer stops
            # once job_ids drains). The snapshot read below is the same
            # registry read the tick already does — a terminal record
            # re-renders honestly (failed + the payload's error), a
            # mid-transition read is tolerated by the render's shape.
            self._render_selected_detail_on_tick()
            return
        # §3.3-f: the detail LIVE-updates per tick for the SELECTED row —
        # riding this same poll (one extra status_snapshot per second, the
        # identical read the tick already does elsewhere — R3: cheap). This
        # covers BOTH follow selections and manual overrides, with no
        # second timer.
        self._render_selected_detail_on_tick()

    def _follow_claim_edge(self, running_now: str | None) -> None:
        """§3.2 CLAIM edge: a running observation whose IDENTITY differs
        from _follow_job_id (an EDGE, not a level — a level would fight
        R-B's user override by re-selecting every tick). On an edge:
        following re-engages, the identity is recorded, and the row is
        programmatically selected THROUGH the guard so itemSelectionChanged
        never misreads our own write as a user override (§3.4). Nothing
        running (idle gap, §3.3-b) or a stale id is a TICK — no writes."""
        if running_now is None or running_now == self._follow_job_id:
            return
        self._follow_job_id = running_now
        self._follow_mode = True
        row = self._batch_row_for_job.get(running_now)
        if row is None:
            return
        self._follow_select_row(row)
        self._render_detail_for_row(row)

    def _follow_select_row(self, row: int) -> None:
        """§3.4: the ONLY sanctioned programmatic selection writer. The
        guard flag is synchronous-safe because selectRow delivers the
        itemSelectionChanged signal before returning in Qt6; blockSignals
        is rejected (it also swallows selection-model maintenance Qt
        relies on). Keep default item flags — the guard flag, not item
        flags, carries the programmatic/user distinction."""
        self._programmatic_select = True
        try:
            self.file_table.selectRow(row)
        finally:
            self._programmatic_select = False

    def _render_selected_detail_on_tick(self) -> None:
        """§3.3-f: re-render the selected row's detail (plan body + live
        overlay) on poll data. Compare-before-write lives in
        _set_details_text — an unchanged running job costs nothing and
        never scrolls the panel.

        The "is this a live batch" gate keys on the seeded row map
        (_batch_row_for_job), NOT on ``self.batch``: since v0.4.14 a
        subs slate is plan-less (``self.batch is None``) while the map
        stays live — keying on the batch made the per-tick render
        Mode-A-only (Mode B got its detail updates only from the claim
        edge and user selections), so a Mode-B batch never landed its
        terminal detail truth. The pending-plan state (batch None, map
        cleared by _render_pending_files) still resolves to "no live
        slate" through the same key."""
        if not self._batch_row_for_job:
            return
        rows = self.file_table.selectionModel().selectedRows()
        if not rows:
            return
        self._render_detail_for_row(rows[0].row())

    def _consume_running_snapshot(self, row: int | None, snapshot: dict, stage: str) -> None:
        # v0.4.15.4 (F-13c): the window tracks the ACTUALLY-running job
        # id — the 1 Hz drain delivers exactly these snapshots — so
        # "Cancel Current" can mean CURRENT. Edge-keyed: a running tick
        # with an unchanged id is a level, not a claim.
        running_id = snapshot.get("job_id")
        if (
            snapshot.get("state") == "running"
            and isinstance(running_id, str)
            and running_id
            and running_id != self._running_job_id
        ):
            self._running_job_id = running_id
        # §3b: the phase-change busy/reset keys on the stage's PHASE
        # FACTS (the label renders progress detail per tick — heartbeat
        # seconds tick inside one and the same stage — so comparing
        # rendered label strings would busy-reset every second). The
        # reset runs BEFORE the eta assignment below (a new stage's
        # live ETA starts empty per the sibling rule); the busy label
        # for the count pass is the Measuring label (§3c state 1).
        key = _stage_label_text(stage)
        # v0.4.12 (item 2): the ETA window keys on the PHASE (the first
        # word of the stage segment — upscale/encode/verify...), never
        # the sub-stage label: chunk labels ("upscaling chunk 3/12")
        # change per chunk, and resetting on them would wipe the EWMA
        # exactly at the boundaries where its samples arrive.
        phase_key = key.split(" ", 1)[0]
        if self._stage_now != key:
            self._stage_eta = None
            self._stage_fraction = None
            if self._eta_phase_now != phase_key:
                # the EWMA window resets at every PHASE transition (a new
                # phase starts with no samples: ETA digits reappear only
                # after >=2 fresh chunk samples).
                self._eta_state = {}
                self._eta_rendered = None
                self._eta_phase_now = phase_key
            self._stage_now = key
            if PREPARED_COUNT_PROBE_STEP in stage:
                self.stage_bar.set_busy(True, _BUSY_MEASURING_LABEL)
            elif key.startswith("starting") or key == "queued":
                self.stage_bar.set_busy(True, "starting ...")
            elif "upscaling" in key:
                self.stage_bar.set_busy(True, "upscaling ...")
            else:
                self.stage_bar.set_busy(True, "estimating ...")
        eta = snapshot.get("eta_seconds")
        if eta is not None:
            # v0.4.13 addendum (CONFIRMED-defect fix, design R6/T6): the
            # payload contract says None-or-float, but a future shape
            # drift must degrade to "no ETA digits", never kill the
            # heartbeat mid-tick.
            try:
                self._stage_eta = float(eta)
            except (TypeError, ValueError):
                self._stage_eta = None
        progress = snapshot.get("progress") or {}
        # v0.4.9 (progress mirror, design §2.1): the fallback arm —
        # when the in-memory state_box is absent (reader process ≠
        # writer process), the payload's progress falls back to the
        # writer's PERSISTED progress_snapshot ("progress" is the
        # mirror's payload key set by tools.py). The state-box arm is
        # unchanged v0.4.8 rendering.
        if not progress:
            snapshot_progress = snapshot.get("progress")
            if isinstance(snapshot_progress, dict):
                progress = snapshot_progress
        # v0.4.16 (model management, ADR-4/G-11): the pill keys on the
        # SAME tick's snapshot — an in-run download counts bytes on the
        # title stack via the existing 1 Hz poll (no second timer). The
        # guard keeps the bare harnesses (which bind this consume method
        # without the pill widgets) untouched.
        if getattr(self, "subs_model_pill", None) is not None:
            self._refresh_subs_model_pill(
                live_dl=progress.get("model_download")
            )
        frames_done = progress.get("frames_done")
        frames_total = progress.get("frames_total")
        phase_now = snapshot.get("current_phase") or ""
        # v0.4.15.3 (F-4): frame counters belong to the UPSCALE phase
        # only — run.py merges (never replaces) progress dicts, so
        # upscale's counters ride every later phase; honoring them
        # outside upscale pins the stage bar at a stale 100% for the
        # whole encode/verify. Empty phase (older/stub writers): fall
        # back to the stage-label text so legacy upscale payloads still
        # count.
        frame_phase = phase_now == "upscale" or (
            not phase_now and "upscaling" in key
        )
        # v0.4.5 (§3b/§3c, D-eta): the stage label's phase_note suffix
        # rides the payload; the same computed string goes through
        # _set_text, so identical values never repaint the label.
        note = progress.get("phase_note")
        rendered = f"{stage} ({note})" if note else stage
        # v0.4.12 (item 2): the honest per-file ETA suffix — computed from
        # the phase-aware `_eta_current_job` (chunk-boundary EWMA in
        # upscale, encode-seconds rate in encode); verify/finalize get NO
        # digits (their elapsed rendering rides the existing heartbeat
        # seconds in the label). The damped value rides the suffix.
        eta = self._eta_current_job(snapshot)
        eta_suffix = self._eta_suffix_for_stage(eta)
        # v0.4.9 (§2.3): the stale-activity suffix rides the stage
        # label (data-never-proof wording; the bar logic is untouched).
        age = snapshot.get("activity_age_seconds")
        if (
            isinstance(age, (int, float))
            and age > STALE_ACTIVITY_SECONDS
            and snapshot.get("state") == "running"
        ):
            rendered = (
                f"{rendered} — last activity {int(age // 60)} min ago"
            )
        self._set_text(self.stage_label, rendered + eta_suffix)
        if frame_phase and frames_total and frames_done is not None and frames_total > 0:
            self._stage_fraction = max(0.0, min(1.0, frames_done / frames_total))
            # v0.4.15.3 (F-4): the measured floor rides every real
            # measurement — the overall bar's monotonic truth.
            self._stage_fraction_measured = self._stage_fraction
            # §3c: honest ETA the moment it exists. The bridge's
            # _eta_seconds math is UNTOUCHED; it still requires a
            # measured benchmark constant AND frames_total > 0. When
            # eta_seconds is still None but frames are flowing, the
            # bar leaves busy mode with the measured fraction only —
            # no number invented.
            self.stage_bar.set_value(self._stage_fraction, self._stage_eta)
        elif self._stage_fraction is None:
            # v0.4.16 (model management, F2): a byte-KNOWN download beat
            # renders determinately on the stage bar for this tick (the
            # set_value path exits busy; EtaProgressBar set_value). The
            # render is stage-bar-only — the overall bar's F-4 measured
            # floor is deliberately NOT keyed on download bytes (the
            # transcribe unit's batch accounting is unchanged).
            dl_beat = progress.get("model_download")
            dl_total = dl_beat.get("bytes_total") if isinstance(dl_beat, dict) else None
            dl_done = dl_beat.get("bytes_done") if isinstance(dl_beat, dict) else None
            if (
                phase_now in ("transcribe", "subtitles")
                and isinstance(dl_total, (int, float))
                and dl_total > 0
                and isinstance(dl_done, (int, float))
                and dl_done >= 0
            ):
                self.stage_bar.set_value(
                    max(0.0, min(1.0, dl_done / dl_total)), None
                )
            else:
                # §3c state 1, v0.4.15.3 (F-4): the pulse NAMES the phase —
                # only the upscale count pass "measures"; every other
                # unmeasurable phase names ITSELF (a duplicated busy call
                # is a no-op anyway). v0.4.16: a total-None download beat
                # stays honest-indeterminate here (the stage label names
                # the download via run_stage_label).
                self.stage_bar.set_busy(
                    True, _PHASE_BUSY_LABELS.get(phase_now, "estimating ...")
                )
        # Overall queue: resolved jobs + current fraction, over total.
        # v0.4.10 (R2): resolved = reached ANY terminal state (the
        # queue position is honest even when a job cancelled/failed);
        # the headline batch BAR renders the colored succeeded-
        # dominated truth separately (_apply_batch_summary).
        resolved_units = sum(1 for c in self.run_counts if c is not None)
        # v0.4.15.3 (F-4): the overall bar renders last-MEASURED truth
        # (monotonic, never fabricated); the stage bar renders the
        # CURRENT phase — measured fraction when measurable, honest
        # busy pulse when not. Without the measured floor the overall
        # bar would visibly RECEDE at every phase change (encode/verify
        # own no fraction).
        frac_units = (
            self._stage_fraction
            if self._stage_fraction is not None
            else self._stage_fraction_measured
        )
        total_units = max(1, self.run_total)
        overall = (resolved_units + frac_units) / total_units
        self.overall_bar.set_value(overall, None)
        # Log tail: the runner's own log (the bridge's _log_tail lives
        # inside the snapshot; render it verbatim).
        tail = snapshot.get("log_tail") or []
        if tail:
            self._render_log_tail("\n".join(str(line) for line in tail[-40:]))

    def _batch_slot_for(self, job_id: object, row: int) -> int:
        """v0.4.10-fix (D2): the stable batch slot for a job identity.

        Terminal jobs DRAIN from ``self.job_ids`` every poll tick, so
        the CURRENT row of a surviving job shifts down and a row-
        indexed truth write would overwrite the wrong state (the D2
        under-count: a 2-job all-succeeded sequential batch stuck at
        1/2, a cancelled job vanishing into a green bar). Slots are
        pinned per batch in submission order: a job_id registered in
        ``_batch_slot_index`` always maps back to THAT slot; an
        unregistered id claims the next slot at-or-after its current
        row that no other job already holds (this also heals a slate
        that missed its head seeding); a stray id whose row lies
        outside the slate resolves out of range so the writer drops
        it — the slate can never be expanded by a late payload. An
        identity-less payload keeps the legacy row write (the bare-
        seam behavior).
        """
        if isinstance(job_id, str) and job_id:
            index = self._batch_slot_index.get(job_id)
            if index is None:
                total = len(self.run_counts)
                claimed = set(self._batch_slot_index.values())
                if 0 <= row < total:
                    index = next(
                        (
                            slot
                            for slot in range(row, total)
                            if slot not in claimed
                        ),
                        total,  # slate full: out of range → payload dropped
                    )
                else:
                    index = total  # out-of-slate row: never fabricated
                self._batch_slot_index[job_id] = index
            return index
        return row

    def _consume_terminal_snapshot(self, row: int | None, snapshot: dict) -> None:
        state = snapshot.get("state") or "finished"
        if state == "succeeded" and snapshot.get("engine_simulated") and snapshot.get("job_id"):
            self._degraded_jobs.add(snapshot["job_id"])
        # v0.4.10-fix (D2): the truth slate is keyed by the STABLE job
        # identity (submission order), never by the shifting table row:
        # terminal jobs drain from self.job_ids each tick, so surviving
        # jobs shift down and a row-indexed write would land in the
        # wrong slot (the under-count / cancelled-overwrite bug).
        # v0.4.12 (item 1): the CELL write keys on the same stable
        # identity — the row derives from `_batch_row_for_job` (seeded in
        # submission order == plan row order), not the draining index.
        # ONE behavior, one keying story for bar and cell alike.
        # v0.4.10 (R2): the bar's truth accounting. run_counts records
        # the terminal state per batch slot (submission order);
        # summarize_batch turns the slate into an immutable
        # BatchSummary (pure, unit-tested); _apply_batch_summary
        # paints the bar (value + QSS chunk color + honest format).
        # A cancelled/failed job advances the bar (a slot resolved)
        # but never raises a succeeded count — the bar can no longer
        # render a full-green "1/1 completed" for a cancelled run.
        job_id = snapshot.get("job_id")
        if isinstance(job_id, str) and job_id in self._batch_row_for_job:
            row = self._batch_row_for_job[job_id]
        slot = self._batch_slot_for(job_id, row if row is not None else -1)
        if job_id == self._running_job_id:
            # v0.4.15.4 (F-13c): the tracked running job reached a
            # terminal state — the button's truth expires with it
            # (mid-batch until the next claim; batch-end sync below
            # re-syncs regardless).
            self._running_job_id = None
        if 0 <= slot < len(self.run_counts):
            if self.run_counts[slot] is None:
                # v0.4.15.4 (N-1): a resolving job RETIRES its measured
                # floor. The floor is the RUNNING job's last honest
                # fraction; without this reset the finished job's 1.0
                # rides into the next job's probe window and the
                # overall bar inflates to (resolved+1)/total, then
                # visibly RECEDES at the next job's first measured
                # tick — the lie-class F-4 exists to kill (v0.4.15.3's
                # floor fixed phase-change recede; this line closes the
                # job-boundary recede the same design introduced).
                self._stage_fraction_measured = 0.0
            self.run_counts[slot] = state
        self.run_summary = summarize_batch(self.run_counts)
        self._apply_batch_summary(self.run_summary)
        if row is not None and row < self.file_table.rowCount():
            cell = self.file_table.item(row, 0)
            if cell is not None:
                color = {
                    "succeeded": Qt.GlobalColor.green,
                    "failed": Qt.GlobalColor.red,
                    "cancelled": Qt.GlobalColor.yellow,
                }.get(state)
                cell.setText(
                    "completed (no AI upscale)"
                    if state == "succeeded" and snapshot.get("engine_simulated")
                    else state
                )
                if color is not None:
                    cell.setForeground(color)

    # -- v0.4.10 (R2): the batch bar's truth rendering -------------------

    def _batch_bar_palette(self, summary: BatchSummary) -> str:
        """The bar's chunk color for the MIXED multi-job bar: the worst
        outcome so far names the bar (cancelled amber > failed red),
        succeeded-only stays green."""
        if summary.cancelled:
            return "#d8a93a"  # amber: something was cancelled
        if summary.failed:
            return "#c4605a"  # red: something failed
        return "#3d9865"  # green: all succeeded so far

    def _apply_batch_summary(self, summary: BatchSummary) -> None:
        """One writer for the batch bar: value, honest format string,
        and chunk color (green succeeded / amber cancelled / red /
        failed). No Qt paint churn on identical values."""
        total = max(1, self.run_total)
        self.progress.setValue(min(summary.resolved, total))
        self.progress.setFormat(_batch_format(summary))
        if getattr(self, "_system_colours", False):
            self.progress.setStyleSheet("")
            return
        color = self._batch_bar_palette(summary)
        if color != self._progress_chunk_color:
            self._progress_chunk_color = color
            self.progress.setStyleSheet(
                f"QProgressBar::chunk {{ background: {color}; border-radius: 5px; }}"
            )

    def _set_text(self, label: QLabel, text: str) -> None:
        """setText only on change: the 1 Hz pass then costs nothing on
        identical values (no per-second repaint churn)."""
        if label.text() != text:
            label.setText(text)

    # -- v0.4.5 log tail: pin-to-tail with user-scroll override (§2) --

    def _log_tail_user_at_bottom(self) -> bool:
        """Rule 3's tolerance read: within _LOG_PIN_TOLERANCE_PX of the
        scrollbar maximum counts as at-the-bottom. Read BEFORE any
        setPlainText — the document-swap re-fires range/valueChanged
        synchronously, so a read after the swap measures the NEW
        document, not where the user was."""
        scrollbar = self.run_log_tail.verticalScrollBar()
        return scrollbar.value() >= scrollbar.maximum() - _LOG_PIN_TOLERANCE_PX

    def _log_scroll_value_changed(self, value: int) -> None:
        """Track the user-scroll override: any upward move past the
        tolerance engages inspecting mode; back within tolerance (Rule
        3) disengages it. One writer — this hook — so the 1 Hz render
        never races the scrollbar."""
        scrollbar = self.run_log_tail.verticalScrollBar()
        at_bottom = value >= self._log_scroll_max - _LOG_PIN_TOLERANCE_PX
        self._log_user_scrolled_up = not at_bottom

    def _log_scroll_range_changed(self, _min: int, maximum: int) -> None:
        """Cache the maximum after each document swap. A grown range
        clears the stale override when the user is still visually at
        the tail (the pre-swap position now sits within tolerance of
        the new max); a range change beyond the tolerance from a
        settled viewport reads as scrolled-away only when the USER
        moved — handled by the next valueChanged, never guessed here.
        """
        self._log_scroll_max = maximum
        scrollbar = self.run_log_tail.verticalScrollBar()
        at_bottom = scrollbar.value() >= maximum - _LOG_PIN_TOLERANCE_PX
        if not at_bottom:
            return
        self._log_user_scrolled_up = False

    def _render_log_tail(self, text: str) -> None:
        """Rule 1-5 render (replaces the old _set_text_log on the tick):
        identical text never touches the document (Rule 5, the same
        setText-only-on-change discipline as labels); on a real change
        "was at bottom" is read BEFORE setPlainText (the §2 Qt
        subtlety), the text is swapped, and the pin decision is made
        from the pre-swap position. A pending force-pin (Rule 4, job
        change) wins over the user's inspecting mode exactly once; the
        pin clears after landing on tail and inspecting resumes with
        the next scroll-away."""
        if self.run_log_tail.toPlainText() == text:
            return
        was_pinned = self._log_tail_user_at_bottom() or not self._log_user_scrolled_up
        self.run_log_tail.setPlainText(text)
        if self._log_force_pin_next:
            self._log_force_pin_next = False
            self._log_user_scrolled_up = False
            self._pin_log_to_tail()
        elif was_pinned:
            self._pin_log_to_tail()

    def _pin_log_to_tail(self) -> None:
        scrollbar = self.run_log_tail.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # -- v0.4.12 (item 2): honest, phase-aware ETA -----------------------

    _ETA_EWMA_ALPHA = 1 / 8  # rolling window over the last ~8 chunks
    _ETA_DAMPING_FRACTION = 0.25  # ≤25% drift ⇒ keep the previous value

    def _eta_state_key(self, job_id: object) -> str:
        """One job's ETA state lives under its stable id (a batch has
        exactly one RUNNING job, but the state must not alias when a
        claim rebase renames the leading id)."""
        return str(job_id)

    def _reset_eta_for_phase(self, job_id: object) -> None:
        """Phase transition: the EWMA window starts empty (a new phase's
        rate has nothing to do with the previous one's); the damped
        render clears too."""
        self._eta_state.pop(self._eta_state_key(job_id), None)
        self._eta_rendered = None

    def _eta_current_job(self, snapshot: dict) -> float | None:
        """The current job's ETA for THIS tick, or None (honest: no
        number is invented).

        Phase-aware per the v0.4.12 design:
        - upscale: frames are the work. Rate = EWMA(Δframes/Δt) over
          chunk boundaries (α=1/8); ETA = (frames_total − frames_done)/
          rate. Needs >=2 chunk samples before any digits appear.
        - encode: output seconds are the work. rate = encode_seconds/Δt
          over the same EWMA; ETA = (duration − encode_seconds)/rate.
        - verify/finalize: NO ETA number ever (ffprobe −count_frames on
          a 200 GB file at 0% GPU — naive extrapolation lies badly);
          callers render elapsed time instead.
        - probe/count-pass/queued: unknown; existing honest markers.
        - engine_simulated stubs never pose as measurements.
        """
        if not snapshot or snapshot.get("state") not in ("running",):
            return None
        if snapshot.get("engine_simulated"):
            return None
        phase = snapshot.get("current_phase") or ""
        if phase in (
            "verify", "finalize", "probe", "subtitles", "done", "",
            # v0.4.14 (Mode B): transcription is segment-counted with an
            # unknown denominator and the subs-mux is a fast stream copy
            # — no honest rate exists, so NO ETA digits are invented
            # (elapsed-only labels cover both; the segments count rides
            # the stage label).
            "transcribe", "subs-mux",
        ):
            return None
        progress = snapshot.get("progress") or {}
        now = time.time()
        key = self._eta_state_key(snapshot.get("job_id"))
        alpha = self._ETA_EWMA_ALPHA

        if phase == "upscale":
            frames_done = progress.get("frames_done")
            frames_total = progress.get("frames_total")
            chunk_index = progress.get("chunk_index")
            if (
                not isinstance(frames_done, (int, float))
                or not isinstance(frames_total, (int, float))
                or frames_total <= 0
            ):
                return None
            state = self._eta_state.get(key) or {}
            if not isinstance(state, dict):
                state = {}
            # Key on CHUNK boundaries: a new chunk is a sample point. A
            # tick inside the same chunk refines nothing (Δframes vs
            # Δt inside one chunk is chunked engine-internal noise);
            # sample at boundaries only.
            prev_state = state.get("upscale") or {}
            prev_chunk = prev_state.get("chunk_index")
            if (
                not isinstance(chunk_index, int)
                or (prev_chunk is not None and chunk_index == prev_chunk)
            ):
                # no boundary: ETA stays the previous computation
                return prev_state.get("eta") if prev_state else None
            if prev_chunk is None:
                # first sample: seed, wait for the second
                self._eta_state[key] = {
                    "upscale": {
                        "chunk_index": chunk_index,
                        "frames_done": frames_done,
                        "t": now,
                        "eta": None,
                    }
                }
                return None
            prev_frames = prev_state.get("frames_done")
            prev_t = prev_state.get("t")
            d_frames = frames_done - prev_frames if prev_frames is not None else 0
            d_t = now - prev_t if prev_t is not None else 0.0
            if d_frames <= 0 or d_t <= 0:
                # no measurable progress between boundaries: keep the
                # previous rate (a stalled chunk has no honest ETA)
                self._eta_state[key] = {
                    "upscale": {
                        "chunk_index": chunk_index,
                        "frames_done": frames_done,
                        "t": now,
                        "eta": prev_state.get("eta"),
                        "rate": prev_state.get("rate"),
                    }
                }
                return prev_state.get("eta")
            rate_inst = d_frames / d_t  # frames per second
            rate_prev = prev_state.get("rate")
            rate = (
                rate_inst
                if rate_prev is None
                else rate_prev + alpha * (rate_inst - rate_prev)
            )
            remaining_frames = max(0.0, float(frames_total - frames_done))
            eta = remaining_frames / rate if rate > 0 else None
            self._eta_state[key] = {
                "upscale": {
                    "chunk_index": chunk_index,
                    "frames_done": frames_done,
                    "t": now,
                    "rate": rate,
                    "eta": eta,
                }
            }
            return eta

        if phase == "encode":
            encode_seconds = progress.get("encode_seconds")
            duration = self._duration_for_snapshot(snapshot)
            if (
                not isinstance(encode_seconds, (int, float))
                or encode_seconds < 0
                or duration is None
                or duration <= 0
            ):
                return None
            state = self._eta_state.get(key) or {}
            if not isinstance(state, dict):
                state = {}
            prev_state = state.get("encode") or {}
            prev_enc = prev_state.get("encode_seconds")
            prev_t = prev_state.get("t")
            if prev_enc is None:
                self._eta_state[key] = {
                    "encode": {"encode_seconds": encode_seconds, "t": now, "eta": None}
                }
                return None
            d_enc = encode_seconds - prev_enc
            d_t = now - prev_t if prev_t is not None else 0.0
            if d_enc <= 0 or d_t <= 0:
                self._eta_state[key] = {
                    "encode": {
                        "encode_seconds": encode_seconds,
                        "t": now,
                        "eta": prev_state.get("eta"),
                        "rate": prev_state.get("rate"),
                    }
                }
                return prev_state.get("eta")
            rate_inst = d_enc / d_t  # output seconds per wall second
            rate_prev = prev_state.get("rate")
            rate = (
                rate_inst
                if rate_prev is None
                else rate_prev + alpha * (rate_inst - rate_prev)
            )
            remaining_out = max(0.0, float(duration - encode_seconds))
            eta = remaining_out / rate if rate > 0 else None
            self._eta_state[key] = {
                "encode": {
                    "encode_seconds": encode_seconds,
                    "t": now,
                    "rate": rate,
                    "eta": eta,
                }
            }
            return eta
        return None

    def _duration_for_snapshot(self, snapshot: dict) -> float | None:
        """The plan-time probe duration for the running file (the encode
        phase's denominator; known from the plan's own probe, not a
        guess)."""
        plan = self.batch
        if plan is None:
            return None
        file_path = snapshot.get("file_path") or ""
        for item in plan.files:
            if str(item.report.path) == file_path:
                return item.report.duration_seconds
        return None

    def _render_damped_eta(
        self, new_eta: float | None, previous: float | None
    ) -> float | None:
        """Display damping (design §3): a new ETA within 25% of the last
        rendered one keeps the previous value (the number must not
        visibly jitter tick-to-tick); only a real change beyond the
        threshold updates the rendered value."""
        if new_eta is None:
            return previous if previous is not None else None
        if previous is None:
            return new_eta
        if previous > 0 and abs(new_eta - previous) / previous <= self._ETA_DAMPING_FRACTION:
            return previous
        return new_eta

    def _eta_suffix_for_stage(self, eta: float | None) -> str:
        """The stage-label suffix ` — ETA ~H:MM:SS` when computable; NO
        digits during verify/finalize (elapsed-only surfaces those
        phases); '' otherwise."""
        if eta is None or eta <= 0:
            return ""
        damped = self._render_damped_eta(eta, self._eta_rendered)
        self._eta_rendered = damped
        if damped is None:
            return ""
        return f" — ETA ~{self._fmt_eta(damped)}"

    def _fmt_eta(self, seconds: float) -> str:
        """ETA clock form H:MM:SS (or M:SS under an hour)."""
        m, s = divmod(max(0, int(seconds)), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _forecast_total_seconds(self) -> float | None:
        """v0.4.12 (item 2): the phase-aware batch ETA — the current
        job's REAL eta (from `_eta_current_job`: measured chunk-rate
        EWMA in upscale, encode-seconds rate in encode) plus each queued
        job's benchmark-cache estimate; queued jobs WITHOUT duration
        data contribute nothing and make the batch honestly unknown.

        Honest rules (unchanged from the previous forecast):
        - a stub-sourced benchmark constant never poses as a measurement;
        - no estimate at all ⇒ None (the UI keeps the honest "…" form).
        """
        if not self.job_ids:
            return None
        total: float | None = 0.0
        seen_estimate = False
        for index, job_id in enumerate(self.job_ids):
            snapshot = self.client.status_snapshot(job_id)
            if snapshot is None:
                continue
            state = snapshot.get("state")
            if state in ("succeeded", "failed", "cancelled"):
                continue
            if index == 0:
                # the CURRENT job: its real ETA when measurable
                # (chunk-rate/encode-rate); the measured benchmark
                # constant remains the honest fallback while no chunk
                # samples exist yet; a stub-sourced constant still reads
                # unknown via _benchmark_eta_for_job.
                current_eta = self._eta_current_job(snapshot)
                if current_eta is not None:
                    eta = self._render_damped_eta(
                        current_eta, self._batch_eta_rendered
                    )
                else:
                    # T6: tolerant conversion (was bare float(live_eta)).
                    eta = (
                        self._tolerant_live_eta(snapshot)
                        if snapshot.get("eta_seconds") is not None
                        else self._benchmark_eta_for_job(snapshot)
                    )
                if eta is not None:
                    total = (total or 0.0) + eta
                    seen_estimate = True
                continue
            plan_eta = self._benchmark_eta_for_job(snapshot)
            if plan_eta is not None:
                total = (total or 0.0) + float(plan_eta)
                seen_estimate = True
            else:
                # No measured constant for a job ahead: the honest
                # forecast is unknown (never a 20-minute guess).
                total = None
                break
        if not seen_estimate:
            return None
        return total

    def _tolerant_live_eta(self, snapshot: dict) -> float | None:
        """v0.4.13 addendum (T6): the ONE tolerant float(eta) conversion
        for the forecast/label paths — the payload contract is
        None-or-float, but shape drift degrades to 'no digits', never a
        raised ValueError out of the 1 Hz heartbeat."""
        live = snapshot.get("eta_seconds")
        if live is None:
            return None
        try:
            return float(live)
        except (TypeError, ValueError):
            return None

    def _benchmark_eta_for_job(self, snapshot: dict) -> float | None:
        """Benchmark-cache ETA for one queued ahead-of-us job, from the
        plan already computed by the GUI (honest: only measured
        constants; a stub-sourced constant is policy-forbidden here and
        reads as unknown)."""
        plan = self.batch
        if plan is None:
            return None
        file_path = snapshot.get("file_path") or ""
        matches = [
            item.eta_estimate
            for item in plan.files
            if str(item.report.path) == file_path and item.eta_estimate is not None
        ]
        if not matches:
            return None
        estimate = matches[0]
        if estimate.get("engine_simulated"):
            # A stub constant must never pose as an AI measurement in
            # the GUI's headline forecast (design §1.5/§8 G2).
            return None
        return estimate.get("eta_seconds")

    def _forecast_total(self) -> None:
        secs = self._forecast_total_seconds()
        # v0.4.12 (item 2): the batch label carries the honest unknown/
        # queued marker when some queued jobs lack estimates while the
        # current job HAS a real ETA (never a fabricated total).
        if secs is None:
            queued_unknown = self._unknown_queued_count()
            if queued_unknown and self._batch_running_eta() is not None:
                marker = (
                    f"{queued_unknown} queued (ETA unknown)"
                    if queued_unknown > 1
                    else "queued (ETA unknown)"
                )
                current = self._batch_running_eta()
                if current is not None:
                    self._set_text(
                        self.total_remaining,
                        f"Current ~{self._fmt_eta(current)} + {marker}",
                    )
                    if self._run_started_at is not None and self._stage_now != "done":
                        self._set_text(
                            self.total_pill,
                            f"Current ~{self._fmt_eta(current)} + {marker}",
                        )
                    return
            self._set_text(self.total_remaining, "Total remaining … (all steps)")
            self._set_text(self.total_pill, "")
            return
        self._set_text(
            self.total_remaining,
            f"Total remaining ~{self._fmt_hm(secs)} (all steps)",
        )
        if self._run_started_at is not None and self._stage_now != "done":
            self._set_text(self.total_pill, f"Total remaining ~{self._fmt_hm(secs)}")

    def _unknown_queued_count(self) -> int:
        """Queued jobs (after the running one) with NO benchmark estimate
        — the honest 'unknown' marker's count."""
        unknown = 0
        for index, job_id in enumerate(self.job_ids):
            if index == 0:
                continue
            snapshot = self.client.status_snapshot(job_id)
            if snapshot is None:
                continue
            if snapshot.get("state") in ("succeeded", "failed", "cancelled"):
                continue
            if self._benchmark_eta_for_job(snapshot) is None:
                unknown += 1
        return unknown

    def _batch_running_eta(self) -> float | None:
        """The current job's REAL eta for the batch label (damped), or
        None when it is not measurable."""
        if not self.job_ids:
            return None
        snapshot = self.client.status_snapshot(self.job_ids[0])
        if snapshot is None:
            return None
        current = self._eta_current_job(snapshot)
        if current is None:
            if snapshot.get("eta_seconds") is not None:
                # v0.4.13 addendum (T6 arm 2): the wrapper reads the same
                # payload — the same "no digits" degrade as the tick path.
                current = self._tolerant_live_eta(snapshot)
        if current is None:
            return None
        return self._render_damped_eta(current, self._batch_eta_rendered)

    @staticmethod
    def _fmt_hm(seconds: "float | None") -> str:
        if seconds is None:
            return "…"
        m, s = divmod(max(0, int(seconds)), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    def _stop_liveness(self) -> None:
        self._elapsed_timer.stop()
        self.activity_dots.stop()
        if self._run_started_at is not None:
            secs = time.time() - self._run_started_at
            self.time_elapsed.setText(f"Total {self._fmt_hm(secs)}")
        self._run_started_at = None
        self._set_text(self.total_pill, "")
        self._set_text(self.total_remaining, "")

    def _settle_run_bars(self) -> None:
        """v0.4.17 (fail-path, Eric's probe-failure repro): the batch is
        over — the bars must stop animating. A busy pulse claims ACTIVE
        work, and a terminal batch has none: when the final job closed
        WITHOUT a measured fraction (a probe/prepare failure, a
        queued-only cancel, a Mode-B probe error) the pre-fix code left
        both bars in `set_busy(True)` pulse mode forever — the GUI kept
        animating "running" over a failed batch while the batch bar and
        the table read honestly. Determinate values hold their
        last-measured truth untouched (F-4: never recede, never
        fabricate) — only the pulse and its busy labels end; the next
        batch start resets the bars anyway (see _begin_job_slate)."""
        stale_labels = set(_PHASE_BUSY_LABELS.values()) | {
            "estimating ...",  # _consume_running_snapshot's fallback
            "starting ...",  # _begin_job_slate / queued
            "queue active ...",  # _begin_job_slate (overall bar)
            _BUSY_MEASURING_LABEL,  # also the upscale busy label above
        }
        for bar in (self.stage_bar, self.overall_bar):
            if bar._busy:
                bar.set_busy(False)
                if bar.eta_label.text() in stale_labels:
                    bar.eta_label.setText("")

    def _batch_finished(self, notes: list[str]) -> None:
        self._stop_liveness()
        # v0.4.17 (fail-path): the bars stop animating with the batch
        # (a finished batch never keeps an "active work" pulse running).
        self._settle_run_bars()
        # v0.4.12 (item 2): the batch is over — the ETA state and the
        # damped render values reset with it (a new batch must never
        # inherit the previous one's rate history).
        self._eta_state = {}
        self._eta_phase_now = None
        self._eta_rendered = None
        self._batch_eta_rendered = None
        # v0.4.12 (item 1, companion fix): the stage label must not outlive
        # the batch — the last job's mid-phase text ("subtitles — OCR/carry
        # (reassembly chunk 44/44 …)") persisted because nothing resets the
        # stage state after the final terminal drain. Clear the label and
        # reset the stage keys so the next batch starts clean.
        self._set_text(self.stage_label, "")
        self._stage_now = "queued"
        self._stage_eta = None
        self._stage_fraction = None
        # v0.4.15.3 (F-4): the batch is over — the measured floor goes
        # with it (a new batch must start its own monotonic truth).
        self._stage_fraction_measured = 0.0
        # v0.4.15.4 (F-13c): the batch is over — no tracked running job
        # survives a finished batch (the button's truth with it).
        self._running_job_id = None
        self.engine_combo.setEnabled(True)
        # v0.4.8 (D5): re-enable the profile combo with its honest tooltip.
        self.profile_combo.setEnabled(True)
        self.profile_combo.setToolTip(PROFILE_TOOLTIP)
        # v0.4.15: re-enable the denoise combo with its honest tooltip.
        self.denoise_combo.setEnabled(True)
        self.denoise_combo.setToolTip(DENOISE_TOOLTIP)
        self._render_recent_jobs()
        self._sync_run_controls()
        outcomes = getattr(self, "run_counts", []) or []
        summary = summarize_batch(outcomes)
        if summary.pending or not summary.total:
            self._set_summary("Batch ended; some outcomes are unknown", _batch_format(summary))
        elif "failed" in outcomes and "cancelled" in outcomes:
            self._set_summary("Batch finished with cancellations and failures", _batch_format(summarize_batch(outcomes)))
        elif "cancelled" in outcomes:
            self._set_summary("Batch cancelled", "Completed jobs are safe; see the row statuses.")
        elif "failed" in outcomes:
            self._set_summary(
                "Batch finished with failures",
                "See the selected row and .vidlift-runs logs for details.",
            )
        elif self._degraded_jobs:
            self._set_summary("Batch completed with no-AI output", f"{len(self._degraded_jobs)} file(s) completed without AI upscale. Review per-file details.")
        else:
            self._set_summary("Batch finished successfully", "Outputs are beside each source (or the chosen output folder).")

    # -- recent-history surface (design §1.9: one registry, two readers) --

    def _render_recent_jobs(self) -> None:
        selected_job = None
        if hasattr(self, "history_table"):
            selection = self.history_table.selectionModel()
            rows = selection.selectedRows() if selection is not None else []
            if rows:
                cell = self.history_table.item(rows[0].row(), 0)
                selected_job = cell.data(Qt.ItemDataRole.UserRole) if cell is not None else None
        try:
            history = self.client.history(limit=8)
        except Exception:
            history = []
        self._recent_rows = history
        if hasattr(self, "history_table"):
            blocked = self.history_table.blockSignals(True)
            self.history_table.setRowCount(len(history))
            for row, record in enumerate(history):
                for col, key in enumerate(("state", "file_path", "error")):
                    cell = QTableWidgetItem(str(record.get(key) or ""))
                    if col == 0:
                        cell.setData(Qt.ItemDataRole.UserRole, record.get("job_id"))
                    cell.setToolTip(cell.text())
                    self.history_table.setItem(row, col, cell)
            selected_row = next((index for index, record in enumerate(history)
                                 if selected_job is not None and record.get("job_id") == selected_job), None)
            if selected_row is not None:
                self.history_table.selectRow(selected_row)
            else:
                self.history_table.clearSelection()
            self.history_table.blockSignals(blocked)
            if self.queue_tabs.currentIndex() == 1:
                if selected_row is not None:
                    self._select_history()
                else:
                    self._set_details_text("RECENT JOB (read only)\n\nSelect a recorded job.")

    def _select_history(self) -> None:
        rows = self.history_table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self._recent_rows):
            record = self._recent_rows[rows[0].row()]
            self._set_details_text("RECENT JOB (read only)\n\n" + "\n".join(
                f"{key}: {value}" for key, value in record.items() if value is not None))

    def _queue_tab_changed(self, index: int) -> None:
        self._set_details_text("Select a recorded job." if index == 1 else "Select a current video row.")
        if index == 1:
            self._select_history()
        else:
            self._render_detail_for_row()

    def _selected_record(self) -> dict:
        if self.queue_tabs.currentIndex() == 1:
            rows = self.history_table.selectionModel().selectedRows()
            return self._recent_rows[rows[0].row()] if rows and rows[0].row() < len(self._recent_rows) else {}
        rows = self.file_table.selectionModel().selectedRows()
        if not rows:
            return {}
        row = rows[0].row()
        job = next((job for job, position in self._batch_row_for_job.items() if position == row), None)
        if job is not None:
            try:
                return self.client.status_snapshot(job) or {}
            except Exception:
                return {}
        item = self.file_table.item(row, 1)
        return {"file_path": item.text()} if item else {}

    def _copy_source_path(self) -> None:
        source = self._selected_record().get("file_path")
        if source:
            QApplication.clipboard().setText(str(source))

    def _copy_table_cells(self, table: QTableWidget) -> None:
        selected = table.selectedIndexes()
        rows = sorted({index.row() for index in selected})
        columns = sorted({index.column() for index in selected})
        indices = {(index.row(), index.column()) for index in selected}
        if indices:
            QApplication.clipboard().setText("\n".join("\t".join(
                table.item(row, col).text() if (row, col) in indices and table.item(row, col) else ""
                for col in columns) for row in rows))

    def _copy_output_path(self) -> None:
        output = self._selected_record().get("output_path")
        if output:
            QApplication.clipboard().setText(str(output))
        else:
            self._show_message("No recorded output path is available. A planned destination is not a finished output.")

    def _open_recorded_path(self, key: str) -> None:
        # A plan is not proof that an output exists. No shell interpolation.
        raw = self._selected_record().get(key)
        if not raw and self.queue_tabs.currentIndex() == 1:
            job = self._selected_record().get("job_id")
            if job:
                try:
                    raw = (self.client.status_snapshot(job) or {}).get(key)
                except Exception as exc:
                    self._show_message(f"Recorded job details are unavailable: {exc}")
                    return
        try:
            path = Path(raw).resolve() if raw and Path(raw).is_file() else None
        except (OSError, RuntimeError) as exc:
            self._show_message(f"Cannot access the recorded file: {exc}")
            return
        if path is None:
            self._show_message("No existing recorded file is available for this action.")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent if key == "output_path" else path))):
            self._show_message("The system could not open this local path.")

    def _stage_subtitle_drop(self, paths: list[str]) -> None:
        self._request_ingestion(paths, subtitles=True)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        """Cancel through the unified path, then give the worker the
        same 30 s ffmpeg terminate window the sibling validated; a
        QThread/worker that ignores cancel for 30 s is the documented
        last-resort terminate case."""
        if self._subs_submitting or self._upscale_submitting:
            event.ignore()
            self._show_message("Finish or dismiss the submission review before closing.")
            return
        if getattr(self, "_subs_model_installing", False):
            event.ignore()
            self._show_message("Subtitle model installation is still running. Wait for it to finish before closing.")
            return
        had_active = bool(self.job_ids) or self.client.active_job_count() > 0
        if had_active:
            answer = QMessageBox.question(
                self,
                "Run in progress",
                "Submitted video/subtitle jobs are queued or running. Closing requests a stop. "
                "Unsent staged files will be discarded, not deleted. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for job_id in list(self.job_ids):
                self.client.cancel(job_id)
            # Wait (bounded) for the registry to reach terminal states.
            deadline = time.monotonic() + _CLOSE_WAIT_RUN_MS / 1000
            while self.job_ids and time.monotonic() < deadline:
                QApplication.processEvents()
                snapshots = self.client.enqueue_snapshots(self.job_ids)
                if all(
                    s is None or (s.get("state") in ("succeeded", "failed", "cancelled"))
                    for s in snapshots
                ):
                    break
                time.sleep(0.05)
        elif self._staged_subs or self.files:
            if QMessageBox.question(
                    self, "Unsent video files", "Discard unsent staging and close? No media will be deleted.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._settings().setValue("ui/window_geometry", self.saveGeometry())
        self._settings().setValue("ui/details_splitter", self.details_splitter.saveState())
        self._ui_closed = True
        for _inputs, cancel, _signals in self._preflights.values():
            cancel.set()
        self.client.stop()
        # v0.4.10 (R1): stop the GPU meter's cadence before teardown —
        # no probe runs past app close; a hung child dies at its own
        # 2 s timeout and the pool's daemon-ish thread ends with the
        # process.
        self.gpu_meter.stop()
        # v0.4.6 (§4.5): stop the LAN monitor beside the worker stop —
        # the sibling never leaves a listener after close. The pill is
        # refreshed too: a close-then-inspect window never shows a
        # stale "Monitor on".
        stop_server()
        self.monitor_port_reported = None
        self._refresh_monitor_pill()
        super().closeEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        self._request_ingestion(paths, subtitles=self.workflow_tabs.currentWidget() is self.subs_setup_scroll)
        event.acceptProposedAction()


def main(argv: list[str] | None = None) -> int:
    # QSettings canonical path BEFORE MainWindow (design §2 rule for
    # run_gui.py applies to every entry: direct `python -m` runs too).
    QApplication.setApplicationName("VidLift")
    QApplication.setOrganizationName(os.environ.get("VIDLIFT_SETTINGS_ORG") or "YetiWurks")

    def _shutdown() -> None:
        from .monitor import stop_server as _stop_monitor

        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, MainWindow):
                try:
                    widget.client.stop()
                except Exception:
                    pass
        # v0.4.6 (§4.5): the monitor never outlives the app (covers
        # teardown paths that bypass closeEvent, e.g. aboutToQuit).
        _stop_monitor()

    app = QApplication(list(argv or sys.argv[1:]))
    app.aboutToQuit.connect(_shutdown)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())