from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from vidlift.plan import build_file_plan, BatchPlan
from vidlift.probe import SubtitleInfo, classify, parse_probe


pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QLabel, QMainWindow

from vidlift.gui import (
    ENGINE_CHOICES,
    MainWindow,
    detail_text,
    format_bytes,
    format_duration,
    warnings_for,
)
from vidlift.run import _apply_subtitle_ocr_preference

from _bcanary import register_b


def marker(name: str) -> None:
    register_b(name)


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
    assert "movie.720p.mp4" in text
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
    window.run_counts = [None]
    window._progress_chunk_color = None
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


# ---- Karen r2 D2: replace-source checkbox layout ----------------------------


def settings_for(monkeypatch, tmp_path):
    """Isolate QSettings into a throwaway INI (design R3)."""
    import PyQt6.QtCore as QtCore

    def _settings(self):
        return QtCore.QSettings(
            str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat
        )

    monkeypatch.setattr(MainWindow, "_settings", _settings)


def test_v047_replace_checkbox_no_overlap_with_cancel(
    client, monkeypatch, tmp_path
):
    # Karen r2 D2: the replace_source checkbox (grid column 5, span 2)
    # must not reach into cancel_button's column 7. Geometry probe
    # mirrors Karen's offscreen repro: no rect intersection, and the
    # checkbox right edge stays left of the Cancel left edge.
    settings_for(monkeypatch, tmp_path)
    app = QApplication.instance() or QApplication([])
    win = MainWindow(client=client)
    try:
        win.show()
        app.processEvents()
        cb = win.replace_source
        cancel = win.cancel_button
        assert not cb.geometry().intersects(cancel.geometry()), (
            f"checkbox {cb.geometry()} overlaps cancel {cancel.geometry()}"
        )
        assert cb.geometry().right() < cancel.geometry().left()
    finally:
        win.job_ids = []
        win._run_started_at = None
        win.close()


# ---- v0.4.8 b18: the profile combo (design D5) ----------------------------------


def test_b18_profile_combo_exists_with_choices_and_default(window):
    from vidlift.gui import PROFILE_CHOICES

    assert window.profile_combo.count() == len(PROFILE_CHOICES)
    for index, (label, data) in enumerate(PROFILE_CHOICES):
        assert window.profile_combo.itemText(index) == label
        assert window.profile_combo.itemData(index) == data
    # D3: the default reads "live-action" unless QSettings carries one
    assert window.profile_combo.currentData() in ("live-action", "anime")


def test_b18_profile_combo_default_is_live_action(window):
    assert window.profile_choice == "live-action"
    assert window.profile_combo.currentData() == "live-action"


def test_b18_profile_combo_persists_on_change(window):
    index = window.profile_combo.findData("anime")
    assert index >= 0
    window.profile_combo.setCurrentIndex(index)
    assert window.profile_choice == "anime"
    stored = window._settings().value("upscale_profile")
    assert str(stored) == "anime"
    # a stale/unknown persisted value reads as live-action, never applied
    window._settings().setValue("upscale_profile", "sdxl-soup")
    assert window._load_profile_choice() == "live-action"


def test_b18_profile_combo_loads_persisted_value(client, monkeypatch, tmp_path):
    import PyQt6.QtCore as QtCore

    settings_path = tmp_path / "settings.ini"
    settings = QtCore.QSettings(
        str(settings_path), QtCore.QSettings.Format.IniFormat
    )
    settings.setValue("upscale_profile", "anime")
    settings.sync()

    def _settings(self):
        return QtCore.QSettings(
            str(settings_path), QtCore.QSettings.Format.IniFormat
        )

    monkeypatch.setattr(MainWindow, "_settings", _settings)
    app = QApplication.instance() or QApplication([])
    win = MainWindow(client=client)
    try:
        assert win.profile_choice == "anime"
        assert win.profile_combo.currentData() == "anime"
    finally:
        win.job_ids = []
        win._run_started_at = None
        win.close()


def test_b18_profile_combo_passes_through_start_options(window, tmp_path):
    # the bridge mapping: options["upscale_profile"] == profile_choice
    plan = _plan_for(tmp_path, "toon.avi", ready=True)
    window.batch = batch_plan_from([plan])
    window.profile_combo.setCurrentIndex(
        window.profile_combo.findData("anime")
    )
    window.start_upscale()
    record = window.client.registry.jobs[0]
    assert record.options["upscale_profile"] == "anime"


def test_b18_profile_combo_disabled_during_batch_and_reenabled_after(
    window, tmp_path
):
    plan = _plan_for(tmp_path, "toon.avi", ready=True)
    window.batch = batch_plan_from([plan])
    window.start_upscale()
    assert window.profile_combo.isEnabled() is False
    job_id = window.job_ids[0]
    record = window.client.registry.require(job_id)
    record.state = "succeeded"
    record.ok = True
    record.finished_at = time.time()
    window._tick_elapsed()  # drains job_ids; the liveness stop path runs
    window._batch_finished([])
    assert window.profile_combo.isEnabled() is True


def test_b18_profile_combo_geometry_does_not_overlap_cancel(
    client, monkeypatch, tmp_path
):
    # b18 (design D5): the v0.4.7 D2 geometry discipline — the profile
    # combo lives on row 2 columns 5-6; cancel_button stays at (1, 7)
    # untouched. Offscreen geometry probe: no rect intersection and the
    # combo's right edge stays left of Cancel's left edge.
    settings_for(monkeypatch, tmp_path)
    app = QApplication.instance() or QApplication([])
    win = MainWindow(client=client)
    try:
        win.show()
        app.processEvents()
        combo = win.profile_combo
        cancel = win.cancel_button
        assert not combo.geometry().intersects(cancel.geometry()), (
            f"profile combo {combo.geometry()} overlaps cancel {cancel.geometry()}"
        )
        assert combo.geometry().right() < cancel.geometry().left()
    finally:
        win.job_ids = []
        win._run_started_at = None
        win.close()


def test_b18_detail_text_appends_anime_sentence_only_for_anime(
    client, monkeypatch, tmp_path
):
    # b18/b22 (design D6): the GUI detail render appends the additive
    # anime sentence exactly when the profile is "anime".
    marker("b22")
    plan = _plan_for(tmp_path, "toon.avi", ready=True)
    without = detail_text(plan, upscale_profile="live-action")
    assert "Anime upscaling model" not in without
    with_anime = detail_text(plan, upscale_profile="anime")
    assert (
        "Anime upscaling model: strong fit for flat fills and line art" in with_anime
    )
    assert "not for live-action content" in with_anime
    # default call (no profile kwarg) omits it — plan surfaces untouched
    assert "Anime upscaling model" not in detail_text(plan)


# ---- v0.4.15.3 F-4: phase-keyed stage bar (stale counters lie) ------------


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


class _StageHarness(QMainWindow):
    """The minimal widget set _consume_running_snapshot touches over a
    SEQUENCE of snapshots (multi-tick: upscale -> encode -> verify —
    exactly the merged-counter shape run.py ships). Same binding set as
    test_progress_honesty._BarHarness, plus the F-4 measured floor."""

    def __init__(self, qapp: QApplication) -> None:
        super().__init__()
        from PyQt6.QtWidgets import QPlainTextEdit, QTableWidget, QTableWidgetItem

        from vidlift.ui.widgets import EtaProgressBar

        self.stage_label = QLabel("")
        self.stage_bar = EtaProgressBar()
        self.overall_bar = EtaProgressBar()
        self.run_log_tail = QPlainTextEdit()
        self._stage_now = "queued"
        self._stage_eta = None
        self._stage_fraction = None
        self._stage_fraction_measured = 0.0
        self.run_total = 1
        self.run_counts = [None]
        self._progress_chunk_color = None
        # the encode-phase ETA denominator reads the plan seam (None:
        # honest no-duration, the harness's shape).
        self.batch = None
        self.job_ids = ["vl-test-f4"]
        self.file_table = QTableWidget(1, 1)
        self.file_table.setItem(0, 0, QTableWidgetItem(""))

        self._consume_running_snapshot = (
            MainWindow._consume_running_snapshot.__get__(self)
        )
        self._set_text = MainWindow._set_text.__get__(self)
        self._render_log_tail = MainWindow._render_log_tail.__get__(self)
        self._eta_state = {}
        self._eta_phase_now = None
        self._eta_rendered = None
        self._batch_eta_rendered = None
        self._eta_current_job = MainWindow._eta_current_job.__get__(self)
        self._eta_suffix_for_stage = MainWindow._eta_suffix_for_stage.__get__(self)
        self._render_damped_eta = MainWindow._render_damped_eta.__get__(self)
        self._eta_state_key = MainWindow._eta_state_key.__get__(self)
        self._ETA_EWMA_ALPHA = MainWindow._ETA_EWMA_ALPHA
        self._ETA_DAMPING_FRACTION = MainWindow._ETA_DAMPING_FRACTION
        self._reset_eta_for_phase = MainWindow._reset_eta_for_phase.__get__(self)
        self._duration_for_snapshot = MainWindow._duration_for_snapshot.__get__(self)
        self._fmt_eta = MainWindow._fmt_eta.__get__(self)

    def consume(self, snapshot: dict) -> None:
        from vidlift.gui import run_stage_label

        self._consume_running_snapshot(0, snapshot, run_stage_label(snapshot))


def _running_snap(phase: str, progress: dict, **extra) -> dict:
    return {
        "state": "running",
        "current_phase": phase,
        "job_id": "vl-test-f4",
        "progress": progress,
        **extra,
    }


_STALE_FRAMES = {"frames_done": 3600, "frames_total": 3600}


def test_f4_stale_counters_never_pin_encode_verify_bar(qapp):
    """Karen's Scenario A/B, promoted to a pin: after the upscale's
    final tick, the encode and verify snapshots CARRY the stale
    counters (run.py merges, never replaces) — the stage bar must stay
    a busy pulse named by the phase, never the measuring label, never
    the stale 1000."""
    from vidlift.gui import _BUSY_MEASURING_LABEL

    win = _StageHarness(qapp)
    # upscale ends: the counters are real here — 100% is honest now.
    win.consume(_running_snap("upscale", dict(_STALE_FRAMES)))
    assert win.stage_bar.bar.value() == 1000

    # encode begins; the counters ride the payload exactly as run.py
    # ships them after its merge.
    win.consume(
        _running_snap("encode", {"step": "encode", **_STALE_FRAMES})
    )
    assert win.stage_bar._busy is True
    assert win.stage_bar.eta_label.text() == "encoding ..."
    assert win.stage_bar.eta_label.text() != _BUSY_MEASURING_LABEL
    assert win._stage_fraction is None  # the stale counters are not honored
    assert win.stage_bar.bar.maximum() == 0  # busy pulse (0..0), not 1000/1000

    # verify begins; the stale counters STILL ride the payload.
    win.consume(
        _running_snap("verify", {"step": "verify", **_STALE_FRAMES})
    )
    assert win.stage_bar._busy is True
    assert win.stage_bar.eta_label.text() == "verifying ..."
    assert win.stage_bar.eta_label.text() != _BUSY_MEASURING_LABEL
    assert win._stage_fraction is None
    assert win.stage_bar.bar.maximum() == 0


def test_f4_mid_upscale_still_measures_and_count_probe_label_unchanged(qapp):
    """Non-regression: the gate did not over-fire — a legitimate
    upscale fraction still drives set_value, and the count pass keeps
    its honest measuring label."""
    from vidlift.gui import _BUSY_MEASURING_LABEL
    from vidlift.run import UPSCALE_STEP_PREPARED_COUNT

    win = _StageHarness(qapp)
    win.consume(
        _running_snap(
            "upscale",
            {"step": "chunk", "chunk_index": 3, "chunk_total": 12,
             "frames_done": 1800, "frames_total": 3600},
        )
    )
    assert win._stage_fraction == pytest.approx(0.5)
    assert win.stage_bar.bar.maximum() == 1000  # out of busy (0..0)
    assert win.stage_bar.bar.value() == 500

    # the count probe: measuring is the phase's own name during upscale.
    win._stage_now = "queued"  # simulate a fresh consume of the count pass
    win.consume(
        _running_snap("upscale", {"step": UPSCALE_STEP_PREPARED_COUNT})
    )
    assert win.stage_bar.eta_label.text() == _BUSY_MEASURING_LABEL


def test_f4_overall_bar_never_recedes_across_phases(qapp):
    """The overall queue bar renders last-MEASURED truth: at the encode
    tick (no fraction owned) it must hold the upscale's measured value,
    not drop back to 0."""
    win = _StageHarness(qapp)
    win.consume(_running_snap("upscale", {"step": "chunk",
                                          "frames_done": 1800,
                                          "frames_total": 3600}))
    mid = win.overall_bar.bar.value()
    assert mid == pytest.approx((0 + 0.5) * 1000, rel=1e-3)
    win.consume(_running_snap("upscale", {"step": "chunk",
                                          "frames_done": 3600,
                                          "frames_total": 3600}))
    end = win.overall_bar.bar.value()
    assert end == 1000
    win.consume(_running_snap("encode", {"step": "encode", **_STALE_FRAMES}))
    during_encode = win.overall_bar.bar.value()
    assert during_encode >= end, (
        "the overall bar receded at the phase change (a lie of its own)"
    )


def test_f4_encode_stage_label_renders_without_stale_frames():
    """run_stage_label on an encode snapshot carrying the merged stale
    counters renders exactly 'encoding' — the lying
    'encoding frames 3600/3600' suffix is gone."""
    from vidlift.gui import run_stage_label

    label = run_stage_label(
        _running_snap("encode", {"step": "encode", **_STALE_FRAMES})
    )
    assert label == "encoding"


# ---- v0.4.15.3 F-6: plan view never promises un-run transcription --------


def test_f6_plan_batch_passes_transcribe_checkbox_to_builder(
    window, monkeypatch
):
    """The GUI's plan call threads the checkbox: unchecked -> the
    builder receives transcription_enabled=False (and checked -> True),
    so the note and the selection share ONE derivation input."""
    import vidlift.gui as gui_mod
    from vidlift.plan import build_batch_plan as real_build_batch_plan
    from vidlift.run import ToolBox

    captured: dict = {}

    def spy(reports, **kwargs):
        captured.update(kwargs)
        return real_build_batch_plan(reports, **kwargs)

    monkeypatch.setattr(gui_mod, "build_batch_plan", spy)
    monkeypatch.setattr(
        gui_mod, "probe_and_classify",
        lambda path, binary: _report(),
    )
    monkeypatch.setattr(
        gui_mod, "find_tools",
        lambda: ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    window.files = ["/media/movie.avi"]
    window.transcribe_subs.setChecked(False)
    window.plan_batch()
    assert captured.get("transcription_enabled") is False
    window.transcribe_subs.setChecked(True)
    window.plan_batch()
    assert captured.get("transcription_enabled") is True


# ---- v0.4.15.4 N-1: overall bar truth at job boundaries ----------------------


def test_n1_overall_bar_never_inflates_or_recedes_at_job_boundary(
    window, tmp_path
):
    """The N-1 regression, promoted: when job1 RESOLVES, its terminal
    fill must retire the measured floor. Karen's 2-job evidence: after
    job1's terminal fill, job2's first RUNNING tick (probe — no
    fraction of its own) must render ~50% — NO inflation to 100% — and
    the transition from job1's upscale end must NOT visibly recede.
    Job2's first real measurement then renders strictly above the
    probe value (the monotonic truth)."""
    plans = [_plan_for(tmp_path, "one.avi"), _plan_for(tmp_path, "two.avi")]
    window.batch = batch_plan_from(plans)
    window.render_batch(window.batch)
    window.start_upscale()
    j1, j2 = list(window.job_ids)
    reg = window.client.registry

    # Job1 ends its upscale at the honest measured 100%: (0+1.0)/2 = 50%.
    r1 = reg.require(j1)
    r1.state = "running"
    r1.current_phase = "upscale"
    r1.progress = {"frames_done": 3600, "frames_total": 3600}
    window._tick_elapsed()
    end_value = window.overall_bar.bar.value()
    assert end_value == pytest.approx(0.5 * 1000, rel=1e-3)

    # Job1 resolves: the terminal fill retires the floor with it.
    r1.state = "succeeded"
    r1.ok = True
    r1.finished_at = time.time()
    window._tick_elapsed()
    assert window.run_counts == ["succeeded", None]
    assert window._stage_fraction_measured == 0.0

    # Job2's first running tick is the PROBE (no fraction of its own):
    # with the retired floor the overall bar holds (1+0.0)/2 ≈ 50%.
    r2 = reg.require(j2)
    r2.state = "running"
    r2.current_phase = "probe"
    window._tick_elapsed()
    probe_value = window.overall_bar.bar.value()
    probe_fraction = probe_value / 1000.0
    assert probe_fraction <= 0.5 + 0.01, (  # ε: bar quantization headroom
        "the overall bar INFLATED to the finished job's measured floor "
        f"at the job boundary (N-1 lie: {probe_fraction})"
    )
    assert probe_value >= end_value, (
        "the overall bar RECEDED at the job boundary (N-1 lie)"
    )

    # Job2's first real measurement: monotonic, strictly above the
    # probe value (72/3600 = 2% → (1+0.02)/2 = 51%).
    r2.current_phase = "upscale"
    r2.progress = {"frames_done": 72, "frames_total": 3600}
    window._tick_elapsed()
    next_value = window.overall_bar.bar.value()
    assert next_value > probe_value, (
        "the overall bar receded at the first measured tick after the "
        "job boundary (N-1 lie)"
    )


def test_n1_floor_reset_at_slot_fill_is_idempotent(window, tmp_path):
    """The idempotence edge: a RE-delivered terminal snapshot (the same
    job) must NOT reset anything mid-next-job — the guard keys on the
    empty slot (`is None`), so once a slot is resolved the floor the
    NEXT job earned survives the replay."""
    plans = [_plan_for(tmp_path, "one.avi"), _plan_for(tmp_path, "two.avi")]
    window.batch = batch_plan_from(plans)
    window.render_batch(window.batch)
    window.start_upscale()
    j1, j2 = list(window.job_ids)
    reg = window.client.registry

    r1 = reg.require(j1)
    r1.state = "running"
    r1.current_phase = "upscale"
    r1.progress = {"frames_done": 900, "frames_total": 3600}  # 25%
    window._tick_elapsed()
    assert window._stage_fraction_measured == pytest.approx(0.25)

    r1.state = "succeeded"
    r1.ok = True
    r1.finished_at = time.time()
    window._tick_elapsed()  # slot 0 fills; the floor retires with it
    assert window.run_counts == ["succeeded", None]
    assert window._stage_fraction_measured == 0.0

    r2 = reg.require(j2)
    r2.state = "running"
    r2.current_phase = "upscale"
    r2.progress = {"frames_done": 1800, "frames_total": 3600}  # 50%
    window._tick_elapsed()
    assert window._stage_fraction_measured == pytest.approx(0.5)

    # Job1's terminal snapshot RE-delivered: slot 0 is already resolved
    # — the guard (`is None`) must not fire, the floor must survive.
    window._consume_terminal_snapshot(
        0, {"job_id": j1, "state": "succeeded"}
    )
    assert window.run_counts == ["succeeded", None]
    assert window._stage_fraction_measured == pytest.approx(0.5), (
        "a re-delivered terminal snapshot reset the running job's "
        "measured floor (N-1 idempotence edge)"
    )


# ---- v0.4.15.4 N-2: busy-label phase parity ----------------------------------


def test_n2_busy_label_map_covers_every_emitted_phase():
    """The map's keys cover EXACTLY the emitted phase set: run.py's
    PHASE_* constants ∪ the two jobs.py Mode-B literals, minus "done"
    (terminal snapshots never busy-pulse). A future phase ADDITION
    fails this pin instead of silently falling back to
    "estimating ...", and a dead key fails it too."""
    from vidlift.run import (
        PHASE_ENCODE,
        PHASE_PLAN,
        PHASE_PREPARE,
        PHASE_PROBE,
        PHASE_SUBTITLES,
        PHASE_UPSCALE,
        PHASE_VERIFY,
    )

    from vidlift.gui import _PHASE_BUSY_LABELS

    emitted = {
        PHASE_PROBE,
        PHASE_PLAN,
        PHASE_PREPARE,
        PHASE_UPSCALE,
        PHASE_ENCODE,
        PHASE_VERIFY,
        PHASE_SUBTITLES,
        "subs-mux",  # jobs.py's Mode-B literal (set_live current_phase)
        "transcribe",  # jobs.py's Mode-B literal
        "done",  # emitted (terminal) — never busy-pulsed
    }
    assert set(_PHASE_BUSY_LABELS) == emitted - {"done"}
    # the dead "finalize" key is gone — no writer emits it.
    assert "finalize" not in _PHASE_BUSY_LABELS


def test_n2_plan_phase_busy_label_is_named(qapp):
    """The plan phase (run.py's PHASE_PLAN, emitted for real) pulses
    its OWN busy label — no longer the generic 'estimating ...'."""
    win = _StageHarness(qapp)
    win.consume(_running_snap("plan", {}))
    assert win.stage_bar.eta_label.text() == "planning ..."


@pytest.mark.usefixtures("transcription_engine_on")
def test_f6_render_batch_overrides_transcribed_note_when_checkbox_off(
    window, tmp_path
):
    """Mirrors the OCR override: a transcribed plan (still holding its
    plan-time note) renders the honest disabled note under an unchecked
    checkbox — covers 'user unchecks after pressing Plan' with no
    replan; checking it again restores the plan's own note."""
    report = _report()
    plan = build_file_plan(report, gb_per_hour=1.5)
    assert plan.subtitle_plan is not None
    assert plan.subtitle_plan.source == "transcribed"

    window.transcribe_subs.setChecked(False)
    window.render_batch(batch_plan_from([plan]))
    assert window.file_table.item(0, 6).text() == (
        "transcription disabled; no subtitles transcribed"
    )

    window.transcribe_subs.setChecked(True)
    window.render_batch(batch_plan_from([plan]))
    assert window.file_table.item(0, 6).text() == plan.subtitle_note
