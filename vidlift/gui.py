"""PyQt desktop interface for planning and running VidLift upscales."""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QDragEnterEvent, QDropEvent, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QCheckBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .plan import BatchPlan, PlannedFile, build_batch_plan
from .probe import ProbeError, probe_and_classify
from .run import EngineCancelled, MissingToolError, RunError, find_tools, run_file


VIDEO_EXTENSIONS = {
    ".avi", ".divx", ".flv", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".ts", ".vob", ".wmv",
}


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


def detail_text(item: PlannedFile, *, subtitle_ocr_enabled: bool = True) -> str:
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
    warnings = warnings_for(item)
    if warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


class UpscaleWorker(QObject):
    """Run planned files serially off the GUI thread."""

    file_started = pyqtSignal(int, str)
    file_finished = pyqtSignal(int, str, str)
    file_failed = pyqtSignal(int, str, str)
    finished = pyqtSignal(int, int, bool)

    def __init__(
        self,
        items: list[tuple[int, PlannedFile]],
        *,
        gb_per_hour: float,
        output_dir: str | None,
        subtitle_ocr_enabled: bool,
    ) -> None:
        super().__init__()
        self.items = items
        self.gb_per_hour = gb_per_hour
        self.output_dir = output_dir
        self.subtitle_ocr_enabled = subtitle_ocr_enabled
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def run(self) -> None:
        completed = 0
        failed = 0
        try:
            tools = find_tools()
        except Exception as exc:
            message = f"Required tools are unavailable: {exc}"
            for row, item in self.items:
                self.file_failed.emit(row, item.report.path, message)
                failed += 1
            self.finished.emit(completed, failed, False)
            return

        for row, item in self.items:
            if self._cancel_requested:
                break
            path = item.report.path
            self.file_started.emit(row, path)
            try:
                result = run_file(
                    path,
                    target="720p",
                    gb_per_hour=self.gb_per_hour,
                    output_dir=self.output_dir,
                    tools=tools,
                    cancel=lambda: self._cancel_requested,
                    subtitle_ocr=self.subtitle_ocr_enabled,
                )
            except EngineCancelled:
                self.file_failed.emit(row, path, "Cancelled")
                break
            except (ProbeError, RunError, OSError, ValueError) as exc:
                self.file_failed.emit(row, path, str(exc))
                failed += 1
                continue
            completed += 1
            note = "Completed"
            if result.state.fail_open:
                note = "Completed without AI upscale (fail-open)"
            elif result.state.engine_simulated:
                note = "Completed without AI upscale (stub engine)"
            self.file_finished.emit(row, result.plan.output_path, note)
        self.finished.emit(completed, failed, self._cancel_requested)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"VidLift {__version__}")
        self.resize(1180, 760)
        self.setAcceptDrops(True)
        self.files: list[str] = []
        self.batch: BatchPlan | None = None
        self.run_thread: QThread | None = None
        self.run_worker: UpscaleWorker | None = None
        self.run_total = 0
        self.run_done = 0

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

        self.plan_button = QPushButton("Plan Batch")
        self.plan_button.setObjectName("PrimaryButton")
        self.plan_button.clicked.connect(self.plan_batch)
        self.start_button = QPushButton("Start Upscale")
        self.start_button.setObjectName("StartButton")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_upscale)
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

    def _build_shell(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(16)

        title_row = QHBoxLayout()
        title = QLabel("VidLift")
        title.setObjectName("Title")
        version = QLabel(f"Version {__version__}")
        version.setObjectName("VersionBadge")
        subtitle = QLabel("Plan and run batch SD video upscales")
        subtitle.setObjectName("Subtitle")
        title_stack = QVBoxLayout()
        title_stack.addWidget(title)
        title_stack.addWidget(version, alignment=Qt.AlignmentFlag.AlignLeft)
        title_stack.addWidget(subtitle)
        title_row.addLayout(title_stack)
        title_row.addStretch(1)
        self.summary_count = QLabel()
        self.summary_count.setObjectName("SummaryCount")
        self.summary_note = QLabel()
        self.summary_note.setObjectName("SummaryNote")
        summary = QVBoxLayout()
        summary.addWidget(self.summary_count, alignment=Qt.AlignmentFlag.AlignRight)
        summary.addWidget(self.summary_note, alignment=Qt.AlignmentFlag.AlignRight)
        title_row.addLayout(summary)
        layout.addLayout(title_row)

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
        grid.addWidget(add, 0, 0)
        grid.addWidget(folder, 0, 1)
        grid.addWidget(clear, 0, 2)
        grid.addWidget(QLabel("GB/hour"), 0, 3)
        grid.addWidget(self.gbph, 0, 4)
        grid.addWidget(browse_output, 0, 5)
        grid.addWidget(self.output_dir, 0, 6)
        grid.addWidget(self.subtitle_ocr, 1, 0, 1, 3)
        grid.addWidget(self.plan_button, 0, 7)
        grid.addWidget(self.start_button, 0, 8)
        grid.setColumnStretch(6, 1)
        layout.addWidget(controls)
        layout.addWidget(self.progress)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.file_table)
        splitter.addWidget(self.details)
        splitter.setSizes([760, 360])
        layout.addWidget(splitter, 1)
        return root

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #101214; color: #edf0ec; }
            QLabel { color: #d9ded7; }
            #Title { font-size: 34px; font-weight: 700; letter-spacing: 0; }
            #Subtitle { color: #98a196; font-size: 14px; }
            #VersionBadge { background: #263036; border: 1px solid #3b4a52; border-radius: 9px; color: #b9c7ce; padding: 2px 8px; font-size: 11px; }
            #SummaryCount { color: #f4c45c; font-size: 22px; font-weight: 700; }
            #SummaryNote { color: #98a196; }
            #Controls { background: #191d1f; border: 1px solid #2b3134; border-radius: 8px; }
            QPushButton { background: #242a2d; border: 1px solid #3a4246; border-radius: 6px; padding: 9px 14px; color: #edf0ec; }
            QPushButton:hover { background: #30383c; border-color: #657178; }
            QPushButton:pressed { background: #1b2022; }
            #PrimaryButton { background: #d8943a; border-color: #f0b45c; color: #15110a; font-weight: 700; }
            #StartButton { background: #3d9865; border-color: #65bd88; color: #07150d; font-weight: 700; }
            #StartButton:disabled { background: #26302a; border-color: #35443a; color: #758079; }
            QProgressBar { background: #15191b; border: 1px solid #30383c; border-radius: 6px; color: #edf0ec; text-align: center; min-height: 22px; }
            QProgressBar::chunk { background: #3d9865; border-radius: 5px; }
            QLineEdit, QTextEdit { background: #15191b; border: 1px solid #30383c; border-radius: 6px; padding: 8px; color: #eef2ea; selection-background-color: #326b78; }
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
        self._add_paths(paths)

    def choose_output_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose output folder", str(Path.home()))
        if folder:
            self.output_dir.setText(folder)

    def clear_files(self) -> None:
        if self.run_thread is not None:
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
        self.start_button.setEnabled(batch.runnable_count > 0)

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
                )
            )

    def _refresh_details(self) -> None:
        if self.batch is not None:
            self.render_batch(self.batch)

    def start_upscale(self) -> None:
        if self.batch is None or self.run_thread is not None:
            return
        items = [
            (row, item)
            for row, item in enumerate(self.batch.files)
            if item.process_verdict == "ready"
        ]
        if not items:
            QMessageBox.information(self, "VidLift", "No files in this plan are ready to upscale.")
            return
        try:
            gbph = float(self.gbph.text())
        except ValueError:
            QMessageBox.warning(self, "VidLift", "GB/hour must be a number.")
            return

        self.run_total = len(items)
        self.run_done = 0
        self.progress.setRange(0, self.run_total)
        self.progress.setValue(0)
        self.progress.setFormat(f"0/{self.run_total} completed")
        self.progress.show()
        self.plan_button.setEnabled(False)
        self.start_button.setText("Cancel Current")
        self.start_button.clicked.disconnect()
        self.start_button.clicked.connect(self.cancel_upscale)

        thread = QThread(self)
        worker = UpscaleWorker(
            items,
            gb_per_hour=gbph,
            output_dir=self.output_dir.text().strip() or None,
            subtitle_ocr_enabled=self.subtitle_ocr.isChecked(),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.file_started.connect(self._run_started)
        worker.file_finished.connect(self._run_finished)
        worker.file_failed.connect(self._run_failed)
        worker.finished.connect(self._batch_finished)
        worker.finished.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self.run_thread = thread
        self.run_worker = worker
        thread.start()

    def cancel_upscale(self) -> None:
        if self.run_worker is not None:
            self.run_worker.cancel()
            self.start_button.setEnabled(False)
            self.start_button.setText("Cancelling...")
            self._set_summary("Cancelling", "Stopping the current file safely.")

    def _run_started(self, row: int, path: str) -> None:
        self.file_table.item(row, 0).setText("running")
        self.file_table.selectRow(row)
        self._set_summary(
            f"{self.run_done}/{self.run_total} completed",
            f"Upscaling {Path(path).name}",
        )
        self.details.setPlainText(
            f"Upscaling:\n{path}\n\n"
            "Progress and diagnostics are written to the .vidlift-runs folder beside the source file."
        )

    def _run_finished(self, row: int, output_path: str, note: str) -> None:
        self.run_done += 1
        self.file_table.item(row, 0).setText("completed")
        self.file_table.item(row, 0).setForeground(Qt.GlobalColor.green)
        self.progress.setValue(self.run_done)
        self.progress.setFormat(f"{self.run_done}/{self.run_total} completed")
        self.details.setPlainText(f"{note}\n\nOutput:\n{output_path}")

    def _run_failed(self, row: int, path: str, message: str) -> None:
        self.run_done += 1
        self.file_table.item(row, 0).setText("failed")
        self.file_table.item(row, 0).setForeground(Qt.GlobalColor.red)
        self.progress.setValue(min(self.run_done, self.run_total))
        self.progress.setFormat(f"{self.run_done}/{self.run_total} completed")
        self.details.setPlainText(f"Failed:\n{path}\n\n{message}")

    def _batch_finished(self, completed: int, failed: int, cancelled: bool) -> None:
        self.run_thread = None
        self.run_worker = None
        self.plan_button.setEnabled(True)
        self.start_button.setText("Start Upscale")
        self.start_button.setEnabled(self.batch is not None and self.batch.runnable_count > 0)
        self.start_button.clicked.disconnect()
        self.start_button.clicked.connect(self.start_upscale)
        if cancelled:
            self._set_summary(f"{completed} completed", "Batch cancelled.")
        elif failed:
            self._set_summary(
                f"{completed} completed, {failed} failed",
                "See the selected row and .vidlift-runs logs for details.",
            )
        else:
            self._set_summary(f"{completed} completed", "Batch finished successfully.")

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
    app = QApplication(list(argv or sys.argv[1:]))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
