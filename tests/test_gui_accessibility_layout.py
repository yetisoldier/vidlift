"""Offscreen keyboard/layout contracts, not Windows assistive-tool sign-off."""
import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtTest import QTest

from test_gui_best_practices import qapp, ui
from _gui_checks import reveal_workflow_widget


@pytest.mark.parametrize("factor", [1.0, 1.5, 2.0])
def test_compact_enlarged_font_actions_reachable(ui, qapp, factor):
    win, _ = ui
    font = QFont(win.font())
    font.setPointSizeF(font.pointSizeF() * factor)
    win.setFont(font)
    win.resize(960, 600)
    win.show()
    qapp.processEvents()
    assert win.minimumSizeHint().width() <= 960
    assert win.minimumSizeHint().height() <= 600
    for name in ("upscale_add_button", "upscale_folder_button", "upscale_clear_button",
                 "plan_button", "start_button", "replace_source", "suppress_srt",
                 "add_subs_button", "subs_add_folder_button", "subs_remove_button",
                 "subs_clear_button", "subs_replace_source", "subs_suppress_srt", "start_subtitles_button"):
        widget = getattr(win, name)
        reveal_workflow_widget(win, widget)
        qapp.processEvents()
        assert not widget.visibleRegion().isEmpty(), name
        assert widget.width() >= widget.minimumSizeHint().width(), (factor, name, widget.size(), widget.minimumSizeHint())
    for scroll in (win.setup_scroll, win.subs_setup_scroll):
        assert scroll.horizontalScrollBar().maximum() == 0


def test_both_starts_and_model_control_in_keyboard_chain(ui, qapp):
    win, _ = ui
    win.show()
    for button in (win.start_button, win.start_subtitles_button, win.subs_model_button):
        button.setEnabled(True)
        button.show()
    qapp.processEvents()
    reached = set()
    widget = win.upscale_add_button
    for _ in range(160):
        widget = widget.nextInFocusChain()
        reached.add(widget)
    assert {win.plan_button, win.start_button, win.start_subtitles_button,
            win.subs_model_button, win.file_table, win.details, win.history_table,
            win.cancel_check_button, win.copy_message_button} <= reached
    win.upscale_add_button.setFocus()
    QTest.keyClick(win.upscale_add_button, Qt.Key.Key_Tab)
    assert qapp.focusWidget() is win.upscale_folder_button
    try:
        QTest.keyClick(win.upscale_folder_button, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
    finally:
        # Offscreen Qt can retain the synthetic modifier across test windows.
        QTest.keyRelease(win.upscale_add_button, Qt.Key.Key_Shift)
    assert qapp.focusWidget() is win.upscale_add_button
    assert qapp.keyboardModifiers() == Qt.KeyboardModifier.NoModifier


def test_history_switch_clears_stale_detail_and_returns_current_selection(ui, qapp, monkeypatch):
    win, client = ui
    win._add_paths(["synthetic-current.avi"])
    win.file_table.selectRow(0)
    monkeypatch.setattr(client, "history", lambda **k: [{"job_id": "history-fixture", "file_path": "old synthetic.mkv", "state": "failed"}])
    win._render_recent_jobs()
    win.queue_tabs.setCurrentIndex(1)
    win.history_table.selectRow(0)
    assert "old synthetic.mkv" in win.details.toPlainText()
    win.queue_tabs.setCurrentIndex(0)
    qapp.processEvents()
    assert "synthetic-current.avi" in win.details.toPlainText()


def test_table_copy_shortcut_does_not_override_details_copy(ui, qapp):
    win, _ = ui
    win._add_paths(["synthetic copy source.avi"])
    win.show()
    qapp.processEvents()
    win.file_table.selectRow(0)
    win.file_table.setFocus()
    qapp.processEvents()
    try:
        QTest.keyClick(win.file_table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    finally:
        QTest.keyRelease(win.file_table, Qt.Key.Key_Control)
    assert "synthetic copy source.avi" in qapp.clipboard().text()
    win.details.setPlainText("selected synthetic detail")
    win.details.selectAll()
    win.details.setFocus()
    try:
        QTest.keyClick(win.details, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    finally:
        QTest.keyRelease(win.details, Qt.Key.Key_Control)
    assert qapp.clipboard().text() == "selected synthetic detail"


def test_window_splitter_state_roundtrip_and_missing_monitor_clamp(ui, qapp, monkeypatch):
    from types import SimpleNamespace
    from PyQt6.QtCore import QRect
    import vidlift.gui as gui
    win, client = ui
    monkeypatch.setattr(gui.MainWindow, "screen", lambda self: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1280, 900)))
    win.resize(1100, 720)
    win.move(7000, 7000)
    win.details_splitter.setSizes([600, 300])
    win.close()
    assert win._settings().contains("ui/window_geometry")
    assert win._settings().contains("ui/details_splitter")
    restored = gui.MainWindow(client=client)
    try:
        assert restored.width() <= 1280 and restored.height() <= 900
        assert 0 <= restored.x() <= 1280 - restored.width()
        assert 0 <= restored.y() <= 900 - restored.height()
        assert restored._settings().value("ui/details_splitter") == win._settings().value("ui/details_splitter")
    finally:
        restored.close()
        restored.deleteLater()
        qapp.processEvents()
