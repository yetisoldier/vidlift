"""v0.4.15.1 startup sweep — stale registry records must not block the GUI.

Eric's 12:43 CST repro (second screenshot): a FRESH app start with an
EMPTY file table and GPU at 0% pops 'Wait for the current upscale to
finish or cancel it first.' on Clear (or "pretty much anything") —
gui.py:1310's guard reads ``client.active_job_count() > 0`` while
NOTHING is running. Root cause: the persistent registry (87 KB of
records on Eric's machine, including survivors of the crashed 0.4.12
session) still carried 'queued' and 'running' records, and
``active_job_count()`` counted them as active.

The existing restart sweep (``Registry.sweep_orphans``, mcp-bridge.md
§6) covers ONLY 'running' records (the crashed-server corpse rule); the
design's "queued jobs resume across a restart" semantics stay binding
for the stdio MCP server. But at GUI startup the auto-resume is wrong:
the executor has not started, so queued records are exactly as dead as
running ones, and every active-count guard in the window reads them.

Red-first contracts:

1. ``build_services(gui_startup=True)`` sweeps demonstrably dead-owner
   queued+running to ``failed`` with startup/restart errors respectively;
   live or unverifiable owners and terminal history remain untouched.
2. ``LocalBridgeClient`` construction IS the GUI startup: after
   construct, ``active_job_count() == 0``, stale records are failed and
   PERSISTED, and their history rows remain (visible, not deleted).
3. The stdio ``McpServer`` path keeps §6 semantics: queued survives.
4. The 12:43 repro end-to-end: seeded stale registry + a fresh window
   with an empty table — ``clear_files()`` must not pop the block
   dialog (the same shape blocks "pretty much anything" today).
5. Start's guard stays quiet: a real plan after startup enqueues.
6. Boundary per the ee234b6 independence design: 'Add Subtitles…'
   opens the picker with stale records in the registry.
7. Registry-level unit pins: default ``sweep_orphans()`` unchanged
   (running-only), the queued extension is a documented privileged
   write (no fabricated ``started_at``), idempotent on second run.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from _gui_checks import wait_for_checks
from _registry_checks import seed_exited_registry

pytest.importorskip("PyQt6")

os.environ.setdefault("VIDLIFT_MONITOR_AUTOSTART", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from vidlift.gui import MainWindow
from vidlift.mcp_bridge.client_local import LocalBridgeClient
from vidlift.mcp_bridge.registry import (
    RESTART_ERROR,
    STARTUP_SWEEP_ERROR,
    Registry,
)
from vidlift.mcp_bridge.server import McpServer, build_services
from vidlift.plan import BatchPlan

from test_v0414_mode_b_gui import _plan_for


# ------------------------------------------------------------------ harness
@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _seed_stale_registry(
    tmp_path,
    *,
    queued: int = 2,
    running: int = 1,
    with_terminals: bool = True,
) -> Registry:
    """The post-crash jobs.json shape: stale queued + running records
    (paths need not exist — the previous session died), plus terminal
    history rows that must survive every sweep untouched."""
    jobs = [{"path": f"/media/gone-queued-{i}.avi", "state": "queued",
             "options": {"engine": "auto"}} for i in range(queued)]
    jobs += [{"path": f"/media/gone-running-{i}.avi", "state": "running",
              "options": {"engine": "auto"}} for i in range(running)]
    if with_terminals:
        jobs += [{"path": "/media/old-done.avi", "state": "succeeded"},
                 {"path": "/media/old-corpse.avi", "state": "failed", "error": RESTART_ERROR}]
    # Same-process enqueue now records a LIVE owner. An exited child models
    # the actual crashed predecessor without stripping ownership metadata.
    return seed_exited_registry(tmp_path, jobs)


def _recovery_error(path):
    return RESTART_ERROR if "/gone-running-" in path else STARTUP_SWEEP_ERROR


def _settings_patch(monkeypatch, tmp_path):
    import PyQt6.QtCore as QtCore

    def _settings(self):
        return QtCore.QSettings(
            str(tmp_path / "settings.ini"), QtCore.QSettings.Format.IniFormat
        )

    monkeypatch.setattr(MainWindow, "_settings", _settings)
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _capture_information(monkeypatch) -> dict:
    """Capture every QMessageBox.information text; auto-ok so clicks
    return cleanly."""
    captured: dict = {"log": []}

    def fake_information(*a, **k):
        captured["log"].append(a[2] if len(a) > 2 else "")
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "information", staticmethod(fake_information))
    return captured


def _teardown_window(win, qapp):
    win.job_ids = []
    win._run_started_at = None
    win.deleteLater()
    qapp.processEvents()


# ------------------------------------------------------------------ tests
def test_build_services_gui_startup_sweep_covers_queued_and_running(
    tmp_path, monkeypatch
):
    """§1: gui_startup=True sweeps queued AND running to failed with the
    queued startup / running restart errors; terminal history is untouched; the result is
    persisted to jobs.json before any consumer can count actives."""
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    _seed_stale_registry(tmp_path)

    services = build_services(tmp_path, gui_startup=True)
    registry = services.registry

    failed = registry.history()
    by_first_path = {
        (r.paths[0] if r.paths else None): r for r in failed
    }
    for stale_path in (
        "/media/gone-queued-0.avi",
        "/media/gone-queued-1.avi",
        "/media/gone-running-0.avi",
    ):
        record = by_first_path[stale_path]
        assert record.state == "failed", stale_path
        assert record.error == _recovery_error(stale_path), stale_path
        assert record.ok is False, stale_path
    # persisted NOW, before any active-job read in the GUI
    rows = json.loads((tmp_path / "jobs.json").read_text())["jobs"]
    persisted = {row["paths"][0]: row for row in rows}
    for stale_path in (
        "/media/gone-queued-0.avi",
        "/media/gone-queued-1.avi",
        "/media/gone-running-0.avi",
    ):
        assert persisted[stale_path]["state"] == "failed", stale_path
        assert persisted[stale_path]["error"] == _recovery_error(stale_path)
    # terminal history rows keep their original truth
    assert by_first_path["/media/old-done.avi"].state == "succeeded"
    assert by_first_path["/media/old-corpse.avi"].state == "failed"
    assert by_first_path["/media/old-corpse.avi"].error == RESTART_ERROR


def test_local_bridge_client_startup_sweep_clears_stale_actives(
    tmp_path, monkeypatch
):
    """§2 RED-FIRST (the GUI seam): LocalBridgeClient construction runs
    the startup sweep — AFTER construct, active_job_count() == 0 even
    though the registry file carried stale queued+running records; the
   swept records stay visible as failed history rows, never deleted."""
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    registry = _seed_stale_registry(tmp_path)
    stale_ids = list(registry._jobs.keys())

    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    # THE Eric-repro red line: pre-fix this reads 3 (2 queued + 1 running)
    assert client.active_job_count() == 0
    # Every stale record is failed with its precise queued/running error.
    for job_id in stale_ids:
        record = client.registry.require(job_id)
        if record.paths and record.paths[0].startswith("/media/gone"):
            assert record.state == "failed", job_id
            assert record.error == _recovery_error(record.paths[0]), job_id
    # terminal history untouched
    seeded = client.registry.history()
    assert len(seeded) == 5  # 3 swept + 2 terminal rows stayed
    states = sorted(r.state for r in seeded)
    assert states == ["failed", "failed", "failed", "failed", "succeeded"]
    # the swept rows are VISIBLE history, not deleted records
    swept_rows = [r for r in seeded if r.paths[0].startswith("/media/gone")]
    assert len(swept_rows) == 3
    assert sum(r.error == STARTUP_SWEEP_ERROR for r in swept_rows) == 2
    assert sum(r.error == RESTART_ERROR for r in swept_rows) == 1
    # persisted before any GUI read
    rows = json.loads((tmp_path / "jobs.json").read_text())["jobs"]
    assert all(
        row["state"] == "failed"
        for row in rows
        if row["paths"] and row["paths"][0].startswith("/media/gone")
    )
    # idempotent: a second client construction over the same state dir
    client2 = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    assert client2.active_job_count() == 0
    assert len([r for r in client2.registry.history()
                if r.paths[0].startswith("/media/gone")]) == 3
    assert {r.job_id: r.to_dict() for r in client2.registry.history()} == {
        r.job_id: r.to_dict() for r in seeded}


def test_stdio_mcp_server_restart_still_resumes_queued(tmp_path):
    """§3 pin: the stdio server's restart sweep is UNCHANGED — a queued
    job enqueued by an MCP agent survives the server restart (design §6
    'queued jobs resume automatically'); only the GUI startup path
    extends to queued."""
    state = tmp_path / "state"
    registry = Registry(state)
    queued = registry.enqueue(
        kind="run", paths=["/media/agent-enqueued.avi"], options={}
    )
    server = McpServer(
        state_dir=state, registry=Registry(state), start_workers=False
    )
    record = server.registry.require(queued.job_id)
    assert record.state == "queued"
    assert record.error is None


def test_gui_startup_empty_table_clear_files_not_blocked(qapp, monkeypatch, tmp_path):
    """§4 RED-FIRST: the exact 12:43 CST repro — fresh app start, EMPTY
    file table, GPU idle, stale records in the registry. Clear must NOT
    pop 'Wait for the current upscale to finish or cancel it first.'
    (and nothing else may dialog during startup or the click)."""
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    _seed_stale_registry(tmp_path)
    _settings_patch(monkeypatch, tmp_path)
    captured = _capture_information(monkeypatch)

    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    win = MainWindow(client=client)
    try:
        assert win.file_table.rowCount() == 0  # fresh start, empty table
        assert client.active_job_count() == 0  # RED pre-fix: 3
        win.clear_files()  # the exact clicked slot from the screenshot
        assert captured["log"] == [], (
            f"block dialog on a fresh start: {captured['log']!r}"
        )
        assert client.active_job_count() == 0
        # history rows still show the swept jobs as failed (visible)
        assert all(
            r.state == "failed" and r.error == _recovery_error(r.paths[0])
            for r in client.registry.history()
            if r.paths and r.paths[0].startswith("/media/gone")
        )
    finally:
        _teardown_window(win, qapp)


def test_gui_start_upscale_guard_quiet_after_startup_sweep(
    qapp, monkeypatch, tmp_path
):
    """§5 RED-FIRST: with stale records swept at startup, Start's guard
    (active_job_count() > 0) does not fire — a real plan enqueues and
    the run slate begins, no dialog anywhere."""
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    _seed_stale_registry(tmp_path)
    _settings_patch(monkeypatch, tmp_path)
    captured = _capture_information(monkeypatch)

    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    win = MainWindow(client=client)
    try:
        assert client.active_job_count() == 0  # RED pre-fix: 3
        plan = _plan_for(tmp_path, "fresh-target.avi")
        win.batch = BatchPlan([plan])
        win.render_batch(win.batch)
        win._update_start_enabled()
        assert win.start_button.isEnabled(), "Start must be enabled (nothing active)"
        win.start_upscale()
        assert captured["log"] == [], (
            f"Start's guard fired after the sweep: {captured['log']!r}"
        )
        assert len(win.job_ids) == 1
        record = client.registry.require(win.job_ids[0])
        assert record.kind == "run"
        assert record.state == "queued"  # executor not started in-test
    finally:
        _teardown_window(win, qapp)


def test_add_subtitles_picker_opens_with_stale_registry(
    qapp, monkeypatch, tmp_path
):
    """§6 boundary (per the ee234b6 independence design): 'Add
    Subtitles…' opens the picker and enqueues even though the registry
    carried stale records — the startup sweep guarantees the guard
    reads zero."""
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    _seed_stale_registry(tmp_path)
    fresh = tmp_path / "fresh-subs-target.mkv"
    fresh.write_bytes(b"x")

    import vidlift.gui as gui_mod

    _settings_patch(monkeypatch, tmp_path)
    captured = _capture_information(monkeypatch)
    monkeypatch.setattr(
        gui_mod.QFileDialog,
        "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(fresh)], "")),
    )

    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    win = MainWindow(client=client)
    try:
        assert client.active_job_count() == 0  # startup swept (RED pre-fix: 3)
        win.add_subs_button.click()
        wait_for_checks(win)
        win.start_subtitles()  # staged Add is submitted only on explicit Start
        assert captured["log"] == [], (
            f"subs picker blocked: {captured['log']!r}"
        )
        assert len(win.job_ids) == 1
        record = client.registry.require(win.job_ids[0])
        assert record.kind == "subs"
        assert record.state == "queued"
        assert record.paths == [str(fresh)]
    finally:
        _teardown_window(win, qapp)


def test_sweep_orphans_defaults_and_queued_extension_pins(tmp_path):
    """§7 unit pins on the registry seam: (a) the stdio-default
    sweep_orphans() is unchanged — running corpses failed with the
    restart error, QUEUED untouched; (b) include_queued=True extends to
    queued with the given error, via a documented privileged write (the
    job never started: no fabricated started_at); (c) idempotent — a
    second sweep returns []."""
    registry = _seed_stale_registry(tmp_path)
    queued_before = [r for r in registry.queued()]

    # (a) stdio default: running-only
    swept = registry.sweep_orphans()
    assert len(swept) == 1
    assert swept[0].state == "failed"
    assert swept[0].error == RESTART_ERROR
    still_queued = registry.queued()
    assert [r.job_id for r in still_queued] == [
        r.job_id for r in queued_before
    ]
    assert all(r.error is None for r in still_queued)

    # (b) startup mode: queued + running, startup error
    swept2 = registry.sweep_orphans(error=STARTUP_SWEEP_ERROR, include_queued=True)
    assert sorted(r.state for r in swept2) == ["failed", "failed"]
    assert all(r.error == STARTUP_SWEEP_ERROR for r in swept2)
    for record in swept2:
        assert record.ok is False
        assert record.finished_at is not None
        if record.paths[0].startswith("/media/gone-queued"):
            # never started — the honest close stamps no started_at
            assert record.started_at is None

    # (c) idempotent: nothing left in non-terminal states
    assert registry.sweep_orphans(
        error=STARTUP_SWEEP_ERROR, include_queued=True
    ) == []
    assert registry.queued() == [] and registry.running() == []


@pytest.mark.parametrize("gui_startup", [False, True])
@pytest.mark.parametrize("owner_status", ["live", "unknown"])
def test_restart_retains_live_or_unverifiable_owner(tmp_path, monkeypatch, gui_startup, owner_status):
    """A second services/server must never reclaim another live/unknown owner."""
    from vidlift.mcp_bridge import registry_transactions

    registry = Registry(tmp_path)
    queued = registry.enqueue(kind="run", paths=["synthetic-queued"], options={})
    running = registry.enqueue(kind="run", paths=["synthetic-running"], options={})
    registry.transition(running, "running")
    if owner_status == "unknown":
        monkeypatch.setattr(registry_transactions, "owner_alive", lambda fact: None)
    else:
        assert registry_transactions.owner_alive(running.to_dict()["owner"]) is True
    before = (tmp_path / "jobs.json").read_bytes()
    services = build_services(tmp_path, gui_startup=gui_startup)
    server = McpServer(state_dir=tmp_path, services=services, start_workers=False)
    assert server.registry.require(queued.job_id).state == "queued"
    assert server.registry.require(running.job_id).state == "running"
    assert server.registry.history() == []
    assert server.executor._threads == []
    assert (tmp_path / "jobs.json").read_bytes() == before