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
