"""Rendered checkbox regressions using real Qt widgets and isolated settings.

No engines, real media or user settings. Offscreen captures are Linux evidence,
not a substitute for Windows native/DPI validation.
"""
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QPoint, QSignalBlocker, Qt
from PyQt6.QtGui import QCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QCheckBox, QStyle, QStyleOptionButton

from _gui_checks import reveal_workflow_widget
from test_gui_best_practices import qapp, ui


CHECKBOXES = (
    "resume_claim", "subtitle_ocr", "transcribe_subs", "replace_source",
    "suppress_srt", "subs_replace_source", "subs_suppress_srt", "monitor_check",
)


def indicator_image(box, qapp):
    qapp.processEvents()
    option = QStyleOptionButton()
    box.initStyleOption(option)
    rect = box.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, box)
    image = box.grab(rect).toImage()
    assert not image.isNull()
    return image


def colour_at(image, x, y):
    scale = image.devicePixelRatio()
    return image.pixelColor(round(x * scale), round(y * scale)).name()


@pytest.mark.parametrize("name", CHECKBOXES)
@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "disabled"])
@pytest.mark.parametrize("checked", [False, True], ids=["unchecked", "checked"])
def test_indicator_has_square_outline_and_checked_mark(ui, qapp, name, enabled, checked):
    win, client = ui
    win.resize(1180, 900)
    win.show()
    box: QCheckBox = getattr(win, name)
    reveal_workflow_widget(win, box)
    # Do not start monitoring or persist preferences just to render a state.
    with QSignalBlocker(box):
        box.setChecked(checked)
    box.setEnabled(enabled)
    win.workflow_tabs.tabBar().setFocus()
    QCursor.setPos(win.mapToGlobal(win.rect().bottomRight()))
    image = indicator_image(box, qapp)
    width = round(image.width() / image.devicePixelRatio())
    height = round(image.height() / image.devicePixelRatio())
    assert width == height and width >= 16
    border = "#b6e5f5" if checked else "#a7b5bd"
    if not enabled:
        border = "#75828b"
    # Four straight sides and square corners, not blank dark background.
    for x, y in ((0, 0), (width - 1, 0), (0, height - 1),
                 (width - 1, height - 1), (width // 2, 0),
                 (width // 2, height - 1), (0, height // 2),
                 (width - 1, height // 2)):
        assert colour_at(image, x, y) == border, (name, enabled, checked, x, y)
    interior = {colour_at(image, x, y)
                for x in range(3, width - 3) for y in range(3, height - 3)}
    if checked:
        assert "#101214" in interior, "Checked state must contain a visible tick"
        assert ("#8fd0e8" if enabled else "#75828b") in interior
    else:
        assert interior == {"#15191b" if enabled else "#191d1f"}
    assert box.isChecked() == checked
    assert not box.isTristate()
    assert client.submitted == []


@pytest.mark.parametrize("name", ["replace_source", "subs_suppress_srt"])
@pytest.mark.parametrize("checked", [False, True])
def test_focus_and_keyboard_mouse_semantics(ui, qapp, name, checked):
    win, client = ui
    win.resize(1180, 900)
    win.show()
    box: QCheckBox = getattr(win, name)
    reveal_workflow_widget(win, box)
    box.setChecked(checked)
    box.setFocus(Qt.FocusReason.TabFocusReason)
    QCursor.setPos(win.mapToGlobal(win.rect().bottomRight()))
    image = indicator_image(box, qapp)
    assert box.hasFocus()
    assert colour_at(image, 0, 0) == "#f0b45c"
    QTest.keyClick(box, Qt.Key.Key_Space)
    assert box.isChecked() != checked
    indicator_centre = QPoint(10, box.height() // 2)
    QTest.mouseClick(box, Qt.MouseButton.LeftButton, pos=indicator_centre)
    assert box.isChecked() == checked
    box.setEnabled(False)
    QTest.keyClick(box, Qt.Key.Key_Space)
    QTest.mouseClick(box, Qt.MouseButton.LeftButton, pos=indicator_centre)
    assert box.isChecked() == checked
    assert client.submitted == []


def test_system_palette_leaves_all_checkboxes_native(ui):
    win, _ = ui
    before = [getattr(win, name).isChecked() for name in CHECKBOXES]
    win._set_system_colours(True)
    assert win.styleSheet() == ""
    assert all(getattr(win, name).styleSheet() == "" for name in CHECKBOXES)
    win._set_system_colours(False)
    assert before == [getattr(win, name).isChecked() for name in CHECKBOXES]


def test_capture_workflow_checkbox_states(ui, qapp, tmp_path):
    win, client = ui
    win.resize(1180, 900)
    win.show()
    for checked in (False, True):
        for name in CHECKBOXES:
            box = getattr(win, name)
            with QSignalBlocker(box):
                box.setChecked(checked)
        for index, slug in ((0, "ai-upscale"), (1, "subtitles-only")):
            win.workflow_tabs.setCurrentIndex(index)
            scroll = win.setup_scroll if index == 0 else win.subs_setup_scroll
            # Show the checkbox rows, not just the top of a scrollable form.
            scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
            win.workflow_tabs.tabBar().setFocus()
            qapp.processEvents()
            names = CHECKBOXES[:5] if index == 0 else CHECKBOXES[5:7]
            for name in names:
                box = getattr(win, name)
                option = QStyleOptionButton()
                box.initStyleOption(option)
                rect = box.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, box)
                assert box.visibleRegion().contains(rect), name
            path = tmp_path / f"{slug}-{'checked' if checked else 'unchecked'}.png"
            assert win.grab().save(str(path))
            assert Path(path).stat().st_size > 0
            print("Workflow screenshot:", path)
    assert client.submitted == []
