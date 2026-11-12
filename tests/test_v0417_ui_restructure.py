"""v0.4.17 workstream-2 — the subtitle-surface UI restructure fences.

The main-window settings surface is two QGroupBox containers ("Upscale"
and "Add subtitles (no upscale)"), each with its own QGridLayout (the
K-1 fix: two independent column budgets). These tests pin, permanently:

 T1  the never-rendered fence: EVERY attribute-owned control is inside
     SOME layout reachable from the central widget (Finding 1's class —
     a created-but-never-laid-out widget — dies structurally here).
 T2  paint truth: each touched checkbox renders REAL pixels offscreen
     (the §0.1 hide-and-grab probe; resume_claim was 0 px pre-fix).
 T3  group separation: Mode B pair share one group, Mode A pair share
     the other, and the titled groups exist.
 T4  cell map + the general zero-overlap check on BOTH grids (the
     v0.4.9 resume/monitor overlap class, Karen's method, enforced).
 T5  the K-1 width gate (≤1360; reference box ≈1290 = v0.4.16.2
     parity) and the height ceiling (≤725).
 T6  transcribe visibility + persistence both directions under key
     "transcribe_subs" (default ON, now VISIBLE).
 T7  resume/monitor row-2 contract: resume lives in the upscale group.
 T8  the K-2 "Source file:" detail strings in both postures (+ the
     unknown-value passthrough + "Resolution handling" forbidden).
 T9  wiring fences: the restructure did not rewire any read site.
 T10 plan/render threading still threads the transcribe checkbox both
     ways (the F6 pair, re-pinned against the new layout).

Harness: the v0.4.14 Mode B scripted-client suite (house pattern —
test_v04151_subs_guard.py imports the same objects): offscreen,
per-run isolated QSettings ini, no real ffmpeg/whisper/network.
NOTE: the T5 px gates live in THIS file (not test_gui.py) because
Windows CI enumerates test_gui.py and its font metrics differ — the
ceiling carries slack (see the T5 provenance comment).
"""

from __future__ import annotations

import inspect
import os

import pytest
from _gui_checks import wait_for_checks

pytest.importorskip("PyQt6")

os.environ.setdefault("VIDLIFT_MONITOR_AUTOSTART", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (
    QApplication,
    QGroupBox,
    QLabel,
    QMessageBox,
    QTableWidgetItem,
    QScrollArea,
    QTabWidget,
)

from vidlift.gui import MainWindow
from vidlift.plan import build_file_plan

from test_v0414_mode_b_gui import (  # noqa: E402  (house harness import)
    _alive,
    _plan_for,
    _ScriptedClient,
    _window,
)


# ---------------------------------------------------------------- harness
@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


# §6.3 exact strings (the assertion source; keep in one place).
EXPLAINER_TEXT = (
    "Adds subtitles from the file's own audio — video streams "
    "are copied (no upscale, no re-encode)."
)
SOURCE_FILE_REPLACED = (
    "Source file: replaced on success — output renamed to <name>.mkv"
)
SOURCE_FILE_KEPT = "Source file: kept — output beside it as <name>.subs.mkv"
COLUMN3_REPLACE = "replace original → .mkv"
COLUMN3_KEEP = "keep original"

ALL_MODE_A_CONTROLS = (
    "transcribe_subs",
    "subtitle_ocr",
    "replace_source",
    "resume_claim",
    "monitor_check",
    "engine_combo",
    "profile_combo",
    "denoise_combo",
    "gbph",
    "output_dir",
    "plan_button",
    "start_button",
    "cancel_button",
    "cancel_batch_button",
    # v0.4.18 (§12 File E): the Mode A suppress-sidecar checkbox rides
    # the upscale group at (3, 6, 1, 3) — same T1 fence.
    "suppress_srt",
)


def _all_laid_out_widgets(win) -> set:
    """Every widget reachable from the central widget through ANY nested
    layout — descending BOTH through layout items (itemAt(i).layout(),
    e.g. the title row) AND each widget's own widget-layout (the two
    QGroupBox grids are widget layouts, so a layout-items-only walk
    would never reach them). A widget created but never added to a
    layout never appears here (T1's kill condition)."""
    seen: set = set()
    root = win.centralWidget().layout()

    def walk(layout):
        if layout is None:
            return
        for index in range(layout.count()):
            item = layout.itemAt(index)
            widget = item.widget()
            if widget is not None and widget not in seen:
                visit(widget)
            sub = item.layout()
            if sub is not None:
                walk(sub)

    def visit(widget):
        seen.add(widget)
        walk(widget.layout())
        if isinstance(widget, QScrollArea):
            content = widget.widget()
            if content is not None:
                walk(content.layout())
        if isinstance(widget, QTabWidget):
            for index in range(widget.count()):
                page = widget.widget(index)
                if page is not None and page not in seen:
                    visit(page)

    walk(root)
    return seen


def _grid_items(layout) -> list[tuple[int, int, int, int]]:
    """(row, col, rowspan, colspan) for every item in a QGridLayout."""
    return [layout.getItemPosition(index) for index in range(layout.count())]


# ------------------------------------------------------------------- T1
def test_every_attribute_control_is_laid_out_including_explainer(
    qapp, monkeypatch, tmp_path
):
    """T1 — the never-rendered fence. If ANY of the 16 attribute-owned
    controls (or the Mode B scope explainer) is created but not inside
    a reachable layout, this fails (Finding 1's exact class)."""
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    laid_out = _all_laid_out_widgets(win)
    for name in ALL_MODE_A_CONTROLS:
        widget = getattr(win, name)
        assert widget in laid_out, (
            f"{name} is constructed but reaches NO layout — the "
            "Finding-1 class (never-visible control) is back"
        )
    for name in (
        "add_subs_button",
        "subs_add_folder_button",  # v0.4.18 (WS1): the Mode B folder twin
        "subs_replace_source",
        "subs_suppress_srt",  # v0.4.18 (WS2): the Mode B suppress checkbox
        "subs_device_combo", "subs_remove_button", "subs_clear_button", "start_subtitles_button",
    ):
        assert getattr(win, name) in laid_out
    # the §6.3 explainer exists as a parented label with the exact text
    texts = {label.text() for label in win.findChildren(QLabel)}
    assert EXPLAINER_TEXT in texts
    next(gen, None)


# ------------------------------------------------------------------- T2
def _paint_delta(qapp, win, widget) -> int:
    """Compare actual viewport pixels without a hide-induced layout/scroll jump."""
    from PyQt6.QtCore import QRect, QPoint
    from _gui_checks import reveal_workflow_widget
    win.show()
    qapp.processEvents()
    parent = reveal_workflow_widget(win, widget)
    qapp.processEvents()
    assert not widget.visibleRegion().isEmpty()
    policy = widget.sizePolicy()
    previous = policy.retainSizeWhenHidden()
    policy.setRetainSizeWhenHidden(True)
    widget.setSizePolicy(policy)
    before = parent.grab().toImage()
    if before.isNull():
        pytest.skip("offscreen platform cannot rasterize; T1/T4 still hold")
    geo = QRect(widget.mapTo(parent, QPoint()), widget.size()).intersected(parent.rect())
    assert not geo.isEmpty()
    widget.hide()
    qapp.processEvents()
    after = parent.grab().toImage()
    widget.show()
    policy.setRetainSizeWhenHidden(previous)
    widget.setSizePolicy(policy)
    qapp.processEvents()
    delta = 0
    for y in range(geo.top(), geo.bottom() + 1):
        for x in range(geo.left(), geo.right() + 1):
            if before.pixel(x, y) != after.pixel(x, y):
                delta += 1
    return delta


def test_touched_checkboxes_render_real_pixels(qapp, monkeypatch, tmp_path):
    """T2 — paint truth. transcribe_subs (Finding 1) and resume_claim
    (Finding-1 twin) painted 0 pixels pre-fix; the §0.1 probe makes
    'visible' mean 'pixels changed when the widget hides'."""
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    for name in (
        "transcribe_subs",
        "resume_claim",
        "monitor_check",
        "subs_replace_source",
        "replace_source",
        # v0.4.18 (§12 File E): both new suppress checkboxes paint real
        # pixels — the Finding-1 twin fence extends to them.
        "suppress_srt",
        "subs_suppress_srt",
    ):
        widget = getattr(win, name)
        delta = _paint_delta(qapp, win, widget)
        assert delta > 0, (
            f"{name} painted 0 changed pixels when hidden — it is not "
            "actually visible (the Finding-1 twin defect class)"
        )
    next(gen, None)


# ------------------------------------------------------------------- T3
def test_group_separation_two_titled_surfaces(qapp, monkeypatch, tmp_path):
    """T3 — Mode B pair share the Mode B group; Mode A pair share the
    upscale group; both titled groups exist with the §6.3 titles."""
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    upscale = win.replace_source.parentWidget()
    subs = win.add_subs_button.parentWidget()
    assert win.subs_replace_source.parentWidget() is not upscale
    assert win.add_subs_button.parentWidget() is win.subs_replace_source.parentWidget()
    assert win.transcribe_subs.parentWidget() is win.replace_source.parentWidget()
    # v0.4.18 (§12 File E): parentage pins for the new controls — the
    # Mode A suppress checkbox in the upscale group; the Mode B folder
    # button + suppress checkbox in the subs group.
    assert win.suppress_srt.parentWidget() is upscale
    assert win.subs_add_folder_button.parentWidget() is subs
    assert win.subs_suppress_srt.parentWidget() is subs
    assert win.resume_claim.parentWidget() is upscale
    assert not win.workflow_tabs.isAncestorOf(win.monitor_check)
    titles = {group.title() for group in win.findChildren(QGroupBox)}
    assert titles >= {"Upscale", "Add subtitles (no upscale)"}
    next(gen, None)


# ------------------------------------------------------------------- T4
def test_cell_map_and_zero_overlap_on_both_grids(qapp, monkeypatch, tmp_path):
    """T4 — the pinned cell map per spec §3, plus the GENERAL pairwise
    no-intersection check over every item of BOTH grids (Karen's
    programmatic method; kills the v0.4.9 overlap class in-repo)."""
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    upscale_grid = win.transcribe_subs.parentWidget().layout()
    subs_grid = win.add_subs_button.parentWidget().layout()

    def _pos(layout, widget):
        def contains(lay):
            return any(lay.itemAt(i).widget() is widget or
                       (lay.itemAt(i).layout() is not None and contains(lay.itemAt(i).layout()))
                       for i in range(lay.count()))
        for index in range(layout.count()):
            entry = layout.itemAt(index)
            if entry.widget() is widget or (entry.layout() is not None and contains(entry.layout())):
                return layout.getItemPosition(index)
        raise AssertionError("widget missing from grid")

    assert _pos(subs_grid, win.add_subs_button) == (0, 0, 1, 1)
    # v0.4.18 (§12 File E): the subs cell map re-pinned for the 4-A9
    # re-tile — replace moved (0,1) → (0,2); the new folder button +
    # Start, replace, suppress, and explainer occupy (0,2) through (0,5).
    assert _pos(subs_grid, win.subs_add_folder_button) == (0, 0, 1, 1)
    assert _pos(subs_grid, win.subs_replace_source) == (2, 0, 1, 1)
    assert _pos(subs_grid, win.subs_suppress_srt) == (2, 1, 1, 1)
    assert _pos(upscale_grid, win.transcribe_subs) == (4, 1, 1, 1)
    # v0.4.18: the Mode A suppress checkbox's verified-free cell
    assert _pos(upscale_grid, win.suppress_srt) == (5, 1, 1, 1)
    assert _pos(upscale_grid, win.resume_claim) == (3, 1, 1, 1)
    assert not win.workflow_tabs.isAncestorOf(win.monitor_check)
    assert _pos(upscale_grid, win.replace_source) == (5, 0, 1, 1)

    for layout in (upscale_grid, subs_grid):
        rects = _grid_items(layout)
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                r1, c1, rs1, cs1 = rects[i]
                r2, c2, rs2, cs2 = rects[j]
                rows_overlap = r1 < r2 + rs2 and r2 < r1 + rs1
                cols_overlap = c1 < c2 + cs2 and c2 < c1 + cs1
                assert not (rows_overlap and cols_overlap), (
                    f"grid items {rects[i]} and {rects[j]} overlap — the "
                    "v0.4.9 painting-over class is regressing"
                )
    next(gen, None)


# ------------------------------------------------------------------- T5
def test_window_minimum_size_gates(qapp, monkeypatch, tmp_path):
    """T5 — K-1's headline gate. Provenance (reference box, offscreen,
    workstream-2 spec §0.1/§3): measured window minimum 1290×696 vs the
    pre-restructure 1532×595; 1290 is EXACT v0.4.16.2 width parity. The
    ceiling 1360 sits under the 1366px laptop edge K-1 named (font-
    metric slack for non-reference platforms); height ceiling 725
    leaves the 1366×768 laptop with a 40px taskbar ≈728px client."""
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.show()
    msh = win.minimumSizeHint()
    assert msh.width() <= 1360, (
        f"window minimum width {msh.width()}px exceeds the 1360px "
        "ceiling (K-1 regression reopen)"
    )
    assert msh.height() <= 725, (
        f"window minimum height {msh.height()}px exceeds the 725px "
        "ceiling (1366×768 laptop gate)"
    )
    next(gen, None)


# ------------------------------------------------------------------- T6
def test_transcribe_visible_on_fresh_settings_and_persists(
    qapp, monkeypatch, tmp_path
):
    """T6 — Finding 1's functional half: the always-on default is
    VISIBLE (checked ON on fresh settings), toggling persists
    'transcribe_subs' and a fresh window restores BOTH directions, the
    label is the §6.3 short string, the tooltip keeps the honesty text
    (1.5 GB model + no-track condition), and Mode B's key is untouched."""
    import PyQt6.QtCore as QtCore

    settings_path = str(tmp_path / "settings.ini")

    def _read():
        _w = QtCore.QSettings(settings_path, QtCore.QSettings.Format.IniFormat)
        _w.sync()
        return _w

    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    # fresh settings render the default-ON state visibly
    assert win.transcribe_subs.isChecked() is True
    assert win.transcribe_subs.text() == "Transcribe missing subtitles"
    tooltip = win.transcribe_subs.toolTip().lower()
    assert "1.5 gb" in tooltip and "no subtitle track" in tooltip
    # OFF persists and a fresh window restores it (item-26 pattern)
    win.transcribe_subs.setChecked(False)
    assert _read().value("transcribe_subs", True, type=bool) is False
    client2 = _ScriptedClient()
    _alive.append(MainWindow(client=client2))
    win2 = _alive[-1]
    assert win2.transcribe_subs.isChecked() is False
    # ON persists and restores too
    win2.transcribe_subs.setChecked(True)
    assert _read().value("transcribe_subs", True, type=bool) is True
    client3 = _ScriptedClient()
    _alive.append(MainWindow(client=client3))
    win3 = _alive[-1]
    assert win3.transcribe_subs.isChecked() is True
    # Mode B's key was never touched by any of the Mode A toggles
    assert _read().contains("subs_replace_source") is False
    next(gen, None)


# ------------------------------------------------------------------- T7
def test_resume_lives_in_upscale_group_row2(qapp, monkeypatch, tmp_path):
    """T7 — the D5 contract: resume_claim is a Mode A control in the
    upscale group; its renderability is proven by T2's nonzero delta
    (the exact regression this release fixes)."""
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    assert win.resume_claim.parentWidget() is win.replace_source.parentWidget()
    grid = win.resume_claim.parentWidget().layout()
    positions = [
        grid.getItemPosition(index)
        for index in range(grid.count())
        if grid.itemAt(index).widget() is win.resume_claim
    ]
    assert positions == [(3, 1, 1, 1)]
    next(gen, None)


# ------------------------------------------------------------------- T8
def test_subs_detail_source_fate_lines_k2(qapp, monkeypatch, tmp_path):
    """T8 — the K-2 renderer. The enqueue→select flow shows the ON and
    OFF lines verbatim (§6.3) and 'Resolution handling' never appears;
    the direct unit call pins all THREE mapping arms."""
    media = tmp_path / "episode.mkv"
    media.write_bytes(b"x")
    import vidlift.gui as gui_mod

    def _detail_for(checked):
        _client = _ScriptedClient()
        _client.make_job(str(media), [{"state": "queued"}], kind="subs")
        _gen = _window(qapp, monkeypatch, tmp_path, _client)
        _win = next(_gen)
        if checked:
            _win.subs_replace_source.setChecked(True)
        monkeypatch.setattr(
            gui_mod.QFileDialog,
            "getOpenFileNames",
            staticmethod(lambda *a, **k: ([str(media)], "")),
        )
        _win.add_subs_button.click()
        wait_for_checks(_win)
        _win.file_table.selectRow(0)
        text = _win.details.toPlainText()
        next(_gen, None)
        return text

    # ON posture (enqueue carried replace_source=True — column 3 agrees)
    detail_on = _detail_for(True)
    assert SOURCE_FILE_REPLACED in detail_on
    assert "Resolution handling" not in detail_on
    # park the shared settings OFF again, then prove the OFF posture
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.subs_replace_source.setChecked(False)
    next(gen, None)
    detail_off = _detail_for(False)
    assert SOURCE_FILE_KEPT in detail_off
    assert "Resolution handling" not in detail_off

    # unit arms: the renderer maps the column-3 literals (+ passthrough)
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    targets = {
        0: COLUMN3_REPLACE,
        1: COLUMN3_KEEP,
        2: "720p",  # unknown future value → honest passthrough
    }
    win.file_table.setRowCount(len(targets))
    for row, target in targets.items():
        win.file_table.setItem(row, 3, QTableWidgetItem(target))
        win.file_table.setItem(row, 1, QTableWidgetItem(str(media)))
        detail = win._subs_detail_text(row)
        if row == 0:
            assert SOURCE_FILE_REPLACED in detail
        elif row == 1:
            assert SOURCE_FILE_KEPT in detail
        else:
            assert "Source file: 720p" in detail
        assert "Resolution handling" not in detail
        # silence-implying-default is dead: the OFF arm renders a line
    next(gen, None)


# ------------------------------------------------------------------- T9
def test_wiring_fences_reads_not_rewired():
    """T9 — the restructure must not have rewired any read site (the
    test-29 cross-wire fence class, re-pinned here so this file alone
    proves the new layout reads the same widgets)."""
    start_src = inspect.getsource(MainWindow.start_upscale)
    assert '"transcription_enabled": self.transcribe_subs.isChecked()' in start_src
    assert '"replace_source": self.replace_source.isChecked()' in start_src
    assert '"resume": self.resume_claim.isChecked()' in start_src
    assert "subs_replace_source" not in start_src
    # v0.4.18 (§12 File E): the options fence extends to suppress_srt
    assert '"suppress_srt": self.suppress_srt.isChecked()' in start_src
    # v0.4.18 (4-A6/4-A7): the Mode B fence re-targeted to the extracted
    # _enqueue_subs_paths — the enqueue line MOVED, the fence follows;
    # the assertion text is extended with suppress_srt (File D-38).
    subs_src = inspect.getsource(MainWindow._enqueue_subs_paths)
    assert "SubtitleOptions(self.subs_replace_source.isChecked()" in subs_src
    assert "self.subs_suppress_srt.isChecked()" in subs_src
    assert '"replace_source": options.replace_source' in subs_src
    assert '"suppress_srt": options.suppress_srt' in subs_src
    assert "self.replace_source.isChecked()" not in subs_src


# ------------------------------------------------------------------ T10
def _mode_a_report():
    payload = """
    {
      "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "mpeg4",
         "width": 640, "height": 360, "sample_aspect_ratio": "1:1",
         "field_order": "progressive", "r_frame_rate": "25/1",
         "avg_frame_rate": "25/1"},
        {"index": 1, "codec_type": "audio", "codec_name": "mp3", "channels": 2}
      ],
      "format": {"format_name": "avi", "duration": "3600.0"}
    }
    """
    from vidlift.probe import classify, parse_probe

    return classify(parse_probe("/media/movie.avi", payload))


def test_plan_batch_threads_transcribe_checkbox(qapp, monkeypatch, tmp_path):
    """T10a — the plan seam threads the checkbox both ways against the
    restructured window (the test_gui.py F6 pair, ported)."""
    from vidlift.plan import build_batch_plan as real_build_batch_plan
    from vidlift.run import ToolBox

    import vidlift.gui as gui_mod

    captured: dict = {}

    def spy(reports, **kwargs):
        captured.update(kwargs)
        return real_build_batch_plan(reports, **kwargs)

    monkeypatch.setattr(gui_mod, "build_batch_plan", spy)
    monkeypatch.setattr(
        gui_mod, "probe_and_classify", lambda path, binary: _mode_a_report()
    )
    monkeypatch.setattr(
        gui_mod, "find_tools", lambda: ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
    )
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.files = ["/media/movie.avi"]
    win.transcribe_subs.setChecked(False)
    win.plan_batch()
    assert captured.get("transcription_enabled") is False
    win.transcribe_subs.setChecked(True)
    win.plan_batch()
    assert captured.get("transcription_enabled") is True
    next(gen, None)


@pytest.mark.usefixtures("transcription_engine_on")
def test_render_batch_disabled_note_override(qapp, monkeypatch, tmp_path):
    """T10b — the honest disabled note override (the F6 render half):
    a transcribed plan renders the disabled note under an unchecked
    checkbox, and restores the plan note when re-checked. Needs the
    engine-on fixture exactly like its test_gui.py original (the
    suite's autouse guard keeps transcription off, and without it the
    transcribed plan's subtitle_plan is None)."""
    from vidlift.plan import BatchPlan

    report = _mode_a_report()
    plan = build_file_plan(report, gb_per_hour=1.5)
    assert plan.subtitle_plan is not None
    assert plan.subtitle_plan.source == "transcribed"
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.file_table.setRowCount(1)
    win.transcribe_subs.setChecked(False)
    win.render_batch(BatchPlan([plan]))
    assert win.file_table.item(0, 6).text() == (
        "transcription disabled; no subtitles transcribed"
    )
    win.transcribe_subs.setChecked(True)
    win.render_batch(BatchPlan([plan]))
    assert win.file_table.item(0, 6).text() == plan.subtitle_note
    next(gen, None)