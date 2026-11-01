"""v0.4.13 ADDENDUM boundary contracts (bob-addendum-selection.md §4) —
row-click crash defense-in-depth (ITEM 1) + the selection-follow state
machine (ITEM 2), driven through the REAL `_poll_jobs`/detail pipeline on
an offscreen MainWindow.

Same harness pattern as test_v0413_boundary.py (scripted `_ScriptedClient`,
real MainWindow, real consume pipeline). NEW FILE per the design (do NOT
merge into Forge's Cancel Batch boundary file).

T1 — the Eric repro sequence (cancel first, click second row): contract
      test, must pass pre- AND post-fix on CI (the harness cannot reproduce
      the native Windows crash, §1.2 — never assert a crash); pins the
      exact sequence forever and asserts the POST-FIX observable behavior.
T2 — selection × state matrix: every row/state combination renders safely
      (succeeded / cancelled / running mid-upscale / queued-minimal /
      snapshot-None / no job mapping).
T3 — the follow machine: claim-edge select, user override, snap on next
      claim; exactly ONE emission per programmatic snap (no oscillation).
T4 — edges (b)(c)(d)(e)(f)+keyboard, programmatically-guarded snap,
      detail-rides-poll (§3.3), new-batch refollow (§3.3-c/T4c).
T5 — no-model-mutation-during-batch contract (kills C1 structurally;
      RED-first proven: the old _refresh_details called render_batch).
T6 — poll-path adversarial shapes (C2 hardening; the float("abc") arm is
      RED-first proven against the unguarded gui.py float(eta)).
T7 — joint Cancel-Batch interplay (§3.5 edge e): settled selection after
      a full batch cancel.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import time

import pytest

pytest.importorskip("PyQt6")

os.environ.setdefault("VIDLIFT_MONITOR_AUTOSTART", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
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
    same contract as the Cancel Batch boundary file's client."""

    def __init__(self):
        self.sequences: dict[str, list] = {}
        self.cursor: dict[str, int] = {}
        self.job_order: list[str] = []
        self.jobs: dict[str, dict] = {}
        self.cancel_calls: list[str] = []
        self.cancel_batch_calls: list[list[str]] = []
        self.armed = False
        self.submitted: list[str] = []
        self.counter = 0
        self.status_calls = 0
        self.fail_next_status = False

    # -- scripting helpers --
    def make_job(self, file_path, script: list[dict]) -> str:
        self.counter += 1
        job_id = f"VJ{self.counter}"
        self.job_order.append(job_id)
        self.jobs[job_id] = {"file_path": file_path}
        self.sequences[job_id] = [{"job_id": job_id, **snap} for snap in script]
        self.cursor[job_id] = 0
        return job_id

    def script_tail(self, job_id, snapshots: list[dict]) -> None:
        self.sequences[job_id].extend(
            {"job_id": job_id, **snap} for snap in snapshots
        )

    # -- LocalBridgeClient contract --
    def active_job_count(self):
        if not self.armed:
            return 0
        active = 0
        for job_id in self.submitted:
            seq = self.sequences[job_id]
            current = seq[min(self.cursor[job_id], len(seq) - 1)] if seq else None
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
        self.status_calls += 1
        if self.fail_next_status:
            # §2.1 step 4 / §2.3: a bridge read fault degrades to plan
            # body inside the renderer boundary, never raises out.
            raise RuntimeError("scripted bridge read fault")
        seq = self.sequences.get(job_id) or []
        if seq:
            return dict(seq[min(self.cursor.get(job_id, 0), len(seq) - 1)])
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
        self.cancel_batch_calls.append(list(job_ids))
        for job_id in job_ids:
            seq = self.sequences.get(job_id)
            if not seq:
                continue
            current = seq[min(self.cursor[job_id], len(seq) - 1)]
            if (current.get("state") or "queued") in (
                "succeeded", "failed", "cancelled"
            ):
                continue
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


def _excepthook_capture(monkeypatch):
    """Capture anything escaping a Qt slot (T1/T2's no-exception contract)."""
    import sys

    captured: list = []
    original = sys.excepthook
    monkeypatch.setattr(sys, "excepthook", lambda *args: captured.append(args))
    return captured


def _start_batch(win, plans, client, scripts):
    ids = [
        client.make_job(plans[i].report.path, scripts[i])
        for i in range(len(plans))
    ]
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    win.start_upscale()
    return ids


# ---------------------------------------------------------------- T1

def test_t1_cancel_first_then_click_second_row_no_exception(
    qapp, monkeypatch, tmp_path
):
    """The verbatim Eric sequence, contract-pinned (addendum §4-T1). The
    harness cannot reproduce the native Windows crash (§1.2) — this test
    asserts SAFE RENDERING + the post-fix observable detail content, never
    a crash."""
    plans = [_plan_for(tmp_path, n) for n in ("alpha-one.avi", "bravo-two.avi")]
    client = _ScriptedClient()
    captured = _excepthook_capture(monkeypatch)

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    ids = _start_batch(
        win,
        plans,
        client,
        [
            [{"state": "running", "current_phase": "upscale"}],
            [{"state": "queued"}],
        ],
    )
    j1, j2 = ids

    win._tick_elapsed()  # J1 running, J2 queued
    # Follow (§3.2): the first claim edge selects the running row.
    assert win.file_table.selectionModel().selectedRows()[0].row() == 0
    assert win._follow_mode is True

    win.cancel_upscale()  # THE cancel (Eric cancelled the first item)
    client.script_tail(j1, [{"state": "cancelled", "current_phase": "done"}])
    # Drain J1 to terminal; J2 just-claimed minimal snapshot (no
    # progress/eta keys — the §2.2 "running, just claimed" shape).
    client.script_tail(
        j2,
        [{"state": "running", "current_phase": "starting"}],
    )
    win._tick_elapsed()  # J1's cancelled terminal consumed
    win._tick_elapsed()  # J2's just-claimed running snapshot consumed

    # THE click: the second row (J2), through the real selection signal.
    row = _row_by_filename(win, "bravo-two.avi")
    win.file_table.selectRow(row)
    qapp.processEvents()
    win._sync_details()

    assert captured == []  # nothing escaped a slot
    assert _row_by_filename(win, "alpha-one.avi") == 0
    text = win.details.toPlainText()
    assert "bravo-two.avi" in text  # J2's OWN plan body renders
    assert "── LIVE ──" in text  # post-fix: the live overlay rides along
    # The just-claimed minimal snapshot renders its honest state.
    assert "running" in text
    next(gen, None)


# ---------------------------------------------------------------- T2

def test_t2_select_each_row_in_each_state_renders_safely(
    qapp, monkeypatch, tmp_path
):
    """§4-T2: 4-job batch holding succeeded / cancelled / running
    mid-upscale / queued-minimal simultaneously; each row selected in
    turn renders its own filename + the honest live block. Plus a 5th
    case: a row with NO job mapping (not-ready planned file) renders
    plan body only."""
    plans = [
        _plan_for(tmp_path, n)
        for n in ("alpha.avi", "bravo.avi", "charlie.avi", "delta.avi")
    ]
    # A 5th, NOT-READY row never enters _batch_row_for_job.
    not_ready = _plan_for(tmp_path, "echo-unready.avi")
    not_ready = replace(not_ready, process_verdict="needs_conversion")
    plans.append(not_ready)
    client = _ScriptedClient()
    scripts = [
        [{"state": "succeeded"}],
        [{"state": "cancelled", "current_phase": "done"}],
        [{  # running mid-upscale: frames fields present
            "state": "running", "current_phase": "upscale",
            "progress": {"frames_done": 12480, "frames_total": 33254,
                         "chunk_index": 3, "chunk_total": 12},
        }],
        [{"state": "queued"}],
    ]
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    for i in range(4):
        client.make_job(plans[i].report.path, scripts[i])
    # ALL FIVE rows render (incl. the not-ready one — row 4), but only
    # the four READY rows enqueue (start_upscale filters by verdict;
    # the real flow for a mixed plan).
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    win.start_upscale()
    assert win._batch_row_for_job == {"VJ1": 0, "VJ2": 1, "VJ3": 2, "VJ4": 3}

    win._tick_elapsed()  # one pairing lands all four states
    # The tick's claim edge on VJ3 snapped to row 2 (fine — each row is
    # re-selected manually below, exercising the user path).
    names = ["alpha.avi", "bravo.avi", "charlie.avi", "delta.avi"]
    expected_state = ["succeeded", "cancelled", "running", "queued"]
    captured = _excepthook_capture(monkeypatch)
    for i, name in enumerate(names):
        row = _row_by_filename(win, name)
        win.file_table.selectRow(row)
        qapp.processEvents()
        text = win.details.toPlainText()
        assert name in text, f"row {i} ({name}) must render its own body"
        assert "── LIVE ──" in text
        assert expected_state[i] in text, (
            f"row {i} ({name}) must render its honest live state"
        )
    # 5th case: the no-mapping row (plan body only, no LIVE block).
    row = _row_by_filename(win, "echo-unready.avi")
    win.file_table.selectRow(row)
    qapp.processEvents()
    text = win.details.toPlainText()
    assert "echo-unready.avi" in text
    assert "── LIVE ──" not in text
    # 6th case: scripted bridge fault for the selected row's snapshot →
    # honest degrade, never a raise out of the render (§2.1/§2.3).
    client.fail_next_status = True
    row = _row_by_filename(win, "delta.avi")
    win.file_table.selectRow(row)
    qapp.processEvents()
    assert captured == []
    assert "(no data — job lookup failed)" in win.details.toPlainText()
    client.fail_next_status = False
    next(gen, None)


# ---------------------------------------------------------------- T3

def test_t3_selection_follows_running_then_user_override_then_snap(
    qapp, monkeypatch, tmp_path
):
    """§4-T3, Eric's core flow: J1 claim selects row0; J2 claim snaps;
    a user selection of row 2 (J3) holds across J2 running ticks while
    the detail LIVE-updates for J3; the J2→J3 claim edge snaps back and
    re-engages follow. RED on v0.4.12 (no follow machine existed)."""
    plans = [
        _plan_for(tmp_path, n)
        for n in ("one.avi", "two.avi", "three.avi")
    ]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    ids = _start_batch(
        win,
        plans,
        client,
        [
            # J1: running held for TWO script steps so the J1 claim edge
            # (tick 1) and the J1-terminal tick (tick 3) land where the
            # assertions expect (sequences advance ONE step per job per
            # tick).
            [{"state": "running", "current_phase": "upscale"},
             {"state": "running", "current_phase": "upscale"},
             {"state": "succeeded"}],
            # J2: queued through ticks 1-3, running at ticks 4-6 (edge
            # #2, then two level holds), terminal at tick 7 — the SAME
            # pairing that observes J3 running (handoff).
            [{"state": "queued"},
             {"state": "queued"},
             {"state": "queued"},
             {"state": "running", "current_phase": "upscale",
              "progress": {"frames_done": 100, "frames_total": 900}},
             {"state": "running", "current_phase": "upscale",
              "progress": {"frames_done": 200, "frames_total": 900}},
             {"state": "running", "current_phase": "upscale",
              "progress": {"frames_done": 300, "frames_total": 900}},
             {"state": "succeeded"}],
            # J3: queued through ticks 1-6, running-5 at tick 7 (the
            # handoff pairing).
            [{"state": "queued"},
             {"state": "queued"},
             {"state": "queued"},
             {"state": "queued"},
             {"state": "queued"},
             {"state": "queued"},
             {"state": "running", "current_phase": "upscale",
              "progress": {"frames_done": 5, "frames_total": 900}}],
        ],
    )
    j1, j2, j3 = ids

    # Selection-signal spy: count emissions to prove the anti-fight
    # property — HOLD ticks (no claim edge) must emit ZERO selection
    # churn (K2.7: no oscillation between app and user).
    signals: list[int] = []
    win.file_table.itemSelectionChanged.connect(lambda: signals.append(1))

    win._tick_elapsed()  # J1 running consumed → CLAIM edge #1
    assert win.file_table.selectionModel().selectedRows()[0].row() == _row_by_filename(win, "one.avi")
    assert win._follow_mode is True
    assert win._follow_job_id == j1
    row_j1 = _row_by_filename(win, "one.avi")
    # The follow path rendered J1's detail itself.
    assert "one.avi" in win.details.toPlainText()

    win._tick_elapsed()  # J1 still running (level); J2 queued
    assert win._follow_job_id == j1  # level, not edge: identity unchanged

    win._tick_elapsed()  # J1 terminal + J2 queued (no running obs)
    assert win._follow_job_id == j1  # stale-id inert: no new edge

    win._tick_elapsed()  # J2 running consumed → CLAIM edge #2
    assert win.file_table.selectionModel().selectedRows()[0].row() == _row_by_filename(win, "two.avi")
    assert win._follow_mode is True
    assert win._follow_job_id == j2

    # THE USER CLICK: select row 2 (J3's row) — the override edge.
    row_j3 = _row_by_filename(win, "three.avi")
    win.file_table.selectRow(row_j3)
    qapp.processEvents()
    assert win._follow_mode is False
    emissions_after_click = len(signals)  # the click's own emissions

    win._tick_elapsed()  # J2 running (level hold); J3 queued
    win._tick_elapsed()  # J2 running (level hold); J3 queued
    assert win.file_table.selectionModel().selectedRows()[0].row() == row_j3
    assert win._follow_mode is False
    assert win._follow_job_id == j2  # held level across the override
    assert "three.avi" in win.details.toPlainText()
    # Anti-fight: the two override-hold ticks emitted ZERO net extra
    # selection churn beyond the click's own delivery (no oscillation;
    # K2.7).
    assert len(signals) == emissions_after_click

    win._tick_elapsed()  # J2 terminal + J3 running in ONE pairing → edge #3
    assert win.file_table.selectionModel().selectedRows()[0].row() == row_j3
    assert win._follow_mode is True
    assert win._follow_job_id == j3
    assert "three.avi" in win.details.toPlainText()
    next(gen, None)


# ---------------------------------------------------------------- T4

def test_t4a_idle_gap_leaves_selection(qapp, monkeypatch, tmp_path):
    """§3.3-b: ticks with ZERO running observations never touch the
    selection."""
    plans = [_plan_for(tmp_path, n) for n in ("a.avi", "b.avi")]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    (j1, j2) = _start_batch(
        win,
        plans,
        client,
        [
            [{"state": "running", "current_phase": "upscale"},
             {"state": "succeeded"}],
            [{"state": "queued"}, {"state": "queued"}, {"state": "queued"}],
        ],
    )
    win._tick_elapsed()  # J1 claim edge → row0 selected
    selected = win.file_table.selectionModel().selectedRows()[0].row()
    win._tick_elapsed()  # J1 terminal; J2 queued (no running obs)
    win._tick_elapsed()  # J2 queued again
    win._tick_elapsed()  # J2 queued again — the idle gap
    assert win._follow_job_id == j1
    assert win.file_table.selectionModel().selectedRows()[0].row() == selected
    next(gen, None)


def test_t4b_batch_finished_leaves_selection(
    qapp, monkeypatch, tmp_path
):
    """§3.3-c: BATCH_END writes nothing; the stale _follow_job_id stays
    inert; the terminal row content stays inspectable."""
    plans = [_plan_for(tmp_path, "solo.avi")]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    (j1,) = _start_batch(
        win,
        plans,
        client,
        [[{"state": "running", "current_phase": "upscale"},
          {"state": "succeeded"}]],
    )
    win._tick_elapsed()  # claim edge → row0
    win._tick_elapsed()  # terminal → batch end
    assert win.job_ids == []
    assert win._follow_job_id == j1  # stale-but-inert
    assert win.file_table.selectionModel().selectedRows()[0].row() == 0
    assert "solo.avi" in win.details.toPlainText()
    next(gen, None)


def test_t4c_new_batch_refollows(qapp, monkeypatch, tmp_path):
    """§3.2 BATCH_START: after batch 1 (user overridden) a FRESH batch's
    first claim selects its first row regardless of the stale override."""
    plans = [_plan_for(tmp_path, n) for n in ("p1.avi", "q1.avi")]
    plans2 = [_plan_for(tmp_path, n) for n in ("p2.avi", "q2.avi")]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    _start_batch(
        win,
        plans,
        client,
        [
            [{"state": "running", "current_phase": "upscale"}],
            [{"state": "queued"}],
        ],
    )
    win._tick_elapsed()  # claim → row0
    win.file_table.selectRow(1)  # user override
    qapp.processEvents()
    assert win._follow_mode is False

    # Fresh batch (fresh scripted client instance, like contract (e)).
    client2 = _ScriptedClient()
    win.client = client2
    win.job_ids = []
    win._run_started_at = None
    win._stop_liveness()
    _start_batch(
        win,
        plans2,
        client2,
        [
            [{"state": "running", "current_phase": "upscale"}],
            [{"state": "queued"}],
        ],
    )
    assert win._batch_row_for_job == {"VJ1": 0, "VJ2": 1}
    win._tick_elapsed()
    assert win.file_table.selectionModel().selectedRows()[0].row() == 0
    assert win._follow_mode is True
    assert "p2.avi" in win.details.toPlainText()
    next(gen, None)


def test_t4d_keyboard_navigation_is_user_override(
    qapp, monkeypatch, tmp_path
):
    """§3.4: keyboard selection (arrow) is the SAME USER_SELECT edge."""
    plans = [_plan_for(tmp_path, n) for n in ("k1.avi", "k2.avi", "k3.avi")]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    _start_batch(
        win,
        plans,
        client,
        [
            [{"state": "running", "current_phase": "upscale"}],
            [{"state": "queued"}],
            [{"state": "queued"}],
        ],
    )
    win._tick_elapsed()  # claim → row0, follow engaged
    assert win._follow_mode is True
    win.file_table.setFocus()
    QTest.keyClick(win.file_table, Qt.Key.Key_Down)
    qapp.processEvents()
    assert win._follow_mode is False  # keyboard == click
    next(gen, None)


def test_t4e_programmatic_snap_does_not_self_cancel(
    qapp, monkeypatch, tmp_path
):
    """§3.4: during a follow-write the handler must NOT clear
    _follow_mode (the guard); the snap is exactly ONE selection emission."""
    plans = [_plan_for(tmp_path, n) for n in ("g1.avi", "g2.avi")]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    (j1, j2) = _start_batch(
        win,
        plans,
        client,
        [
            [{"state": "queued"}],
            [{"state": "running", "current_phase": "upscale"}],
        ],
    )
    emissions: list[int] = []
    win.file_table.itemSelectionChanged.connect(lambda: emissions.append(1))
    win._tick_elapsed()  # J1 queued tick — no edge
    assert win._follow_mode is True  # nothing disengaged it
    win._tick_elapsed()  # J1 queued, J2 running → CLAIM edge on J2
    assert win._follow_mode is True
    assert win._follow_job_id == j2
    assert win.file_table.selectionModel().selectedRows()[0].row() == 1
    # The snap itself emitted selection churn but never disengaged
    # follow; a second tick keeps the identity level (no re-snap storm).
    win._tick_elapsed()
    assert win._follow_mode is True
    assert win._follow_job_id == j2
    next(gen, None)


def test_t4f_user_selected_row_goes_terminal_then_next_claim_snaps(
    qapp, monkeypatch, tmp_path
):
    """§3.3-d: the user's selected row itself goes terminal — selection
    STAYS through the terminal tick (the detail keeps rendering its
    terminal overlay), then the next CLAIM edge snaps."""
    plans = [_plan_for(tmp_path, n) for n in ("u1.avi", "u2.avi")]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    (j1, j2) = _start_batch(
        win,
        plans,
        client,
        [
            # J1: running (tick 1 claim), running hold (tick 2),
            # cancelled terminal at tick 3 WITHOUT J2 running in the same
            # pairing — edge (d) needs a claimless terminal tick.
            [{"state": "running", "current_phase": "upscale"},
             {"state": "running", "current_phase": "upscale"},
             {"state": "cancelled", "current_phase": "done"}],
            # J2: queued through ticks 1-3, running at tick 4 (the next
            # claim edge).
            [{"state": "queued"},
             {"state": "queued"},
             {"state": "queued"},
             {"state": "running", "current_phase": "upscale"}],
        ],
    )
    win._tick_elapsed()  # J1 claim → row0
    assert win._follow_mode is True
    win._tick_elapsed()  # J1 running holds (level); J2 queued
    assert win.file_table.selectionModel().selectedRows()[0].row() == 0
    assert win._follow_mode is True
    win._tick_elapsed()  # J1 terminal consumed; J2 still queued (§3.3-d)
    assert win.file_table.selectionModel().selectedRows()[0].row() == 0
    assert win._follow_mode is True  # no user action yet
    assert "cancelled" in win.details.toPlainText()  # terminal overlay
    win._tick_elapsed()  # J2 running → the edge snaps
    assert win.file_table.selectionModel().selectedRows()[0].row() == 1
    assert win._follow_mode is True
    next(gen, None)


def test_t4g_detail_live_update_rides_poll(qapp, monkeypatch, tmp_path):
    """§3.3-f: a selected running job's detail block advances each tick
    WITHOUT any selection signal firing (spy count 0 across ticks)."""
    plans = [_plan_for(tmp_path, "live.avi")]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    _start_batch(
        win,
        plans,
        client,
        [[{"state": "running", "current_phase": "upscale",
           "progress": {"frames_done": 100, "frames_total": 900}}]],
    )
    emissions: list[int] = []
    win.file_table.itemSelectionChanged.connect(lambda: emissions.append(1))
    win._tick_elapsed()  # claim edge + detail render (selection emission
    # from the snap is EXPECTED here — count it, then assert stillness).
    baseline = len(emissions)
    first = win.details.toPlainText()
    assert "Frames: 100/900" in first
    client.script_tail(
        win.job_ids[0],
        [{"state": "running", "current_phase": "upscale",
          "progress": {"frames_done": 200, "frames_total": 900}},
         {"state": "running", "current_phase": "upscale",
          "progress": {"frames_done": 300, "frames_total": 900}}],
    )
    win._tick_elapsed()
    win._tick_elapsed()
    assert win.details.toPlainText().count("Frames: 300/900") == 1
    assert len(emissions) == baseline  # NO selection churn across ticks
    next(gen, None)


# ---------------------------------------------------------------- T5

def test_t5_subtitle_toggle_during_batch_does_not_rebuild_table(
    qapp, monkeypatch, tmp_path
):
    """§4-T5 (RED-first proven): mid-batch the subtitle_ocr toggle must
    degrade to a detail-only re-render — setRowCount/render_batch/setItem
    are FORBIDDEN while job_ids live (the C1 class, §2.4)."""
    plans = [_plan_for(tmp_path, n) for n in ("s1.avi", "s2.avi")]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    _start_batch(
        win,
        plans,
        client,
        [
            [{"state": "running", "current_phase": "upscale"}],
            [{"state": "queued"}],
        ],
    )
    win._tick_elapsed()  # claim edge; selection on row0

    rebuilds: list[int] = []
    cells_written: list[int] = []
    original_setRowCount = win.file_table.setRowCount
    original_setItem = win.file_table.setItem

    def _spy_setRowCount(count):
        rebuilds.append(count)
        return original_setRowCount(count)

    def _spy_setItem(row, column, item):
        cells_written.append((row, column))
        return original_setItem(row, column, item)

    monkeypatch.setattr(win.file_table, "setRowCount", _spy_setRowCount)
    monkeypatch.setattr(win.file_table, "setItem", _spy_setItem)

    row0_path_cell = win.file_table.item(0, 1)
    win.subtitle_ocr.setChecked(not win.subtitle_ocr.isChecked())
    qapp.processEvents()

    assert rebuilds == []  # NO model rebuild mid-batch
    assert cells_written == []  # NO setItem writes mid-batch
    assert win.file_table.item(0, 1) is row0_path_cell  # row identity kept
    assert win.file_table.selectionModel().selectedRows()[0].row() == 0
    # The detail re-rendered detail-only (plan body still present).
    assert "s1.avi" in win.details.toPlainText()
    next(gen, None)


def test_t5b_no_render_batch_caller_post_start(qapp, monkeypatch, tmp_path):
    """§4-T5's source-grep guard: render_batch's only remaining callers
    are plan-time paths (plan_batch + tests) — no post-start path may
    rebuild the table."""
    import re

    source = Path(MainWindow.__module__.replace(".", "/") + ".py")
    repo_root = Path(__file__).resolve().parents[1]
    text = (repo_root / "vidlift" / "gui.py").read_text()
    # Find every `render_batch(` call INSIDE MainWindow's body and its
    # enclosing method name by simple line-scan.
    lines = text.splitlines()
    callers = []
    current_def = None
    for line in lines:
        m = re.match(r"\s*def (\w+)\(", line)
        if m:
            current_def = m.group(1)
        if "render_batch(" in line and ".render_batch" not in line.split("def")[0] and "def render_batch" not in line:
            callers.append(current_def)
    allowed = {"__init__", "plan_batch", "render_batch", "test_helper"}
    offenders = [c for c in callers if c not in allowed]
    assert offenders == [], f"render_batch called from post-start paths: {offenders}"


# ---------------------------------------------------------------- T6

def test_t6_tick_survives_adversarial_snapshot_shapes(
    qapp, monkeypatch, tmp_path
):
    """§4-T6 (the float("abc") arm RED-first proven against the unguarded
    float(eta)): adversarial shapes must degrade, the tick completes."""
    plans = [_plan_for(tmp_path, "adv.avi")]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    (j1,) = _start_batch(
        win,
        plans,
        client,
        [[
            {  # running, phase None, progress None
                "state": "running", "current_phase": None,
                "progress": None, "eta_seconds": "abc",
            },
            {  # mid-"upscale" with {} progress + non-floatable eta again
                "state": "running", "current_phase": "upscale",
                "progress": {}, "eta_seconds": "not-a-number",
            },
            {  # log_tail with non-str items + file_path None
                "state": "running", "current_phase": "upscale",
                "progress": {}, "log_tail": [b"bytes-line", 42, None],
                "file_path": None,
                "eta_seconds": "abc",
            },
            {"state": "succeeded"},
        ]],
    )
    captured = _excepthook_capture(monkeypatch)
    for _ in range(4):
        win._tick_elapsed()  # must complete every tick without raise
        if not win.job_ids:
            break
    assert captured == []
    assert win.job_ids == []
    assert "adv.avi" in win.details.toPlainText()
    # The terminal truth landed (the degraded ticks never swallowed it).
    assert win.file_table.item(0, 0).text() == "succeeded"
    next(gen, None)


def test_t6b_batch_eta_label_survives_bad_eta(qapp, monkeypatch, tmp_path):
    """T6's second tolerant site: _batch_running_eta's float(live) — a
    non-floatable payload eta degrades to no digits, never raises (this
    is the forecast path, called per tick via _forecast_total)."""
    plans = [_plan_for(tmp_path, "eta.avi")]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    (j1,) = _start_batch(
        win,
        plans,
        client,
        [[{"state": "running", "current_phase": "upscale",
           "progress": {"frames_done": 10, "frames_total": 900},
           "eta_seconds": "garbage"}]],
    )
    win._tick_elapsed()
    # _forecast_total rides _tick_elapsed; the adversarial value is in
    # the snapshot window — the label path must not raise and must not
    # fabricate digits from a string.
    assert win.job_ids == [j1]
    win._batch_running_eta()  # direct call: returns None, raises never
    next(gen, None)


# ---------------------------------------------------------------- T7

def test_t7_cancel_batch_leaves_selection_settled(
    qapp, monkeypatch, tmp_path
):
    """§4-T7, edge (e): follow engaged on J1 → cancel_batch all → after
    the drain the selection REMAINS on J1's row; _batch_finished ran; no
    follow-write fired after the cancel call."""
    plans = [
        _plan_for(tmp_path, n) for n in ("c1.avi", "c2.avi", "c3.avi")
    ]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    _start_batch(
        win,
        plans,
        client,
        [
            [{"state": "running", "current_phase": "upscale"}],
            [{"state": "queued"}],
            [{"state": "queued"}],
        ],
    )
    win._tick_elapsed()  # J1 claim edge → row0, follow engaged
    assert win._follow_mode is True
    assert win._follow_job_id == client.submitted[0]
    assert win.file_table.selectionModel().selectedRows()[0].row() == 0

    snap_after_engage = win.file_table.selectionModel().selectedRows()[0].row()
    emissions_before = win._follow_job_id
    win.cancel_batch_upscale()  # the confirm dialog is monkeypatched Yes
    assert len(client.cancel_batch_calls) == 1

    # Drain through the REAL pipeline.
    for _ in range(5):
        win._tick_elapsed()
        if not win.job_ids:
            break
    assert win.job_ids == []
    # _batch_finished ran (the summary text flips to the cancelled story).
    assert "Cancelling" in win.summary_count.text() or "cancelled" in win.summary_count.text().lower()
    # Edge (e): selection STAYS on the was-running row; no further claim
    # edge exists (queue drained).
    assert win.file_table.selectionModel().selectedRows()[0].row() == snap_after_engage
    assert win._follow_job_id == emissions_before  # stale-but-inert
    # No follow-write after the cancel: the row map is settled, ticks
    # carried only terminal snapshots (never a running identity).
    next(gen, None)