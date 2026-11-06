"""Safety regressions for staged subtitle submission."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication, QMessageBox

from test_v0414_mode_b_gui import _ScriptedClient, _window
from vidlift.gui import MainWindow

_PRODUCTION_CONFIRM = MainWindow._confirm_options


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_partial_failure_retains_row_and_options(qapp, monkeypatch, tmp_path):
    import vidlift.gui as gui

    paths = [tmp_path / f"episode-{i}.mkv" for i in range(3)]
    for path in paths:
        path.write_bytes(b"source")
    client = _ScriptedClient()
    for path in (paths[0], paths[2]):
        client.make_job(str(path), [{"state": "queued"}], kind="subs")
    monkeypatch.setattr(gui, "subs_plan_verdict", lambda *a, **k: "ready")
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    confirmations = []
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    # _window auto-accepts this seam; capture the actual review payload,
    # not the unrelated legacy QMessageBox.question path.
    monkeypatch.setattr(win, "_confirm_options",
                        lambda title, details: confirmations.append(details) or True)
    win.subs_replace_source.setChecked(True)
    win._enqueue_subs_paths([str(paths[0]), str(paths[1])])
    win.subs_replace_source.setChecked(False)
    win.subs_suppress_srt.setChecked(True)
    win._enqueue_subs_paths([str(paths[2])])
    original = client.enqueue_subs
    calls = []

    def enqueue(selected, **options):
        calls.append((selected[0], options))
        if selected[0] == str(paths[1]):
            raise OSError("disk full")
        return original(selected, **options)

    monkeypatch.setattr(client, "enqueue_subs", enqueue)
    win.start_subtitles()
    assert [p for p, _ in calls] == [str(p) for p in paths]
    batch = calls[0][1]["batch_id"]
    assert batch and all(options["batch_id"] == batch for _, options in calls)
    assert calls[0][1] == {"replace_source": True, "suppress_srt": False, "device": "auto", "batch_id": batch}
    assert calls[2][1] == {"replace_source": False, "suppress_srt": True, "device": "auto", "batch_id": batch}
    assert all(str(p) in confirmations[0] for p in paths)
    assert "\n" in confirmations[0]
    assert len(confirmations) == 1
    assert "replace" in confirmations[0].lower()
    assert "delete .srt sidecar after embed" in confirmations[0].lower()
    assert win.job_ids == ["VJ1", "VJ2"]
    assert [r["path"] for r in win._staged_subs] == [str(paths[1])]
    assert "disk full" in win._staged_subs[0]["error"]
    for job, index in win._batch_row_for_job.items():
        expected = paths[0] if job == "VJ1" else paths[2]
        assert win.file_table.item(index, 1).text() == str(expected)
    failed = next(i for i in range(win.file_table.rowCount())
                  if win.file_table.item(i, 1).text() == str(paths[1]))
    assert "disk full" in win.file_table.item(failed, 7).text()
    assert all(p.read_bytes() == b"source" for p in paths)
    next(gen, None)


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
    from test_gui_submission_boundary import prepare_mode
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
