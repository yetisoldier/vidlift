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

PLAN_HELPER_TEXT = "1. Add files → 2. Plan → 3. Start. Planning never writes to disk."


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value / 1_000_000_000:.2f} GB"


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
    if item.process_verdict != "ready":
        warnings.append("Skipped unless explicitly forced in a later pipeline version")
    return warnings


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
        # v0.4.8 (design D5): the profile combo lands on row 2 (the only
        # row 1 cells left are behind cancel_button at (1, 7)), so the
        # monitor span narrows 7 -> 5 columns to free (2, 5)/(2, 6).
        grid.addWidget(QLabel("Upscale profile"), 2, 5)
        grid.addWidget(self.profile_combo, 2, 6)
        grid.addWidget(self.monitor_check, 2, 0, 1, 5)
        self.monitor_check.toggled.connect(self._toggle_monitor)
        grid.addWidget(self.cancel_button, 1, 7)
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
        folder = QFileDialog.getExistingDirectory(self, "Choose output folder", str(Path.home()))
        if folder:
            self.output_dir.setText(folder)

    def clear_files(self) -> None:
        if self.client.active_job_count() > 0:
            QMessageBox.information(self, "VidLift", "Wait for the current upscale to finish or cancel it first.")
            return
        self.files = []
        self.batch = None
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
        in-flight jobs (design §5: re-enable at the LAST terminal)."""
        if self.client.active_job_count() > 0:
            # a run is live; Start is the cancel affordance via disable–
            # enable symmetry handled in _sync_run_controls
            return
        allowed = (
            self.batch is not None
            and self.batch.runnable_count > 0
            and not self.job_ids
        )
        self.start_button.setEnabled(allowed)

    def _sync_details(self) -> None:
        if self.batch is None:
            return
        rows = self.file_table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        if 0 <= row < len(self.batch.files):
            self.details.setPlainText(
                detail_text(
                    self.batch.files[row],
                    subtitle_ocr_enabled=self.subtitle_ocr.isChecked(),
                    upscale_profile=self.profile_choice,
                )
            )

    def _refresh_details(self) -> None:
        if self.batch is not None:
            self.render_batch(self.batch)

    # ---- Start: enqueue through the unified bridge path (design §5) ----

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
        self.run_panel.show()
        self.stage_bar.reset()
        self.overall_bar.reset()
        self.overall_bar.set_busy(True, "queue active ...")
        self.stage_bar.set_busy(True, "starting ...")
        self.run_total = len(self.job_ids)
        self.run_done = 0
        self.progress.setRange(0, self.run_total)
        self.progress.setValue(0)
        self.progress.setFormat(f"0/{self.run_total} completed")
        self.progress.show()
        self._sync_run_controls()
        # Liveness: dots animate, clock ticks until the last job lands
        # (all done/failed/cancelled paths pass through _stop_liveness).
        self._run_started_at = time.time()
        self._elapsed_timer.start()
        self.activity_dots.start()
        self._stage_now = "queued"
        self._stage_eta = None
        self._stage_fraction = None
        self.time_elapsed.setText("Elapsed 0:00")
        # Seed the forecast immediately instead of waiting one tick.
        self._forecast_total()
        self._render_recent_jobs()

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

    def _sync_run_controls(self) -> None:
        active = self.client.active_job_count() > 0 and bool(self.job_ids)
        self.cancel_button.setEnabled(active)
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
        # v0.4.5 (§2 Rule 4): the rendered job-set key — the leading
        # job id — force-pins the log tail once when it changes (new
        # batch start / next job claimed / terminal drain rebase).
        rendered_key = self.job_ids[0] if self.job_ids else None
        if rendered_key != self._log_rendered_key:
            self._log_rendered_key = rendered_key
            if rendered_key is not None:
                self._log_force_pin_next = True
        for index, (job_id, snapshot) in enumerate(zip(self.job_ids, snapshots)):
            row = index  # row i of the table ↔ job_ids[i] while a run is live
            if snapshot is None:
                still_active.append(job_id)
                continue
            state = snapshot.get("state") or "queued"
            stage = run_stage_label(snapshot)
            if row < self.file_table.rowCount():
                status_cell = self.file_table.item(row, 0)
                if status_cell is not None:
                    status_cell.setText(state)
            terminal = state in ("succeeded", "failed", "cancelled")
            if not terminal:
                still_active.append(job_id)
                if state == "running":
                    self._consume_running_snapshot(row, snapshot, stage)
            else:
                self._consume_terminal_snapshot(row, snapshot)
                terminal_notes.append(
                    f"{Path(snapshot.get('file_path') or '?').name}: {state}"
                )
        self.job_ids = still_active if still_active else []
        if self._run_started_at is not None and not self.job_ids:
            # The batch actually finished (every job reached a terminal
            # state and was drained above). Running this on every tick
            # would stop the liveness timer on the FIRST tick while
            # jobs are still queued/running — the run panel would
            # freeze at "queued" forever (the v0.4.4 validation D1
            # repro: timer dead, rows never update).
            self._batch_finished(terminal_notes)

    def _consume_running_snapshot(self, row: int, snapshot: dict, stage: str) -> None:
        # §3b: the phase-change busy/reset keys on the stage's PHASE
        # FACTS (the label renders progress detail per tick — heartbeat
        # seconds tick inside one and the same stage — so comparing
        # rendered label strings would busy-reset every second). The
        # reset runs BEFORE the eta assignment below (a new stage's
        # live ETA starts empty per the sibling rule); the busy label
        # for the count pass is the Measuring label (§3c state 1).
        key = _stage_label_text(stage)
        if self._stage_now != key:
            self._stage_eta = None
            self._stage_fraction = None
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
            self._stage_eta = float(eta)
        progress = snapshot.get("progress") or {}
        frames_done = progress.get("frames_done")
        frames_total = progress.get("frames_total")
        # v0.4.5 (§3b/§3c, D-eta): the stage label's phase_note suffix
        # rides the payload; the same computed string goes through
        # _set_text, so identical values never repaint the label.
        note = progress.get("phase_note")
        rendered = f"{stage} ({note})" if note else stage
        self._set_text(self.stage_label, rendered)
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
        # Overall queue: completed jobs + current fraction, over total.
        done_units = self.run_done
        frac_units = self._stage_fraction or 0.0
        total_units = max(1, self.run_total)
        overall = (done_units + frac_units) / total_units
        self.overall_bar.set_value(overall, None)
        # Log tail: the runner's own log (the bridge's _log_tail lives
        # inside the snapshot; render it verbatim).
        tail = snapshot.get("log_tail") or []
        if tail:
            self._render_log_tail("\n".join(str(line) for line in tail[-40:]))

    def _consume_terminal_snapshot(self, row: int, snapshot: dict) -> None:
        self.run_done += 1
        state = snapshot.get("state") or "finished"
        if row < self.file_table.rowCount():
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
        self.progress.setValue(min(self.run_done, self.run_total))
        self.progress.setFormat(f"{self.run_done}/{self.run_total} completed")

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

    def _forecast_total_seconds(self) -> float | None:
        """Forecast the whole remaining batch (design §1.5): the live
        per-file eta_seconds from the bridge contract when available,
        plus benchmark-cache estimates for the files ahead; honest
        ``None`` stays ``None`` — the UI shows "…", never a made-up
        number."""
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
            live_eta = snapshot.get("eta_seconds")
            if index == 0 and live_eta is not None:
                total = (total or 0.0) + float(live_eta)
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
        if secs is None:
            self._set_text(self.total_remaining, "Total remaining … (all steps)")
            self._set_text(self.total_pill, "")
            return
        self._set_text(
            self.total_remaining,
            f"Total remaining ~{self._fmt_hm(secs)} (all steps)",
        )
        if self._run_started_at is not None and self._stage_now != "done":
            self._set_text(self.total_pill, f"Total remaining ~{self._fmt_hm(secs)}")

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
        self.engine_combo.setEnabled(True)
        # v0.4.8 (D5): re-enable the profile combo with its honest tooltip.
        self.profile_combo.setEnabled(True)
        self.profile_combo.setToolTip(PROFILE_TOOLTIP)
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