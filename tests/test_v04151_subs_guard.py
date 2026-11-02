"""v0.4.15.1 — the 'Add Subtitles…' guard fix (Eric's live bug).

Eric hit this at 2026-11-02 12:12 CST: with files loaded and planned
(verdict rows on the table) and NOTHING running (GPU 1%, zero jobs in
the registry), 'Add Subtitles…' still blocked with "Wait for the
current batch to finish or cancel it first."

Root cause: the choose_subs_files guard read
``self.client.active_job_count() > 0 or self.job_ids`` — the second
clause is the WINDOW's batch state, not running work. A stale slate
(terminal-but-undrained ids, the mid case scripted below) or any
window-owned ids made the guard fire with an idle registry.

Bob's decision (v0.4.15.1): the planned-files state is NOT a batch.
Standalone subs mode is INDEPENDENT of the upscale batch slate — so
'Add Subtitles…' must open while an upscale batch RUNS (subs jobs ride
the same serial executor's queue; they never contend with the running
encode for its worker, and the executor serializes everything). The
block dialog fires only on a REAL conflict: a live (queued/running)
subs job for the SAME source file (the run-side gate precedent,
subs_job.py:144-146, made visible pre-flight).

Red-first contracts against v0.4.15 (d39d3d4):

1. Picker opens with a stale window slate (job_ids non-empty) and zero
   active jobs — the Eric reproduction. RED on shipped code: the block
   dialog appears instead of the file dialog.
2. Picker opens DURING a running upscale batch; picked files enqueue
   and APPEND into the still-live upscale slate (independence), the
   live slate's structures remain coherent (slots/run_counts/row map
   extended, never reset).
3. Same-source conflict: while a subs job for file A is live (queued/
   running), picking A again shows the block dialog and changes
   nothing; picking only NEW files opens the picker and enqueues.
4. An enqueue failure mid-concurrent-submission must NOT clobber the
   live upscale slate's job_ids (the old reset path destroyed them).

Conventions: QT_QPA_PLATFORM=offscreen, QSettings isolated to tmp_path,
QMessageBox/dialog monkeypatched with capture flags, the scripted
_ScriptedClient harness from the v0.4.14 Mode B suite.
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
from vidlift.plan import BatchPlan

from test_v0414_mode_b_gui import (
    _plan_for,
    _ScriptedClient,
    _window as _mode_b_window,
)


# ------------------------------------------------------------------ tests
@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _window(qapp, monkeypatch, tmp_path, client):
    yield from _mode_b_window(qapp, monkeypatch, tmp_path, client)
def test_picker_opens_with_stale_slate_and_zero_active_jobs(
    qapp, monkeypatch, tmp_path
):
    """§1 RED-FIRST (the Eric bug): the mid case — a planned batch sits
    on the table, the window still owns a stale job_ids entry (the
    shipped guard's false-positive state: non-empty window ids, zero
    active jobs) — must OPEN the picker, not the block dialog."""
    plans = [_plan_for(tmp_path, name) for name in ("eric-1.avi", "eric-2.avi")]
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    # The Eric state: the window believes it owns batch ids while the
    # registry reports nothing active (stale/undrained slate).
    win.job_ids = ["VJ9"]
    assert client.active_job_count() == 0

    import vidlift.gui as gui_mod

    picked = []
    new_file = tmp_path / "eric-subs-target.mkv"
    new_file.write_bytes(b"x")
    picked.append(str(new_file))
    captured: dict = {"opened": False, "blocked": False}

    def fake_dialog(*a, **k):
        captured["opened"] = True
        return picked, ""

    monkeypatch.setattr(gui_mod.QFileDialog, "getOpenFileNames", fake_dialog)

    def fake_information(*a, **k):
        captured["blocked"] = True
        captured["blocked_text"] = a[2] if len(a) > 2 else ""
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "information", staticmethod(fake_information))

    win.add_subs_button.click()

    assert captured["opened"], (
        "picker must open when nothing is running — the planned-files "
        "state is not a batch"
    )
    assert not captured["blocked"], (
        f"block dialog shown with zero active jobs: {captured['blocked_text']!r}"
    )


def test_subs_enqueue_during_running_upscale_batch_appends(
    qapp, monkeypatch, tmp_path
):
    """§2 (Bob's independence decision): with an upscale batch LIVE
    (active jobs > 0), 'Add Subtitles…' must still open and enqueue;
    the picked files append into the STILL-LIVE upscale slate without
    resetting its slots/run_counts/row map."""
    plans = [_plan_for(tmp_path, name) for name in ("live-a.avi", "live-b.avi")]
    client = _ScriptedClient()
    client.make_job(plans[0].report.path, [{"state": "queued"}])
    client.make_job(plans[1].report.path, [{"state": "queued"}])
    subs_file = tmp_path / "sidecar-ep.mkv"
    subs_file.write_bytes(b"x")
    client.make_job(str(subs_file), [{"state": "queued"}], kind="subs")

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(subs_file)], "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    win.start_upscale()
    assert win.job_ids == ["VJ1", "VJ2"]
    assert client.active_job_count() == 2  # upscale batch is live

    win.add_subs_button.click()
    assert client.last_enqueued_kinds[-1] == "subs"
    assert "VJ3" in win.job_ids
    # appended into the LIVE slate: slots extended in submission order,
    # run_counts extended, row map gained the new id — nothing reset
    assert win._batch_slots == ["VJ1", "VJ2", "VJ3"]
    assert win.run_total == 3
    assert win.run_counts == [None, None, None]
    assert win._batch_row_for_job["VJ3"] == 2
    next(gen, None)


def test_same_source_subs_conflict_blocks_with_dialog(
    qapp, monkeypatch, tmp_path
):
    """§3a: while a subs job for file A is live, picking A again shows
    the block dialog and enqueues nothing (the run-side never-overwrite
    gate, made visible pre-flight per Bob's standing rule)."""
    file_a = tmp_path / "same-a.mkv"
    file_a.write_bytes(b"x")
    client = _QueueViewsClient()
    client.make_job(
        str(file_a),
        [
            {"state": "queued"},
            {"state": "running", "current_phase": "transcribe",
             "progress": {"segments_done": 2}},
        ],
        kind="subs",
    )

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(file_a)], "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    win._tick_elapsed()
    win._tick_elapsed()
    assert "transcribing" in win.stage_label.text()

    captured: dict = {"opened": False, "blocked": False}

    def fake_dialog(*a, **k):
        captured["opened"] = True
        return [str(file_a)], ""

    monkeypatch.setattr(gui_mod.QFileDialog, "getOpenFileNames", fake_dialog)

    def fake_information(*a, **k):
        captured["blocked"] = True
        captured["blocked_text"] = a[2] if len(a) > 2 else ""
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "information", staticmethod(fake_information))

    win.add_subs_button.click()
    assert captured["opened"], "picker must open; the conflict check is on its result"
    assert captured["blocked"], "same-source conflict must show the block dialog"
    assert "Wait for the current batch" in captured["blocked_text"]
    assert win.job_ids == ["VJ1"]  # nothing new enqueued
    assert win._batch_slots == ["VJ1"]
    next(gen, None)


def test_second_subs_batch_allowed_when_files_differ(
    qapp, monkeypatch, tmp_path
):
    """§3b (Bob's standing rule): concurrent subs batches are allowed —
    the executor serializes them — so a live subs batch must NOT block
    a second batch of DIFFERENT files."""
    file_a = tmp_path / "batch1-a.mkv"
    file_a.write_bytes(b"x")
    file_b = tmp_path / "batch2-b.mkv"
    file_b.write_bytes(b"x")
    client = _QueueViewsClient()
    client.make_job(str(file_a), [{"state": "queued"}], kind="subs")
    client.make_job(str(file_b), [{"state": "queued"}], kind="subs")

    import vidlift.gui as gui_mod

    results = [[str(file_a)], [str(file_b)]]
    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: (results.pop(0), "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    assert win.job_ids == ["VJ1"]
    win.add_subs_button.click()  # second batch, different file — allowed
    assert win.job_ids == ["VJ1", "VJ2"]
    assert client.last_enqueued_kinds == ["subs"]  # the LAST call's kinds
    next(gen, None)


def test_enqueue_failure_mid_concurrent_submission_keeps_live_slate(
    qapp, monkeypatch, tmp_path
):
    """§4: a failed concurrent subs submission (missing file) must NOT
    clobber the live upscale slate — the old except path reset
    self.job_ids = [] unconditionally, which would orphan a RUNNING
    upscale batch's cancel/poll set."""
    plans = [_plan_for(tmp_path, name) for name in ("keep-a.avi", "keep-b.avi")]
    client = _ScriptedClient()
    client.make_job(plans[0].report.path, [{"state": "queued"}])
    client.make_job(plans[1].report.path, [{"state": "queued"}])
    ghost = tmp_path / "vanishing.mkv"  # never created

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(ghost)], "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    win.start_upscale()
    assert win.job_ids == ["VJ1", "VJ2"]

    warned: dict = {"shown": False}
    orig_warning = QMessageBox.warning

    def fake_warning(*a, **k):
        warned["shown"] = True
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake_warning))

    win.add_subs_button.click()  # picker opens; enqueue raises FileNotFoundError
    assert warned["shown"], "the failure must surface to the user"
    # the live upscale slate survives untouched
    assert win.job_ids == ["VJ1", "VJ2"]
    assert win._batch_slots == ["VJ1", "VJ2"]
    assert win.run_total == 2
    next(gen, None)


def test_active_subs_paths_scopes_kind_and_state():
    """Unit pin on the helper: ONLY live (running+queued) subs records'
    paths count as conflicts; upscale records never do."""
    class _Record:
        def __init__(self, kind, paths):
            self.kind = kind
            self.paths = paths

    running = [_Record("subs", ["/media/a.mkv"]), _Record("run", ["/media/b.avi"])]
    queued = [_Record("run", ["/media/c.avi"]), _Record("subs", ["/media/d.mkv"])]
    from vidlift.gui import MainWindow
    conflict = MainWindow._active_subs_paths(running, queued)
    assert conflict == {"/media/a.mkv", "/media/d.mkv"}
    assert MainWindow._active_subs_paths([], []) == frozenset()
    assert MainWindow._active_subs_paths([], queued) == {"/media/d.mkv"}

class _QueueViewsClient(_ScriptedClient):
    """_ScriptedClient + the local client's queue_views surface: live
    (queued/running) SUBS records only, built from the scripted job
    order + per-id current snapshot state — the shape the real
    LocalBridgeClient returns (JobRecords with .kind/.paths)."""

    class _Record:
        def __init__(self, kind, paths):
            self.kind = kind
            self.paths = paths

    def queue_views(self):
        running, queued = [], []
        for job_id in self.submitted:
            snap = self.status_snapshot(job_id)
            state = (snap or {}).get("state") or "queued"
            record = self._Record(
                self.subs_kinds.get(job_id, "run"),
                [self.jobs[job_id]["file_path"]],
            )
            if state in ("succeeded", "failed", "cancelled"):
                continue
            if state == "running":
                running.append(record)
            else:
                queued.append(record)
        return running, queued
