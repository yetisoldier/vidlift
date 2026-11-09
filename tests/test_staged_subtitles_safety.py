"""Safety regressions for staged subtitle submission."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication, QMessageBox

from test_v0414_mode_b_gui import _ScriptedClient, _window
from vidlift.gui import MainWindow

_PRODUCTION_CONFIRM = MainWindow._confirm_options


def prepare_mode(win, tmp_path, kind):
    """Reconcile helper (Forge report deviation D-05): previously
    imported from the CUT tests/test_gui_submission_boundary.py (spec
    §2.4). Inlined verbatim so the kept destructive-review consent
    tests keep exercising their kept-surface assertions against the
    trimmed tree; no assertion changed."""
    from test_gui_best_practices import media
    from test_gui import _plan_for
    from vidlift.plan import BatchPlan

    if kind == "subs":
        path = media(tmp_path)
        win.subs_replace_source.setChecked(True)
        win.subs_suppress_srt.setChecked(True)
        win._enqueue_subs_paths([path])
        return path, win.start_subtitles
    plan = _plan_for(tmp_path)
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.replace_source.setChecked(True)
    win.suppress_srt.setChecked(True)
    return plan.report.path, win.start_upscale


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_clear_confirmation_preserves_staged_options(qapp, monkeypatch, tmp_path):
    import vidlift.gui as gui

    path = tmp_path / "episode.mkv"
    path.write_bytes(b"source")
    monkeypatch.setattr(gui, "subs_plan_verdict", lambda *a, **k: "ready")
    gen = _window(qapp, monkeypatch, tmp_path, _ScriptedClient())
    win = next(gen)
    win.subs_replace_source.setChecked(True)
    win._enqueue_subs_paths([str(path)])
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    win.clear_files()
    assert len(win._staged_subs) == 1
    assert win.file_table.rowCount() == 1
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    win.clear_files()
    assert win._staged_subs == []
    assert not win.start_subtitles_button.isEnabled()
    assert path.read_bytes() == b"source"
    next(gen, None)


@pytest.mark.parametrize("kind", ["subs", "run"])
@pytest.mark.parametrize("action", ["escape", "return", "accept"])
def test_real_destructive_review_requires_explicit_consent(qapp, monkeypatch, tmp_path, kind, action):
    from pathlib import Path
    from PyQt6.QtCore import QTimer, Qt
    from PyQt6.QtTest import QTest
    from vidlift.ui.review import BatchReviewDialog

    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    try:
        monkeypatch.setattr(win, "_confirm_options", _PRODUCTION_CONFIRM.__get__(win, MainWindow))
        source, start = prepare_mode(win, tmp_path, kind)
        job_id = client.make_job(source, [{"state": "queued"}], kind=kind)
        original = Path(source).read_bytes()
        sidecar = Path(source).with_suffix(".srt")
        sidecar.write_bytes(b"review sidecar sentinel")
        observed = []

        def interact():
            dialog = QApplication.activeModalWidget()
            if not isinstance(dialog, BatchReviewDialog):
                observed.append(None)
                if dialog is not None:
                    dialog.reject()
                return
            observed.append(dialog.review.toPlainText())
            if action == "accept":
                QTest.mouseClick(dialog.start_button, Qt.MouseButton.LeftButton)
            else:
                QTest.keyClick(dialog, Qt.Key.Key_Escape if action == "escape" else Qt.Key.Key_Return)

        QTimer.singleShot(30, interact)
        start()
        assert len(observed) == 1 and observed[0] is not None
        assert str(source) in observed[0]
        assert "replace" in observed[0].lower()
        assert client.submitted == ([job_id] if action == "accept" else [])
        assert Path(source).read_bytes() == original
        assert sidecar.read_bytes() == b"review sidecar sentinel"
    finally:
        next(gen, None)
