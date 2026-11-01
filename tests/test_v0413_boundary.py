"""v0.4.13 boundary contracts (design §7.2) — Cancel Batch, driven through
the REAL `_poll_jobs` consume path on an offscreen MainWindow.

Same harness pattern as test_v0412_boundary.py (scripted `_ScriptedClient`,
real MainWindow, real consume pipeline), extended with a
`cancel_batch_calls: list[list[str]]` recorder and a `cancel_batch(ids)`
that returns per-id results while advancing scripted states.

Contracts:
(a) PRIMARY 3-job cancel — RED-FIRST against pre-feature main: the click
    must issue ONE client.cancel_batch call with the full id list (no such
    path exists on v0.4.12), and the queued jobs must land "cancelled"
    WITHOUT ever appearing "running" in a consumed snapshot.
(b) Same-tick race: J1's terminal snapshot arrives on the SAME tick the
    cancel lands — J2 must never run.
(c) Idle-queue cancel: head already terminal, queued jobs still cancel
    (Cancel Batch enabled iff bool(job_ids), §4.2).
(d) Stage/ETA cleanup invariants at batch end post-cancel (the v0.4.12
    _batch_finished reset reached through the SAME drain).
(e) Back-to-back hygiene: cancel mid-run, then a fresh batch behaves like
    a first-ever run.
"""

from __future__ import annotations

import os
from pathlib import Path
import time

import pytest

pytest.importorskip("PyQt6")

os.environ.setdefault("VIDLIFT_MONITOR_AUTOSTART", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from vidlift.gui import MainWindow
from vidlift.plan import BatchPlan, build_file_plan
from vidlift.probe import classify, parse_probe


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


_alive: list = []


def _report(name):
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
    report = classify(parse_probe(f"/media/{name}", payload))
    return report


def _plan_for(tmp_path, name):
    report = _report(name)
    report.path = str(tmp_path / name)
    Path(str(report.path)).write_text("")  # the real client validates existence
    return build_file_plan(report, gb_per_hour=1.5)


class _ScriptedClient:
    """The GUI's bridge seam with a scripted snapshot sequence per job id;
    extends the v0.4.12 pattern with the batch-cancel recorder."""

    def __init__(self):
        self.sequences: dict[str, list] = {}
        self.cursor: dict[str, int] = {}
        self.job_order: list[str] = []
        self.jobs: dict[str, dict] = {}
        self.cancel_calls: list[str] = []
        self.cancel_batch_calls: list[list[str]] = []
        self.armed = False
        self.submitted: list[str] = []
        self.pending_sequences: list[tuple[str, list]] = []
        self.counter = 0

    # -- scripting helpers --
    def prepare(self, job_id, file_path):
        self.sequences[job_id] = []
        self.cursor[job_id] = 0
        self.job_order.append(job_id)
        self.jobs[job_id] = {"file_path": file_path}

    def make_job(self, file_path, script: list[dict]) -> str:
        """Allocate a fresh scripted id and pre-seed its sequence so
        every reader (status_snapshot/active_job_count) sees a coherent
        record immediately — the real registry creates the record at
        enqueue time, and the GUI snapshots it at the next tick."""
        self.counter += 1
        job_id = f"VJ{self.counter}"
        self.job_order.append(job_id)
        self.jobs[job_id] = {"file_path": file_path}
        self.sequences[job_id] = [{"job_id": job_id, **snap} for snap in script]
        self.cursor[job_id] = 0
        return job_id

    def script_tail(self, job_id, snapshots: list[dict]) -> None:
        """Append snapshots after the job's current script (the cancel
        path appends the terminal for a flagged running job)."""
        self.sequences[job_id].extend(
            {"job_id": job_id, **snap} for snap in snapshots
        )

    def push(self, job_id, snapshot):
        self.sequences.setdefault(job_id, []).append(
            {"job_id": job_id, **snapshot}
        )

    def load_pending(self):
        """Bind every pending script to its id (called after the ids are
        known so the cancel handler can advance the scripts)."""
        for job_id, script in self.pending_sequences:
            for snap in script:
                self.push(job_id, snap)
        self.pending_sequences = []

    # -- LocalBridgeClient contract --
    def active_job_count(self):
        if not self.armed:
            return 0
        active = 0
        for job_id in self.submitted:
            seq = self.sequences[job_id]
            curs = self.cursor[job_id]
            current = seq[min(curs, len(seq) - 1)] if seq else None
            # The GUI's rule for a drained tail is "record gone from the
            # live query path" — for accounting parity with the real
            # bridge, a terminal record stops being active either way;
            # an EXHAUSTED non-terminal tail (record still queued/
            # running in the registry) stays active.
            if current is None:
                active += 1
            elif current.get("state") not in ("succeeded", "failed", "cancelled"):
                active += 1
        return active

    def enqueue_run(self, paths, options=None, engine="auto"):
        for path in paths:
            if not Path(path).is_file():
                raise FileNotFoundError(f"no such file on this machine: {path}")
        self.armed = True
        fresh = [jid for jid in self.job_order if jid not in self.submitted]
        chosen = fresh[: len(paths)]
        self.submitted.extend(chosen)
        return list(chosen)

    def status_snapshot(self, job_id):
        seq = self.sequences.get(job_id) or []
        curs = self.cursor.get(job_id, 0)
        # The drained record reads like the real client: the terminal
        # truth survives (the registry keeps terminal records; only
        # queue Views drop them). An exhausted non-terminal sequence
        # stays queued/running.
        if seq:
            return dict(seq[min(curs, len(seq) - 1)])
        return None

    def enqueue_snapshots(self, job_ids):
        ticks = []
        for job_id in job_ids:
            ticks.append(self.status_snapshot(job_id))
            if self.cursor.get(job_id, 0) < len(self.sequences.get(job_id, [])):
                self.cursor[job_id] += 1
        return ticks

    def cancel(self, job_id):
        self.cancel_calls.append(job_id)
        return "cancelled"

    def cancel_batch(self, job_ids):
        """The v0.4.13 seam: record the call and advance every scripted
        non-terminal job per the REAL registry semantics — the mark
        lands BETWEEN polls, so the job's NEXT snapshot reads cancelled
        (a never-started job) or, for the flagged running head, the
        engine dies through terminate and a later tick reads cancelled.
        The consumed history is never rewritten (real polling is
        stateless, but the record's past states stay past)."""
        self.cancel_batch_calls.append(list(job_ids))
        for job_id in job_ids:
            seq = self.sequences.get(job_id)
            if not seq:
                continue
            curs = self.cursor.get(job_id, 0)
            current = seq[min(curs, len(seq) - 1)]
            if (current.get("state") or "queued") in (
                "succeeded", "failed", "cancelled"
            ):
                continue  # already terminal: untouched
            # queued or running: the mark/flag lands now; the next poll
            # carries the terminal truth.
            self.script_tail(job_id, [{"state": "cancelled",
                                       "current_phase": "done"}])
        return {job_id: "cancel-batch-issued" for job_id in job_ids}

    def history(self, limit=20):
        return []

    def stop(self, **kwargs):
        pass


def _window(qapp, monkeypatch, tmp_path, client):
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
    _alive.append(MainWindow(client=client))
    win = _alive[-1]
    yield win
    win.job_ids = []
    win._run_started_at = None
    win.deleteLater()
    qapp.processEvents()
    _alive.clear()
    qapp.processEvents()


def _row_by_filename(win, filename: str) -> int:
    for row in range(win.file_table.rowCount()):
        cell = win.file_table.item(row, 1)
        if cell is not None and cell.text().endswith(filename):
            return row
    raise AssertionError(f"no row displays {filename!r}")


def _row_state(win, filename: str) -> str:
    row = _row_by_filename(win, filename)
    cell = win.file_table.item(row, 0)
    return cell.text() if cell is not None else ""



def _row_color(win, filename: str) -> str:
    from PyQt6.QtGui import QColor
    row = _row_by_filename(win, filename)
    cell = win.file_table.item(row, 0)
    color = cell.foreground()
    return QColor(color.color()).name()


def test_v0413_contract_a_three_job_cancel_batch(
    qapp, monkeypatch, tmp_path
):
    """PRIMARY (design §7.2a, Karen §1): click Cancel Batch through the
    button; the running file stops and the queued files land cancelled
    WITHOUT EVER showing running; bar reads "0/3 succeeded, 3 cancelled"
    in amber; every physical row shows its own yellow cancelled."""
    names = ["alpha-one.avi", "bravo-two.avi", "charlie-three.avi"]
    plans = [_plan_for(tmp_path, n) for n in names]
    client = _ScriptedClient()
    client.make_job(
        plans[0].report.path,
        [{"state": "running", "current_phase": "upscale"}],
    )
    client.make_job(plans[1].report.path, [{"state": "queued"}])
    client.make_job(plans[2].report.path, [{"state": "queued"}])

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    win.start_upscale()
    assert win.job_ids == ["VJ1", "VJ2", "VJ3"]
    assert win.cancel_batch_button.isEnabled()  # §4.2 rule

    # Tick 1: J1 running, J2/J3 queued.
    win._tick_elapsed()
    assert _row_state(win, "alpha-one.avi") == "running"
    assert _row_state(win, "bravo-two.avi") == "queued"
    assert _row_state(win, "charlie-three.avi") == "queued"

    # THE click: through the button (dialog monkeypatched Yes).
    win.cancel_batch_button.click()

    # §4.1: ONE batch call with the FULL id set (fails on pre-feature:
    # no cancel_batch seam exists at all).
    assert client.cancel_batch_calls == [["VJ1", "VJ2", "VJ3"]]
    assert not win.cancel_batch_button.isEnabled()
    assert "Cancelling batch" in win.stage_label.text()

    # Drain to terminal through the REAL pipeline.
    for _ in range(4):
        win._tick_elapsed()
        if not win.job_ids:
            break

    assert win.job_ids == []
    # every row its own truth, never "running" for the queued two
    assert _row_state(win, "alpha-one.avi") == "cancelled"
    assert _row_state(win, "bravo-two.avi") == "cancelled"
    assert _row_state(win, "charlie-three.avi") == "cancelled"
    # bar: exact honest text + amber
    assert win.progress.format() == "0/3 succeeded, 3 cancelled"
    assert win._progress_chunk_color == "#d8a93a"
    # every PHYSICAL row shows its own yellow cancelled (v0.4.12 map)
    assert _row_color(win, "alpha-one.avi") == "#ffff00"
    assert _row_color(win, "bravo-two.avi") == "#ffff00"
    assert _row_color(win, "charlie-three.avi") == "#ffff00"
    next(gen, None)


def test_v0413_contract_b_same_tick_finish_never_starts_next(
    qapp, monkeypatch, tmp_path
):
    """Race shade (§7.2b, Karen §2): J1's terminal lands on the SAME tick
    the cancel is issued — J2 must never show running in any consumed
    snapshot and ends cancelled."""
    plans = [_plan_for(tmp_path, n) for n in ("delta-one.avi", "echo-two.avi")]
    client = _ScriptedClient()
    client.make_job(
        plans[0].report.path,
        [{"state": "running", "current_phase": "upscale"},
         {"state": "succeeded"}],
    )
    client.make_job(
        plans[1].report.path,
        [{"state": "queued"}, {"state": "queued"},
         {"state": "cancelled"}],
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    win.start_upscale()

    win._tick_elapsed()  # J1 running, J2 queued
    assert _row_state(win, "delta-one.avi") == "running"

    # Same tick as the click, J1's TERMINAL snapshot arrives (the real
    # race: worker lands terminal, loops into claim). The scripted
    # registry answers the cancel BEFORE J2's next snapshot: J2 is still
    # queued -> cancelled, never running.
    win.cancel_batch_button.click()
    win._tick_elapsed()  # J1 succeeded terminal + J2 unchanged this tick
    win._tick_elapsed()  # J2's cancelled snapshot
    for _ in range(3):
        if not win.job_ids:
            break
        win._tick_elapsed()

    assert _row_state(win, "delta-one.avi") == "succeeded"
    assert _row_state(win, "echo-two.avi") == "cancelled"
    # No consumed J2 snapshot EVER carried running.
    seq = client.sequences["VJ2"]
    assert all(
        snap.get("state") != "running"
        for snap in seq[: client.cursor["VJ2"]]
    )
    assert win.progress.format() in (
        "1/2 succeeded, 1 cancelled",
        "0/2 succeeded, 2 cancelled",
    )
    next(gen, None)


def test_v0413_contract_c_idle_queue_cancel(qapp, monkeypatch, tmp_path):
    """Idle-queue (§7.2c, Karen §3): head already terminal, nothing
    running, J2/J3 queued — Cancel Batch is enabled (iff bool(job_ids)),
    click cancels both, bar amber, controls resync idle."""
    names = ["foxtrot-one.avi", "golf-two.avi", "hotel-three.avi"]
    plans = [_plan_for(tmp_path, n) for n in names]
    client = _ScriptedClient()
    client.make_job(plans[0].report.path, [{"state": "succeeded"}])
    client.make_job(plans[1].report.path, [{"state": "queued"}])
    client.make_job(plans[2].report.path, [{"state": "queued"}])

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    win.start_upscale()

    win._tick_elapsed()  # J1's terminal arrives; J2/J3 remain queued
    assert win.job_ids == ["VJ2", "VJ3"]
    # nothing is running, so Cancel Current loses its running target —
    # but the shipped rule keeps it enabled while ANY batch job remains
    # (a per-job cancel of a queued job is legal); the v0.4.13 RULE is
    # what is pinned here: Cancel Batch is enabled iff a batch set exists.
    assert win.cancel_batch_button.isEnabled()

    win.cancel_batch_button.click()
    assert client.cancel_batch_calls == [["VJ2", "VJ3"]]
    for _ in range(3):
        win._tick_elapsed()
        if not win.job_ids:
            break
    assert win.job_ids == []
    assert _row_state(win, "golf-two.avi") == "cancelled"
    assert _row_state(win, "hotel-three.avi") == "cancelled"
    assert win.progress.format() == "1/3 succeeded, 2 cancelled"
    assert win._progress_chunk_color == "#d8a93a"
    # controls resync to idle: no batch set -> Cancel Batch disabled
    assert not win.cancel_batch_button.isEnabled()
    next(gen, None)


def test_v0413_contract_d_no_batch_no_button_and_keep_running_decline(
    qapp, monkeypatch, tmp_path
):
    """§7.2d + Karen §4/§6: with no batch, Cancel Batch is disabled and a
    stray press does nothing; answering the confirm with No ("Keep
    Running") changes nothing."""
    plan = _plan_for(tmp_path, "idle-lone.avi")
    client = _ScriptedClient()

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    # nothing enqueued: no batch, no button; a stray click is a no-op
    assert not win.cancel_batch_button.isEnabled()
    win.cancel_batch_button.click()
    assert client.cancel_batch_calls == []

    # Now a live batch, but the user declines the confirm.
    client.make_job(plan.report.path, [{"state": "running",
                                        "current_phase": "upscale"}])
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.start_upscale()
    win._tick_elapsed()
    assert _row_state(win, "idle-lone.avi") == "running"

    asked = {}

    def _decline(*args, **kwargs):
        asked["asked"] = True
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(_decline))
    win.cancel_batch_button.click()
    assert asked["asked"]  # the safety dialog fired (Karen §6)
    assert client.cancel_batch_calls == []  # answer No -> nothing happened
    assert _row_state(win, "idle-lone.avi") == "running"
    assert win.cancel_batch_button.isEnabled()  # still able to cancel later
    next(gen, None)


def test_v0413_contract_e_stage_eta_cleanup_and_back_to_back(
    qapp, monkeypatch, tmp_path
):
    """§7.2d+e, Karen §5: after the cancelled batch drains, the v0.4.12
    cleanup invariants hold (stage label empty, _stage_now queued, ETA
    state empty, rendered values None) AND a fresh batch behaves like a
    first-ever run — new ids, re-seeded rows, no stale writes."""
    plans = [_plan_for(tmp_path, n) for n in ("india-one.avi", "juliet-two.avi")]
    client = _ScriptedClient()
    client.make_job(
        plans[0].report.path,
        [{"state": "running", "current_phase": "upscale"}],
    )
    client.make_job(plans[1].report.path, [{"state": "queued"}])

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    win.start_upscale()
    win._tick_elapsed()
    win.cancel_batch_button.click()
    for _ in range(4):
        win._tick_elapsed()
        if not win.job_ids:
            break
    assert win.job_ids == []

    # The v0.4.12 _batch_finished cleanup invariants, reached through the
    # SAME drain (§7.2d) — no side door.
    assert win.stage_label.text() == ""
    assert win._stage_now == "queued"
    assert win._stage_eta is None
    assert win._stage_fraction is None
    assert win._eta_state == {}
    assert win._eta_rendered is None
    assert win._batch_eta_rendered is None
    assert win.engine_combo.isEnabled()
    assert win.profile_combo.isEnabled()

    # (e) fresh batch: new ids, re-seeded identity map, first-run behavior.
    fresh_plans = [_plan_for(tmp_path, n) for n in ("kilo-one.avi", "lima-two.avi")]
    client = _ScriptedClient()
    client.make_job(
        fresh_plans[0].report.path,
        [{"state": "running", "current_phase": "upscale"},
         {"state": "succeeded"}],
    )
    client.make_job(
        fresh_plans[1].report.path,
        [{"state": "running", "current_phase": "upscale"},
         {"state": "succeeded"}],
    )
    win.client = client
    win.batch = BatchPlan(fresh_plans)
    win.render_batch(win.batch)
    win.start_upscale()
    assert win._batch_row_for_job == {"VJ1": 0, "VJ2": 1}
    for _ in range(4):
        win._tick_elapsed()
        if not win.job_ids:
            break
    assert win.job_ids == []
    assert _row_state(win, "kilo-one.avi") == "succeeded"
    assert _row_state(win, "lima-two.avi") == "succeeded"
    assert win.progress.format() == "2/2 succeeded"
    assert win._progress_chunk_color == "#3d9865"  # fresh green, not amber
    next(gen, None)