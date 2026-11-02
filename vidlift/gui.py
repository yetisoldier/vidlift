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

import sys
import time
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QTimer
from PyQt6.QtGui import QAction, QDragEnterEvent, QDropEvent, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
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
    QSETTINGS_MONITOR_KEYS,
    DENOISE_DEFAULT,
    QSETTINGS_DENOISE_KEYS,
)
from .mcp_bridge.client_local import (
    BatchTooLarge,
    LocalBridgeClient,
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
from .run import MissingToolError, find_tools
from .run import summarize_batch, BatchSummary
from .ui.gpu_meter import (
    GPU_METER_HEIGHT,
    GPU_METER_WIDTH,
    GpuMeterWidget,
)
from .ui.widgets import ActivityDots, EtaProgressBar


VIDEO_EXTENSIONS = {
    ".avi", ".divx", ".flv", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".ts", ".vob", ".wmv",
}

# v0.4.4 GUI safety gate (design §5): folder-add refuses absurd
# single-shot selections before planning; per-click Start enqueues at
# most MAX_GUI_BATCH job records (client_local also enforces).
MAX_FILES = 500

# v0.4.5 (progress-honesty design §2, D-tail): pin-to-tail tolerance
# for the run log widget. Within this many pixels of the scrollbar's
# maximum the log is "at the bottom" and stays pinned to the tail;
# farther up counts as user-inspecting and disables the pin.
_LOG_PIN_TOLERANCE_PX = 8

# v0.4.5 (§3c, D-eta): the honest busy label while the count pass is
# still producing the denominator — it names what is being measured,
# never a fabricated percent.
_BUSY_MEASURING_LABEL = "measuring — counting frames…"

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

PLAN_HELPER_TEXT = "1. Add files → 2. Plan → 3. Start. Planning never writes to disk."


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
        f"Video: HEVC MP4, {item.output_width}x{item.output_height}, {item.video_bitrate_kbps}k",
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
    if frames_done is not None and frames_total:
        chunk_text = f" frames {frames_done}/{frames_total}"
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
    if note:
        if isinstance(note, str) and note.startswith(base):
            # the note already carries the base render (the engine's
            # chunk notes embed the chunk label); render it once.
            return note
        return f"{base} ({note})"
    if step and not (
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
        return f"{base} ({step})"
    return base


class MainWindow(QMainWindow):
    def __init__(self, *, client: LocalBridgeClient | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"VidLift {__version__}")
        self.resize(1180, 760)
        self.setAcceptDrops(True)
        self.files: list[str] = []
        self.batch: BatchPlan | None = None

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
        self.monitor_check = QCheckBox(
            "LAN monitoring (agents check upscale progress from other machines)"
        )
        self.monitor_pill = QLabel("")
        self.monitor_pill.setObjectName("VersionBadge")
        self.monitor_port_reported: int | None = None

        # Sibling port: the 1 Hz single-writer liveness timer.
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._tick_elapsed)
        self._run_started_at: float | None = None
        self._stage_now = "queued"
        self._stage_eta: float | None = None
        self._stage_fraction: float | None = None

        self.file_table = QTableWidget(0, 8)
        self.file_table.setHorizontalHeaderLabels(
            ["Status", "File", "Source", "Target", "Output", "Size", "Subtitles", "Warnings"]
        )
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.verticalHeader().hide()
        header = self.file_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.file_table.itemSelectionChanged.connect(self._sync_details)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Add video files, then run Plan Batch.")

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
            "finished 720p MP4 into its original path.\n\n"
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
        self.transcribe_subs = QCheckBox(
            "Add subtitles by transcription when none exist"
        )
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
        self.plan_button.clicked.connect(self.plan_batch)
        self.start_button = QPushButton("Start Upscale")
        self.start_button.setObjectName("StartButton")
        self.start_button.setEnabled(False)
        self.start_button.setToolTip(
            "Enqueue every ready file as a tracked job. A running engine choice cannot change; cancel to change it."
        )
        self.start_button.clicked.connect(self.start_upscale)
        self.cancel_button = QPushButton("Cancel Current")
        self.cancel_button.setEnabled(False)
        self.cancel_button.setToolTip("Stop the running job safely; queued jobs stay queued.")
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
        self.add_subs_button = QPushButton("Add Subtitles…")
        self.add_subs_button.setEnabled(True)
        self.add_subs_button.setToolTip(
            "Add subtitles by transcribing each file's own audio — without "
            "re-encoding the video.\n\n"
            "The output lands beside each source as <name>.subs.mp4 plus a "
            ".srt sidecar; the source is never modified. Existing videos at "
            "any resolution are welcome. First use downloads a ~1.5 GB model "
            "once."
        )
        self.add_subs_button.clicked.connect(self.choose_subs_files)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.hide()

        add_action = QAction("Add files", self)
        add_action.triggered.connect(self.add_files)
        self.addAction(add_action)

        self.setCentralWidget(self._build_shell())
        self._apply_style()
        self._set_summary("No files planned yet", "Drop videos here or choose Add Files.")
        self._render_recent_jobs()
        # v0.4.6 (§4.1): default-on start AFTER the shell exists — the
        # pill is built, so a fallback-port report can reach it.
        self._start_monitor()
        # v0.4.10 (R1): the meter starts after the shell exists too —
        # its first sample lands on a fully built top bar. With
        # nvidia-smi absent it simply renders "GPU n/a" every 0.5 s.
        self.gpu_meter.start()

    # -- engine persistence (design §7) ---------------------------------

    def _settings(self) -> QSettings:
        return QSettings("YetiWurks", "VidLift")

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
        empty token means local-only open."""
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
        return {
            "enabled": bool(enabled),
            "host": str(host or MONITOR_HOST_DEFAULT),
            "port": port,
            "token": str(token or ""),
        }

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
            self.monitor_pill.setText(f"Monitor off (port busy: {exc})")
            return
        if actual_port != monitor["port"]:
            self._settings().setValue(QSETTINGS_MONITOR_KEYS["port"], actual_port)
        self.monitor_port_reported = actual_port
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
            else:
                stop_server()
                self.monitor_port_reported = None
        except OSError as exc:
            self._settings().setValue(QSETTINGS_MONITOR_KEYS["enabled"], False)
            self.monitor_port_reported = None
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

    def _replace_source_changed(self, checked: bool) -> None:
        """Persist the replace-source preference (QSettings, house
        rule). The checkbox itself warns the user in its label; only
        the stored value changes here."""
        self._settings().setValue("replace_source", bool(checked))

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
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)

        title_row = QHBoxLayout()
        title = QLabel("VidLift")
        title.setObjectName("Title")
        title_stack = QVBoxLayout()
        title_stack.addWidget(title)
        title_stack.addWidget(self._version_pill(), alignment=Qt.AlignmentFlag.AlignLeft)
        title_stack.addWidget(self.monitor_pill, alignment=Qt.AlignmentFlag.AlignLeft)
        title_stack.addWidget(QLabel("Plan and run batch SD video upscales"))
        title_row.addLayout(title_stack)
        title_row.addStretch(1)
        self.summary_count = QLabel()
        self.summary_count.setObjectName("SummaryCount")
        self.summary_note = QLabel()
        self.summary_note.setObjectName("SummaryNote")
        self.total_pill = QLabel()
        self.total_pill.setObjectName("TotalPill")
        summary = QVBoxLayout()
        summary.addWidget(self.total_pill, alignment=Qt.AlignmentFlag.AlignRight)
        summary.addWidget(self.summary_count, alignment=Qt.AlignmentFlag.AlignRight)
        summary.addWidget(self.summary_note, alignment=Qt.AlignmentFlag.AlignRight)
        title_row.addLayout(summary)
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
        layout.addWidget(helper)

        controls = QFrame()
        controls.setObjectName("Controls")
        grid = QGridLayout(controls)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        add = QPushButton("Add Files")
        add.setToolTip("Choose one or more video files")
        add.clicked.connect(self.add_files)
        folder = QPushButton("Add Folder")
        folder.setToolTip("Add recognized videos from a folder")
        folder.clicked.connect(self.add_folder)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear_files)
        browse_output = QPushButton("Output Folder")
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
        # v0.4.9 (design §1.4): the resume checkbox rides row 2 beside
        # the profile combo (row 1 is full; the monitor span below
        # already narrowed for exactly this reason).
        grid.addWidget(self.resume_claim, 2, 1, 1, 2)
        # v0.4.8 (design D5): the profile combo lands on row 2 (the only
        # row 1 cells left are behind cancel_button at (1, 7)), so the
        # monitor span narrows 7 -> 5 columns to free (2, 5)/(2, 6).
        grid.addWidget(QLabel("Upscale profile"), 2, 5)
        grid.addWidget(self.profile_combo, 2, 6)
        grid.addWidget(self.monitor_check, 2, 0, 1, 5)
        self.monitor_check.toggled.connect(self._toggle_monitor)
        grid.addWidget(self.cancel_button, 1, 7)
        # v0.4.13 (design §4.1): Cancel Batch lands immediately right of
        # Cancel Current — column 8, row 1, was free (Start owns (0, 8)).
        grid.addWidget(self.cancel_batch_button, 1, 8)
        # v0.4.14 (Mode B): "Add Subtitles…" lands on row 2, column 7 —
        # the only free option-row cell (row 2 holds the monitor span
        # (0,5) over the resume checkbox (1,2) overlap, profile label
        # (2,5)/combo (2,6); (2,7) and (2,8) are the only untouched
        # cells). Row 2 reads as the options/actions row tail, directly
        # below the Cancel cluster's column.
        grid.addWidget(self.add_subs_button, 2, 7)
        # v0.4.15 (opt-in denoise): the three-state combo rides a new
        # options row (rows 0-2 are full) directly under the profile
        # combo, keeping the options/actions rows reading top-down as
        # one settings block. The label names the WHEN (before the
        # upscale); the tooltip carries the full guidance verbatim.
        grid.addWidget(QLabel(DENOISE_COMBO_LABEL), 3, 1)
        grid.addWidget(self.denoise_combo, 3, 2)
        grid.setColumnStretch(6, 1)
        layout.addWidget(controls)
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
        run_layout.addWidget(QLabel("Log (tail)"), 4, 0, 1, 3)
        run_layout.addWidget(self.run_log_tail, 5, 0, 1, 3)
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
        splitter.addWidget(self.file_table)
        splitter.addWidget(self.details)
        splitter.setSizes([760, 360])
        layout.addWidget(splitter, 1)
        return root

    def _version_pill(self) -> QLabel:
        """The second, persistent version surface (design §1.6/§4.2): a
        header pill beside the title, in addition to the title text."""
        pill = QLabel(f"v{__version__}")
        pill.setObjectName("VersionBadge")
        return pill

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #101214; color: #edf0ec; }
            QLabel { color: #d9ded7; }
            #Title { font-size: 34px; font-weight: 700; letter-spacing: 0; }
            #VersionBadge { background: #263036; border: 1px solid #3b4a52; border-radius: 9px; color: #b9c7ce; padding: 2px 8px; font-size: 11px; }
            #TotalPill { color: #8fd0a8; font-size: 13px; font-weight: 600; }
            #Muted { color: #98a196; font-size: 13px; }
            #StageLabel { color: #e8ddc0; font-size: 15px; }
            #RunPanel { background: #191d1f; border: 1px solid #2b3134; border-radius: 8px; }
            #Controls { background: #191d1f; border: 1px solid #2b3134; border-radius: 8px; }
            QPushButton { background: #242a2d; border: 1px solid #3a4246; border-radius: 6px; padding: 9px 14px; color: #edf0ec; }
            QPushButton:hover { background: #30383c; border-color: #657178; }
            QPushButton:pressed { background: #1b2022; }
            QPushButton:disabled { color: #758079; border-color: #353d41; }
            #PrimaryButton { background: #d8943a; border-color: #f0b45c; color: #15110a; font-weight: 700; }
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
            QSplitter::handle { background: #252c2f; width: 2px; }
            """
        )
        self.file_table.setAlternatingRowColors(True)
        fixed = QFont("JetBrains Mono")
        fixed.setStyleHint(QFont.StyleHint.Monospace)
        self.details.setFont(fixed)
        self.run_log_tail.setFont(fixed)

    def _set_summary(self, count: str, note: str) -> None:
        self.summary_count.setText(count)
        self.summary_note.setText(note)

    def _add_paths(self, paths: list[str]) -> None:
        added = 0
        known = set(self.files)
        for path in paths:
            clean = str(Path(path))
            if clean not in known:
                self.files.append(clean)
                known.add(clean)
                added += 1
        if added:
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
        self._set_summary(f"{len(self.files)} queued", "Run Plan Batch to inspect them.")
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
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", str(Path.home()))
        if not folder:
            return
        paths = [
            str(path)
            for path in sorted(Path(folder).iterdir())
            if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
        ]
        if len(paths) > MAX_FILES:
            QMessageBox.warning(
                self,
                "VidLift",
                f"That folder holds {len(paths)} videos; add at most {MAX_FILES} at once.",
            )
            return
        self._add_paths(paths)

    def choose_output_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose an output folder", str(Path.home()))
        if folder:
            self.output_dir.setText(folder)

    # -- Add Subtitles (v0.4.14 Mode B, no upscale) ---------------------

    def _subs_file_rows(self, paths: list[str]) -> list[list[str]]:
        """The subs table rows (same 8-column schema as the plan view;
        the Status cell carries the subs verdict instead of the upscale
        one) — the shared-table render contract for subs batches."""
        rows: list[list[str]] = []
        for path in paths:
            existing = Path(path[:-4] + ".subs.mp4")
            status = (
                "blocked — subtitles output already exists (will not be overwritten)"
                if existing.exists()
                else "subs ready"
            )
            rows.append(
                [
                    status,
                    path,
                    "transcribe",
                    "keep original",
                    path[:-4] + ".subs.mp4",
                    "",
                    "transcribed from the file's own audio",
                    (
                        "output exists: " + Path(path[:-4] + ".subs.mp4").name
                        + " (will not be overwritten)"
                        if existing.exists()
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
        # v0.4.15.1: the ONLY blocking condition — a live subs job over
        # the same source. Pure read over queue_views(); a client
        # without that surface (older scripted harnesses) yields no
        # conflict info and never blocks — independence-first, the
        # fail-open direction (the registry-side never-overwrite gate
        # at subs_job.py still holds the ground truth).
        live_subs_paths: frozenset[str] = frozenset()
        try:
            running, queued = self.client.queue_views()
        except AttributeError:
            running, queued = [], []
        live_subs_paths = self._active_subs_paths(running or [], queued or [])
        conflicts = [path for path in paths if path in live_subs_paths]
        if conflicts:
            shown = ", ".join(Path(path).name for path in conflicts[:5])
            more = f" (+{len(conflicts) - 5} more)" if len(conflicts) > 5 else ""
            QMessageBox.information(
                self, "VidLift",
                "Wait for the current batch to finish or cancel it first.\n"
                f"Already queued for subtitles: {shown}{more}",
            )
            return
        pending_ids: list[str] | None = None
        if self.job_ids:
            pending_ids = list(self.job_ids)
        try:
            fresh_ids = self.client.enqueue_subs(paths)
        except (BatchTooLarge, FileNotFoundError, BridgeError) as exc:
            QMessageBox.warning(self, "VidLift", str(exc))
            if pending_ids is not None:
                # the live batch's ids survive untouched — only stale
                # (unknown/terminal) window ids are dropped
                self.job_ids = MainWindow._live_window_ids(pending_ids, self.client)
            else:
                self.job_ids = []
            return
        if not fresh_ids:
            if pending_ids is not None:
                self.job_ids = pending_ids
            return
        # v0.4.15.1: the window tracks ENQUEUED-BY-THIS-WINDOW ids with
        # a LIVE (non-terminal, snapshot-provable) state — stale ids
        # (the Eric false-positive class) are dropped from tracking, a
        # concurrent submission EXTENDS the live list, and the poll
        # loop keeps draining terminal ids per tick.
        live_pending = MainWindow._live_window_ids(pending_ids or [], self.client)
        self.job_ids = live_pending + [str(job_id) for job_id in fresh_ids]
        # v0.4.14 (Mode B): a subs batch reuses the SHARED table — its
        # rows append after any prior batch's rows (the table is the
        # window's batch view; an old batch's terminal rows stay
        # readable above — clear_files empties the whole table).
        seed = self.file_table.rowCount()
        self.file_table.setRowCount(seed + len(paths))
        for offset, cells in enumerate(self._subs_file_rows(paths)):
            for column, text in enumerate(cells):
                self.file_table.setItem(
                    seed + offset, column, QTableWidgetItem(text)
                )
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
                self._batch_row_for_job[job_id] = len(live) + index
            self.run_summary = summarize_batch(self.run_counts)
        else:
            self._reset_batch_progress()
            self._begin_job_slate(self.job_ids, row_offset=seed)
        self._set_summary(
            f"{len(self._batch_slots)} queued for subtitles"
            if pending_ids is not None
            else f"{len(self.job_ids)} queued for subtitles",
            "Transcribing audio and soft-muxing subtitles (no upscale).",
        )

    def clear_files(self) -> None:
        if self.client.active_job_count() > 0:
            QMessageBox.information(self, "VidLift", "Wait for the current upscale to finish or cancel it first.")
            return
        self.files = []
        self.batch = None
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
        self._set_summary("No files planned yet", "Drop videos here or choose Add Files.")

    def plan_batch(self) -> None:
        if not self.files:
            QMessageBox.information(self, "VidLift", "Add one or more video files first.")
            return
        try:
            gbph = float(self.gbph.text())
        except ValueError:
            QMessageBox.warning(self, "VidLift", "GB/hour must be a number.")
            return
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
        if not reports:
            QMessageBox.warning(self, "VidLift", "No files could be probed.\n\n" + "\n".join(failures[:5]))
            return
        try:
            self.batch = build_batch_plan(
                reports,
                target="720p",
                gb_per_hour=gbph,
                output_dir=self.output_dir.text().strip() or None,
            )
        except Exception as exc:
            QMessageBox.critical(self, "VidLift planning failed", str(exc))
            return
        self.render_batch(self.batch)
        if failures:
            QMessageBox.warning(self, "VidLift", "Some files failed to probe.\n\n" + "\n".join(failures[:8]))

    def render_batch(self, batch: BatchPlan) -> None:
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
        if row is None:
            rows = self.file_table.selectionModel().selectedRows()
            if not rows:
                return
            row = rows[0].row()
        if self.batch is None:
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
            if not (0 <= row < len(self.batch.files)):
                return
            item = self.batch.files[row]
            text = detail_text(
                item,
                subtitle_ocr_enabled=self.subtitle_ocr.isChecked(),
                upscale_profile=self.profile_choice,
            )
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
            f"Job: {job_id}   State: {state} — {rendered}{eta_suffix}",
        ]
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
        if target and target != "keep original":
            lines.append(f"Resolution handling: {target}")
        if warning:
            lines.extend(["", "Warnings:", f"- {warning}"])
        return "\n".join(lines)

    def _set_details_text(self, text: str) -> None:
        """§3.3-f: the details QTextEdit has no change-guard — setPlainText
        resets the user's scroll. Compare-before-write so a paused/idle
        running job does not repaint-kick the panel every second."""
        if self.details.toPlainText() != text:
            self.details.setPlainText(text)

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
        self, job_ids: list[str], *, row_offset: int = 0
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
            job_id: row_offset + index
            for index, job_id in enumerate(self._batch_slots)
        }
        if len(self._batch_row_for_job) != len(self._batch_slots):
            raise RuntimeError(
                "batch row map seeding mismatch: "
                f"{len(self._batch_row_for_job)} rows for "
                f"{len(self._batch_slots)} job ids"
            )
        self.run_counts: list[str | None] = [None] * self.run_total
        self.run_summary = summarize_batch(self.run_counts)
        # v0.4.13 addendum (§3.2 BATCH_START): follow engages with
        # _follow_job_id=None so the FIRST claim is an identity edge.
        self._follow_mode = True
        self._follow_job_id = None
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

    def start_upscale(self) -> None:
        if self.client.active_job_count() > 0 or self.job_ids:
            return
        if self.batch is None:
            return
        items = [item for item in self.batch.files if item.process_verdict == "ready"]
        if not items:
            QMessageBox.information(self, "VidLift", "No files in this plan are ready to upscale.")
            return
        try:
            gbph = float(self.gbph.text())
        except ValueError:
            QMessageBox.warning(self, "VidLift", "GB/hour must be a number.")
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
        }
        try:
            self.job_ids = self.client.enqueue_run(
                [item.report.path for item in items],
                options=options,
                engine=self.engine_choice,
            )
        except (BatchTooLarge, FileNotFoundError, BridgeError) as exc:
            QMessageBox.warning(self, "VidLift", str(exc))
            self.job_ids = []
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
        self._begin_job_slate(self.job_ids, row_offset=0)

    def cancel_upscale(self) -> None:
        """The single cancel path (design §5): the registry's own
        claim-time CancelFlag, exactly what an MCP agent's cancel flips."""
        for job_id in self.job_ids:
            result = self.client.cancel(job_id)
            if result in ("cancelling", "cancelled"):
                break
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
        active = self.client.active_job_count() > 0 and bool(self.job_ids)
        self.cancel_button.setEnabled(active)
        # v0.4.13 (design §4.2): Cancel Batch enabled iff this window
        # owns a live batch set — INCLUDING the idle-queue case (nothing
        # running right now, queued jobs remain): cancel_batch marks all
        # queued and the batch drains. Independent of Cancel Current's
        # rule (which additionally requires an active count); the
        # asymmetry is honest — Cancel Current targets nothing when idle,
        # Cancel Batch still has queued targets.
        self.cancel_batch_button.setEnabled(bool(self.job_ids))
        if active:
            self.start_button.setEnabled(False)
            self.plan_button.setEnabled(False)
        elif not self.job_ids:
            self.plan_button.setEnabled(True)
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
        snapshots = self.client.enqueue_snapshots(self.job_ids)
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
        for index, (job_id, snapshot) in enumerate(zip(self.job_ids, snapshots)):
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
            return
        # §3.3-f: the detail LIVE-updates per tick for the SELECTED row —
        # riding this same poll (one extra status_snapshot per second, the
        # identical read the tick already does elsewhere — R3: cheap). This
        # covers BOTH follow selections and manual overrides, with no
        # second timer. Skipped on the batch-end tick (selected row's own
        # terminal state was just written by _batch_finished's summary
        # path; the next user click re-renders). A batch-end tick with
        # selection would also re-snap nothing — BATCH_END writes no
        # selection (§3.2).
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
        never scrolls the panel."""
        if self.batch is None or not self._batch_row_for_job:
            return
        rows = self.file_table.selectionModel().selectedRows()
        if not rows:
            return
        self._render_detail_for_row(rows[0].row())

    def _consume_running_snapshot(self, row: int | None, snapshot: dict, stage: str) -> None:
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
        frames_done = progress.get("frames_done")
        frames_total = progress.get("frames_total")
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
        if frames_total and frames_done is not None and frames_total > 0:
            self._stage_fraction = max(0.0, min(1.0, frames_done / frames_total))
            # §3c: honest ETA the moment it exists. The bridge's
            # _eta_seconds math is UNTOUCHED; it still requires a
            # measured benchmark constant AND frames_total > 0. When
            # eta_seconds is still None but frames are flowing, the
            # bar leaves busy mode with the measured fraction only —
            # no number invented.
            self.stage_bar.set_value(self._stage_fraction, self._stage_eta)
        elif self._stage_fraction is None:
            # §3c state 1: measuring… names what the pipeline is doing
            # to GET an ETA (the count pass), never a bare "estimating"
            # pulse forever. A duplicated busy call is a no-op anyway.
            self.stage_bar.set_busy(True, _BUSY_MEASURING_LABEL)
        # Overall queue: resolved jobs + current fraction, over total.
        # v0.4.10 (R2): resolved = reached ANY terminal state (the
        # queue position is honest even when a job cancelled/failed);
        # the headline batch BAR renders the colored succeeded-
        # dominated truth separately (_apply_batch_summary).
        resolved_units = sum(1 for c in self.run_counts if c is not None)
        frac_units = self._stage_fraction or 0.0
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
        if 0 <= slot < len(self.run_counts):
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

    def _batch_finished(self, notes: list[str]) -> None:
        self._stop_liveness()
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
        self.engine_combo.setEnabled(True)
        # v0.4.8 (D5): re-enable the profile combo with its honest tooltip.
        self.profile_combo.setEnabled(True)
        self.profile_combo.setToolTip(PROFILE_TOOLTIP)
        # v0.4.15: re-enable the denoise combo with its honest tooltip.
        self.denoise_combo.setEnabled(True)
        self.denoise_combo.setToolTip(DENOISE_TOOLTIP)
        self._render_recent_jobs()
        self._sync_run_controls()
        if any("cancelled" in note for note in notes):
            self._set_summary("Batch cancelled", "Completed jobs are safe; see the row statuses.")
        elif any("failed" in note for note in notes):
            self._set_summary(
                "Batch finished with failures",
                "See the selected row and .vidlift-runs logs for details.",
            )
        else:
            self._set_summary("Batch finished successfully", "Outputs are beside each source (or the chosen output folder).")

    # -- recent-history surface (design §1.9: one registry, two readers) --

    def _render_recent_jobs(self) -> None:
        try:
            history = self.client.history(limit=8)
        except Exception:
            history = []
        self._recent_rows = history

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        """Cancel through the unified path, then give the worker the
        same 30 s ffmpeg terminate window the sibling validated; a
        QThread/worker that ignores cancel for 30 s is the documented
        last-resort terminate case."""
        had_active = bool(self.job_ids) or self.client.active_job_count() > 0
        if had_active:
            answer = QMessageBox.question(
                self,
                "Run in progress",
                "An upscale is still running. Close anyway? (It will stop.)",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.cancel_upscale()
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
        expanded: list[str] = []
        for path in paths:
            candidate = Path(path)
            if candidate.is_dir():
                expanded.extend(
                    str(child)
                    for child in sorted(candidate.iterdir())
                    if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS
                )
            else:
                expanded.append(str(candidate))
        self._add_paths(expanded)


def main(argv: list[str] | None = None) -> int:
    # QSettings canonical path BEFORE MainWindow (design §2 rule for
    # run_gui.py applies to every entry: direct `python -m` runs too).
    QApplication.setApplicationName("VidLift")
    QApplication.setOrganizationName("YetiWurks")

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