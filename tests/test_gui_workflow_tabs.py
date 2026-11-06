"""Workflow navigation is presentation only, with mode-specific input routing.

Synthetic media and an isolated scripted client: no engine, downloads or real
user settings. These Linux offscreen checks are not Windows/GPU validation.
"""
import copy
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
from PyQt6.QtGui import QDropEvent, QFont
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QFileDialog, QPushButton, QTabBar

from _gui_checks import wait_for_checks
from test_gui_best_practices import media, qapp, ui
from test_gui import _plan_for
from vidlift.gui import MainWindow


UPSCALE_CONTROLS = (
    "upscale_add_button", "upscale_folder_button", "upscale_clear_button",
    "plan_button", "start_button", "gbph", "output_dir", "upscale_output_button",
    "engine_combo", "profile_combo", "denoise_combo", "resume_claim",
    "subtitle_ocr", "transcribe_subs", "replace_source", "suppress_srt",
)
SUBTITLE_CONTROLS = (
    "add_subs_button", "subs_add_folder_button", "start_subtitles_button",
    "subs_device_combo", "subs_remove_button", "subs_clear_button",
    "subs_replace_source", "subs_suppress_srt",
)


def switch_tab(win: MainWindow, qapp: QApplication, index: int):
    bar = win.workflow_tabs.tabBar()
    assert bar is not None
    QTest.mouseClick(bar, Qt.MouseButton.LeftButton, pos=bar.tabRect(index).center())
    qapp.processEvents()
    assert win.workflow_tabs.currentIndex() == index


@pytest.mark.parametrize("factor", [1.0, 1.5, 2.0])
def test_prominent_tabs_show_only_their_workflow_without_prior_scrolling(ui, qapp, factor):
    win, client = ui
    font = QFont(win.font())
    font.setPointSizeF(font.pointSizeF() * factor)
    win.setFont(font)
    win.resize(960, 600)
    win.show()
    qapp.processEvents()
    assert [win.workflow_tabs.tabText(i) for i in range(win.workflow_tabs.count())] == [
        "AI Upscale", "Subtitles Only"]
    assert win.workflow_tabs.currentIndex() == 0
    bar = win.workflow_tabs.tabBar()
    assert bar.visibleRegion().contains(bar.rect())
    assert win.minimumSizeHint().width() <= 960
    assert win.minimumSizeHint().height() <= 600
    for index, visible, hidden in ((0, UPSCALE_CONTROLS, SUBTITLE_CONTROLS),
                                   (1, SUBTITLE_CONTROLS, UPSCALE_CONTROLS)):
        switch_tab(win, qapp, index)
        assert all(getattr(win, name).isVisible() for name in visible)
        assert all(not getattr(win, name).isVisible() for name in hidden)
        assert win.file_table.isVisible() and win.details.isVisible()
        assert win.monitor_check.isVisible()
        assert win.subs_model_pill.isVisible()
    # No ensureWidgetVisible call: subtitles are discoverable immediately,
    # even if the user has not scrolled the upscale settings at all.
    assert win.setup_scroll.verticalScrollBar().value() == 0
    assert win.subs_setup_scroll.verticalScrollBar().value() == 0
    for button in (win.add_subs_button, win.subs_add_folder_button, win.start_subtitles_button):
        assert button.visibleRegion().contains(button.rect())
    assert client.submitted == []


def test_switch_preserves_plan_staging_options_selection_and_settings(ui, qapp, tmp_path, monkeypatch):
    import vidlift.gui as gui
    win, client = ui
    plan = _plan_for(tmp_path)
    win.files = [plan.report.path]
    win.gbph.setText("2.25")
    win.output_dir.setText(str(tmp_path / "output"))
    win.engine_combo.setCurrentIndex(win.engine_combo.findData("stub"))
    win.profile_combo.setCurrentIndex(win.profile_combo.findData("anime"))
    win.denoise_combo.setCurrentIndex(1)
    win.replace_source.setChecked(True)
    win.resume_claim.setChecked(True)
    monkeypatch.setattr(gui, "find_tools", lambda: SimpleNamespace(ffprobe="unused"))
    monkeypatch.setattr(gui, "probe_and_classify", lambda *a, **k: plan.report)
    win.plan_batch()
    assert win.start_button.isEnabled()
    win.subs_device_combo.setCurrentIndex(win.subs_device_combo.findData("cpu"))
    win.subs_suppress_srt.setChecked(True)
    win._enqueue_subs_paths([media(tmp_path)])
    staged = copy.deepcopy(win._staged_subs)
    # Later options must not overwrite the existing staged snapshot.
    win.subs_device_combo.setCurrentIndex(win.subs_device_combo.findData("cuda"))
    win.subs_replace_source.setChecked(True)
    win.subs_suppress_srt.setChecked(False)
    win.file_table.selectRow(1)
    win.show()
    qapp.processEvents()
    batch = win.batch
    inputs = win._planned_inputs
    details = win.details.toPlainText()
    rows = [[win.file_table.item(r, c).text() if win.file_table.item(r, c) else None
             for c in range(win.file_table.columnCount())] for r in range(win.file_table.rowCount())]
    settings = win._settings()
    saved = {key: settings.value(key) for key in settings.allKeys()}
    def forbidden(*a, **k):
        pytest.fail("Switching workflows must not request work or submit jobs")
    for method in ("enqueue_run", "enqueue_subs"):
        monkeypatch.setattr(client, method, forbidden)
    monkeypatch.setattr(win, "_request_ingestion", forbidden)
    monkeypatch.setattr(win, "_request_preparation", forbidden)
    monkeypatch.setattr(win, "_confirm_options", forbidden)
    for index in (1, 0, 1, 0):
        switch_tab(win, qapp, index)
    assert win.batch is batch and win._planned_inputs is inputs
    assert not win._plan_dirty and win.start_button.isEnabled()
    assert win.files == [plan.report.path]
    assert win._staged_subs == staged
    assert win.file_table.currentRow() == 1
    assert win.details.toPlainText() == details
    assert rows == [[win.file_table.item(r, c).text() if win.file_table.item(r, c) else None
                     for c in range(win.file_table.columnCount())] for r in range(win.file_table.rowCount())]
    assert win.gbph.text() == "2.25" and win.output_dir.text() == str(tmp_path / "output")
    assert win.engine_combo.currentData() == "stub" and win.profile_combo.currentData() == "anime"
    assert win.denoise_combo.currentIndex() == 1 and win.resume_claim.isChecked()
    assert win.replace_source.isChecked() and not win.suppress_srt.isChecked()
    assert win.subs_replace_source.isChecked() and not win.subs_suppress_srt.isChecked()
    assert win.subs_device_combo.currentData() == "cuda"
    assert saved == {key: settings.value(key) for key in settings.allKeys()}
    assert client.submitted == client.cancel_calls == client.cancel_batch_calls == []


@pytest.mark.parametrize("index", [0, 1])
def test_window_drop_routes_to_selected_workflow_without_submitting(ui, qapp, tmp_path, index):
    win, client = ui
    path = media(tmp_path)
    win.workflow_tabs.setCurrentIndex(index)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path), QUrl("https://example.invalid/ignored.mkv")])
    event = QDropEvent(QPointF(20, 20), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    win.dropEvent(event)
    assert event.isAccepted()
    wait_for_checks(win)
    assert win.files == ([] if index else [path])
    assert [r["path"] for r in win._staged_subs] == ([path] if index else [])
    assert client.submitted == []


@pytest.mark.parametrize("index", [0, 1])
def test_file_picker_buttons_keep_explicit_mode_routing(ui, qapp, tmp_path, monkeypatch, index):
    win, client = ui
    path = media(tmp_path)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: ([path], "")))
    win.show()
    switch_tab(win, qapp, index)
    button: QPushButton = win.add_subs_button if index else win.upscale_add_button
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    wait_for_checks(win)
    assert win.files == ([] if index else [path])
    assert [r["path"] for r in win._staged_subs] == ([path] if index else [])
    assert client.submitted == []


def test_switch_during_ingestion_keeps_original_mode_and_frozen_options(ui, qapp, tmp_path, monkeypatch):
    import vidlift.gui as gui
    win, client = ui
    path = media(tmp_path)
    started, release = threading.Event(), threading.Event()
    collect = gui._collect_inputs
    def delayed(paths, cancel):
        started.set()
        assert release.wait(2), "Test did not release background ingestion"
        return collect(paths, cancel)
    monkeypatch.setattr(gui, "_collect_inputs", delayed)
    win.workflow_tabs.setCurrentIndex(1)
    win.subs_device_combo.setCurrentIndex(win.subs_device_combo.findData("cpu"))
    win.subs_replace_source.setChecked(True)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path)])
    event = QDropEvent(QPointF(20, 20), Qt.DropAction.CopyAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    try:
        win.dropEvent(event)
        assert started.wait(1)
        win.workflow_tabs.setCurrentIndex(0)
        win.subs_device_combo.setCurrentIndex(win.subs_device_combo.findData("cuda"))
        win.subs_replace_source.setChecked(False)
        assert win._preparation_kind == "add-subs"
    finally:
        release.set()
        wait_for_checks(win)
    assert win.files == []
    assert win._staged_subs[0]["options"].device == "cpu"
    assert win._staged_subs[0]["options"].replace_source
    assert client.submitted == []


def test_running_job_and_shared_results_survive_workflow_switch(ui, qapp, tmp_path):
    win, client = ui
    path = media(tmp_path)
    client.make_job(path, [{"state": "running", "current_phase": "transcribe"}], kind="subs")
    win._enqueue_subs_paths([path])
    win.start_subtitles()
    win._elapsed_timer.stop()
    win._poll_jobs()
    win.show()
    qapp.processEvents()
    ids = list(win.job_ids)
    row_map = dict(win._batch_row_for_job)
    win.file_table.selectRow(0)
    details = win.details.toPlainText()
    win.queue_tabs.setCurrentIndex(1)
    for index in (1, 0, 1):
        switch_tab(win, qapp, index)
        assert win.run_panel.isVisible()
        assert win.cancel_button.visibleRegion().contains(win.cancel_button.rect())
        assert win.cancel_batch_button.visibleRegion().contains(win.cancel_batch_button.rect())
        assert win.job_ids == ids and win._batch_row_for_job == row_map
        assert win.queue_tabs.currentIndex() == 1
        assert win.history_table.isVisible() and win.details.isVisible()
    win.queue_tabs.setCurrentIndex(0)
    assert win.details.toPlainText() == details
    assert client.submitted == ids
    assert client.cancel_calls == client.cancel_batch_calls == []
    assert not win.start_subtitles_button.isEnabled()


def test_keyboard_switch_does_not_submit(ui, qapp):
    win, client = ui
    win.show()
    qapp.processEvents()
    bar: QTabBar | None = win.workflow_tabs.tabBar()
    assert bar is not None
    bar.setFocus()
    QTest.keyClick(bar, Qt.Key.Key_Right)
    assert win.workflow_tabs.currentIndex() == 1
    QTest.keyClick(bar, Qt.Key.Key_Left)
    assert win.workflow_tabs.currentIndex() == 0
    assert client.submitted == client.cancel_calls == client.cancel_batch_calls == []


def test_independent_scroll_positions_survive_switches(ui, qapp):
    win, _ = ui
    win.resize(960, 600)
    win.show()
    qapp.processEvents()
    upscale_bar = win.setup_scroll.verticalScrollBar()
    upscale_bar.setValue(upscale_bar.maximum())
    upscale_position = upscale_bar.value()
    assert upscale_position > 0
    switch_tab(win, qapp, 1)
    subs_bar = win.subs_setup_scroll.verticalScrollBar()
    assert subs_bar.value() == 0
    subs_bar.setValue(subs_bar.maximum())
    subs_position = subs_bar.value()
    assert subs_position > 0
    switch_tab(win, qapp, 0)
    assert upscale_bar.value() == upscale_position
    switch_tab(win, qapp, 1)
    assert subs_bar.value() == subs_position


def test_desktop_subtitle_settings_fit_and_header_does_not_squeeze_tabs(ui, qapp):
    win, _ = ui
    win.resize(1180, 760)
    win.show()
    qapp.processEvents()
    switch_tab(win, qapp, 1)
    assert win.subs_setup_scroll.verticalScrollBar().maximum() == 0
    assert win.workflow_tabs.y() < 250
    assert win.subs_model_pill.width() > 100
    assert win.details_splitter.height() >= 120
    for name in SUBTITLE_CONTROLS:
        widget = getattr(win, name)
        assert widget.visibleRegion().contains(widget.rect()), name


def test_visible_subtitle_start_keeps_review_rejection_and_media_safe(ui, qapp, tmp_path, monkeypatch):
    win, client = ui
    path = media(tmp_path)
    win.subs_replace_source.setChecked(True)
    win.subs_suppress_srt.setChecked(True)
    win._enqueue_subs_paths([path])
    reviews = []
    def reject(title, details):
        reviews.append((title, details))
        return False
    monkeypatch.setattr(win, "_confirm_options", reject)
    win.show()
    switch_tab(win, qapp, 1)
    assert reviews == [] and client.submitted == []
    QTest.mouseClick(win.start_subtitles_button, Qt.MouseButton.LeftButton)
    wait_for_checks(win)
    assert len(reviews) == 1 and path in reviews[0][1]
    assert len(win._staged_subs) == 1
    assert win._staged_subs[0]["options"].replace_source
    assert win._staged_subs[0]["options"].suppress_srt
    from pathlib import Path
    assert Path(path).read_bytes() == b"synthetic source sentinel"
    assert client.submitted == client.cancel_calls == client.cancel_batch_calls == []
