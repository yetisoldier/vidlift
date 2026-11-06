"""UI best-practices regressions: synthetic inputs, isolated settings and no media runs."""
import os
from types import SimpleNamespace

import pytest
from _gui_checks import gui_application, wait_for_checks, reveal_workflow_widget

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")
from PyQt6.QtCore import Qt, QUrl, QPoint
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from vidlift.plan import BatchPlan
from vidlift.ui.view_state import SubtitleOptions, positive_gbph
from test_v0414_mode_b_gui import _ScriptedClient, _window
from test_gui import _plan_for


@pytest.fixture
def qapp():
    return gui_application()


@pytest.fixture
def ui(qapp, monkeypatch, tmp_path):
    import vidlift.gui as gui
    client = _ScriptedClient()
    monkeypatch.setattr(gui, "subs_plan_verdict", lambda *a, **k: "ready")
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    generator = _window(qapp, monkeypatch, tmp_path, client)
    win = next(generator)
    yield win, client
    next(generator, None)


def media(tmp_path, name="episode.mkv"):
    path = tmp_path / name
    path.write_bytes(b"synthetic source sentinel")
    return str(path)


@pytest.mark.parametrize("text", ["", " ", "oops", "0", "-0.1", "NaN", "inf", "-inf"])
def test_invalid_size_never_enqueues(ui, tmp_path, text):
    win, client = ui
    win.batch = BatchPlan([_plan_for(tmp_path)])
    win.render_batch(win.batch)
    win.gbph.setText(text)
    win.plan_batch()
    win.start_upscale()
    assert client.submitted == []
    assert "finite number greater than zero" in win.message_label.text()


def test_size_and_snapshots_are_pure_and_frozen():
    assert positive_gbph(" 1.5 ") == 1.5
    from dataclasses import FrozenInstanceError
    options = SubtitleOptions(device="cuda", suppress_srt=True)
    with pytest.raises(FrozenInstanceError):
        options.device = "cpu"
    assert "kept if embedding fails" in options.sidecar_note
    with pytest.raises(ValueError):
        SubtitleOptions(device="gpu")


def test_device_and_policy_snapshots_drive_display_and_submission(ui, tmp_path, monkeypatch):
    win, client = ui
    paths = [media(tmp_path, n) for n in ("a.mkv", "b.mkv")]
    for p in paths:
        client.make_job(p, [{"state": "queued"}], kind="subs")
    win.subs_device_combo.setCurrentIndex(win.subs_device_combo.findData("cuda"))
    win.subs_suppress_srt.setChecked(True)
    win._enqueue_subs_paths([paths[0]])
    win.subs_device_combo.setCurrentIndex(win.subs_device_combo.findData("cpu"))
    win.subs_suppress_srt.setChecked(False)
    win._enqueue_subs_paths([paths[1]])
    assert "CUDA requested" in win._subs_detail_text(0)
    assert "SRT removed only after successful embed" in win._subs_detail_text(0)
    assert "SRT sidecar kept" in win._subs_detail_text(1)
    calls = []
    enqueue = client.enqueue_subs
    def record(paths, **options):
        calls.append(options)
        return enqueue(paths, **options)
    monkeypatch.setattr(client, "enqueue_subs", record)
    win.start_subtitles()
    assert [c["device"] for c in calls] == ["cuda", "cpu"]
    assert win._staged_subs == []
    assert "CUDA requested" in win._subs_detail_text(0)
    assert win._settings().value("subs_device") == "cpu"
    assert not win._settings().contains("transcription_device")


def test_upscale_add_and_plan_preserve_staged_identity_and_details(ui, tmp_path):
    win, client = ui
    path = media(tmp_path)
    win._enqueue_subs_paths([path])
    saved_id = win._staged_subs[0]["id"]
    plan = _plan_for(tmp_path)
    win._add_paths([plan.report.path])
    assert win.file_table.rowCount() == 2
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    assert win.file_table.rowCount() == 2
    assert win._row_meta(1)["staged_id"] == saved_id
    win._render_detail_for_row(1)
    assert "Staged only" in win.details.toPlainText()
    assert client.submitted == []


def test_remove_and_clear_staged_during_run_preserve_job_map(ui, tmp_path):
    win, client = ui
    paths = [media(tmp_path, n) for n in ("run.mkv", "later.mkv")]
    client.make_job(paths[0], [{"state": "queued"}], kind="subs")
    win._enqueue_subs_paths([paths[0]])
    win.start_subtitles()
    job_map = dict(win._batch_row_for_job)
    win._enqueue_subs_paths([paths[1]])
    assert not win.start_subtitles_button.isEnabled()
    assert "active batch" in win.subs_start_help.text()
    # Add itself never submits, even while another job runs.
    assert len(client.submitted) == 1
    win.file_table.selectRow(1)
    win.remove_staged_subtitles()
    assert win._staged_subs == []
    assert win._batch_row_for_job == job_map
    assert win.file_table.item(job_map["VJ1"], 1).text() == paths[0]
    win._enqueue_subs_paths([paths[1]])
    win.clear_staged_subtitles()
    assert win._batch_row_for_job == job_map
    assert client.cancel_calls == []
    assert client.cancel_batch_calls == []
    from pathlib import Path
    assert all(Path(p).read_bytes() == b"synthetic source sentinel" for p in paths)


def test_changed_eligibility_updates_one_retained_row(ui, tmp_path, monkeypatch):
    import vidlift.gui as gui
    win, client = ui
    path = media(tmp_path)
    win._enqueue_subs_paths([path])
    monkeypatch.setattr(gui, "subs_plan_verdict", lambda *a, **k: "blocked-output-exists")
    win.start_subtitles()
    assert win.file_table.rowCount() == 1
    assert win._staged_subs[0]["verdict"] == "blocked-output-exists"
    assert not win.start_subtitles_button.isEnabled()
    assert "not be overwritten" in win.file_table.item(0, 7).text()
    assert client.submitted == []


def test_ambiguous_recording_error_cannot_be_blindly_retried(ui, tmp_path, monkeypatch):
    win, client = ui
    win._enqueue_subs_paths([media(tmp_path)])
    calls = []
    def fail(*a, **k):
        calls.append(k)
        raise OSError("disk full during recording")
    monkeypatch.setattr(client, "enqueue_subs", fail)
    win.start_subtitles()
    win.start_subtitles()
    assert len(calls) == 1
    assert win._staged_subs[0]["uncertain"]
    assert not win.start_subtitles_button.isEnabled()
    assert "submission unknown" == win.file_table.item(0, 0).text()


@pytest.mark.parametrize("change", ["size", "destination", "transcription", "ocr"])
def test_plan_input_changes_require_replan(ui, tmp_path, monkeypatch, change):
    import vidlift.gui as gui
    win, client = ui
    plan = _plan_for(tmp_path)
    win.files = [plan.report.path]
    monkeypatch.setattr(gui, "find_tools", lambda: SimpleNamespace(ffprobe="unused"))
    monkeypatch.setattr(gui, "probe_and_classify", lambda *a, **k: plan.report)
    win.plan_batch()
    assert win.start_button.isEnabled()
    if change == "size":
        win.gbph.setText("2.5")
    elif change == "destination":
        win.output_dir.setText(str(tmp_path / "different"))
    elif change == "transcription":
        win.transcribe_subs.toggle()
    else:
        win.subtitle_ocr.toggle()
    assert not win.start_button.isEnabled()
    win.start_upscale()
    assert client.submitted == []
    assert "Plan again" in win.message_label.text()
    win.plan_batch()
    assert win.start_button.isEnabled()


@pytest.mark.parametrize("outcomes", [["failed", "succeeded"], ["cancelled", "succeeded"]])
def test_final_summary_includes_earlier_outcomes(ui, outcomes):
    win, _ = ui
    win.run_counts = outcomes
    win._batch_finished(["last.mkv: succeeded"])
    assert "successfully" not in win.summary_count.text()
    assert ("failures" if "failed" in outcomes else "cancelled") in win.summary_count.text()


def test_confirmation_default_no_and_no_media_mutation(ui, tmp_path, monkeypatch):
    win, client = ui
    path = media(tmp_path)
    win.subs_replace_source.setChecked(True)
    win._enqueue_subs_paths([path])
    captured = []
    from PyQt6.QtCore import QTimer
    from vidlift.ui.review import BatchReviewDialog
    def no(title, details):
        dialog = BatchReviewDialog(title, details, win)
        captured.append(dialog)
        QTimer.singleShot(0, dialog.back_button.click)
        return dialog.exec() == dialog.DialogCode.Accepted
    monkeypatch.setattr(win, "_confirm_options", no)
    win.start_subtitles()
    assert captured[0].back_button.isDefault()
    assert not captured[0].start_button.isDefault()
    assert path in captured[0].review.toPlainText()
    assert len(win._staged_subs) == 1
    assert client.submitted == []


def test_subtitle_drop_routes_to_staging_not_upscale(ui, tmp_path):
    win, client = ui
    path = media(tmp_path)
    event = SimpleNamespace(mimeData=lambda: SimpleNamespace(urls=lambda: [QUrl.fromLocalFile(path)]),
                            acceptProposedAction=lambda: None)
    win.controls_subs.dropEvent(event)
    wait_for_checks(win)
    assert win.files == []
    assert win._staged_subs[0]["path"] == path
    assert client.submitted == []


def test_readonly_history_and_open_actions(ui, tmp_path, monkeypatch):
    import vidlift.gui as gui
    win, client = ui
    output = tmp_path / "synthetic output.mp4"
    output.write_bytes(b"not real media")
    history = [{"job_id": "old", "file_path": "synthetic input.mkv", "state": "failed",
                "error": "fixture failure", "output_path": str(output)}]
    monkeypatch.setattr(client, "history", lambda **k: history)
    win._render_recent_jobs()
    win.queue_tabs.setCurrentIndex(1)
    win.history_table.selectRow(0)
    assert "fixture failure" in win.details.toPlainText()
    assert client.submitted == []
    opened = []
    monkeypatch.setattr(gui.QDesktopServices, "openUrl", staticmethod(lambda u: opened.append(u.toLocalFile()) or True))
    win._open_recorded_path("output_path")
    assert opened == [str(tmp_path)]
    output.unlink()
    win._open_recorded_path("output_path")
    assert len(opened) == 1
    assert "No existing recorded file" in win.message_label.text()


def test_keyboard_device_and_accessible_controls(ui, qapp):
    win, _ = ui
    win.show()
    reveal_workflow_widget(win, win.subs_device_combo)
    win.subs_device_combo.setFocus()
    QTest.keyClick(win.subs_device_combo, Qt.Key.Key_End)
    assert win.subs_device_combo.currentData() == "cpu"
    assert win.subs_device_combo.accessibleName() == "Subtitle transcription device requested"
    assert win.gbph.accessibleName().endswith("GB per hour")
    assert win.stage_bar.bar.accessibleName() == "Current file progress"
    assert ":focus" in win.styleSheet()
    assert win.cancel_button.parentWidget() is win.run_panel
    qapp.processEvents()
    assert win.minimumSizeHint().width() <= 960
    assert win.minimumSizeHint().height() <= 600


def test_synthetic_geometry_probe(ui, qapp, tmp_path):
    win, _ = ui
    win.resize(960, 600)
    win.show()
    qapp.processEvents()
    for name in ("transcribe_subs", "subs_replace_source", "replace_source", "subs_suppress_srt"):
        widget = getattr(win, name)
        viewport = reveal_workflow_widget(win, widget)
        qapp.processEvents()
        print(name, "geometry", widget.geometry(), "visible", widget.visibleRegion().boundingRect(),
              "parent", widget.parentWidget().size(), "viewport", viewport.size())
        assert not widget.visibleRegion().isEmpty(), name
        image = tmp_path / (name + ".png")
        win.grab().save(str(image))
        print("Synthetic screenshot:", image)


def _drain_until(qapp, predicate, seconds=3):
    import time
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(5)
    assert predicate(), "Qt worker did not finish within the bounded fixture deadline"


def test_background_plan_keeps_events_alive_and_discards_stale_results(ui, qapp, tmp_path, monkeypatch):
    import threading
    import vidlift.gui as gui
    from PyQt6.QtCore import QTimer
    win, client = ui
    plan = _plan_for(tmp_path)
    win.files = [plan.report.path]
    started, release = threading.Event(), threading.Event()
    worker_threads = []
    def probe(*a, **k):
        worker_threads.append(threading.get_ident())
        started.set()
        release.wait(2)
        return plan.report
    monkeypatch.setattr(gui, "find_tools", lambda: SimpleNamespace(ffprobe="not executed"))
    monkeypatch.setattr(gui, "probe_and_classify", probe)
    try:
        win.request_plan()
        assert started.wait(1)
        assert win._plan_checking
        assert not win.start_button.isEnabled()
        heartbeat = []
        QTimer.singleShot(0, lambda: heartbeat.append(True))
        qapp.processEvents()
        assert heartbeat == [True]
        assert len(worker_threads) == 1
        assert worker_threads[0] != threading.get_ident()
        win.gbph.setText("2.25")
        assert not win._plan_checking
        release.set()
        _drain_until(qapp, lambda: not win._preflights)
        assert win.batch is None
        assert client.submitted == []
        win.request_plan()
        _drain_until(qapp, lambda: not win._preflights)
        assert win.batch is not None
        assert win._planned_inputs.gbph == 2.25
        assert win.start_button.isEnabled()
        assert client.submitted == []
    finally:
        release.set()
        _drain_until(qapp, lambda: not win._preflights)


def test_preflight_runner_bounds_and_reaps_only_its_child(monkeypatch):
    import sys
    import threading
    import vidlift.ui.preflight as preflight
    from vidlift.probe import ProbeError
    original = preflight.subprocess.Popen
    children = []
    def spawn(*a, **k):
        child = original(*a, **k)
        children.append(child)
        return child
    monkeypatch.setattr(preflight.subprocess, "Popen", spawn)
    runner = preflight.probe_runner(threading.Event(), timeout=0.05)
    with pytest.raises(ProbeError, match="timed out"):
        runner(sys.executable, ["-c", "import time; time.sleep(30)"])
    assert len(children) == 1
    assert children[0].poll() is not None
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(ProbeError, match="cancelled"):
        preflight.probe_runner(cancelled)(sys.executable, ["-c", "print('synthetic')"])
    assert len(children) == 1


def test_uncorrelated_active_subtitle_is_not_adopted_after_enqueue_error(ui, tmp_path, monkeypatch):
    win, client = ui
    path = media(tmp_path)
    job_id = client.make_job(path, [{"state": "queued"}], kind="subs")
    recorded = []
    monkeypatch.setattr(client, "queue_views", lambda: ([], list(recorded)), raising=False)
    enqueue = client.enqueue_subs
    def persist_then_fail(paths, **options):
        enqueue(paths, **options)
        recorded.append(SimpleNamespace(job_id=job_id, kind="subs", paths=[path]))
        raise OSError("fixture persistence acknowledgement failure")
    monkeypatch.setattr(client, "enqueue_subs", persist_then_fail)
    win._enqueue_subs_paths([path])
    win.start_subtitles()
    # A same-path singleton could belong to another writer. No exact receipt
    # was returned, so even this apparent match must remain quarantined.
    assert win.job_ids == []
    assert win._staged_subs[0]["uncertain"]
    assert client.submitted == [job_id]
    assert not win._batch_row_for_job
    assert win.file_table.item(0, 1).text() == path


def test_partial_upscale_submission_tracks_successes_without_row_aliasing(ui, tmp_path, monkeypatch):
    import dataclasses
    win, client = ui
    plans = []
    for name in ("first.mkv", "bad.mkv", "last.mkv"):
        plan = _plan_for(tmp_path)
        path = media(tmp_path, name)
        plans.append(dataclasses.replace(plan, report=dataclasses.replace(plan.report, path=path)))
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    calls = []
    from vidlift.mcp_bridge.client_local import GuiSubmissionResult
    def submit(kind, path, **options):
        calls.append(path)
        if path == plans[1].report.path:
            return GuiSubmissionResult("rejected", error=FileNotFoundError("synthetic vanished source"))
        return GuiSubmissionResult("accepted", (client.make_job(path, [{"state": "queued"}], kind="run"),))
    monkeypatch.setattr(client, "submit_gui_job", submit, raising=False)
    win.start_upscale()
    assert calls == [p.report.path for p in plans]
    assert len(win.job_ids) == 2
    assert sorted(win._batch_row_for_job.values()) == [0, 2]
    assert win.file_table.item(1, 0).text() == "not submitted"
    for job, row in win._batch_row_for_job.items():
        assert win._row_meta(row)["plan"].report.path == client.jobs[job]["file_path"]
    assert "synthetic vanished source" in win.message_label.text()


def test_history_refresh_follows_job_identity_not_row_number(ui, monkeypatch):
    win, client = ui
    old = {"job_id": "old-fixture", "file_path": "old synthetic.mkv", "state": "failed"}
    new = {"job_id": "new-fixture", "file_path": "new synthetic.mkv", "state": "succeeded"}
    history = [old]
    monkeypatch.setattr(client, "history", lambda **k: list(history))
    win._render_recent_jobs()
    win.queue_tabs.setCurrentIndex(1)
    win.history_table.selectRow(0)
    history.insert(0, new)
    win._render_recent_jobs()
    assert win._selected_record()["job_id"] == old["job_id"]
    assert "old synthetic.mkv" in win.details.toPlainText()
    history.pop()
    win._render_recent_jobs()
    assert win._selected_record() == {}
    assert "Select a recorded job" in win.details.toPlainText()


def test_large_review_is_scrollable_complete_and_escape_is_safe(qapp):
    from PyQt6.QtCore import QTimer
    from vidlift.ui.review import BatchReviewDialog
    details = "\n".join(f"synthetic source {i}: replace source after verified success" for i in range(500))
    dialog = BatchReviewDialog("Review synthetic batch", details)
    assert dialog.review.toPlainText() == details
    assert dialog.review.isReadOnly()
    assert dialog.back_button.isDefault()
    assert not dialog.start_button.autoDefault()
    QTimer.singleShot(0, lambda: QTest.keyClick(dialog, Qt.Key.Key_Escape))
    assert dialog.exec() == dialog.DialogCode.Rejected
    dialog.deleteLater()


def test_system_colours_is_opt_in_and_does_not_change_processing_preferences(ui):
    win, _ = ui
    before = (win.gbph.text(), win.replace_source.isChecked(), win.subs_device_combo.currentData())
    win._set_system_colours(True)
    assert win.styleSheet() == ""
    assert win._settings().value("ui/system_palette", type=bool)
    assert before == (win.gbph.text(), win.replace_source.isChecked(), win.subs_device_combo.currentData())
    win._set_system_colours(False)
    assert ":focus" in win.styleSheet()


def test_details_preserve_selection_and_scroll_on_same_job_update(ui, qapp):
    from PyQt6.QtGui import QTextCursor
    win, _ = ui
    original = "Job: synthetic\n" + "stable copyable detail\n" * 80
    win._set_details_text(original)
    cursor = win.details.textCursor()
    cursor.setPosition(15)
    cursor.setPosition(24, QTextCursor.MoveMode.KeepAnchor)
    win.details.setTextCursor(cursor)
    selected = cursor.selectedText()
    win._set_details_text(original + "state: running\n")
    assert win.details.textCursor().selectedText() == selected


def test_mixed_failed_cancelled_outcomes_are_both_reported(ui):
    win, _ = ui
    win.run_counts = ["failed", "cancelled", "succeeded"]
    win._batch_finished(["last.mkv: succeeded"])
    assert "cancellations and failures" in win.summary_count.text()
    assert "1 failed" in win.summary_note.text()
    assert "1 cancelled" in win.summary_note.text()


def test_visible_upscale_clear_is_mode_scoped_and_never_cancels(ui, tmp_path):
    win, client = ui
    subtitle = media(tmp_path, "subtitle.mkv")
    plan = _plan_for(tmp_path)
    win._enqueue_subs_paths([subtitle])
    staged_id = win._staged_subs[0]["id"]
    win._add_paths([plan.report.path])
    win.upscale_clear_button.click()
    assert win.files == []
    assert win.file_table.rowCount() == 1
    assert win._staged_subs[0]["id"] == staged_id
    assert win._row_meta(0)["staged_id"] == staged_id
    assert client.cancel_calls == []
    assert client.cancel_batch_calls == []


def test_long_errors_are_bounded_without_losing_copyable_details(ui, qapp):
    win, _ = ui
    message = "\n".join(f"synthetic <source> {i}: fixture error" for i in range(500))
    win._show_message(message)
    assert len(win.message_label.text()) < 500
    assert win.message_label.textFormat() == Qt.TextFormat.PlainText
    assert win.message_label.toolTip() == message
    win.copy_message_button.click()
    assert qapp.clipboard().text() == message


def test_unresolved_upscale_recording_disables_blind_retry(ui, tmp_path, monkeypatch):
    win, client = ui
    plan = _plan_for(tmp_path)
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    calls = []
    def fail(*a, **k):
        calls.append(True)
        raise OSError("synthetic registry acknowledgement failure")
    monkeypatch.setattr(client, "enqueue_run", fail)
    win.start_upscale()
    win.start_upscale()
    assert calls == [True]
    assert win.job_ids == []
    assert "unresolved" in win.message_label.text()


def test_close_during_background_check_ignores_result(ui, qapp, tmp_path, monkeypatch):
    import threading
    import vidlift.gui as gui
    win, client = ui
    plan = _plan_for(tmp_path)
    win.files = [plan.report.path]
    entered, release = threading.Event(), threading.Event()
    def probe(*a, **k):
        entered.set()
        release.wait(2)
        return plan.report
    monkeypatch.setattr(gui, "find_tools", lambda: SimpleNamespace(ffprobe="not executed"))
    monkeypatch.setattr(gui, "probe_and_classify", probe)
    try:
        win.request_plan()
        assert entered.wait(1)
        win.close()
        assert win._ui_closed
        release.set()
        _drain_until(qapp, lambda: not win._preflights)
        assert win.batch is None
        assert client.submitted == []
    finally:
        release.set()
        _drain_until(qapp, lambda: not win._preflights)
