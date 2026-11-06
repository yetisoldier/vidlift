"""Interactive worker/lifetime contracts. Synthetic files, no media execution."""
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMessageBox

from _gui_checks import wait_for_checks
from test_gui import _plan_for
from test_gui_best_practices import media, qapp, ui
from vidlift.plan import BatchPlan


def paused_probe(monkeypatch):
    import vidlift.gui as gui
    entered, release = threading.Event(), threading.Event()
    threads = []

    def check(*args, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3), "Test must release the synthetic probe"
        return "ready"

    monkeypatch.setattr(gui, "subs_plan_verdict", check)
    return entered, release, threads


def test_interactive_add_is_responsive_and_freezes_options(ui, qapp, tmp_path, monkeypatch):
    import vidlift.gui as gui
    win, client = ui
    path = media(tmp_path)
    monkeypatch.setattr(gui.QFileDialog, "getOpenFileNames", lambda *a, **k: ([path], ""))
    win.subs_device_combo.setCurrentIndex(win.subs_device_combo.findData("cuda"))
    win.subs_suppress_srt.setChecked(True)
    entered, release, threads = paused_probe(monkeypatch)
    try:
        win.add_subs_button.click()
        assert entered.wait(1)
        assert win._staged_subs == []
        assert client.submitted == []
        beats = []
        QTimer.singleShot(0, lambda: beats.append(True))
        qapp.processEvents()
        assert beats == [True]
        assert threads == [threads[0]] and threads[0] != threading.get_ident()
        win.subs_suppress_srt.setChecked(False)
        win.subs_device_combo.setCurrentIndex(win.subs_device_combo.findData("cpu"))
    finally:
        release.set()
        wait_for_checks(win)
    staged = win._staged_subs[0]
    assert staged["options"].device == "cuda"
    assert staged["options"].suppress_srt
    assert client.submitted == []


@pytest.mark.parametrize("discard", ["cancel", "clear", "close"])
def test_add_result_cannot_reappear_after_discard(ui, qapp, tmp_path, monkeypatch, discard):
    win, client = ui
    path = media(tmp_path)
    entered, release, _ = paused_probe(monkeypatch)
    try:
        win._stage_subtitle_drop([path])
        assert entered.wait(1)
        if discard == "cancel":
            win.cancel_check_button.click()
        elif discard == "clear":
            win.clear_staged_subtitles()
        else:
            win.close()
            assert win._ui_closed
    finally:
        release.set()
        wait_for_checks(win)
    assert win._staged_subs == []
    assert win.file_table.rowCount() == 0
    assert client.submitted == []


def test_start_rechecks_off_thread_once_and_cannot_double_submit(ui, qapp, tmp_path, monkeypatch):
    win, client = ui
    path = media(tmp_path)
    job = client.make_job(path, [{"state": "queued"}], kind="subs")
    win._enqueue_subs_paths([path])
    entered, release, threads = paused_probe(monkeypatch)
    try:
        win.start_subtitles_button.click()
        assert entered.wait(1)
        win.start_subtitles_button.click()
        win.request_start_subtitles()  # Even a direct reentrant request cannot queue a second check.
        assert client.submitted == []
        assert len(win._preflights) == 1
        assert not win.start_subtitles_button.isEnabled()
    finally:
        release.set()
        wait_for_checks(win)
    assert client.submitted == [job]
    assert len(threads) == 1  # No repeated synchronous ffprobe during submit.
    assert threads[0] != threading.get_ident()
    assert not win._staged_subs


def test_removing_staging_during_start_check_prevents_submit(ui, qapp, tmp_path, monkeypatch):
    win, client = ui
    path = media(tmp_path)
    client.make_job(path, [{"state": "queued"}], kind="subs")
    win._enqueue_subs_paths([path])
    entered, release, _ = paused_probe(monkeypatch)
    try:
        win.start_subtitles_button.click()
        assert entered.wait(1)
        win.file_table.selectRow(0)
        win.remove_staged_subtitles()
    finally:
        release.set()
        wait_for_checks(win)
    assert not client.submitted
    assert not win._staged_subs
    assert "Staging changed" in win.message_label.text()


def test_newly_blocked_start_result_is_retained_once(ui, qapp, tmp_path, monkeypatch):
    import vidlift.gui as gui
    win, client = ui
    path = media(tmp_path)
    win._enqueue_subs_paths([path])
    monkeypatch.setattr(gui, "subs_plan_verdict", lambda *a, **k: "blocked-output-exists")
    win.start_subtitles_button.click()
    wait_for_checks(win)
    assert len(win._staged_subs) == win.file_table.rowCount() == 1
    assert win._staged_subs[0]["verdict"] == "blocked-output-exists"
    assert not win.start_subtitles_button.isEnabled()
    assert not client.submitted


@pytest.mark.parametrize("subtitles", [False, True])
def test_recursive_enumeration_cancels_without_late_add(ui, qapp, tmp_path, monkeypatch, subtitles):
    import vidlift.gui as gui
    win, client = ui
    entered, release = threading.Event(), threading.Event()
    threads = []
    path = media(tmp_path)

    def collect(folder, **kwargs):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return [path]

    monkeypatch.setattr(gui, "collect_video_files", collect)
    monkeypatch.setattr(gui.QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path))
    try:
        if subtitles:
            win.subs_add_folder_button.click()
        else:
            win.upscale_folder_button.click()
        assert entered.wait(1)
        beats = []
        QTimer.singleShot(0, lambda: beats.append(True))
        qapp.processEvents()
        assert beats == [True]
        assert threads[0] != threading.get_ident()
        if subtitles:
            win.clear_staged_subtitles()
        else:
            win.clear_upscale_files()
    finally:
        release.set()
        wait_for_checks(win)
    assert win.files == [] and win._staged_subs == []
    assert not client.submitted


def test_active_polling_continues_during_subtitle_preparation(ui, qapp, tmp_path, monkeypatch):
    win, client = ui
    active = media(tmp_path, "active.mkv")
    later = media(tmp_path, "later.mkv")
    job = client.make_job(active, [{"state": "queued"}, {"state": "running", "current_phase": "transcribe"}], kind="subs")
    win._enqueue_subs_paths([active])
    win.start_subtitles()
    entered, release, _ = paused_probe(monkeypatch)
    try:
        win._stage_subtitle_drop([later])
        assert entered.wait(1)
        win._tick_elapsed()
        win._tick_elapsed()
        assert "transcribing" in win.stage_label.text()
        assert win._batch_row_for_job[job] == 0
    finally:
        release.set()
        wait_for_checks(win)
    assert len(win._staged_subs) == 1
    assert client.submitted == [job]


def test_both_reviews_reserve_submission_before_nested_dialog(ui, tmp_path, monkeypatch):
    win, client = ui
    path = media(tmp_path)
    job = client.make_job(path, [{"state": "queued"}], kind="subs")
    win.subs_replace_source.setChecked(True)
    win._enqueue_subs_paths([path])
    reviews = []

    def consent(title, details):
        reviews.append(details)
        win.start_subtitles()
        return True

    monkeypatch.setattr(win, "_confirm_options", consent)
    win.start_subtitles()
    assert client.submitted == [job]
    assert len(reviews) == 1
    assert not win._subs_submitting


def test_upscale_review_freezes_engine_and_blocks_reentry(ui, tmp_path, monkeypatch):
    win, client = ui
    plan = _plan_for(tmp_path)
    job = client.make_job(plan.report.path, [{"state": "queued"}], kind="run")
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.replace_source.setChecked(True)
    engines = []
    enqueue = client.enqueue_run

    def record(*args, **kwargs):
        engines.append(kwargs["engine"])
        return enqueue(*args, **kwargs)

    def consent(title, details):
        win.start_upscale()
        win.engine_combo.setCurrentIndex(win.engine_combo.findData("stub"))
        return True

    monkeypatch.setattr(client, "enqueue_run", record)
    monkeypatch.setattr(win, "_confirm_options", consent)
    original_engine = win.engine_choice
    win.start_upscale()
    assert client.submitted == [job]
    assert engines == [original_engine]
    assert not win._upscale_submitting


def test_large_submission_failure_batch_has_one_notification(ui, tmp_path, monkeypatch):
    win, client = ui
    paths = [media(tmp_path, f"synthetic-{i}.mkv") for i in range(25)]
    win._enqueue_subs_paths(paths)
    notices = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: notices.append(a[2]))

    def fail(*a, **k):
        raise FileNotFoundError("synthetic source vanished")

    monkeypatch.setattr(client, "enqueue_subs", fail)
    win.start_subtitles()
    assert len(notices) == 1
    assert len(win._staged_subs) == 25
    assert win.file_table.rowCount() == 25
    assert win._message_text.count("synthetic source vanished") == 25
    assert not client.submitted


def test_empty_receipt_is_unknown_not_duplicate_staging(ui, tmp_path, monkeypatch):
    win, client = ui
    win._enqueue_subs_paths([media(tmp_path)])
    monkeypatch.setattr(client, "enqueue_subs", lambda *a, **k: [])
    win.start_subtitles()
    assert win.file_table.rowCount() == 1
    assert win._staged_subs[0]["uncertain"]
    assert not win.start_subtitles_button.isEnabled()


def test_pending_upscale_visible_during_subtitle_run(ui, tmp_path):
    win, client = ui
    path = media(tmp_path)
    client.make_job(path, [{"state": "queued"}], kind="subs")
    win._enqueue_subs_paths([path])
    win.start_subtitles()
    original_map = dict(win._batch_row_for_job)
    later = media(tmp_path, "upscale-next.avi")
    win._add_paths([later])
    assert win.file_table.item(1, 1).text() == later
    assert win._row_meta(1)["kind"] == "pending-upscale"
    win.clear_upscale_files()
    assert win._batch_row_for_job == original_map
    assert win.file_table.rowCount() == 1
    assert not client.cancel_calls


def test_close_during_model_install_stays_open(ui):
    win, client = ui
    win._subs_model_installing = True
    event = QCloseEvent()
    win.closeEvent(event)
    assert not event.isAccepted()
    assert not win._ui_closed
    assert "installation is still running" in win.message_label.text()
    win._subs_model_installing = False


def test_terminal_receipt_is_not_lost_before_next_submission(ui, tmp_path):
    win, client = ui
    first, last = [media(tmp_path, n) for n in ("first.mkv", "last.mkv")]
    early = client.make_job(first, [{"state": "failed", "error": "early fixture failure"}], kind="subs")
    late = client.make_job(last, [{"state": "queued"}, {"state": "succeeded"}], kind="subs")
    win._enqueue_subs_paths([first, last])
    win.start_subtitles()
    assert win.job_ids == [early, late]
    win._tick_elapsed()
    assert win.run_counts[0] == "failed"
    win._tick_elapsed()
    assert win.run_counts == ["failed", "succeeded"]
    assert "failures" in win.summary_count.text()


@pytest.mark.parametrize("response", [[], [None], [{"job_id": "wrong-fixture", "state": "succeeded"}]])
def test_short_or_misidentified_status_never_drops_job(ui, tmp_path, monkeypatch, response):
    win, client = ui
    path = media(tmp_path)
    job = client.make_job(path, [{"state": "queued"}], kind="subs")
    win._enqueue_subs_paths([path])
    win.start_subtitles()
    monkeypatch.setattr(client, "enqueue_snapshots", lambda ids: response)
    win._tick_elapsed()
    assert win.job_ids == [job]
    assert win.run_counts == [None]
    assert "successfully" not in win.summary_count.text()


def test_no_ai_terminal_summary_is_not_plain_success(ui, tmp_path):
    win, client = ui
    plan = _plan_for(tmp_path)
    client.make_job(plan.report.path, [{"state": "succeeded", "engine_simulated": True}], kind="run")
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.start_upscale()
    win._tick_elapsed()
    assert "no-AI" in win.summary_count.text()
    assert "without AI upscale" in win.summary_note.text()


def test_uncertain_subtitle_cannot_be_retried_by_clear_and_readd(ui, tmp_path, monkeypatch):
    win, client = ui
    path = media(tmp_path)
    win._enqueue_subs_paths([path])
    calls = []

    def fail(*a, **k):
        calls.append(True)
        raise RuntimeError("synthetic post-wake error")

    monkeypatch.setattr(client, "enqueue_subs", fail)
    win.start_subtitles()
    win.clear_staged_subtitles()
    win._enqueue_subs_paths([path])
    win.start_subtitles()
    assert calls == [True]
    assert win.file_table.rowCount() == 1
    assert not win.start_subtitles_button.isEnabled()


def test_failed_upscale_check_keeps_explainable_row(ui, qapp, tmp_path, monkeypatch):
    import vidlift.gui as gui
    from vidlift.probe import ProbeError
    win, client = ui
    plan = _plan_for(tmp_path)
    failed = media(tmp_path, "bad-check.avi")
    win.files = [plan.report.path, failed]
    monkeypatch.setattr(gui, "find_tools", lambda: SimpleNamespace(ffprobe="not executed"))

    def probe(path, **kwargs):
        if path == failed:
            raise ProbeError("synthetic damaged header")
        return plan.report

    monkeypatch.setattr(gui, "probe_and_classify", probe)
    win.request_plan()
    wait_for_checks(win)
    assert win.file_table.rowCount() == 2
    assert win.file_table.item(1, 0).text() == "check failed"
    win.file_table.selectRow(1)
    assert "synthetic damaged header" in win.details.toPlainText()
    assert not client.submitted


def test_staged_row_identity_survives_submission_on_every_cell(ui, tmp_path):
    win, client = ui
    path = media(tmp_path)
    job = client.make_job(path, [{"state": "queued"}], kind="subs")
    win._enqueue_subs_paths([path])
    identity = win._row_meta(0)["row_id"]
    win.start_subtitles()
    row = win._batch_row_for_job[job]
    for column in range(win.file_table.columnCount()):
        metadata = win.file_table.item(row, column).data(Qt.ItemDataRole.UserRole)
        assert metadata["row_id"] == identity
        assert metadata["job_id"] == job


def test_newly_blocked_destructive_row_is_not_offered_for_consent(ui, tmp_path, monkeypatch):
    import vidlift.gui as gui
    win, client = ui
    win.subs_replace_source.setChecked(True)
    win._enqueue_subs_paths([media(tmp_path)])
    reviews = []
    monkeypatch.setattr(win, "_confirm_options", lambda *args: reviews.append(args) or True)
    monkeypatch.setattr(gui, "subs_plan_verdict", lambda *a, **k: "blocked-output-exists")
    win.start_subtitles_button.click()
    wait_for_checks(win)
    assert reviews == []
    assert not client.submitted
    assert win._staged_subs[0]["verdict"] == "blocked-output-exists"


def test_upscale_details_show_run_choices_and_then_frozen_submission(ui, tmp_path):
    win, client = ui
    plan = _plan_for(tmp_path)
    client.make_job(plan.report.path, [{"state": "queued"}], kind="run")
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.file_table.selectRow(0)
    win.suppress_srt.setChecked(True)
    assert "SRT removed only after successful embed" in win.details.toPlainText()
    assert "Run options (reviewed at Start)" in win.details.toPlainText()
    win.start_upscale()
    win.suppress_srt.setChecked(False)
    win._render_detail_for_row(0)
    assert "Submitted options (frozen)" in win.details.toPlainText()
    assert "SRT removed only after successful embed" in win.details.toPlainText()
