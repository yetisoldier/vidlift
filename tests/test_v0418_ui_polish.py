"""v0.4.18 WS4 — QGroupBox title position + the F-1 Mode B pin.

Bob spec §12 file D:

  35 the applied stylesheet carries `QGroupBox#Controls` with
     `margin-top: 20px` (D12's margin-growth mechanism, the negative
     `top:` branch rejected) and the `::title` rule keeps its
     `subcontrol-position: top left` + `left: 12px` — with NO negative
     `top:` anywhere in the sheet.
  36 F-1 exhaustiveness: the responsive Mode B grid holds exactly eight
     layout items. Picker/actions/device sublayouts retain exhaustive
     leaf-widget membership, exact cells and the scope/options guidance.
  37 both new checkboxes: default OFF, persisted round-trip on a fresh
     window, four-key isolation (suppress_srt / subs_suppress_srt are
     cross-isolated from BOTH replace keys), tooltips contain the
     fallback contract word, and both paint REAL pixels (the
     v0.4.17 T2 hide-and-grab probe).
  38 the options source fences: start_upscale submits
     suppress_srt from Mode A's checkbox; _enqueue_subs_paths submits
     it from Mode B's checkbox (the checkbox state IS the submitted
     option).

Conventions: the v0.4.14 Mode B harness (offscreen, isolated QSettings,
scripted client); no real ffmpeg/whisper/network.
"""

from __future__ import annotations

import inspect
import os

import pytest

pytest.importorskip("PyQt6")

os.environ.setdefault("VIDLIFT_MONITOR_AUTOSTART", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel

from vidlift.gui import MainWindow

from test_v0414_mode_b_gui import _ScriptedClient, _window  # house harness

# the v0.4.17 paint probe (§0.1), imported per spec §12 file D-37
from test_v0417_ui_restructure import _paint_delta


# ---------------------------------------------------------------- harness
@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


SUBS_GRID_MEMBERS = (
    "add_subs_button",
    "subs_add_folder_button",
    "subs_replace_source",
    "subs_suppress_srt",
    "start_subtitles_button",
    "subs_device_combo",
    "subs_remove_button",
    "subs_clear_button",
)
SUBS_GRID_CELLS = [
    (0, 0, 1, 1),
    (0, 1, 1, 1),
    (1, 0, 1, 1),
    (1, 1, 1, 1),
    (2, 0, 1, 1),
    (2, 1, 1, 1),
    (3, 0, 1, 2),  # scope explainer
    (4, 0, 1, 2),  # snapshot/output guidance
]


# ------------------------------------------------------------------- 35
def test_35_qss_margin_growth_title_rule_intact(qapp, monkeypatch, tmp_path):
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    sheet = win.styleSheet()
    assert "QGroupBox#Controls {" in sheet
    assert "margin-top: 20px" in sheet, sheet[
        :500
    ]  # D12: the margin-growth mechanism, 14 -> 20
    # the ::title rule is byte-identical: origin/position/left retained
    assert "QGroupBox#Controls::title" in sheet
    assert "subcontrol-origin: margin" in sheet
    assert "subcontrol-position: top left" in sheet
    assert "left: 12px" in sheet
    # the rejected negative-offset branch is nowhere in the sheet
    assert "top: -" not in sheet
    next(gen, None)


# ------------------------------------------------------------------- 36
def test_36_f1_exhaustive_mode_b_membership_pin(qapp, monkeypatch, tmp_path):
    """D13 (spec deviation 5 recorded): the note's `== 3` cannot survive
    this release's own additions — the pin asserts the release's real
    final set with EXACT membership and cells."""
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    subs_grid = win.add_subs_button.parentWidget().layout()
    assert subs_grid.count() == len(SUBS_GRID_CELLS)

    def leaves(layout):
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() is not None:
                yield item.widget()
            elif item.layout() is not None:
                yield from leaves(item.layout())

    widgets = list(leaves(subs_grid))
    expected_attrs = {getattr(win, name) for name in SUBS_GRID_MEMBERS}
    labels = [w for w in widgets if isinstance(w, QLabel)]
    attr_widgets = [w for w in widgets if w not in labels]
    assert len(labels) == 3  # Device field label, scope and snapshot guidance.
    explainer = next(label for label in labels if label.text().startswith("Adds subtitles"))
    assert set(attr_widgets) == expected_attrs
    # the explainer is the v0.4.17 scope text (unchanged contract)
    assert explainer.text() == (
        "Adds subtitles from the file's own audio — video streams "
        "are copied (no upscale, no re-encode)."
    )
    # cells: exact row/col/spans for each widget
    assert [subs_grid.getItemPosition(i) for i in range(subs_grid.count())] == SUBS_GRID_CELLS
    next(gen, None)


# ------------------------------------------------------------------- 37
def test_37_checkboxes_default_off_persist_isolated_paint(
    qapp, monkeypatch, tmp_path
):
    import PyQt6.QtCore as QtCore

    settings_path = str(tmp_path / "settings.ini")

    def _read():
        _w = QtCore.QSettings(settings_path, QtCore.QSettings.Format.IniFormat)
        _w.sync()
        return _w

    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    # both default OFF on fresh settings (D5: strictly opt-in)
    assert win.suppress_srt.isChecked() is False
    assert win.subs_suppress_srt.isChecked() is False
    # labels are the S-18 exact strings
    assert win.suppress_srt.text() == "Suppress .srt sidecar on success"
    assert win.subs_suppress_srt.text() == "Suppress .srt sidecar on success"
    # the fallback contract words ride both tooltips (S-7/S-8)
    assert "fallback" in win.suppress_srt.toolTip().lower()
    assert "fallback" in win.subs_suppress_srt.toolTip().lower()

    # persistence, both directions, round-trip on a fresh window
    win.subs_suppress_srt.setChecked(True)
    assert _read().value("subs_suppress_srt", False, type=bool) is True
    win.suppress_srt.setChecked(True)
    assert _read().value("suppress_srt", False, type=bool) is True
    client2 = _ScriptedClient()
    _alive.append(MainWindow(client=client2))
    win2 = _alive[-1]
    assert win2.suppress_srt.isChecked() is True
    assert win2.subs_suppress_srt.isChecked() is True
    win2.suppress_srt.setChecked(False)
    win2.subs_suppress_srt.setChecked(False)
    assert _read().value("suppress_srt", False, type=bool) is False
    assert _read().value("subs_suppress_srt", False, type=bool) is False

    # the four-key isolation: each toggle writes ONLY its own key —
    # both suppress keys are cross-isolated from BOTH replace keys
    client3 = _ScriptedClient()
    _alive.append(MainWindow(client=client3))
    win3 = _alive[-1]
    win3.suppress_srt.setChecked(True)
    win3.subs_suppress_srt.setChecked(True)
    stored = _read()
    assert stored.contains("replace_source") is False
    assert stored.contains("subs_replace_source") is False
    assert stored.value("suppress_srt", False, type=bool) is True
    assert stored.value("subs_suppress_srt", False, type=bool) is True
    next(gen, None)


def test_37b_new_controls_paint_real_pixels(qapp, monkeypatch, tmp_path):
    """The T2 hide-and-grab probe (imported from
    test_v0417_ui_restructure) on both new checkboxes: hiding them must
    change pixels — the never-rendered Finding-1 class cannot return."""
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    for name in ("suppress_srt", "subs_suppress_srt"):
        widget = getattr(win, name)
        delta = _paint_delta(qapp, win, widget)
        assert delta > 0, (
            f"{name} painted 0 changed pixels when hidden — it is not "
            "actually visible (the Finding-1 twin defect class)"
        )
    next(gen, None)


# ------------------------------------------------------------------- 38
def test_38_options_source_fences():
    """File D-38: the checkbox state IS the submitted option — both
    submission fences carry the flag from their OWN checkbox."""
    start_src = inspect.getsource(MainWindow.start_upscale)
    assert '"suppress_srt": self.suppress_srt.isChecked()' in start_src
    enq_src = inspect.getsource(MainWindow._enqueue_subs_paths)
    assert "self.subs_suppress_srt.isChecked()" in enq_src
    assert '"suppress_srt": options.suppress_srt' in enq_src
    start_subs_src = inspect.getsource(MainWindow.start_subtitles)
    assert 'suppress_srt=row["suppress_srt"]' in start_subs_src
    # no cross-wiring: each fence names only its own checkbox
    assert "subs_suppress_srt" not in start_src
    assert "self.suppress_srt.isChecked" not in enq_src.replace(
        "self.subs_suppress_srt.isChecked", ""
    )


# the _alive list must exist for the fresh-window round trips (imported
# via _window's module above)
from test_v0414_mode_b_gui import _alive  # noqa: E402  (house harness)