"""v0.4.14 Mode B GUI — the "Add Subtitles… (no upscale)" surface.

Red-first contracts against pre-feature main (button/flow/slate do not
exist):

1. Button + tooltip: the control exists in the controls grid with the
   honest "without re-encoding the video" tooltip.
2. Click -> QFileDialog.getOpenFileNames (ANY resolution, same filter
   family as Add Files — no resolution gate) -> EVERY picked file
   enqueued as a ``subs`` job through client.enqueue_subs (the shared
   registry/executor surface) in pick order.
3. Real consume path: rows render their OWN states (queued -> running
   -> terminal) through the scripted-client harness used by the
   v0.4.12/v0.4.13 boundary suites; row identity rides
   _batch_row_for_job (submission order), never a shifting index.
4. Cancel-mid-subs: Cancel Current through the REAL cancel path lands
   ``cancelled`` (never failed) via the claim-time CancelFlag; Cancel
   Batch marks the queued remainder (the registry's state machine is
   job-kind-blind).
5. Collision gate: a picked file whose {stem}.subs.mp4 output already
   exists shows the blocked/warning row BEFORE submission — never
   overwritten, never silently dropped (the job-level guard stays the
   run-side gate; this is the visible pre-flight).
6. ETA honesty: the running subs snapshot consumes with the segment
   count in the stage label and NO ETA digits (transcribe/subs-mux
   phases are excluded from _eta_current_job — no fabricated numbers).
7. Back-to-batch hygiene: a subs batch followed by an upscale batch
   (and a MIXED sequence: subs slate then another subs slate with
   different row counts) must behave cleanly — _batch_row_for_job
   reseeded per submission, no aliasing into the previous batch's
   rows/slots (the v0.4.12 boundary rule extended to subs batches).

Conventions: QT_QPA_PLATFORM=offscreen, QSettings isolated to tmp_path,
QMessageBox/dialog monkeypatched, scripted `_ScriptedClient` (the
v0.4.13 boundary harness) and the REAL MainWindow consume pipeline.
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


# ---------------------------------------------------------------- harness
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
    """The GUI's bridge seam with a scripted snapshot sequence per job id
    (the v0.4.13 boundary harness, plus the subs enqueue surface)."""

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
        self.subs_kinds: dict[str, str] = {}
        self.last_subs_replace_source: bool = False  # v0.4.17

    # -- scripting helpers --
    def make_job(self, file_path, script: list[dict], kind="run") -> str:
        self.counter += 1
        job_id = f"VJ{self.counter}"
        self.job_order.append(job_id)
        self.jobs[job_id] = {"file_path": file_path}
        self.sequences[job_id] = [{"job_id": job_id, **snap} for snap in script]
        self.cursor[job_id] = 0
        self.subs_kinds[job_id] = kind
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
            curs = self.cursor[job_id]
            current = seq[min(curs, len(seq) - 1)] if seq else None
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
        self.last_enqueued_kinds = ["run"] * len(chosen)
        return list(chosen)

    def enqueue_subs(self, paths, *, replace_source=False):
        """The Mode B surface: kind-checked ids in pick order.

        v0.4.17: the GUI passes the replace-source checkbox state as a
        keyword (the spec's item 11 call shape) — the double accepts and
        records it (last_subs_replace_source) so the File B tests can
        assert ON/OFF thread through; a defaulted kwarg keeps every
        scripted caller (incl. the v0.4.15.1 guard suite) green."""
        for path in paths:
            if not Path(path).is_file():
                raise FileNotFoundError(f"no such file on this machine: {path}")
        self.last_subs_replace_source = bool(replace_source)
        self.armed = True
        fresh = [jid for jid in self.job_order if jid not in self.submitted]
        chosen = fresh[: len(paths)]
        if len(chosen) != len(paths):
            raise AssertionError("scripted subs ids exhausted")
        # v0.4.15.1: a re-pick of an already-scripted file during a live
        # batch re-hands THAT file's id (the registry would hold a live
        # record for it; a scripted double-hand stays identity-stable so
        # the window's union dedupes instead of minting a fake twin).
        already = [
            jid for jid in self.submitted
            if self.jobs.get(jid, {}).get("file_path") in paths
        ]
        self.submitted.extend(jid for jid in chosen if jid not in self.submitted)
        self.last_enqueued_kinds = [self.subs_kinds.get(jid) for jid in chosen]
        return list(dict.fromkeys(already + chosen))

    def status_snapshot(self, job_id):
        seq = self.sequences.get(job_id) or []
        curs = self.cursor.get(job_id, 0)
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
    # The active-batch guards pop an information box (never a question);
    # a blocked click must return cleanly, so modal informations are
    # auto-ok for this harness.
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
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


# ------------------------------------------------------------------ tests
def test_subs_button_exists_with_honest_tooltip(qapp, monkeypatch, tmp_path):
    """Pre-flight: the Mode B button lives in the grid with a tooltip
    that says subtitles are added WITHOUT re-encoding the video."""
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    assert win.add_subs_button.text().startswith("Add Subtitles")
    tooltip = win.add_subs_button.toolTip().lower()
    assert "without re-encoding" in tooltip
    assert win.add_subs_button.isEnabled()
    next(gen, None)


def test_subs_button_enqueues_picked_files_as_subs_jobs(
    qapp, monkeypatch, tmp_path
):
    """PRIMARY (§2 of the task): click -> multi-select file dialog ->
    one 'subs' job per picked file through client.enqueue_subs."""
    names = ["alpha-ep.mkv", "bravo-ep.mp4"]
    picked = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x")
        picked.append(str(path))
    client = _ScriptedClient()
    client.make_job(picked[0], [{"state": "queued"}], kind="subs")
    client.make_job(picked[1], [{"state": "queued"}], kind="subs")

    import vidlift.gui as gui_mod

    recorded: dict = {}

    orig_dialog = gui_mod.QFileDialog.getOpenFileNames

    def fake_dialog(*a, **k):
        recorded["parent"] = a[0] if a else None
        recorded["filter"] = k.get("filter") or (a[3] if len(a) > 3 else None)
        return picked, ""

    monkeypatch.setattr(gui_mod.QFileDialog, "getOpenFileNames", fake_dialog)

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()

    assert recorded["parent"] is win
    # same picker family as Add Files; NO resolution filter in the string
    filt = str(recorded["filter"])
    assert "Video files" in filt and "All files" in filt
    assert "640" not in filt and "resolution" not in filt.lower()

    assert win.job_ids == ["VJ1", "VJ2"]
    assert client.last_enqueued_kinds == ["subs", "subs"]
    # rows exist for BOTH picked files, in pick order, with the subs
    # verdict in the Status cell (not the upscale plan render)
    assert _row_by_filename(win, "alpha-ep.mkv") == 0
    assert _row_by_filename(win, "bravo-ep.mp4") == 1
    row0 = win.file_table.item(0, 0).text()
    assert row0 == "subs ready"
    # the slate engaged like Start's: honest bar + follow armed
    # (offscreen windows are never shown, so an explicitly-shown child
    # reports not-hidden rather than visible)
    assert not win.progress.isHidden()
    assert win.run_total == 2
    assert win._batch_row_for_job == {"VJ1": 0, "VJ2": 1}
    assert win._batch_slots == ["VJ1", "VJ2"]
    assert win.cancel_batch_button.isEnabled()
    next(gen, None)


def test_subs_rows_render_own_states_through_real_consume(
    qapp, monkeypatch, tmp_path
):
    """Rows show their OWN per-row truth through the REAL _poll_jobs
    consume path: queued -> running (with the subs stage label) ->
    terminal, per-row, never aliased across rows."""
    names = ["one-show.mkv", "two-show.mkv"]
    picked = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x")
        picked.append(str(path))
    client = _ScriptedClient()
    client.make_job(
        picked[0],
        [
            {"state": "queued"},
            {"state": "running", "current_phase": "transcribe",
             "progress": {"segments_done": 14,
                          "step": "transcribing segment 14"}},
            {"state": "running", "current_phase": "subs-mux"},
            {"state": "succeeded", "current_phase": "done"},
        ],
        kind="subs",
    )
    client.make_job(picked[1], [{"state": "queued"}], kind="subs")

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: (picked, "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    assert win.job_ids == ["VJ1", "VJ2"]

    # Tick 1: J1 queued, J2 queued.
    win._tick_elapsed()
    assert _row_state(win, "one-show.mkv") == "queued"
    assert _row_state(win, "two-show.mkv") == "queued"

    # Tick 2: J1 running mid-transcribe with the honest segment label;
    # J2 STILL queued (its own state — the aliased-row regression would
    # paint "running" on both rows).
    win._tick_elapsed()
    assert _row_state(win, "one-show.mkv") == "running"
    assert _row_state(win, "two-show.mkv") == "queued"
    assert "transcribing audio" in win.stage_label.text()
    assert "14 segments" in win.stage_label.text()

    # Tick 3: J1 in the soft-mux sub-stage.
    win._tick_elapsed()
    assert "remuxing subtitles" in win.stage_label.text()
    assert "no re-encode" in win.stage_label.text()

    # Tick 4: J1 terminal, drains; J2 queued.
    win._tick_elapsed()
    assert _row_state(win, "one-show.mkv") == "succeeded"
    assert _row_state(win, "two-show.mkv") == "queued"
    assert win.job_ids == ["VJ2"]

    next(gen, None)


def test_subs_cancel_current_mid_transcribe_lands_cancelled(
    qapp, monkeypatch, tmp_path
):
    """§4: Cancel Current through the REAL cancel path while a subs job
    is mid-transcribe -> the row lands 'cancelled' (never 'failed'),
    driven by the same claim-flag semantics the upscale path uses."""
    path = tmp_path / "mid-ep.mkv"
    path.write_bytes(b"x")
    client = _ScriptedClient()
    client.make_job(
        str(path),
        [
            {"state": "queued"},
            {"state": "running", "current_phase": "transcribe",
             "progress": {"segments_done": 3}},
            {"state": "cancelled", "current_phase": "done"},
        ],
        kind="subs",
    )

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(path)], "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    win._tick_elapsed()  # queued
    win._tick_elapsed()  # running mid-transcribe
    assert _row_state(win, "mid-ep.mkv") == "running"
    assert "transcribing audio" in win.stage_label.text()

    win.cancel_button.click()  # the real cancel path
    assert client.cancel_calls == ["VJ1"]

    win._tick_elapsed()  # the terminal lands
    assert _row_state(win, "mid-ep.mkv") == "cancelled"
    assert win.file_table.item(0, 0).foreground().color().name() == "#ffff00"
    next(gen, None)


def test_subs_cancel_batch_cancels_whole_batch(
    qapp, monkeypatch, tmp_path
):
    """Subs batch + Cancel Batch through the button: ONE call with the
    full id set; the queued remainder never shows running; the bar
    renders the amber cancelled truth (v0.4.13 machinery, kind-blind)."""
    names = ["c-one.mkv", "c-two.mkv", "c-three.mkv"]
    picked = []
    for name in names:
        path = tmp_path / name
        path.write_bytes(b"x")
        picked.append(str(path))
    client = _ScriptedClient()
    client.make_job(picked[0], [{"state": "queued"}], kind="subs")
    client.make_job(picked[1], [{"state": "queued"}], kind="subs")
    client.make_job(picked[2], [{"state": "queued"}], kind="subs")

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: (picked, "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    assert win.cancel_batch_button.isEnabled()
    win.cancel_batch_button.click()

    assert client.cancel_batch_calls == [["VJ1", "VJ2", "VJ3"]]
    for _ in range(4):
        win._tick_elapsed()
        if not win.job_ids:
            break
    assert win.job_ids == []
    assert _row_state(win, "c-one.mkv") == "cancelled"
    assert _row_state(win, "c-two.mkv") == "cancelled"
    assert _row_state(win, "c-three.mkv") == "cancelled"
    assert win.progress.format() == "0/3 succeeded, 3 cancelled"
    assert win._progress_chunk_color == "#d8a93a"
    next(gen, None)


def test_subs_collision_blocked_row_never_overwrites(
    qapp, monkeypatch, tmp_path
):
    """§4 constraint: a picked file whose {stem}.subs.mp4 already exists
    renders the blocked + visible-warning row BEFORE submission; the
    file is enqueued anyway (the run-side gate refuses, never
    overwrites) and the OLD output bytes are untouched by the GUI."""
    good = tmp_path / "fresh-ep.mkv"
    good.write_bytes(b"x")
    blocked = tmp_path / "done-ep.mkv"
    blocked.write_bytes(b"source-bytes")
    existing_out = tmp_path / "done-ep.subs.mp4"
    existing_out.write_bytes(b"already-there")

    client = _ScriptedClient()
    client.make_job(str(good), [{"state": "queued"}], kind="subs")
    client.make_job(str(blocked), [{"state": "queued"}], kind="subs")

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(good), str(blocked)], "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()

    row_g = _row_by_filename(win, "fresh-ep.mkv")
    row_b = _row_by_filename(win, "done-ep.mkv")
    assert win.file_table.item(row_g, 0).text() == "subs ready"
    assert "blocked" in win.file_table.item(row_b, 0).text()
    warning = win.file_table.item(row_b, 7).text()
    assert "output exists" in warning and "will not be overwritten" in warning
    # the untouched prior output
    assert existing_out.read_bytes() == b"already-there"
    assert win.job_ids == ["VJ1", "VJ2"]
    next(gen, None)


def test_subs_rows_output_cell_uses_subs_output_path_for_every_extension(
    qapp, monkeypatch, tmp_path
):
    """F-5 (v0.4.15.3): every row's Output cell is exactly
    subs_output_path(p) — the one regex-based derivation. The .ts case
    must produce movie.subs.mp4, NOT the old slice's 'movi.subs.mp4',
    and no row may carry a doubled dot."""
    from vidlift.subs_job import subs_output_path

    gen = _window(qapp, monkeypatch, tmp_path, _ScriptedClient())
    win = next(gen)
    paths = [
        "/m/movie.ts",
        "/m/movie.divx",
        "/m/movie.mpeg",
        "/m/movie.mp4",
    ]
    rows = win._subs_file_rows(paths)
    for row, path in zip(rows, paths):
        assert row[4] == subs_output_path(path), path
        assert ".." not in row[4]
    assert rows[0][4] == "/m/movie.subs.mp4"  # not 'movi.subs.mp4'
    assert rows[1][4] == "/m/movie.subs.mp4"  # '.divx' fully stripped
    assert rows[2][4] == "/m/movie.subs.mp4"  # '.mpeg' fully stripped
    assert rows[3][4] == "/m/movie.subs.mp4"
    next(gen, None)


def test_subs_rows_collision_preflight_fires_for_non_4char_extensions(
    qapp, monkeypatch, tmp_path
):
    """F-5 (v0.4.15.3): with movie.subs.mp4 pre-existing beside
    movie.ts, the Status cell is the blocked verdict — the pre-flight
    finally fires for extensions the old path[:-4] slice mangled (which
    silently disabled this guard for .ts/.divx/.mpeg)."""
    source = tmp_path / "done-ep.ts"
    source.write_bytes(b"source-bytes")
    existing = tmp_path / "done-ep.subs.mp4"
    existing.write_bytes(b"already-there")

    gen = _window(qapp, monkeypatch, tmp_path, _ScriptedClient())
    win = next(gen)
    rows = win._subs_file_rows([str(source)])
    assert rows[0][0] == (
        "blocked — subtitles output already exists (will not be overwritten)"
    )
    assert "output exists: done-ep.subs.mp4" in rows[0][7]
    assert "will not be overwritten" in rows[0][7]
    # the untouched prior output
    assert existing.read_bytes() == b"already-there"
    next(gen, None)


def test_subs_eta_stays_honest_no_digits_for_transcribe(
    qapp, monkeypatch, tmp_path
):
    """§3: the ETA machinery tolerates the transcribe phase — the
    snapshot consumes with segment counts on the label but NO ETA
    digits and no fabricated numbers anywhere."""
    path = tmp_path / "eta-ep.mkv"
    path.write_bytes(b"x")
    client = _ScriptedClient()
    client.make_job(
        str(path),
        [
            {"state": "queued"},
            {"state": "running", "current_phase": "transcribe",
             "progress": {"segments_done": 7,
                          "step": "transcribing segment 7"}},
        ],
        kind="subs",
    )

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(path)], "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    win._tick_elapsed()
    win._tick_elapsed()

    stage = win.stage_label.text()
    assert "transcribing audio" in stage
    assert "7 segments" in stage
    assert "ETA" not in stage
    # the machinery itself refuses the phase
    snapshot = client.status_snapshot("VJ1")
    assert win._eta_current_job(snapshot) is None
    # the segment count is NOT dressed up as a fraction anywhere
    assert win._stage_fraction is None
    next(gen, None)


def test_subs_batch_then_upscale_batch_hygiene(
    qapp, monkeypatch, tmp_path
):
    """§5: a full subs batch drains, then an upscale batch runs —
    the fresh batch must behave like a first-ever run (the v0.4.13
    back-to-back hygiene rule extended across FLOWS: _batch_row_for_job
    reseeded, slots reseeded, follow re-engaged, no stale alias in
    either direction)."""
    subs_path = tmp_path / "hygiene-subs.mkv"
    subs_path.write_bytes(b"x")
    picked = [str(subs_path)]
    client = _ScriptedClient()
    client.make_job(
        str(subs_path),
        [{"state": "queued"}, {"state": "succeeded", "current_phase": "done"}],
        kind="subs",
    )
    upscale_name = "hygiene-up.avi"
    plan = _plan_for(tmp_path, upscale_name)

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: (picked, "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    win._tick_elapsed()  # queued
    win._tick_elapsed()  # terminal lands + drains
    assert win.job_ids == []
    assert _row_state(win, "hygiene-subs.mkv") == "succeeded"
    # the ended slate's structures stay until the next submission
    # reseeds them (the upscale start below does exactly that)
    assert win._batch_row_for_job == {"VJ1": 0}

    # the upscale batch rides the SAME window (scripted run id VJ2)
    client.make_job(str(plan.report.path), [{"state": "queued"}])
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.start_upscale()
    assert win.job_ids == ["VJ2"]
    assert win._batch_row_for_job == {"VJ2": 0}
    assert win._batch_slots == ["VJ2"]
    assert win._follow_mode is True
    assert win._follow_job_id is None
    assert win.run_total == 1
    assert win.run_counts == [None]
    # a plan render REPLACES the table (a plan is its own batch view;
    # the subs history row may only survive into the next subs append,
    # never across a new plan whose row map seeds from 0)
    assert win.file_table.rowCount() == 1
    win._tick_elapsed()
    assert _row_state(win, upscale_name) == "queued"
    next(gen, None)


def test_subs_batch_then_second_subs_batch_reseeds_rows(
    qapp, monkeypatch, tmp_path
):
    """Two subs slates back to back with DIFFERENT row counts: the
    second submission must reseed the row map and slots so no payload
    of the new batch can alias into the old batch's rows (the mixed-
    batches-behave-cleanly contract; the old rows stay readable)."""
    first = tmp_path / "b1-one.mkv"
    first.write_bytes(b"x")
    second_pair = [tmp_path / "b2-one.mkv", tmp_path / "b2-two.mkv"]
    for path in second_pair:
        path.write_bytes(b"x")

    client = _ScriptedClient()
    client.make_job(
        str(first),
        [{"state": "queued"}, {"state": "succeeded", "current_phase": "done"}],
        kind="subs",
    )
    client.make_job(str(second_pair[0]), [{"state": "queued"}], kind="subs")
    client.make_job(str(second_pair[1]), [{"state": "queued"}], kind="subs")

    import vidlift.gui as gui_mod

    results = [[str(first)], [str(p) for p in second_pair]]
    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: (results.pop(0), "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    win._tick_elapsed()
    win._tick_elapsed()
    assert win.job_ids == []
    assert _row_state(win, "b1-one.mkv") == "succeeded"

    # second, LARGER subs slate on the same window; rows append BELOW
    # the first slate's history row, so the identity map carries the
    # offset (slots/run_counts stay 0-based submission-order arrays).
    win.add_subs_button.click()
    assert win.job_ids == ["VJ2", "VJ3"]
    assert win._batch_row_for_job == {"VJ2": 1, "VJ3": 2}
    assert win._batch_slots == ["VJ2", "VJ3"]
    assert win.run_total == 2
    assert win.run_counts == [None, None]
    assert win.progress.format() == "0/2 succeeded"
    # the first slate's terminal row survives as history
    assert _row_state(win, "b1-one.mkv") == "succeeded"
    win._tick_elapsed()
    assert _row_state(win, "b2-one.mkv") == "queued"
    assert _row_state(win, "b2-two.mkv") == "queued"
    next(gen, None)


def test_start_upscale_machinery_unchanged_on_plan_batches(
    qapp, monkeypatch, tmp_path
):
    """Regression guard: after the shared-slate refactor, the upscale
    flow's slate + cancel machinery behaves EXACTLY as on
    v0.4.13 (the seeded-map contract, pinned by the v0.4.12/13 tests,
    re-verified through one plan batch here)."""
    names = ["up-one.avi", "up-two.avi"]
    plans = [_plan_for(tmp_path, n) for n in names]
    client = _ScriptedClient()
    client.make_job(plans[0].report.path, [{"state": "queued"}])
    client.make_job(plans[1].report.path, [{"state": "queued"}])

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan(plans)
    win.render_batch(win.batch)
    win.start_upscale()
    assert win.job_ids == ["VJ1", "VJ2"]
    assert win._batch_row_for_job == {"VJ1": 0, "VJ2": 1}
    assert win._batch_slots == ["VJ1", "VJ2"]
    assert win.run_counts == [None, None]
    assert not win.engine_combo.isEnabled()
    assert not win.profile_combo.isEnabled()
    assert win.cancel_batch_button.isEnabled()
    win._tick_elapsed()
    assert _row_state(win, "up-one.avi") == "queued"
    assert _row_state(win, "up-two.avi") == "queued"
    next(gen, None)

# ------------------------------------------------- Mode A isolation
def test_mode_a_checkbox_not_in_subs_options(tmp_path):
    """§4 constraint: the subs enqueue surface carries NO Mode A options
    (transcription_enabled/subtitle_ocr) — the transcribe_subs checkbox
    feeds only the run-tool options dict, so a subs-only job is
    unaffected by the checkbox either way (structural, not behavioral)."""
    import inspect
    from vidlift.mcp_bridge.client_local import LocalBridgeClient

    src = inspect.getsource(LocalBridgeClient.enqueue_subs)
    assert "transcription_enabled" not in src
    assert "subtitle_ocr" not in src


# ------------------------------------------------- GUI live detail overlay
def test_subs_gui_live_details_render_from_snapshot(
    qapp, monkeypatch, tmp_path
):
    """The selected subs row's detail panel renders the LIVE overlay
    from the snapshot facts (state, rendered stage); the running
    transcribe snapshot carries the segment progress."""
    path = tmp_path / "detail-ep.mkv"
    path.write_bytes(b"x")
    client = _ScriptedClient()
    client.make_job(
        str(path),
        [
            {"state": "queued"},
            {"state": "running", "current_phase": "transcribe",
             "progress": {"segments_done": 9,
                          "step": "transcribing segment 9"}},
        ],
        kind="subs",
    )

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(path)], "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    win._tick_elapsed()
    win._tick_elapsed()
    row = _row_by_filename(win, "detail-ep.mkv")
    win.file_table.selectRow(row)
    qapp.processEvents()
    text = win.details.toPlainText()
    assert "VJ1" in text
    assert "live" in text.lower()
    assert "transcribing" in text.lower()
    assert "9" in text
    next(gen, None)


# ------------------------------------------------- mixed-flow guard
def test_subs_active_batch_blocks_second_flow(qapp, monkeypatch, tmp_path):
    """While a subs batch is live, BOTH flows are blocked — Start
   Upscale by the plan-less _update_start_enabled rule, and a second
    Add Subtitles by the active-count guard (one batch at a time —
    no interleaved slates)."""
    first = tmp_path / "mix-one.mkv"
    first.write_bytes(b"x")
    client = _ScriptedClient()
    client.make_job(
        str(first),
        [{"state": "queued"},
         {"state": "running", "current_phase": "transcribe",
          "progress": {"segments_done": 1}}],
        kind="subs",
    )

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(first)], "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    win._tick_elapsed()
    win._tick_elapsed()
    assert "transcribing" in win.stage_label.text()
    # the plan-less state keeps Start disabled (never ships an upscale
    # re-plan across a live subs queue)
    assert not win.start_button.isEnabled()
    # a second Add Subtitles click is refused (a fresh picker list would
    # otherwise enqueue a second overlapping slate)
    second = tmp_path / "mix-two.mkv"
    second.write_bytes(b"x")
    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(second)], "")),
    )
    win.add_subs_button.click()
    assert win.job_ids == ["VJ1"]  # unchanged — the click was refused
    next(gen, None)


# ------------------------------------------ v0.4.17: replace-source checkbox
def _grid_of(win):
    """The controls grid that owns the Mode B action row."""
    return win.add_subs_button.parentWidget().layout()


def _grid_position(win, widget) -> tuple[int, int]:
    layout = _grid_of(win)
    for index in range(layout.count()):
        if layout.itemAt(index).widget() is widget:
            row, column, _row_span, _col_span = layout.getItemPosition(index)
            return row, column
    raise AssertionError("widget not found in the controls grid")


def test_subs_replace_checkbox_exists_beside_button_default_off_tooltip(
    qapp, monkeypatch, tmp_path
):
    """v0.4.17 item 25: the checkbox lives at grid (2, 8) — immediately
    right of 'Add Subtitles…' at (2, 7) — default OFF, with the .mp4
    naming rule (D6), the keep-both promise, and the scope
    disambiguation in its tooltip."""
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    # placement: co-located, right of the Mode B button
    assert _grid_position(win, win.add_subs_button) == (2, 7)
    assert _grid_position(win, win.subs_replace_source) == (2, 8)
    # default OFF (fresh QSettings, the absent-defaults rule)
    assert win.subs_replace_source.isChecked() is False
    # labels: the mode named, the irrecoverability named
    assert win.subs_replace_source.text() == (
        "Replace original after adding subtitles (deletes the source)"
    )
    tooltip = win.subs_replace_source.toolTip().lower()
    assert "35 hours.avi" in tooltip and "35 hours.mp4" in tooltip
    assert ".mp4" in tooltip
    assert "both files are kept" in tooltip  # the D2/D5 keep-both promise
    assert "sidecar" in tooltip  # the D1 rename-to-match note
    # the button tooltip no longer claims an absolute "never modified"
    assert "can replace it" in win.add_subs_button.toolTip().lower()
    assert "scope" not in tooltip  # sanity: the scope sentence is real
    assert "upscale panel has its own replace option" in tooltip
    next(gen, None)


def test_subs_replace_checkbox_persists_and_restores(qapp, monkeypatch, tmp_path):
    """v0.4.17 item 26: toggling persists QSettings key
    'subs_replace_source'; a fresh window restores it — both directions
    (ON persisted → restored ON; OFF persisted → restored OFF)."""
    import PyQt6.QtCore as QtCore

    settings_path = str(tmp_path / "settings.ini")

    def _read():
        _w = QtCore.QSettings(settings_path, QtCore.QSettings.Format.IniFormat)
        _w.sync()
        return _w

    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.subs_replace_source.setChecked(True)
    assert _read().value("subs_replace_source", False, type=bool) is True

    # a fresh window restores the persisted state
    client2 = _ScriptedClient()
    _alive.append(MainWindow(client=client2))
    win2 = _alive[-1]
    assert win2.subs_replace_source.isChecked() is True

    # and the OFF direction persists + restores too
    win2.subs_replace_source.setChecked(False)
    assert _read().value("subs_replace_source", False, type=bool) is False
    client3 = _ScriptedClient()
    _alive.append(MainWindow(client=client3))
    win3 = _alive[-1]
    assert win3.subs_replace_source.isChecked() is False
    next(gen, None)


def test_subs_replace_key_isolated_from_mode_a_key(qapp, monkeypatch, tmp_path):
    """v0.4.17 item 26 (key isolation): the Mode B toggle never writes
    Mode A's 'replace_source' key, and Mode A's toggle never writes the
    Mode B key ('subs_replace_source'). Each direction runs against its
    own clean QSettings file so the absence assertion is decisive."""
    import PyQt6.QtCore as QtCore

    def _file_isolated_settings(name):
        path = str(tmp_path / f"settings-{name}.ini")

        def _settings(self):
            return QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat)

        def _read():
            _w = QtCore.QSettings(path, QtCore.QSettings.Format.IniFormat)
            _w.sync()
            return _w

        return _settings, _read

    # direction 1 (Mode B toggle → Mode A key untouched)
    write_b, read_b = _file_isolated_settings("b-writes")
    monkeypatch.setattr(MainWindow, "_settings", write_b)
    _alive.append(MainWindow(client=_ScriptedClient()))
    win_b = _alive[-1]
    win_b.subs_replace_source.setChecked(True)
    stored_b = read_b()
    assert stored_b.value("subs_replace_source", False, type=bool) is True
    assert stored_b.contains("replace_source") is False

    # direction 2 (Mode A toggle → Mode B key untouched)
    write_a, read_a = _file_isolated_settings("a-writes")
    monkeypatch.setattr(MainWindow, "_settings", write_a)
    _alive.append(MainWindow(client=_ScriptedClient()))
    win_a = _alive[-1]
    win_a.replace_source.setChecked(True)
    stored_a = read_a()
    assert stored_a.value("replace_source", False, type=bool) is True
    assert stored_a.contains("subs_replace_source") is False


def test_subs_pick_flow_threads_checkbox_state_into_enqueue(
    qapp, monkeypatch, tmp_path
):
    """v0.4.17 item 27: pick with the checkbox ON → enqueue_subs
    receives replace_source=True; OFF → False (the scripted-client
    harness records the exact state submitted)."""
    media = tmp_path / "episode.mkv"
    media.write_bytes(b"x")

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog,
        "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(media)], "")),
    )

    # ON: the state being enqueued is True
    client = _ScriptedClient()
    client.make_job(str(media), [{"state": "queued"}], kind="subs")
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.subs_replace_source.setChecked(True)
    win.add_subs_button.click()
    assert client.last_subs_replace_source is True
    # park the shared settings file OFF again for the second phase
    win.subs_replace_source.setChecked(False)
    next(gen, None)

    # OFF: the state being enqueued is False
    client2 = _ScriptedClient()
    client2.make_job(str(media), [{"state": "queued"}], kind="subs")
    gen2 = _window(qapp, monkeypatch, tmp_path, client2)
    win2 = next(gen2)
    assert win2.subs_replace_source.isChecked() is False
    win2.add_subs_button.click()
    assert client2.last_subs_replace_source is False
    next(gen2, None)


def test_subs_file_rows_column3_tracks_submitted_state(
    qapp, monkeypatch, tmp_path
):
    """v0.4.17 item 28: column 3 reads 'replace original → .mp4' when
    ON and 'keep original' when OFF (the row is rendered at enqueue
    time from the exact state being enqueued)."""
    media = tmp_path / "episode.mkv"
    media.write_bytes(b"x")
    client = _ScriptedClient()
    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    off_rows = win._subs_file_rows([str(media)])
    assert off_rows[0][3] == "keep original"
    win.subs_replace_source.setChecked(True)
    on_rows = win._subs_file_rows([str(media)])
    assert on_rows[0][3] == "replace original → .mp4"
    next(gen, None)


def test_mode_a_replace_option_never_cross_wires_mode_b(
    qapp, monkeypatch, tmp_path
):
    """v0.4.17 item 29 (the Eric-confusion regression fence): the
    upscale start path still reads ONLY Mode A's own replace_source
    checkbox, and the Mode B enqueue reads only the Mode B checkbox."""
    import inspect

    start_src = inspect.getsource(MainWindow.start_upscale)
    assert '"replace_source": self.replace_source.isChecked()' in start_src
    assert "subs_replace_source" not in start_src
    subs_src = inspect.getsource(MainWindow.choose_subs_files)
    assert "replace_source=self.subs_replace_source.isChecked()" in subs_src
    # and the Mode B path never reads Mode A's checkbox
    assert "self.replace_source.isChecked()" not in subs_src
    assert "replace_source=self.subs_replace_source.isChecked()" in subs_src
