"""PyQt planning workbench for VidLift.

This GUI intentionally stops at pre-flight planning. The long-running
encode path is not wired yet, so the interface focuses on the decision
Eric needs before spending hours on a file: ready/skip verdict, output
geometry, size target, subtitles, and warnings.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtCore import Qt
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
    QPushButton,
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


def detail_text(item: PlannedFile) -> str:
    report = item.report
    source = report.video.codec_name if report.video else "no video"
    lines = [
        str(report.path),
        "",
        f"Verdict: {item.process_verdict}",
        f"Source: {report.source_class} ({source})",
        f"Duration: {format_duration(report.duration_seconds)}",
        f"Displayed geometry: {report.displayed_width}x{report.displayed_height}",
        f"Target geometry: {item.output_width}x{item.output_height}",
        f"Output: {item.output_path}",
        f"Bitrate: video {item.video_bitrate_kbps}k, audio {item.audio.bitrate_kbps}k {item.audio.label}",
        f"Estimated size: {format_bytes(item.estimated_output_bytes)}",
        f"Subtitles: {item.subtitle_note}",
        "",
        f"Expectation: {item.expectation}",
    ]
    warnings = warnings_for(item)
    if warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"VidLift {__version__}")
        self.resize(1180, 760)
        self.setAcceptDrops(True)
        self.files: list[str] = []
        self.batch: BatchPlan | None = None

        self.file_table = QTableWidget(0, 7)
        self.file_table.setHorizontalHeaderLabels(
            ["Verdict", "File", "Source", "Target", "Size", "Subtitles", "Warnings"]
        )
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.verticalHeader().hide()
        header = self.file_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self.file_table.itemSelectionChanged.connect(self._sync_details)

        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText("Add video files, then run Plan Batch.")

        self.gbph = QLineEdit("1.5")
        self.gbph.setFixedWidth(90)
        self.gbph.setToolTip("Target output size in gigabytes per hour")
        self.output_dir = QLineEdit()
        self.output_dir.setPlaceholderText("Beside each input")

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
        subtitle = QLabel("Batch pre-flight for SD video upscales")
        subtitle.setObjectName("Subtitle")
        title_stack = QVBoxLayout()
        title_stack.addWidget(title)
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
        plan = QPushButton("Plan Batch")
        plan.setObjectName("PrimaryButton")
        plan.clicked.connect(self.plan_batch)
        grid.addWidget(add, 0, 0)
        grid.addWidget(folder, 0, 1)
        grid.addWidget(clear, 0, 2)
        grid.addWidget(QLabel("GB/hour"), 0, 3)
        grid.addWidget(self.gbph, 0, 4)
        grid.addWidget(browse_output, 0, 5)
        grid.addWidget(self.output_dir, 0, 6)
        grid.addWidget(plan, 0, 7)
        grid.setColumnStretch(6, 1)
        layout.addWidget(controls)

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
            #SummaryCount { color: #f4c45c; font-size: 22px; font-weight: 700; }
            #SummaryNote { color: #98a196; }
            #Controls { background: #191d1f; border: 1px solid #2b3134; border-radius: 8px; }
            QPushButton { background: #242a2d; border: 1px solid #3a4246; border-radius: 6px; padding: 9px 14px; color: #edf0ec; }
            QPushButton:hover { background: #30383c; border-color: #657178; }
            QPushButton:pressed { background: #1b2022; }
            #PrimaryButton { background: #d8943a; border-color: #f0b45c; color: #15110a; font-weight: 700; }
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
        self.file_table.setRowCount(len(self.files))
        for row, path in enumerate(self.files):
            for column, text in enumerate(["pending", path, "", "", "", "", ""]):
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
        self.files = []
        self.batch = None
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
        reports = []
        failures: list[str] = []
        for path in self.files:
            try:
                reports.append(probe_and_classify(path))
            except ProbeError as exc:
                failures.append(f"{path}: {exc}")
        if not reports:
            QMessageBox.warning(self, "VidLift", "No files could be probed.\n\n" + "\n".join(failures[:5]))
            return
        self.batch = build_batch_plan(
            reports,
            target="720p",
            gb_per_hour=gbph,
            output_dir=self.output_dir.text().strip() or None,
        )
        self.render_batch(self.batch)
        if failures:
            QMessageBox.warning(self, "VidLift", "Some files failed to probe.\n\n" + "\n".join(failures[:8]))

    def render_batch(self, batch: BatchPlan) -> None:
        self.file_table.setRowCount(len(batch.files))
        for row, item in enumerate(batch.files):
            report = item.report
            warnings = "; ".join(warnings_for(item))
            values = [
                item.process_verdict,
                report.path,
                report.source_class,
                f"{item.output_width}x{item.output_height}",
                format_bytes(item.estimated_output_bytes),
                item.subtitle_note,
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
            f"Estimated total {format_bytes(batch.total_estimated_bytes)}",
        )

    def _sync_details(self) -> None:
        if self.batch is None:
            return
        rows = self.file_table.selectionModel().selectedRows()
        if not rows:
            return
        row = rows[0].row()
        if 0 <= row < len(self.batch.files):
            self.details.setPlainText(detail_text(self.batch.files[row]))

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
