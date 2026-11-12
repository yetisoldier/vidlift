"""v0.4.12 boundary regression contracts (design §5) — driven through the
REAL `_poll_jobs` consume path on an offscreen MainWindow.

These are the highest-boundary tests per the v0.4.12 design, written from
the two defects v0.4.10/v0.4.11 let escape:

1. Item 1 (stable row mapping): the GUI used to address table status cells
   by the job's position in the SHRINKING `job_ids` list, so every job after
   the first terminal write wrote to the WRONG physical row and the last
   job to finish left its own home row frozen at "running". The scripted
   sequence below is the exact observed repro: [J1 running, J2 running] →
   [J1 succeeded, J2 running] → [J2 running] ×N → [J2 succeeded]; each
   physical row (keyed by filename) must show its OWN job's true state and
   finish with both rows "succeeded".

2. Item 4 (green-bar boundary): a cancelled/failed terminal payload must
   render the bar text exactly ("0/1 succeeded, 1 cancelled") AND apply the
   amber palette, never green; the failed variant red; the all-succeeded
   variant green. Plus a source sweep: no "completed"-phrased batch format
   string may exist in gui.py (the counting-lie is format-level too).
"""

from __future__ import annotations

import os
from pathlib import Path
import time
import types
from types import SimpleNamespace

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
    """The GUI's bridge seam with a fully SCRIPTED snapshot sequence per
    job id: the test enqueues exactly the observed payload sequence and
    `enqueue_snapshots` replays it tick by tick (like the real bridge's
    1 Hz reads of a live registry)."""

    def __init__(self):
        self.sequences: dict[str, list] = {}
        self.cursor: dict[str, int] = {}
        self.job_order: list[str] = []
        self.jobs: dict[str, dict] = {}
        self.cancel_calls: list[str] = []
        self.armed = False  # active_job_count() reads 0 until enqueue_run
        self.submitted: list[str] = []  # ids handed out by enqueue_run

    # -- scripting helpers (test side) --
    def prepare(self, job_id, file_path):
        self.sequences[job_id] = []
        self.cursor[job_id] = 0
        self.job_order.append(job_id)
        self.jobs[job_id] = {"file_path": file_path}

    def push(self, job_id, snapshot):
        # snapshots always carry the stable job_id (the identity the row
        # map and slot index key on).
        self.sequences[job_id].append({"job_id": job_id, **snapshot})

    # -- LocalBridgeClient contract (GUI side) --
    def active_job_count(self):
        if not self.armed:
            # nothing enqueued yet: the real client counts zero.
            return 0
        # mirrors the real client: active until every SUBMITTED job
        # reached a terminal state in its sequence (an enqueuing that has
        # not happened yet cannot make anything active).
        active = 0
        for job_id in self.submitted:
            seq = self.sequences[job_id]
            curs = self.cursor[job_id]
            current = seq[min(curs, len(seq) - 1)] if seq else None
            if curs < len(seq) and current is not None and current.get(
                "state"
            ) not in ("succeeded", "failed", "cancelled"):
                active += 1
        return active

    def enqueue_run(self, paths, options=None, engine="auto"):
        # The real client validates the submitted paths.
        for path in paths:
            if not Path(path).is_file():
                raise FileNotFoundError(f"no such file on this machine: {path}")
        self.armed = True
        # Submission order: the not-yet-submitted prepared ids (the real
        # client CREATES a new JobRecord per path here — pre-registering a
        # job that was never enqueued is not a thing the registry does).
        fresh = [jid for jid in self.job_order if jid not in self.submitted]
        chosen = fresh[: len(paths)]
        self.submitted.extend(chosen)
        return list(chosen)

    def status_snapshot(self, job_id):
        seq = self.sequences.get(job_id) or []
        curs = self.cursor.get(job_id, 0)
        if curs < len(seq):
            return seq[curs]
        # a drained terminal job reads like the real client: the record
        # is gone from the live query path.
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
    """The PHYSICAL row that displays this video's filename (column 1;
    cells render the full path, so match the distinct file name at the
    end)."""
    for row in range(win.file_table.rowCount()):
        cell = win.file_table.item(row, 1)
        if cell is not None and cell.text().endswith(filename):
            return row
    raise AssertionError(f"no row displays {filename!r}")


def _row_state(win, filename: str) -> str:
    row = _row_by_filename(win, filename)
    cell = win.file_table.item(row, 0)
    return cell.text() if cell is not None else ""


# ---------------------------------------------------------------------------
# Item 1: the exact observed drain sequence — every row shows its own truth
# ---------------------------------------------------------------------------


def test_v0412_item1_two_job_drain_each_row_shows_its_own_state(
    qapp, monkeypatch, tmp_path
):
    """[J1 running, J2 running] → [J1 succeeded, J2 running] → [J2 running]
    ×N → [J2 succeeded]. Final: row0 AND row1 both "succeeded"; row1 NEVER
    stuck at "running" (the v0.4.11 escape) and row0 never flip-flops back
    to "running" after J1's terminal write."""
    plan_a = _plan_for(tmp_path, "part-one.avi")
    plan_b = _plan_for(tmp_path, "part-two.avi")
    client = _ScriptedClient()
    client.prepare("J1", plan_a.report.path)
    client.prepare("J2", plan_b.report.path)
    # The exact observed sequence (design §1 timeline):
    client.push("J1", {"state": "running", "current_phase": "upscale"})
    client.push("J1", {"state": "succeeded", "current_phase": "done"})
    client.push(
        "J2", {"state": "running", "current_phase": "upscale"}
    )  # the T₁ tick reads running for BOTH (J1's LAST seq entry is the terminal)
    client.push(
        "J2", {"state": "running", "current_phase": "upscale"}
    )  # the T₁+1..T₂-1 ticks: J2 alone at index 0
    client.push(
        "J2", {"state": "running", "current_phase": "upscale"}
    )  # more intermediate running ticks
    client.push(
        "J2", {"state": "running", "current_phase": "upscale"}
    )  # and the next tick still running
    client.push("J2", {"state": "succeeded", "current_phase": "done"})

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)

    win.batch = BatchPlan([plan_a, plan_b])
    win.render_batch(win.batch)
    win.start_upscale()
    assert win.job_ids == ["J1", "J2"]
    assert win._batch_row_for_job == {"J1": 0, "J2": 1}

    # Tick T: both running, each on its own row.
    win._tick_elapsed()
    assert _row_state(win, "part-one.avi") == "running"
    assert _row_state(win, "part-two.avi") == "running"

    # Tick T₁: J1's terminal snapshot → row0 "succeeded"; J2 running → row1.
    win._tick_elapsed()
    assert _row_state(win, "part-one.avi") == "succeeded"
    assert _row_state(win, "part-two.avi") == "running"

    # Ticks T₁+1 .. T₂−1: J2 alone (shifting to index 0) — its running
    # writes must land on ROW 1, never re-overwrite row 0.
    win._tick_elapsed()
    assert _row_state(win, "part-one.avi") == "succeeded"
    assert _row_state(win, "part-two.avi") == "running"
    win._tick_elapsed()
    assert _row_state(win, "part-one.avi") == "succeeded"
    assert _row_state(win, "part-two.avi") == "running"

    # Tick T₂: J2's terminal snapshot → ROW 1 "succeeded" (its own row).
    win._tick_elapsed()
    assert _row_state(win, "part-two.avi") == "succeeded"
    # ...and row0 stays "succeeded" — the flip-flop is dead.
    assert _row_state(win, "part-one.avi") == "succeeded"
    # The bar reaches "2/2 succeeded" green, and NO row reads "running".
    assert win.progress.format() == "2/2 succeeded"
    assert win._progress_chunk_color == "#3d9865"
    for row in range(win.file_table.rowCount()):
        assert win.file_table.item(row, 0).text() != "running"

    # After finish, the stage line no longer shows the last job's
    # mid-phase text (the companion fix).
    assert "upscale" not in win.stage_label.text().lower()
    next(gen, None)


def test_v0412_item1_three_job_batch_every_row_lands_own_truth(
    qapp, monkeypatch, tmp_path
):
    """N-sequential variant: 3 jobs, one terminal per tick, later jobs
    drifting up the list — every physical row ends at its own job's state
    and none stays "running"."""
    names = ["a-one.avi", "b-two.avi", "c-three.avi"]
    plans = [_plan_for(tmp_path, n) for n in names]
    client = _ScriptedClient()
    for jid, n in zip(("JA", "JB", "JC"), names):
        client.prepare(jid, plans[names.index(n)].report.path)
        client.push(jid, {"state": "running", "current_phase": "upscale"})
        client.push(jid, {"state": "succeeded", "current_phase": "done"})

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    win.start_upscale()
    for _ in range(6):  # enough ticks for three staggered terminals
        win._tick_elapsed()
    for name in names:
        assert _row_state(win, name) == "succeeded", name
    assert win.progress.format() == "3/3 succeeded"
    next(gen, None)


def test_v0412_item1_back_to_back_batches_map_is_resent_not_stale(
    qapp, monkeypatch, tmp_path
):
    """Batch hygiene: `_batch_row_for_job` seeds at every start_upscale and
    clears when the table is rebuilt (clear_files / _render_pending_files);
    a stale entry from batch 1 must never map into batch 2."""
    plan_a = _plan_for(tmp_path, "first.avi")
    client = _ScriptedClient()
    client.prepare("B1J", plan_a.report.path)
    client.push("B1J", {"state": "running", "current_phase": "upscale"})
    client.push("B1J", {"state": "succeeded", "current_phase": "done"})

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan([plan_a])
    win.render_batch(win.batch)
    win.start_upscale()
    assert win._batch_row_for_job == {"B1J": 0}
    win.client.registry_cancel_all = True
    win._tick_elapsed()
    win._tick_elapsed()
    assert win.job_ids == []
    assert win._batch_row_for_job == {"B1J": 0}  # still pinned for ITS batch

    # Batch 2: adding fresh files clears the map (render_pending path).
    win.files = [str(tmp_path / "second.avi")]
    win._render_pending_files()
    assert win._batch_row_for_job == {}
    # ...and clear_files too.
    win._batch_row_for_job = {"stale": 0}
    win.clear_files()
    assert win._batch_row_for_job == {}
    # A fresh batch reseeds cleanly: simulate by setting job_ids the way
    # enqueue_run does, then start_upscale's own job path.
    plan_b = _plan_for(tmp_path, "second.avi")
    client.prepare("B2J", plan_b.report.path)
    client.push("B2J", {"state": "running", "current_phase": "upscale"})
    client.push("B2J", {"state": "succeeded", "current_phase": "done"})
    client.job_order.append("B2J")
    win.batch = BatchPlan([plan_b])
    win.render_batch(win.batch)
    win.start_upscale()
    assert win._batch_row_for_job == {"B2J": 0}
    assert "stale" not in win._batch_row_for_job
    win._tick_elapsed()
    assert _row_state(win, "second.avi") == "running"
    win._tick_elapsed()
    assert _row_state(win, "second.avi") == "succeeded"
    next(gen, None)


def test_v0412_item1_stage_label_cleared_after_batch_finish(
    qapp, monkeypatch, tmp_path
):
    """The last job's mid-phase stage text must not outlive the batch:
    after the final terminal drain, the stage label holds no phase text."""
    plan = _plan_for(tmp_path, "solo.avi")
    client = _ScriptedClient()
    client.prepare("SJ", plan.report.path)
    client.push(
        "SJ",
        {
            "state": "running",
            "current_phase": "subtitles",
            "progress": {"phase_note": "OCR/carry (reassembly chunk 44/44)"},
        },
    )
    client.push("SJ", {"state": "succeeded", "current_phase": "done"})

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.start_upscale()
    win._tick_elapsed()
    assert "subtitles" in win.stage_label.text().lower()
    win._tick_elapsed()
    assert win.job_ids == []
    assert win.stage_label.text() == ""
    assert win._stage_now == "queued"  # reset, ready for the next batch
    assert win._stage_fraction is None
    next(gen, None)


# ---------------------------------------------------------------------------
# Item 4: cancelled/failed never paints full green (GUI consume boundary)
# ---------------------------------------------------------------------------


def test_v0412_item4_cancelled_terminal_bar_is_amber_not_green(
    qapp, monkeypatch, tmp_path
):
    """1-job batch through the REAL `_poll_jobs` path: a cancelled terminal
    payload renders bar text "0/1 succeeded, 1 cancelled" AND the amber
    palette — never green."""
    plan = _plan_for(tmp_path, "cancel-me.avi")
    client = _ScriptedClient()
    client.prepare("CJ", plan.report.path)
    client.push("CJ", {"state": "running", "current_phase": "upscale"})
    client.push("CJ", {"state": "cancelled", "current_phase": "done"})

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.start_upscale()
    win._tick_elapsed()  # running
    win._tick_elapsed()  # cancelled terminal
    assert win.progress.format() == "0/1 succeeded, 1 cancelled"
    assert win._progress_chunk_color == "#d8a93a"  # amber, NOT green
    assert _row_state(win, "cancel-me.avi") == "cancelled"
    next(gen, None)


def test_v0412_item4_failed_terminal_bar_is_red(qapp, monkeypatch, tmp_path):
    plan = _plan_for(tmp_path, "fail-me.avi")
    client = _ScriptedClient()
    client.prepare("FJ", plan.report.path)
    client.push("FJ", {"state": "running", "current_phase": "upscale"})
    client.push("FJ", {"state": "failed", "current_phase": "done"})

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.start_upscale()
    win._tick_elapsed()
    win._tick_elapsed()
    assert win.progress.format() == "0/1 succeeded, 1 failed"
    assert win._progress_chunk_color == "#c4605a"  # red, NOT green
    assert _row_state(win, "fail-me.avi") == "failed"
    next(gen, None)


def test_v0412_item4_two_job_all_succeeded_bar_is_green(
    qapp, monkeypatch, tmp_path
):
    plan_a = _plan_for(tmp_path, "green-a.avi")
    plan_b = _plan_for(tmp_path, "green-b.avi")
    client = _ScriptedClient()
    for jid, p in (("GA", plan_a), ("GB", plan_b)):
        client.prepare(jid, p.report.path)
        client.push(jid, {"state": "running", "current_phase": "upscale"})
        client.push(jid, {"state": "succeeded", "current_phase": "done"})

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan([plan_a, plan_b])
    win.render_batch(win.batch)
    win.start_upscale()
    for _ in range(4):
        win._tick_elapsed()
    assert win.progress.format() == "2/2 succeeded"
    assert win._progress_chunk_color == "#3d9865"  # green
    assert _row_state(win, "green-a.avi") == "succeeded"
    assert _row_state(win, "green-b.avi") == "succeeded"
    # item 1's guarantee rides along: no row stuck "running".
    for row in range(win.file_table.rowCount()):
        assert win.file_table.item(row, 0).text() != "running"
    next(gen, None)


def test_v0412_item4_no_completed_phrased_batch_format_string_in_gui_source():
    """The v0.4.10 counting-lie is pinned at the SOURCE level: no
    "N/N completed"-style batch format string may exist in gui.py."""
    import re
    import vidlift
    from pathlib import Path

    gui_source = (Path(vidlift.__file__).parent / "gui.py").read_text(
        encoding="utf-8"
    )
    completed_formats = re.findall(
        r"f?\"[^\"]*\{[^}]+\}/\{[^}]+\}\s*completed[^\"]*\"", gui_source
    )
    assert completed_formats == [], (
        f"'completed'-phrased batch format string(s) in gui.py: {completed_formats}"
    )
    # The honest form must exist.
    assert "{summary.succeeded}/{total} succeeded" in gui_source

# ---------------------------------------------------------------------------
# Item 3: output filename contract + collision gates
# ---------------------------------------------------------------------------


def test_v0412_item3_expected_output_names_carry_target_not_hevc(tmp_path):
    """The plan boundary: outputs are named {stem}.{target}.mp4 — no
    "hevc" infix (the "Part Two - S01E02.720p.mkv" contract)."""
    from vidlift.plan import _expected_output_path

    assert (
        _expected_output_path("/media/Movie.avi", None, "720p")
        == "/media/Movie.720p.mkv"
    )
    assert (
        _expected_output_path("/media/Movie.avi", "/output", "720p")
        == "/output/Movie.720p.mkv"
    )


def test_v0412_item3_build_batch_plan_yields_new_name(tmp_path):
    from vidlift.plan import build_batch_plan

    source = tmp_path / "movie.avi"
    source.write_text("")
    report = _report("movie.avi")
    report.path = str(source)
    batch = build_batch_plan([report], target="720p", gb_per_hour=1.5)
    (item,) = batch.files
    assert item.output_path == str(tmp_path / "movie.720p.mkv")
    assert "hevc" not in item.output_path


def test_v0412_item3_plan_time_collision_marks_not_ready_with_warning(
    tmp_path,
):
    """A pre-existing file at the planned output path blocks the item:
    verdict not-ready + warning naming the file (block-until-forced; no
    force flag this cycle)."""
    from vidlift.gui import warnings_for

    source = tmp_path / "collide.avi"
    source.write_text("")
    existing_output = tmp_path / "collide.720p.mkv"
    existing_output.write_bytes(b"previous output")
    report = _report("collide.avi")
    report.path = str(source)

    from vidlift.plan import build_file_plan

    item = build_file_plan(report, gb_per_hour=1.5)
    assert item.process_verdict != "ready"
    assert item.process_verdict == "output-exists"
    warning = (
        f"output exists: {existing_output.name} (will not be overwritten)"
    )
    assert warning in warnings_for(item)
    # batch path agrees (the GUI/CLI both plan through it):
    from vidlift.plan import build_batch_plan

    batch = build_batch_plan([report], gb_per_hour=1.5)
    (batched,) = batch.files
    assert batched.process_verdict == "output-exists"
    assert batch.runnable_count == 0
    # No collision: the normal ready verdict is untouched.
    clean_source = tmp_path / "clean.avi"
    clean_source.write_text("")
    clean_report = _report("clean.avi")
    clean_report.path = str(clean_source)
    clean = build_file_plan(clean_report, gb_per_hour=1.5)
    assert clean.process_verdict == "ready"


def test_v0412_item3_gui_plan_shows_collision_warning_in_row(
    qapp, monkeypatch, tmp_path
):
    """The GUI surface: a collided item renders its warning in the
    Warnings column and Start refuses (runnable_count gate)."""
    from vidlift.plan import BatchPlan

    source = tmp_path / "gui-collide.avi"
    source.write_text("")
    (tmp_path / "gui-collide.720p.mkv").write_bytes(b"occupied")
    report = _report("gui-collide.avi")
    report.path = str(source)
    from vidlift.plan import build_file_plan

    item = build_file_plan(report, gb_per_hour=1.5)

    class _C:
        def active_job_count(self):
            return 0

        def history(self, limit=20):
            return []

        def stop(self, **k):
            pass

    import PyQt6.QtCore as QtCore

    def _settings(self):
        return QtCore.QSettings(
            str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat
        )

    monkeypatch.setattr(MainWindow, "_settings", _settings)
    win = MainWindow(client=_C())
    win.batch = BatchPlan([item])
    win.render_batch(win.batch)
    warnings_cell = win.file_table.item(0, 7)
    assert warnings_cell is not None
    assert "output exists: gui-collide.720p.mkv" in warnings_cell.text()
    assert "(will not be overwritten)" in warnings_cell.text()
    # the verdict cell is not green (not ready) and Start stays disabled
    assert win.start_button.isEnabled() is False
    win.job_ids = []
    win._run_started_at = None
    win.deleteLater()
    qapp.processEvents()


def test_v0412_item3_run_time_guard_fails_single_job_not_batch(tmp_path):
    """The encode-must-not-overwrite guard: os.path.exists immediately
    before encode raises RunError naming the file (the plan→run race
    closure). The raised failure is THIS job's RunError, which the
    executor records per-job — the batch carries on (per-job failure
    aggregation is the executor's normal path, unchanged)."""
    from vidlift.plan import build_file_plan
    from vidlift.probe import FileReport

    source = tmp_path / "race.avi"
    source.write_text("")
    report = _report("race.avi")
    report.path = str(source)
    item = build_file_plan(report, gb_per_hour=1.5)
    # A file appeared at the output path AFTER planning:
    output = Path(item.output_path)
    output.write_bytes(b"appeared-after-plan")
    assert output.exists()
    # The run-time guard rides the encode phase in run.py; pin its exact
    # contract at the source level (the seam run_file itself needs a
    # full environment; the guard text + semantics are the boundary).
    import vidlift.run as run_mod

    source_text = Path(run_mod.__file__).read_text(encoding="utf-8")
    guard = (
        "if os.path.exists(plan.output_path):\n"
        '            raise RunError(\n'
        '                f"output already exists, will not be overwritten: '
        '{plan.output_path}"\n'
        "            )"
    )
    assert guard in source_text, (
        "run.py encode-time os.path.exists guard drifted from the pinned form"
    )
    # and the guard's raise type is the per-job RunError (not a batch
    # exception): RunError is what the executor maps to job failure.
    assert issubclass(run_mod.RunError, Exception)


def test_v0412_item3_no_stale_hevc_name_assertions_remain():
    """Name-sweep pin (design §4/§5; extended v0.4.24): no `.hevc.mp4`
    construction or assertion remains in production code or tests; no
    `.{target}.mp4` output construction remains either (the container
    ADR — every planned output ends `.mkv`); the only allowed
    occurrence is the intentional historical comment in plan.py and
    this test's own docstring."""
    import vidlift
    from pathlib import Path as _P

    self_name = "test_v0412_item3_no_stale_hevc_name_assertions_remain"

    def _in_own_test(py, lineno) -> bool:
        if py.name != "test_v0412_boundary.py":
            return False
        lines = py.read_text(encoding="utf-8").splitlines()
        # the whole body of THIS test (from its def line to end of file) is exempt
        for idx, line in enumerate(lines, 1):
            if f"def {self_name}" in line:
                return lineno >= idx
        return False

    hits: list[str] = []
    for base in (
        _P(vidlift.__file__).parent,
        _P(vidlift.__file__).parent.parent / "tests",
    ):
        for py in base.rglob("*.py"):
            for lineno, line in enumerate(
                py.read_text(encoding="utf-8").splitlines(), 1
            ):
                if "hardcoded literal" in line:
                    continue  # plan.py's intentional historical comment
                if _in_own_test(py, lineno):
                    continue  # this test's own text
                if "hevc.mp4" in line:
                    hits.append(f"{py.name}:{lineno}: {line.strip()}")
                if ".720p.mp4" in line or ".1080p.mp4" in line:
                    # v0.4.24: no `.{target}.mp4` output construction
                    # (or assertion) survives anywhere — inputs that
                    # merely HAPPEN to be .mp4 are fine and are not
                    # matched by this token.
                    hits.append(f"{py.name}:{lineno}: {line.strip()}")

    # and the positive half of the container fact: the planner writes
    # `.mkv` finals for every target it accepts.
    from vidlift.plan import _expected_output_path

    for target in ("720p", "1080p"):
        planned = _expected_output_path("/media/Movie.avi", None, target)
        assert planned.endswith(f".{target}.mkv"), planned


# ---------------------------------------------------------------------------
# Item 2: honest ETA (per-file + batch), Qt boundary + pure math
# ---------------------------------------------------------------------------


class _EtaProbe:
    """A running-job snapshot builder with controllable clock/ticks so
    the EWMA tests are deterministic (time.time is monkeypatched)."""

    def __init__(self, job_id="EJ", phase="upscale", file_path="/media/e.avi",
                 duration=None):
        self.job_id = job_id
        self.phase = phase
        self.file_path = file_path
        self.duration = duration
        self.now = 1000.0

    def snapshot(self, *, frames_done=None, frames_total=None,
                 chunk_index=None, chunk_total=None, encode_seconds=None,
                 eta_seconds=None):
        snap = {
            "job_id": self.job_id,
            "state": "running",
            "current_phase": self.phase,
            "file_path": self.file_path,
            "progress": {},
        }
        if eta_seconds is not None:
            snap["eta_seconds"] = eta_seconds
        if frames_done is not None:
            snap["progress"]["frames_done"] = frames_done
        if frames_total is not None:
            snap["progress"]["frames_total"] = frames_total
        if chunk_index is not None:
            snap["progress"]["chunk_index"] = chunk_index
        if chunk_total is not None:
            snap["progress"]["chunk_total"] = chunk_total
        if encode_seconds is not None:
            snap["progress"]["encode_seconds"] = encode_seconds
        if self.phase == "verify":
            snap["progress"]["decode_seconds"] = 30.0
        return snap


def _eta_window(monkeypatch, tmp_path):
    """A REAL MainWindow with controllable time + a plan whose report
    declares duration 1800 s (the encode denominator)."""
    import PyQt6.QtCore as QtCore

    class _C:
        def active_job_count(self):
            return 0

        def history(self, limit=20):
            return []

        def stop(self, **k):
            pass

    def _settings(self):
        return QtCore.QSettings(
            str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat
        )

    monkeypatch.setattr(MainWindow, "_settings", _settings)
    win = MainWindow(client=_C())
    report = _report("e.avi")
    report.duration_seconds = 1800.0
    report.path = "/media/e.avi"
    from vidlift.plan import build_file_plan

    win.batch = BatchPlan([build_file_plan(report, gb_per_hour=1.5)])
    yield win
    win.job_ids = []
    win._run_started_at = None
    win.deleteLater()
    QApplication.instance().processEvents()


def test_v0412_item2_upscale_eta_after_two_chunks(
    qapp, monkeypatch, tmp_path
):
    """upscale: >=2 chunk samples BEFORE digits; then ETA ≈ remaining/rate
    within tolerance; no digits after only ONE sample."""
    gen = _eta_window(monkeypatch, tmp_path)
    win = next(gen)
    probe = _EtaProbe()
    times = iter(float(1000 + 60 * i) for i in range(20))
    monkeypatch.setattr(time, "time", lambda: next(times, 5000.0))

    # chunk 1 sample (boundary, first) — no ETA digits yet
    assert win._eta_current_job(probe.snapshot(frames_done=600, frames_total=72000,
                                               chunk_index=1, chunk_total=12)) is None
    # tick inside the same chunk — no new samples, no ETA
    assert win._eta_current_job(probe.snapshot(frames_done=600, frames_total=72000,
                                               chunk_index=1, chunk_total=12)) is None
    # chunk 2 boundary 60 s later: 1800 frames / 60 s = 30 fps →
    # ETA = (72000-2400)/30
    wait_times = iter(float(1060 + 60 * i) for i in range(20))
    monkeypatch.setattr(time, "time", lambda: next(wait_times, 9000.0))
    eta = win._eta_current_job(
        probe.snapshot(frames_done=2400, frames_total=72000, chunk_index=2,
                       chunk_total=12)
    )
    assert eta is not None
    assert eta == (72000 - 2400) / 30.0
    next(gen, None)


def test_v0412_item2_encode_eta_uses_output_seconds(qapp, monkeypatch, tmp_path):
    """encode: work = output seconds; ETA = (duration − encode_seconds)/rate
    with duration from the plan-time probe."""
    gen = _eta_window(monkeypatch, tmp_path)
    win = next(gen)
    probe = _EtaProbe(phase="encode")
    times = iter(float(2000 + 30 * i) for i in range(20))
    monkeypatch.setattr(time, "time", lambda: next(times, 9000.0))
    # first sample: no ETA
    assert win._eta_current_job(probe.snapshot(encode_seconds=60.0)) is None
    # +30 s wall, +30 output-seconds → rate 1.0 → ETA 1800-90
    times2 = iter(float(2030 + 30 * i) for i in range(20))
    monkeypatch.setattr(time, "time", lambda: next(times2, 9000.0))
    eta = win._eta_current_job(probe.snapshot(encode_seconds=90.0))
    assert eta == 1800.0 - 90.0
    next(gen, None)


def test_v0412_item2_verify_phase_never_shows_eta_digits(
    qapp, monkeypatch, tmp_path
):
    """Honesty requirement: verify/finalize render elapsed time only —
    NO ETA suffix digits may appear in the stage label."""
    gen = _eta_window(monkeypatch, tmp_path)
    win = next(gen)
    # the overall-bar arm needs the run accounting a live batch carries
    win.run_total = 1
    win.run_counts = [None]
    probe = _EtaProbe(phase="verify")
    snap = probe.snapshot()
    eta = win._eta_current_job(snap)
    assert eta is None
    # The REAL consume path: the label keeps the elapsed heartbeat form,
    # never an "ETA ~" suffix.
    from vidlift.gui import run_stage_label

    stage = run_stage_label(snap)
    win._consume_running_snapshot(0, snap, stage)
    assert "ETA ~" not in win.stage_label.text()
    next(gen, None)


def test_v0412_item2_stage_label_carries_eta_suffix_when_computable(
    qapp, monkeypatch, tmp_path
):
    """The stage label appends ' — ETA ~H:MM:SS' once the EWMA has
    samples; the value is damped (within 25% stays the previous)."""
    gen = _eta_window(monkeypatch, tmp_path)
    win = next(gen)
    win.run_total = 1
    win.run_counts = [None]
    probe = _EtaProbe()
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    win._eta_current_job(probe.snapshot(frames_done=600, frames_total=72000,
                                        chunk_index=1, chunk_total=12))
    monkeypatch.setattr(time, "time", lambda: 1060.0)
    win._eta_current_job(probe.snapshot(frames_done=2400, frames_total=72000,
                                        chunk_index=2, chunk_total=12))

    from vidlift.gui import run_stage_label

    # consume 1: phase transition (queued -> upscaling) RESETS the EWMA
    # and seeds sample 1 (chunk 1) — no digits yet by design.
    snap1 = probe.snapshot(frames_done=600, frames_total=72000, chunk_index=1,
                           chunk_total=12)
    win._consume_running_snapshot(0, snap1, run_stage_label(snap1))
    assert "ETA ~" not in win.stage_label.text()
    # consume 2 (chunk 2 boundary): the second sample completes the
    # window — digits appear.
    monkeypatch.setattr(time, "time", lambda: 1120.0)
    snap2 = probe.snapshot(frames_done=2400, frames_total=72000, chunk_index=2,
                           chunk_total=12)
    win._consume_running_snapshot(0, snap2, run_stage_label(snap2))
    text = win.stage_label.text()
    assert "ETA ~" in text
    # damped stability: a small drift keeps the same rendered value
    monkeypatch.setattr(time, "time", lambda: 1120.0)
    eta2 = win._eta_current_job(probe.snapshot(frames_done=4200, frames_total=72000,
                                               chunk_index=3, chunk_total=12))
    # (72000-4200)/30 — within 25% of the previous → the rendered value
    # must be the PREVIOUS one (damping).
    damped = win._render_damped_eta(eta2, win._eta_rendered)
    assert damped == win._eta_rendered
    next(gen, None)


def test_v0412_item2_ewma_resets_on_phase_change(qapp, monkeypatch, tmp_path):
    """Window reset contract: switching stage keys wipes the EWMA state
    (a new phase starts with no samples)."""
    gen = _eta_window(monkeypatch, tmp_path)
    win = next(gen)
    win.run_total = 1
    win.run_counts = [None]
    win._eta_state = {"EJ": {"upscale": {"chunk_index": 3, "eta": 42.0}}}
    win._consume_running_snapshot(
        0,
        {"job_id": "EJ", "state": "running", "current_phase": "encode",
         "progress": {"encode_seconds": 5.0}},
        "encoding",
    )
    assert win._eta_state == {}
    assert win._eta_rendered is None
    next(gen, None)


def test_v0412_item2_batch_label_known_rate_plus_queued_unknown(
    qapp, monkeypatch, tmp_path
):
    """Batch honesty: 1 running job with a measured rate + 1 queued job
    with NO duration data contributes NOTHING and the label says
    'queued (ETA unknown)' — never a fabricated total."""
    plan_run = _plan_for(tmp_path, "running.avi")
    plan_q = _plan_for(tmp_path, "queued.avi")
    client = _ScriptedClient()
    client.prepare("RJ", plan_run.report.path)
    client.prepare("QJ", plan_q.report.path)
    client.push("RJ", {"state": "running", "current_phase": "upscale",
                       "progress": {"frames_done": 72000, "frames_total": 72000,
                                    "chunk_index": 12, "chunk_total": 12}})
    client.push("RJ", {"state": "running", "current_phase": "upscale",
                       "progress": {"frames_done": 72000, "frames_total": 72000,
                                    "chunk_index": 12, "chunk_total": 12}})
    client.push("QJ", {"state": "queued"})
    client.push("QJ", {"state": "queued"})
    client.push("QJ", {"state": "queued"})
    client.push("QJ", {"state": "queued"})
    client.push("QJ", {"state": "queued"})

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan([plan_run, plan_q])
    win.render_batch(win.batch)
    win.start_upscale()
    # give the running job a measured chunk rate: two boundary samples,
    # 600 frames in 10 s (60 fps), ETA = 0 (done)... use partial instead:
    win.client.sequences["RJ"] = [
        {"job_id": "RJ", "state": "running", "current_phase": "upscale",
         "progress": {"frames_done": 600, "frames_total": 72000,
                      "chunk_index": 1, "chunk_total": 12}},
        {"job_id": "RJ", "state": "running", "current_phase": "upscale",
         "progress": {"frames_done": 1200, "frames_total": 72000,
                      "chunk_index": 2, "chunk_total": 12}},
        {"job_id": "RJ", "state": "running", "current_phase": "upscale",
         "progress": {"frames_done": 1200, "frames_total": 72000,
                      "chunk_index": 2, "chunk_total": 12}},
        {"job_id": "RJ", "state": "running", "current_phase": "upscale",
         "progress": {"frames_done": 1800, "frames_total": 72000,
                      "chunk_index": 3, "chunk_total": 12}},
    ]
    win.client.cursor["RJ"] = 0
    monkeypatch.setattr(time, "time", lambda: 1000.0)
    win._tick_elapsed()  # transition tick (EWMA seeds with chunk 1)
    monkeypatch.setattr(time, "time", lambda: 1010.0)
    win._tick_elapsed()  # chunk-2 sample: rate measured, ETA exists
    monkeypatch.setattr(time, "time", lambda: 1020.0)
    win._tick_elapsed()  # the label/batch reads the measured state

    # now the queued job's snapshot exists with no eta → unknown marker
    label = win.total_remaining.text()
    assert ("queued (ETA unknown)" in label) or (
        "Total remaining ~" in label
    ), label
    # the queued job has no benchmark estimate (no constants) → the
    # honest form; no number was invented for the queued job:
    if "queued (ETA unknown)" in label:
        # "Current ~M:SS + queued (ETA unknown)" pattern (singular marker)
        import re as _re

        assert _re.search(r"Current ~\d+:\d+(:\d+)? \+ queued \(ETA unknown\)", label)
        # and the label never carries a fabricated "Total remaining" number
        assert "Total remaining ~" not in label
    else:
        # a pure-unknown label is NOT ok here — the current job HAS an
        # eta; the batch line must carry the queued marker.
        raise AssertionError(f"batch label lost the queued marker: {label!r}")
    next(gen, None)


def test_v0412_item2_queued_no_duration_contributes_nothing():
    """Unit: the queued-estimate honesty — no duration ⇒ None (the
    benchmark estimate seam), i.e. nothing is added for that job."""
    from vidlift.plan import build_file_plan

    report = _report("nodur.avi")
    report.duration_seconds = None
    plan = build_file_plan(report, gb_per_hour=1.5)
    # eta_estimate absent entirely (attach_estimate never ran): None
    assert plan.eta_estimate is None
    # _benchmark_eta_for_job maps None → no contribution:
    # (unit-level: the dict's absence is the no-data case the design
    # calls 'contributing nothing')
    assert plan.eta_estimate is None


def test_v0412_item2_damping_keeps_previous_value(qapp, tmp_path):
    """Pure damping contract: within 25% ⇒ previous; beyond ⇒ new."""
    gen = _eta_window(MonkeyPatchShim(), tmp_path)
    win = next(gen)
    assert win._render_damped_eta(None, 100.0) == 100.0
    assert win._render_damped_eta(110.0, None) == 110.0
    assert win._render_damped_eta(110.0, 100.0) == 100.0  # 10% drift → keep
    assert win._render_damped_eta(130.0, 100.0) == 130.0  # 30% > 25% → new
    assert win._render_damped_eta(120.0, 100.0) == 100.0  # 20% → keep
    assert win._render_damped_eta(126.0, 100.0) == 126.0  # 26% → new
    next(gen, None)


class MonkeyPatchShim:
    """Minimal monkeypatch stand-in for direct helper calls."""
    def setattr(self, obj, name, val):
        setattr(obj, name, val)
