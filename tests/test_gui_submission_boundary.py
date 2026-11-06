"""Submission faults use synthetic sources and never start a media executor."""
from pathlib import Path

import pytest

from test_gui_best_practices import qapp, ui, media
from test_gui import _plan_for
from vidlift.mcp_bridge.tools import BridgeError
from vidlift.plan import BatchPlan


@pytest.mark.parametrize("kind", ["subs", "run"])
@pytest.mark.parametrize("error_type", [BridgeError, FileNotFoundError, OSError])
def test_post_record_error_without_active_receipt_is_quarantined(
    ui, tmp_path, monkeypatch, kind, error_type
):
    """Karen's recorded-but-invisible BridgeError case, in both modes."""
    win, client = ui
    path = media(tmp_path)
    recorded = []

    def record_then_error(paths, **kwargs):
        recorded.extend(paths)
        if error_type is BridgeError:
            raise BridgeError("io_error", "synthetic post-record failure")
        raise error_type("synthetic post-record failure")

    monkeypatch.setattr(client, "queue_views", lambda: ([], []), raising=False)
    monkeypatch.setattr(client, "enqueue_subs" if kind == "subs" else "enqueue_run", record_then_error)
    if kind == "subs":
        win.subs_replace_source.setChecked(True)
        win.subs_suppress_srt.setChecked(True)
        win._enqueue_subs_paths([path])
        start = win.start_subtitles
    else:
        plan = _plan_for(tmp_path)
        path = plan.report.path
        win.batch = BatchPlan([plan])
        win.render_batch(win.batch)
        win.replace_source.setChecked(True)
        win.suppress_srt.setChecked(True)
        start = win.start_upscale
    source = Path(path).read_bytes()
    start()
    start()
    assert recorded == [path], "unacknowledged work must not be retried"
    if kind == "subs":
        assert win._staged_subs[0]["uncertain"]
        win.clear_staged_subtitles()
        win._enqueue_subs_paths([path])
        start()
        assert win._staged_subs[0]["uncertain"]
        assert not win.start_subtitles_button.isEnabled()
    else:
        assert path in win._uncertain_upscale_paths
    assert recorded == [path]
    assert Path(path).read_bytes() == source
    assert win.file_table.item(0, 0).text() == "submission unknown"


def prepare_mode(win, tmp_path, kind):
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


@pytest.mark.parametrize("kind", ["subs", "run"])
@pytest.mark.parametrize("fault", ["before-save", "after-save", "after-notify", "terminal", "queue-unreadable"])
def test_gui_real_bridge_boundary(ui, tmp_path, monkeypatch, kind, fault):
    _check_gui_real_bridge_boundary(ui, tmp_path, monkeypatch, kind, fault)


@pytest.mark.parametrize("kind", ["subs", "run"])
@pytest.mark.parametrize("stage", ["prepare", "commit"])
@pytest.mark.parametrize("fault", ["before-save", "after-save"])
@pytest.mark.parametrize("error_type", [BridgeError, OSError])
def test_gui_real_bridge_write_stages(ui, tmp_path, monkeypatch, kind, stage, fault, error_type):
    _check_gui_real_bridge_boundary(ui, tmp_path, monkeypatch, kind, fault,
                                    stage=stage, error_type=error_type)


def _check_gui_real_bridge_boundary(ui, tmp_path, monkeypatch, kind, fault,
                                    *, stage="prepare", error_type=BridgeError):
    from vidlift.mcp_bridge.client_local import LocalBridgeClient
    from vidlift.mcp_bridge.registry import Registry
    win, _ = ui
    bridge = LocalBridgeClient(state_dir=tmp_path / "registry", start_executor=False)
    win.client = bridge
    path, start = prepare_mode(win, tmp_path, kind)
    source = Path(path).read_bytes()
    sidecar = Path(path).with_suffix(".srt")
    sidecar.write_bytes(b"sidecar sentinel")
    original_save = bridge.registry._save_unlocked
    executor = bridge.services.executor
    assert executor is not None
    original_notify = executor.notify_new_work
    saves = []
    notifications = []

    def fail():
        if error_type is BridgeError:
            raise BridgeError("io_error", "synthetic submission-boundary failure")
        raise error_type("synthetic submission-boundary failure")

    def save():
        saves.append(True)
        targeted = len(saves) == (1 if stage == "prepare" else 2)
        if targeted and fault == "before-save":
            fail()
        original_save()
        if targeted and fault == "after-save":
            fail()

    def notify():
        notifications.append(True)
        original_notify()
        if fault in {"before-save", "after-save"}:
            return
        if fault in {"terminal", "queue-unreadable"}:
            record = bridge.registry.queued()[0]
            bridge.registry.transition(record, "running")
            bridge.registry.transition(record, "failed")
        if fault == "queue-unreadable":
            monkeypatch.setattr(bridge, "queue_views", fail)
        fail()

    monkeypatch.setattr(bridge.registry, "_save_unlocked", save)
    monkeypatch.setattr(bridge.services.executor, "notify_new_work", notify)
    start()
    writes_after_first_start = len(saves)
    start()
    assert len(saves) == writes_after_first_start, "repeated Start must not write or enqueue again"
    disk = Registry(tmp_path / "registry")
    receipts = disk.list_submissions()
    assert set(bridge.registry._jobs) == set(disk._jobs)
    if fault == "before-save":
        # Neither an unpersisted candidate nor a prepared intent is work.
        assert not bridge.registry._jobs and not disk._jobs
        assert not notifications
        assert [r["state"] for r in receipts] == (["prepared"] if stage == "commit" else [])
        assert all(not r["job_ids"] for r in receipts)
        assert not win.job_ids
        if kind == "subs":
            assert win._staged_subs[0]["uncertain"]
        else:
            assert path in win._uncertain_upscale_paths
        assert win.file_table.item(0, 0).text() == "submission unknown"
    else:
        assert len(disk._jobs) == len(bridge.registry._jobs) == 1
        record = next(iter(disk._jobs.values()))
        assert len(receipts) == 1 and receipts[0]["state"] == "accepted"
        assert receipts[0]["job_ids"] == [record.job_id]
        assert record.options["replace_source"] is True
        assert record.options["suppress_srt"] is True
        assert notifications == [True]
        assert win.job_ids == [record.job_id]
        assert win._row_meta(win._batch_row_for_job[record.job_id])["job_id"] == record.job_id
        assert not win._staged_subs
        assert path not in win._uncertain_upscale_paths
        replay = bridge.submit_gui_job(kind, path, options=receipts[0]["payload"]["options"],
                                       engine="auto", submission_id=receipts[0]["submission_id"])
        assert list(replay.job_ids) == [record.job_id]
        assert len(saves) == writes_after_first_start
        assert notifications == [True], "accepted replay must not wake or enqueue twice"
        assert list(Registry(tmp_path / "registry")._jobs) == [record.job_id]
        if fault in {"terminal", "queue-unreadable"}:
            # An exact receipt is pollable even though the active queue is
            # empty or cannot be read. Outcome accounting must still drain it.
            win._tick_elapsed()
            assert win.run_counts == ["failed"]
            assert not win.job_ids
    assert Path(path).read_bytes() == source
    assert sidecar.read_bytes() == b"sidecar sentinel"
    assert executor._threads == []


@pytest.mark.parametrize("kind", ["subs", "run"])
def test_gui_proven_prewrite_rejection_remains_retryable(ui, tmp_path, kind):
    from vidlift.mcp_bridge.client_local import LocalBridgeClient
    win, _ = ui
    bridge = LocalBridgeClient(state_dir=tmp_path / "registry", start_executor=False)
    win.client = bridge
    path, start = prepare_mode(win, tmp_path, kind)
    source = Path(path).read_bytes()
    Path(path).unlink()  # test-owned fixture, never user media
    start()
    assert not bridge.registry._jobs
    assert not win.job_ids
    assert path not in win._uncertain_upscale_paths
    if kind == "subs":
        assert not win._staged_subs[0]["uncertain"]
        assert win.start_subtitles_button.isEnabled()
    Path(path).write_bytes(source)
    start()
    assert len(bridge.registry.queued()) == 1
    assert len(win.job_ids) == 1
    record = bridge.registry.require(win.job_ids[0])
    assert record.options["replace_source"] is True
    assert record.options["suppress_srt"] is True
    assert not win._staged_subs


def test_queue_read_failure_before_submit_is_safe_retry(ui, tmp_path, monkeypatch):
    win, client = ui
    path = media(tmp_path)
    job = client.make_job(path, [{"state": "queued"}], kind="subs")
    win._enqueue_subs_paths([path])

    def unreadable():
        raise BridgeError("io_error", "synthetic queue unavailable")

    monkeypatch.setattr(client, "queue_views", unreadable, raising=False)
    win.start_subtitles()
    assert not client.submitted
    assert not win._staged_subs[0]["uncertain"]
    monkeypatch.setattr(client, "queue_views", lambda: ([], []))
    win.start_subtitles()
    assert client.submitted == [job]


@pytest.mark.parametrize("active_kind", ["subs", "run"])
def test_public_subtitle_start_waits_for_batch_and_keeps_frozen_options(ui, tmp_path, active_kind):
    from _gui_checks import wait_for_checks
    win, client = ui
    first = media(tmp_path, "active.mkv")
    active = client.make_job(first, [{"state": "queued"}], kind=active_kind)
    if active_kind == "subs":
        win._enqueue_subs_paths([first])
        win.start_subtitles()
    else:
        plan = _plan_for(tmp_path)
        client.jobs[active]["file_path"] = plan.report.path
        win.batch = BatchPlan([plan])
        win.render_batch(win.batch)
        win.start_upscale()
    mapping = dict(win._batch_row_for_job)
    later = media(tmp_path, "later.mkv")
    pending = client.make_job(later, [{"state": "queued"}], kind="subs")
    win.subs_replace_source.setChecked(True)
    win.subs_suppress_srt.setChecked(True)
    win._enqueue_subs_paths([later])
    win.subs_replace_source.setChecked(False)
    win.subs_suppress_srt.setChecked(False)
    staged_id = win._staged_subs[0]["id"]
    assert not win.start_subtitles_button.isEnabled()
    assert "active batch" in win.subs_start_help.text()
    win.request_start_subtitles()
    win.start_subtitles()
    assert not win._preflights
    assert client.submitted == [active]
    assert win._batch_row_for_job == mapping
    client.script_tail(active, [{"state": "cancelled"}])
    win._tick_elapsed()
    win._tick_elapsed()
    assert win.start_subtitles_button.isEnabled()
    assert client.submitted == [active], "draining must not auto-submit staging"
    win.request_start_subtitles()
    wait_for_checks(win)
    assert client.submitted == [active, pending]
    assert win._row_meta(win._batch_row_for_job[pending])["row_id"] == staged_id
    assert client.last_subs_replace_source is True
    assert client.last_subs_suppress_srt is True


def test_active_job_arriving_during_review_blocks_start(ui, tmp_path, monkeypatch):
    win, client = ui
    win.subs_replace_source.setChecked(True)
    win._enqueue_subs_paths([media(tmp_path)])

    def review(*args):
        monkeypatch.setattr(client, "active_job_count", lambda: 1)
        return True

    monkeypatch.setattr(win, "_confirm_options", review)
    win.start_subtitles()
    assert not client.submitted
    assert len(win._staged_subs) == 1
    assert not win.start_subtitles_button.isEnabled()


def test_external_batch_ending_refreshes_start_without_auto_submission(ui, tmp_path, monkeypatch):
    win, client = ui
    active = [1]
    monkeypatch.setattr(client, "active_job_count", lambda: active[0])
    win._enqueue_subs_paths([media(tmp_path)])
    assert not win.start_subtitles_button.isEnabled()
    assert win._staged_controls_timer.isActive()
    active[0] = 0
    win._staged_controls_timer.timeout.emit()
    assert win.start_subtitles_button.isEnabled()
    assert not client.submitted


def test_internal_subtitle_receipt_tracking_does_not_require_review_notice_state(ui, tmp_path, monkeypatch):
    from vidlift.mcp_bridge.client_local import LocalBridgeClient
    win, _ = ui
    bridge = LocalBridgeClient(state_dir=tmp_path / "registry", start_executor=False)
    win.client = bridge
    path = media(tmp_path)

    def fail():
        raise BridgeError("io_error", "synthetic notification failure")

    monkeypatch.setattr(bridge.services.executor, "notify_new_work", fail)
    assert win._submit_subs_paths([path]) == {path}
    record = bridge.registry.queued()[0]
    assert win.job_ids == [record.job_id]
    assert win._row_meta(win._batch_row_for_job[record.job_id])["job_id"] == record.job_id
    assert "Tracking that exact job" in win._message_text


def test_subtitle_start_cannot_enter_during_upscale_review(ui, tmp_path, monkeypatch):
    win, client = ui
    plan = _plan_for(tmp_path)
    active = client.make_job(plan.report.path, [{"state": "queued"}], kind="run")
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.replace_source.setChecked(True)
    later = media(tmp_path)
    client.make_job(later, [{"state": "queued"}], kind="subs")
    win._enqueue_subs_paths([later])

    def consent(*args):
        win.start_subtitles()
        win.request_start_subtitles()
        assert not client.submitted
        assert not win._preflights
        assert not win.start_subtitles_button.isEnabled()
        assert "submission review" in win.subs_start_help.text()
        return True

    monkeypatch.setattr(win, "_confirm_options", consent)
    win.start_upscale()
    assert client.submitted == [active]
    assert [r["path"] for r in win._staged_subs] == [later]
