"""v0.4.10 defect-fix tests (D2 + D3) — the fix lane's regression net.

D2 (High): the v0.4.10 batch bar keyed `run_counts` by the SHIFTING
table row while terminal jobs drain from `self.job_ids` every poll
tick — surviving jobs shift down and their terminal state landed in
the wrong slot. A 2-job sequential all-succeeded batch stuck forever
at "1/2 succeeded", and a cancelled job's state could be overwritten
by a later success (green bar lie). These tests drive the REAL
`_poll_jobs` path (real MainWindow, scripted snapshots, sequential
ticks the way Karen's verification did) — the shipped v0.4.10 tests
dodged the bug by exercising only single-job batches and same-tick
multi-job snapshots.

D3 (Medium): a None utilization (nvidia-smi exit 0 printing "N/A"
per device, or garbage-but-present stdout) rendered a confident
"0%" instead of "n/a". These tests drive the REAL widget seam
(`_accept_sample` → `_rendered_values`) with exactly the samples
`parse_nvidia_smi_output` produces for those inputs.
"""

from __future__ import annotations

import os
import time
import types
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

# Offscreen + no monitor listener + isolated settings: the standard
# test-suite contract (same guards the house conftest installs for
# the suite; re-declared here so any direct run of this file alone
# stays inert).
os.environ.setdefault("VIDLIFT_MONITOR_AUTOSTART", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressBar, QTableWidgetItem, QTableWidget

from vidlift.gui import MainWindow
from vidlift.plan import BatchPlan, build_file_plan
from vidlift.probe import classify, parse_probe
from vidlift.run import summarize_batch
from vidlift.ui.gpu_meter import GpuMeterWidget, parse_nvidia_smi_output


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


_alive: list = []


# ---------------------------------------------------------------------------
# D2: batch-truth slots survive the sequential drain (real _poll_jobs)
# ---------------------------------------------------------------------------


def _report():
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
    return classify(parse_probe("/media/movie.avi", payload))


def _plan_for(tmp_path, name):
    report = _report()
    report.path = str(tmp_path / name)
    return build_file_plan(report, gb_per_hour=1.5)


class _FakeRegistry:
    """In-memory registry stand-in (the exact call surface the GUI's
    1 Hz path touches; mirrors test_gui.py's FakeRegistry contract)."""

    def __init__(self):
        self.jobs = []
        self.counter = 0

    def enqueue(self, *, kind, paths, options):
        self.counter += 1
        record = SimpleNamespace(
            job_id=f"vl-fix-{self.counter:04d}",
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


class _FakeExecutor:
    def __init__(self, registry):
        self.registry = registry
        self.notified = 0

    def start(self):
        pass

    def stop(self, **kwargs):
        pass

    def notify_new_work(self):
        self.notified += 1


class _FakeClient:
    """The GUI's bridge seam: snapshots carry job_id (the stable
    identity the D2 slot lookup keys on), like the real
    LocalBridgeClient.status_snapshot."""

    def __init__(self):
        self.registry = _FakeRegistry()
        self.executor = _FakeExecutor(self.registry)
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
        return {
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
                }
            )
        return rows

    def stop(self, **kwargs):
        pass


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    """A REAL MainWindow (offscreen) with isolated QSettings, no modal
    close prompt, and REAL `_poll_jobs` machinery — kept ALIVE for the
    whole test (the fixture owns the reference and tears it down
    first, so the worker thread never outlives the Qt objects).
    Construction was seen to abort at C++ level offscreen when the
    object dies mid-test; the module-scoped-style hold avoids it."""
    import PyQt6.QtCore as QtCore

    def _settings(self):
        return QtCore.QSettings(
            str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat
        )

    monkeypatch.setattr(MainWindow, "_settings", _settings)
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    monkeypatch.setattr(time, "sleep", lambda s: None)
    _alive.append(MainWindow(client=_FakeClient()))
    win = _alive[-1]
    win._batch_finished_fired = 0
    real_finished = win._batch_finished

    def _counting_finished(notes):
        win._batch_finished_fired += 1
        real_finished(notes)

    win._batch_finished = _counting_finished
    yield win
    win.job_ids = []
    win._run_started_at = None
    win.deleteLater()
    qapp.processEvents()
    _alive.clear()
    qapp.processEvents()


def _bar(win):
    """The full batch-bar truth tuple at any instant."""
    # run_summary may have been produced from a stale/other slate only
    # through a bug; assert the rendered bar == summarize(run_counts).
    assert win.run_summary == summarize_batch(win.run_counts)
    return (
        win.progress.format(),
        win.progress.value(),
        win._progress_chunk_color,
        list(win.run_counts),
        win.run_total,
    )


def test_d2_two_job_sequential_all_succeeded_reaches_2_of_2(window, tmp_path):
    """The D2 core repro through the real `_poll_jobs`: job a succeeds
    and drains in round 1; job b is still running and shifts into row
    0; when b succeeds in round 2 the bar must reach "2/2 succeeded"
    (the v0.4.10 bug kept it at "1/2" forever)."""
    plans = [_plan_for(tmp_path, "d2_a.avi"), _plan_for(tmp_path, "d2_b.avi")]
    window.batch = BatchPlan(list(plans))
    window.render_batch(window.batch)
    window.start_upscale()
    assert window.run_total == 2
    job_a, job_b = window.job_ids
    assert window._batch_slots == [job_a, job_b]
    assert window._batch_slot_index == {job_a: 0, job_b: 1}

    fa = window.client.registry.require(job_a)
    fa.state = "succeeded"
    fa.ok = True
    fa.finished_at = time.time()
    window.client.registry.require(job_b).state = "running"

    window._tick_elapsed()  # round 1: a terminal (drains), b running
    assert _bar(window) == ("1/2 succeeded", 1, "#3d9865", ["succeeded", None], 2)

    fb = window.client.registry.require(job_b)
    fb.state = "succeeded"
    fb.ok = True
    fb.finished_at = time.time()

    window._tick_elapsed()  # round 2: b terminal — MUST land in slot 1
    assert _bar(window) == ("2/2 succeeded", 2, "#3d9865", ["succeeded", "succeeded"], 2)
    assert window._batch_finished_fired == 1
    assert window.job_ids == []


def test_d2_sequential_cancelled_then_succeeded_never_turns_green(
    window, tmp_path
):
    """The D2 green-lie repro through the real `_poll_jobs`: a
    cancelled job's slot must NEVER be overwritten by a later
    success — the bar stays amber with "1/2 succeeded, 1 cancelled"
    (the v0.4.10 bug painted green "1/2 succeeded" and lost the
    cancellation)."""
    plans = [_plan_for(tmp_path, "d2_c.avi"), _plan_for(tmp_path, "d2_d.avi")]
    window.batch = BatchPlan(list(plans))
    window.render_batch(window.batch)
    window.start_upscale()
    job_a, job_b = window.job_ids

    window.client.registry.require(job_b).state = "running"
    assert window.client.registry.cancel(job_a) == "cancelled"

    window._tick_elapsed()  # round 1: a cancelled (drains), b running
    assert _bar(window) == (
        "0/2 succeeded, 1 cancelled",
        1,
        "#d8a93a",
        ["cancelled", None],
        2,
    )

    fb = window.client.registry.require(job_b)
    fb.state = "succeeded"
    fb.ok = True
    fb.finished_at = time.time()

    window._tick_elapsed()  # round 2: b succeeded — slot 0 untouched
    assert _bar(window) == (
        "1/2 succeeded, 1 cancelled",
        2,
        "#d8a93a",
        ["cancelled", "succeeded"],
        2,
    )
    assert window._batch_finished_fired == 1
    assert window.job_ids == []


def test_d2_cancelled_state_is_never_overwritten_or_lost(window, tmp_path):
    """Regression guard for the overwrite path itself: after a
    cancelled job's terminal write, NO number of later terminal ticks
    may change that slot's recorded state or drop it from the
    summary's cancelled count."""
    plans = [_plan_for(tmp_path, "d2_e.avi"), _plan_for(tmp_path, "d2_f.avi")]
    window.batch = BatchPlan(list(plans))
    window.render_batch(window.batch)
    window.start_upscale()
    job_a, job_b = window.job_ids

    window.client.registry.require(job_b).state = "running"
    window.client.registry.cancel(job_a)
    window._tick_elapsed()
    assert window.run_counts == ["cancelled", None]
    assert window.run_summary.cancelled == 1

    # drive the later job through several lifecycle states before its
    # own terminal write (running snapshots land on the same rows).
    fb = window.client.registry.require(job_b)
    fb.state = "running"
    window._tick_elapsed()
    fb.state = "succeeded"
    fb.ok = True
    fb.finished_at = time.time()
    window._tick_elapsed()
    window._tick_elapsed()  # one extra drain tick for good measure

    # the cancelled slot still reads "cancelled" — never overwritten,
    # never vanished from the summary; the bar can never be green.
    assert window.run_counts == ["cancelled", "succeeded"]
    assert window.run_summary.cancelled == 1
    assert window.run_summary.succeeded == 1
    assert window.run_summary.all_succeeded is False
    assert window._progress_chunk_color == "#d8a93a"
    assert window._batch_finished_fired == 1


def test_d2_single_job_cancelled_bar_truth_still_holds(window, tmp_path):
    """The v0.4.10 headline truth (cancelled 1/1 → "0/1 succeeded,
    1 cancelled", amber) must survive the slot-identity refactor."""
    plans = [_plan_for(tmp_path, "d2_single.avi")]
    window.batch = BatchPlan(list(plans))
    window.render_batch(window.batch)
    window.start_upscale()
    assert window.run_total == 1
    (job_a,) = window.job_ids
    window.client.registry.require(job_a).state = "running"
    window.client.registry.cancel(job_a)
    window._tick_elapsed()
    assert _bar(window) == ("0/1 succeeded, 1 cancelled", 1, "#d8a93a", ["cancelled"], 1)
    assert window._batch_finished_fired == 1


def test_d2_slots_are_stable_and_never_reorder_or_shrink(window, tmp_path):
    """The stable-slot registry contract: `_batch_slots`/`_batch_slot_index`
    are pinned at batch start in submission order; the sequential
    drain must never remove slots, reorder them, or let the truth
    index disagree with them."""
    plans = [_plan_for(tmp_path, "d2_g.avi"), _plan_for(tmp_path, "d2_h.avi")]
    window.batch = BatchPlan(list(plans))
    window.render_batch(window.batch)
    window.start_upscale()
    job_a, job_b = window.job_ids
    frozen = (window._batch_slots, dict(window._batch_slot_index))

    window.client.registry.require(job_a).state = "succeeded"
    window.client.registry.require(job_a).ok = True
    window.client.registry.require(job_a).finished_at = time.time()
    window._tick_elapsed()
    # after the drain: job_ids shrank, slots did NOT
    assert window.job_ids == [job_b]
    assert window._batch_slots == frozen[0]
    assert window._batch_slot_index == frozen[1]

    window.client.registry.require(job_b).state = "succeeded"
    window.client.registry.require(job_b).ok = True
    window.client.registry.require(job_b).finished_at = time.time()
    window._tick_elapsed()
    assert window._batch_slots == frozen[0]
    assert window._batch_slot_index == frozen[1]
    assert window.run_total == 2
    assert window.run_counts == ["succeeded", "succeeded"]


def test_d2_three_job_sequential_drain_is_order_immune(window, tmp_path):
    """Extra drain-shift coverage: three jobs resolving one per tick
    (a, b, c in successive rounds) — every slot lands by identity and
    the final bar counts all three."""
    plans = [
        _plan_for(tmp_path, "d2_i.avi"),
        _plan_for(tmp_path, "d2_j.avi"),
        _plan_for(tmp_path, "d2_k.avi"),
    ]
    window.batch = BatchPlan(list(plans))
    window.render_batch(window.batch)
    window.start_upscale()
    job_a, job_b, job_c = window.job_ids

    for tick_index, job in enumerate((job_a, job_b, job_c)):
        record = window.client.registry.require(job)
        prev_states = (job_a, job_b, job_c)[:tick_index]
        # (each earlier job already drained; later siblings still queue)
        record.state = "succeeded" if job is not job_b else "cancelled"
        record.ok = record.state == "succeeded"
        record.finished_at = time.time()
        for waiter in (
            job
            for job in (job_a, job_b, job_c)
            if window.client.registry.require(job).state == "queued"
        ):
            # siblings not yet terminal this round keep the batch alive
            window.client.registry.require(waiter).state = "running"
        window._tick_elapsed()

    assert window.run_counts == ["succeeded", "cancelled", "succeeded"]
    assert window.run_summary.succeeded == 2
    assert window.run_summary.cancelled == 1
    assert window.progress.format() == "2/3 succeeded, 1 cancelled"
    assert window._progress_chunk_color == "#d8a93a"
    assert window.progress.value() == 3
    assert window._batch_finished_fired == 1


# ---------------------------------------------------------------------------
# D2: the bare-seam identity write (unit level, real writer method)
# ---------------------------------------------------------------------------


class _Seam:
    """Bare seam carrying ONLY the real MainWindow accounting methods
    (the same house pattern as test_v0410_gui_meter's _RenderSeam)."""

    _consume_terminal_snapshot = MainWindow._consume_terminal_snapshot
    _apply_batch_summary = MainWindow._apply_batch_summary
    _batch_bar_palette = MainWindow._batch_bar_palette
    _batch_slot_for = MainWindow._batch_slot_for


def _seam_with(run_total, rows=2):
    seam = _Seam()
    seam.run_total = run_total
    seam.run_counts = [None] * run_total
    seam._progress_chunk_color = None
    seam.run_summary = None
    seam._batch_slots = []
    seam._batch_slot_index = {}
    seam.progress = QProgressBar()
    seam.progress.setRange(0, max(1, run_total))
    seam.file_table = QTableWidget(rows, 1)
    for i in range(rows):
        seam.file_table.setItem(i, 0, QTableWidgetItem(""))
    return seam


def test_d2_seam_unknown_job_falls_back_to_row(qapp):
    """A terminal payload WITHOUT a job_id (or for an unregistered id)
    still lands by row — the legacy behavior preserved, and a stray
    id can never expand the slate."""
    seam = _seam_with(2)
    # identity fallback with an id never seen in the slot registry and
    # a row inside the slate: claims the row's slot
    seam._consume_terminal_snapshot(
        1, {"job_id": "vl-unknown", "state": "failed", "engine_simulated": False}
    )
    assert seam.run_counts == [None, "failed"]
    # an unknown id at an OUT-OF-RANGE row cannot expand the slate
    seam2 = _seam_with(2)
    seam2._consume_terminal_snapshot(
        7, {"job_id": "vl-unknown-2", "state": "succeeded", "engine_simulated": False}
    )
    assert seam2.run_counts == [None, None]
    # an identity-LESS payload keeps the row write
    seam3 = _seam_with(1)
    seam3._consume_terminal_snapshot(0, {"state": "cancelled", "engine_simulated": False})
    assert seam3.run_counts == ["cancelled"]


def test_d2_seam_same_job_row_shift_writes_one_slot(qapp):
    """The exact D2 mechanics at the writer level: job a lands in slot
    0; later, job b (now at ROW 0 after the drain) writes ITS slot 1 —
    the same row, a different slot."""
    seam = _seam_with(2)
    seam._consume_terminal_snapshot(
        0, {"job_id": "a", "state": "succeeded", "engine_simulated": False}
    )
    # job b was at row 1 while both were live; after a's drain it is
    # at row 0 — the row-based writer would have overwritten a.
    seam._consume_terminal_snapshot(
        0, {"job_id": "b", "state": "succeeded", "engine_simulated": False}
    )
    assert seam.run_counts == ["succeeded", "succeeded"]
    assert seam.run_summary.succeeded == 2
    assert seam.run_summary.resolved == 2


# ---------------------------------------------------------------------------
# D3: the meter's big number never invents 0% (real widget seam)
# ---------------------------------------------------------------------------


def test_d3_all_na_sample_renders_na_not_zero(qapp):
    """The exact sample parse_nvidia_smi_output("N/A\\nN/A\\n")
    produces (tool_available=True, utilization=None) must render the
    n/a state — never a confident "0%" (the D3 bug)."""
    window = GpuMeterWidget()
    try:
        sample = parse_nvidia_smi_output("N/A\nN/A\n")
        assert sample.tool_available is True
        assert sample.utilization is None
        window._accept_sample(sample)
        values = window._rendered_values()
        assert values == (None, "")  # the n/a state
        assert values[0] != 0.0  # no invented 0%
    finally:
        window.stop()


def test_d3_garbage_exit0_sample_renders_na_not_zero(qapp):
    """Garbage-but-exit-0 stdout ("not even csv\\n") parses to
    tool_available=True, utilization=None — the widget must render
    n/a there too."""
    window = GpuMeterWidget()
    try:
        sample = parse_nvidia_smi_output("not even csv\n")
        assert sample.tool_available is True
        assert sample.utilization is None
        window._accept_sample(sample)
        assert window._rendered_values() == (None, "")
    finally:
        window.stop()


def test_d3_paingate_none_utilization_is_na(qapp):
    """The painter's branch selection mirrors _rendered_values: a
    None-utilization (but tool-available) sample selects the n/a arm.
    (Exercised through a render — must not raise and must not invent
    a percent.)"""
    from PyQt6.QtGui import QImage

    window = GpuMeterWidget()
    try:
        window._accept_sample(parse_nvidia_smi_output("N/A\nN/A\n"))
        img = QImage(window.width(), window.height(), QImage.Format.Format_ARGB32)
        window.render(img)  # the n/a paint arm — no crash, no "0%"
        # and back to a real value: the percent arm renders again
        window._accept_sample(parse_nvidia_smi_output("55\n"))
        img2 = QImage(window.width(), window.height(), QImage.Format.Format_ARGB32)
        window.render(img2)
        assert window._rendered_values() == (55.0, "")
    finally:
        window.stop()


def test_d3_real_value_still_renders_percent(qapp):
    """A real utilization still renders the percent + hint exactly as
    before — the gate fix must never swallow honest numbers."""
    window = GpuMeterWidget()
    try:
        from vidlift.ui.gpu_meter import GpuSample

        window._accept_sample(GpuSample(66, 2, True))
        assert window._rendered_values() == (66.0, "x2")
    finally:
        window.stop()