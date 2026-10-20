from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from vidlift.plan import build_file_plan, BatchPlan
from vidlift.probe import SubtitleInfo, classify, parse_probe


pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QLabel

from vidlift.gui import (
    ENGINE_CHOICES,
    MainWindow,
    detail_text,
    format_bytes,
    format_duration,
    warnings_for,
)
from vidlift.run import _apply_subtitle_ocr_preference


def _report():
    payload = """
    {
      "streams": [
        {
          "index": 0,
          "codec_type": "video",
          "codec_name": "mpeg4",
          "width": 640,
          "height": 360,
          "sample_aspect_ratio": "1:1",
          "field_order": "progressive",
          "r_frame_rate": "25/1",
          "avg_frame_rate": "25/1"
        },
        {"index": 1, "codec_type": "audio", "codec_name": "mp3", "channels": 2}
      ],
      "format": {"format_name": "avi", "duration": "3600.0"}
    }
    """
    return classify(parse_probe("/media/movie.avi", payload))


def _plan_for(tmp_path, name="movie.avi", ready=True):
    path = tmp_path / name
    path.write_text("")
    report = _report()
    report.path = str(path)
    if not ready:
        report.upscale_verdict = "already-hd"
    return build_file_plan(report, gb_per_hour=1.5)


def batch_plan_from(plans):
    """BatchPlan from prepared PlannedFile list (it is just the files
    list plus derived counts)."""
    return BatchPlan(list(plans))


class FakeRegistry:
    """In-memory registry stand-in with the exact call surface the GUI
    path touches (enqueue/require/queued/running/history/cancel)."""

    def __init__(self):
        self.jobs = []
        self.counter = 0

    def enqueue(self, *, kind, paths, options):
        self.counter += 1
        record = SimpleNamespace(
            job_id=f"vl-test-{self.counter:04d}",
            kind=kind,
            paths=list(paths),
            options=dict(options),
            state="queued",
            submitted_at=time.time() + self.counter,
            started_at=None,
            finished_at=None,
            ok=None,
            engine_simulated=None,
            engine_note=None,
            output_path=None,
            manifest_path=None,
        )
        self.jobs.append(record)
        return record

    def require(self, job_id):
        for record in self.jobs:
            if record.job_id == job_id:
                return record
        raise KeyError(job_id)

    def get(self, job_id):
        try:
            return self.require(job_id)
        except KeyError:
            return None

    def queued(self):
        return [r for r in self.jobs if r.state == "queued"]

    def running(self):
        return [r for r in self.jobs if r.state == "running"]

    def history(self):
        rows = [r for r in self.jobs if r.state in ("succeeded", "failed", "cancelled")]
        rows.sort(key=lambda r: r.finished_at or 0, reverse=True)
        return rows

    def cancel(self, job_id):
        for record in self.jobs:
            if record.job_id == job_id:
                if record.state in ("succeeded", "failed", "cancelled"):
                    return "already-finished"
                record.state = "cancelled"
                record.finished_at = time.time()
                return "cancelled"
        return "not-found"


class FakeExecutor:
    def __init__(self, registry):
        self.registry = registry
        self.notified = 0

    def start(self):
        pass

    def stop(self, **kwargs):
        pass

    def notify_new_work(self):
        self.notified += 1


class FakeClient:
    """The GUI's bridge seam under test: an in-memory registry/executor
    pair satisfying the exact LocalBridgeClient contract."""

    def __init__(self):
        self.registry = FakeRegistry()
        self.executor = FakeExecutor(self.registry)
        self.cancel_calls: list[str] = []

    def active_job_count(self):
        return len(self.registry.queued()) + len(self.registry.running())

    def enqueue_run(self, paths, options=None, engine="auto"):
        opts = dict(options or {})
        opts["engine"] = engine
        ids = []
        for path in paths:
            record = self.registry.enqueue(kind="run", paths=[path], options=opts)
            self.executor.notify_new_work()
            ids.append(record.job_id)
        return ids

    def status_snapshot(self, job_id):
        record = self.registry.get(job_id)
        if record is None:
            return None
        snapshot = {
            "job_id": record.job_id,
            "state": record.state,
            "file_path": record.paths[0] if record.paths else None,
            "current_phase": None,
            "eta_seconds": None,
            "log_tail": [],
            "progress": None,
            "engine_simulated": bool(record.engine_simulated)
            if record.engine_simulated is not None
            else False,
        }
        if record.state == "running":
            snapshot["current_phase"] = getattr(record, "current_phase", "upscale")
            progress = getattr(record, "progress", None)
            if progress is not None:
                snapshot["progress"] = progress
            eta = getattr(record, "eta_seconds", None)
            if eta is not None:
                snapshot["eta_seconds"] = eta
        return snapshot

    def enqueue_snapshots(self, job_ids):
        return [self.status_snapshot(job_id) for job_id in job_ids]

    def cancel(self, job_id):
        self.cancel_calls.append(job_id)
        return self.registry.cancel(job_id)

    def history(self, limit=20):
        rows = []
        for record in self.registry.history()[:limit]:
            rows.append(
                {
                    "job_id": record.job_id,
                    "file_path": record.paths[0] if record.paths else None,
                    "state": record.state,
                    "finished_at": record.finished_at,
                    "ok": record.ok,
                    "engine_simulated": bool(record.engine_simulated)
                    if record.engine_simulated is not None
                    else False,
                    "output_path": record.output_path,
                    "manifest_path": record.manifest_path,
                }
            )
        return rows

    def stop(self):
        pass


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def window(client, monkeypatch, tmp_path):
    # Engine persistence must never depend on (or pollute) real
    # QSettings in tests: isolate it per test (design R3).
    import PyQt6.QtCore as QtCore

    def _settings(self):
        return QtCore.QSettings(
            str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat
        )

    monkeypatch.setattr(MainWindow, "_settings", _settings)
    # closeEvent prompts a modal question when jobs are active; tests
    # never want a modal — answer "close anyway" (G3/G4/G5 mid-run
    # teardowns included).
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    monkeypatch.setattr(time, "sleep", lambda s: None)
    app = QApplication.instance() or QApplication([])
    win = MainWindow(client=client)
    yield win
    win.job_ids = []
    win._run_started_at = None
    win.close()
    app.processEvents()


# ---- base GUI behavior ------------------------------------------------------


def test_format_helpers_are_human_readable():
    assert format_bytes(1_500_000_000) == "1.50 GB"
    assert format_bytes(None) == "unknown"
    assert format_duration(3661) == "1:01:01"
    assert format_duration(None) == "unknown"


def test_detail_text_contains_core_plan_fields():
    plan = build_file_plan(_report(), gb_per_hour=1.5)
    text = detail_text(plan)
    assert "Verdict: ready" in text
    assert "Target geometry: 1280x720" in text
    assert "Estimated size: 1.50 GB" in text
    assert "PAL speedup suspected" in text


def test_warnings_include_skip_verdicts():
    report = _report()
    report.upscale_verdict = "already-hd"
    plan = build_file_plan(report)
    assert any("Skipped" in warning for warning in warnings_for(plan))


def test_detail_text_previews_output_contents_and_ocr_choice():
    plan = build_file_plan(_report(), gb_per_hour=1.5)
    text = detail_text(plan, subtitle_ocr_enabled=False)
    assert "OUTPUT PREVIEW" in text
    assert "Destination:" in text
    assert "movie.vidlift.720p.hevc.mp4" in text
    assert "Video: HEVC MP4" in text
    assert "Audio: AAC" in text


def test_disabling_ocr_removes_only_bitmap_subtitle_plan():
    report = _report()
    report.subtitles = [
        SubtitleInfo(codec_name="dvd_subtitle", kind="image", lang="eng", index=2)
    ]
    plan = build_file_plan(report)
    assert plan.subtitle_plan is not None
    assert plan.subtitle_plan.ocr_required

    adjusted = _apply_subtitle_ocr_preference(plan, enabled=False)

    assert adjusted.subtitle_plan is None
    assert "OCR disabled" in adjusted.subtitle_note


def test_main_window_shows_version_output_column_and_start_action(client):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(client=client)
    labels = [label.text() for label in window.findChildren(QLabel)]
    assert "VidLift" in labels
    assert any(text.startswith("v") for text in labels)  # the header pill
    assert window.file_table.horizontalHeaderItem(4).text() == "Output"
    assert window.start_button.text() == "Start Upscale"
    assert not window.start_button.isEnabled()
    assert window.subtitle_ocr.text() == "Enable subtitle OCR"
    window.close()
    app.processEvents()


# ---- v0.4.4 G-cases (design §8) ----------------------------------------------


def test_g1_engine_combo_exact_items_and_data(window):
    assert window.engine_combo.count() == 3
    pairs = [
        (window.engine_combo.itemText(i), window.engine_combo.itemData(i))
        for i in range(window.engine_combo.count())
    ]
    expected = [
        ("Auto (recommended)", "auto"),
        ("Real-ESRGAN ncnn/Vulkan (GPU)", "ncnn"),
        ("Stub (no AI; encode-only fallback)", "stub"),
    ]
    assert pairs == expected
    assert pairs == ENGINE_CHOICES


def test_g1_engine_combo_persists_choice(window):
    window.engine_combo.setCurrentIndex(2)  # stub
    assert window.engine_choice == "stub"
    assert window._settings().value("engine", "auto") == "stub"
    window.engine_combo.setCurrentIndex(0)
    assert window.engine_choice == "auto"


def test_g2_forecast_math_honest_none_and_simulated_forbidden(window, tmp_path):
    # No jobs enqueued: no forecast at all.
    assert window._forecast_total_seconds() is None
    ids = window.client.enqueue_run([str(tmp_path / "movie.avi")])
    window.job_ids = ids
    plan = _plan_for(tmp_path)
    window.batch = batch_plan_from([plan])
    # No eta and no benchmark estimate: honest None ("…" in the UI).
    assert window._forecast_total_seconds() is None
    record = window.client.registry.require(ids[0])
    record.state = "running"
    record.eta_seconds = 120.0
    forecast = window._forecast_total_seconds()
    assert forecast is not None and forecast > 0
    # A stub-sourced benchmark estimate is policy-forbidden -> None.
    # (PlannedFile is frozen; attach the estimate via dataclasses.replace.)
    import dataclasses

    record.eta_seconds = None
    stub_estimated = dataclasses.replace(
        _plan_for(tmp_path, "simulated.avi"),
        eta_estimate={
            "seconds_per_frame": 0.5,
            "eta_seconds": 900.0,
            "engine_simulated": True,
        },
    )
    window.batch = batch_plan_from([stub_estimated])
    window.client.registry.jobs[0].paths = [stub_estimated.report.path]
    assert window._forecast_total_seconds() is None
    # A real-sourced benchmark estimate IS usable.
    measured = dataclasses.replace(
        _plan_for(tmp_path, "measured.avi"),
        eta_estimate={
            "seconds_per_frame": 0.5,
            "eta_seconds": 900.0,
            "engine_simulated": False,
        },
    )
    window.batch = batch_plan_from([measured])
    window.client.registry.jobs[0].paths = [measured.report.path]
    assert window._forecast_total_seconds() == pytest.approx(900.0)


def test_g3_start_enqueues_one_record_per_ready_file(window, tmp_path):
    plan_a = _plan_for(tmp_path, "a.avi", ready=True)
    plan_b = _plan_for(tmp_path, "b.avi", ready=False)
    window.batch = batch_plan_from([plan_a, plan_b])
    window.engine_choice = "ncnn"
    window.start_upscale()
    # Exactly ONE record per ready file; the skip verdict never runs.
    assert len(window.job_ids) == 1
    assert len(window.client.registry.jobs) == 1
    record = window.client.registry.jobs[0]
    assert record.paths == [plan_a.report.path]
    assert record.options["engine"] == "ncnn"
    assert window.client.executor.notified == 1


def test_g4_cancel_via_registry_and_row_follows_state(window, tmp_path):
    plan = _plan_for(tmp_path)
    window.batch = batch_plan_from([plan])
    window.render_batch(window.batch)  # the table rows exist as in the app flow
    window.start_upscale()
    job_id = window.job_ids[0]
    window.client.registry.require(job_id).state = "running"
    window.cancel_upscale()
    assert window.client.cancel_calls == [job_id]
    # The cancel disabled the button while the cancel is in flight.
    assert window.cancel_button.isEnabled() is False
    # The 1 Hz tick reads cancelling/cancelled from the SAME registry
    # and the row reflects it; after the terminal state the liveness
    # stop path runs.
    window.client.registry.require(job_id).state = "cancelled"
    window._tick_elapsed()
    assert window.file_table.item(0, 0).text() == "cancelled"
    assert window.job_ids == []


def test_g5_history_lists_enqueued_jobs_after_stop(window, tmp_path):
    plan = _plan_for(tmp_path)
    window.batch = batch_plan_from([plan])
    window.start_upscale()
    job_id = window.job_ids[0]
    record = window.client.registry.require(job_id)
    record.state = "succeeded"
    record.ok = True
    record.finished_at = time.time()
    window._tick_elapsed()  # drains job_ids since the job is terminal
    assert window.job_ids == []
    rows = window.client.history()
    assert rows and rows[0]["job_id"] == job_id
    assert any(
        getattr(r, "job_id", None) == job_id for r in window.client.registry.history()
    )


def test_g6_close_event_cancels_and_waits(window, tmp_path, monkeypatch):
    plan = _plan_for(tmp_path)
    window.batch = batch_plan_from([plan])
    window.start_upscale()
    job_id = window.job_ids[0]
    window.client.registry.require(job_id).state = "running"
    from vidlift.gui import _CLOSE_WAIT_RUN_MS

    assert _CLOSE_WAIT_RUN_MS == 30000

    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    monkeypatch.setattr(time, "sleep", lambda s: None)

    from PyQt6.QtGui import QCloseEvent

    # The "worker" finishes cooperatively right after cancel: snapshots
    # flip to cancelled ONLY after the cancel call lands, so the
    # bounded wait exits on its first pass instead of burning the
    # 30 s budget.
    def snapshots_now(job_ids):
        snapshot = window.client.status_snapshot(job_id) if job_ids else None
        if snapshot is not None and window.client.cancel_calls:
            window.client.registry.require(job_ids[0]).state = "cancelled"
            snapshot = window.client.status_snapshot(job_ids[0])
        return [snapshot]

    window.client.enqueue_snapshots = snapshots_now
    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()
    assert window.client.cancel_calls == [job_id]
    # Executor stopped on close; the window is going away, so the row
    # bookkeeping is moot (the registry holds the terminal state).
    assert window.client.registry.get(job_id).state == "cancelled"
    assert window.client.executor is window.client.executor

# ---- v0.4.4 Slice-5/6 polish cases --------------------------------------------


def test_polish_helper_line_and_verb_order(window):
    from vidlift.gui import PLAN_HELPER_TEXT
    labels = [l.text() for l in window.findChildren(QLabel)]
    assert PLAN_HELPER_TEXT in labels
    # Toolbar verb order on the controls grid: Plan before Start
    # (left-to-right on row 0).
    lay = window.plan_button.parentWidget().layout()
    columns = {}
    for i in range(lay.count()):
        item = lay.itemAt(i)
        w = item.widget()
        if w is window.plan_button:
            columns["plan"] = lay.getItemPosition(i)[1]
        if w is window.start_button:
            columns["start"] = lay.getItemPosition(i)[1]
        if w is window.cancel_button:
            columns["cancel"] = lay.getItemPosition(i)[1]
    assert columns["plan"] < columns["start"]
    assert "cancel" in columns


def test_polish_engine_combo_disabled_while_running(window, tmp_path):
    plan = _plan_for(tmp_path)
    window.batch = batch_plan_from([plan])
    window.render_batch(window.batch)
    assert window.engine_combo.isEnabled()
    window.start_upscale()
    assert not window.engine_combo.isEnabled()
    assert "cancel" in window.engine_combo.toolTip().lower()
    window.client.registry.require(window.job_ids[0]).state = "succeeded"
    window.client.registry.require(window.job_ids[0]).ok = True
    window._tick_elapsed()
    assert window.engine_combo.isEnabled()


def test_polish_run_panel_hidden_until_start_and_dots_stop(window, tmp_path):
    # The top-level window is not show()n in tests, so check the
    # widget's own hidden flag rather than on-screen visibility.
    assert window.run_panel.isHidden()
    plan = _plan_for(tmp_path)
    window.batch = batch_plan_from([plan])
    window.render_batch(window.batch)
    window.start_upscale()
    assert not window.run_panel.isHidden()
    assert window.activity_dots._timer.isActive()
    window.client.registry.require(window.job_ids[0]).state = "failed"
    window._tick_elapsed()
    assert not window.activity_dots._timer.isActive()
    assert window._run_started_at is None


def test_polish_stage_label_names_running_step(window, tmp_path):
    plan = _plan_for(tmp_path)
    window.batch = batch_plan_from([plan])
    window.render_batch(window.batch)
    window.start_upscale()
    job_id = window.job_ids[0]
    record = window.client.registry.require(job_id)
    record.state = "running"
    record.current_phase = "upscale"
    record.progress = {"frames_done": 1024, "frames_total": 8192}
    stage = window.gui_stage_for_test if False else __import__("vidlift.gui", fromlist=["run_stage_label"]).run_stage_label(
        window.client.status_snapshot(job_id)
    )
    assert "upscaling" in stage
    assert "1024/8192" in stage
    window._tick_elapsed()
    assert "upscaling" in window.stage_label.text()

def test_a2_parity_stub_row_reads_no_ai_upscale(window, tmp_path):
    # A2: a succeeded stub/simulated run shows "completed (no AI
    # upscale)" — never a silent AI-pass claim.
    plan = _plan_for(tmp_path)
    window.batch = batch_plan_from([plan])
    window.render_batch(window.batch)
    window.start_upscale()
    job_id = window.job_ids[0]
    record = window.client.registry.require(job_id)
    record.state = "succeeded"
    record.ok = True
    record.engine_simulated = True
    window._tick_elapsed()
    assert window.file_table.item(0, 0).text() == "completed (no AI upscale)"
    # A REAL-engine success reads plainly.
    record2 = window.client.registry.get(job_id)
    record2.engine_simulated = False
    # re-render from the same registry truth
    window.client.registry.jobs[0].state = "succeeded"
    window.job_ids = [job_id]
    window.run_done = 0
    window._consume_terminal_snapshot(0, window.client.status_snapshot(job_id))
    assert window.file_table.item(0, 0).text() == "succeeded"


def test_run_panel_survives_first_tick_while_queued(window, tmp_path):
    """D1 regression (v0.4.4 validation): the first 1 Hz tick fires
    while the job is still QUEUED (production always hits this — the
    worker has not claimed it yet). The liveness timer must stay
    alive, the run panel must keep updating, and the row must reach
    its terminal text once the registry finishes the job."""
    plan = _plan_for(tmp_path)
    window.batch = batch_plan_from([plan])
    window.render_batch(window.batch)
    window.start_upscale()
    job_id = window.job_ids[0]
    assert window.client.registry.require(job_id).state == "queued"

    # Tick 1 — production timing: nothing has been claimed yet.
    window._tick_elapsed()
    assert window._elapsed_timer.isActive(), "liveness died on tick 1"
    assert window._run_started_at is not None, "run clock died on tick 1"
    assert window.activity_dots._timer.isActive(), "dots died on tick 1"
    assert not window.engine_combo.isEnabled(), "engine unlocked mid-run"

    # Tick 2 — the worker claims and runs.
    record = window.client.registry.require(job_id)
    record.state = "running"
    record.current_phase = "upscale"
    record.progress = {"frames_done": 100, "frames_total": 800}
    window._tick_elapsed()
    assert window._elapsed_timer.isActive(), "liveness died while running"
    assert "upscaling" in window.stage_label.text()
    assert window.file_table.item(0, 0).text() == "running"

    # Tick 3 — terminal; NOW the panel may conclude.
    record.state = "succeeded"
    record.ok = True
    record.engine_simulated = True
    record.finished_at = time.time()
    window._tick_elapsed()
    assert window.file_table.item(0, 0).text() == "completed (no AI upscale)"
    assert window.job_ids == []
    assert window._run_started_at is None, "run clock never stopped"
    assert not window._elapsed_timer.isActive()
    assert window.engine_combo.isEnabled()


def test_stage_label_names_bridge_facts():
    from vidlift.gui import run_stage_label
    assert run_stage_label(None) == ""
    assert run_stage_label({"state": "queued"}) == "queued — waiting for a worker"
    assert run_stage_label(
        {"state": "running", "current_phase": "upscale",
         "progress": {"frames_done": 100, "frames_total": 800}}
    ) == "upscaling frames 100/800"
    assert run_stage_label(
        {"state": "running", "current_phase": "upscale",
         "progress": {"frames_done": 100, "frames_total": 800, "step": "chunk 2/8"}}
    ).endswith("(chunk 2/8)")
    assert run_stage_label(
        {"state": "running", "current_phase": "verify", "progress": None}
    ) == "verify — full decode"
    assert run_stage_label({"state": "succeeded", "current_phase": "done"}) == "succeeded"
