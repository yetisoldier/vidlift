"""v0.4.17 fail-path regression — a failed job reaches EVERY GUI surface.

Eric's repro (2026-11-03, live on v0.4.16.0): a Mode-B "subtitles only"
run on ``The Adventures of Pete & Pete (1993) - S03E13 - Saturday.avi``
failed at the "probe — reading source metadata" phase ~19 s in. The
registry was correct — /history carried the failed record, /status
drained active_jobs — and the batch bar + job table landed "failed", but
three surfaces kept claiming RUNNING:

- the selected row's details panel kept its body "Verdict: running" and
  the live overlay line "running — probe — reading source metadata";
- both run bars (stage + overall) stayed in the busy pulse forever (a
  probe failure never produces a measured fraction, so nothing had ever
  called them out of pulse mode when the batch ended);
- AND no surface anywhere named WHY: /history rows carried
  ``engine_note: null`` with no error key at all, and the GUI log tail
  was empty because a probe failure writes no run log.

Root causes (all read-path, no transport redesign, api_version stays 1):

- ``_poll_jobs`` skipped the per-tick details render on the batch-end
  tick (its old comment claimed _batch_finished "just wrote" the
  terminal state — it writes the summary/batch-bar/table only, NEVER
  the details panel); the per-tick render's own "is a batch live" guard
  additionally keyed on ``self.batch is None`` — a v0.4.13 guard that
  predates Mode B and made the plan-less subs slate invisible to the
  per-tick render forever (only the claim edge and user selections
  rendered subs details);
- ``_batch_finished`` never settled the bars;
- the two /history serializations (``LocalBridgeClient.history`` and
  ``tool_list``'s history block) dropped the record's own ``error``
  field, and the GUI overlay never rendered one.

This file pins the fixed journey. A probe-phase failure (Mode B is the
primary repro; Mode A variant + queued-cancel variant cover the shared
pipeline) ends with the details body, live overlay, and bars reflecting
``failed`` — with the failure reason surfaced verbatim from the payload
the /history rows and the bridge status already serve. The v0.4.16.0
cancel fix is preserved (it owns the registry/executor truth; this fix
only repairs GUI read-path consumption of the same snapshots).

v0.4.19 (D-2, Bob's decision): the client double is the HOUSE harness
(``test_v0414_mode_b_gui``'s ``_ScriptedClient``/``_window``), not a
per-file copy — the v0.4.18 ``suppress_srt`` ripple was the drift this
rule exists to kill.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from _gui_checks import wait_for_checks

from vidlift.mcp_bridge import (
    BridgeServices,
    JobExecutor,
    McpServer,
    Registry,
)
from vidlift.mcp_bridge.client_local import LocalBridgeClient
from vidlift.plan import BatchPlan, build_file_plan
from vidlift.probe import classify, parse_probe

# ---- bridge/transport-level pieces (no Qt) ---------------------------------


_PROBE_FAILURE = "ffprobe failed on"


class _ProbeFailingSubsRunner:
    """The subs command seam whose ffprobe fails honestly — the Eric
    repro's shape: the source .avi's metadata read dies at exit 1 with a
    diagnostic on stderr. Everything else is never reached (the failure
    IS the probe)."""

    def __call__(self, binary: str, args: list[str]) -> tuple[int, str, str]:
        if "-show_format" in args and "-show_streams" in args:
            return 1, "", "Permission denied"
        return 1, "", f"unexpected seam call: {binary} {' '.join(args)}"


def _subs_probe_failure_server(tmp_path, media: Path) -> McpServer:
    """The real stdio bridge over a tmp registry, with the failing probe
    seam wired on services (the exact shape test_v0416_cancel_propagation
    drives) — the FULL fail journey: claim → subs job → probe fails →
    registry stamps error → terminal failed, all through production
    code."""
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    services = BridgeServices(
        registry=None,
        state_dir=state,
        probe_runner=_ProbeFailingSubsRunner(),
        command_runner=_ProbeFailingSubsRunner(),
    )
    return McpServer(state_dir=state, services=services, start_workers=True)


def _call(server: McpServer, name: str, arguments: dict, request_id: int = 1) -> dict:
    response = server.handle_line(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
    )
    message = json.loads(response)
    return json.loads(message["result"]["content"][0]["text"])


def test_bridge_subs_probe_failure_surfaces_reason_in_status_and_history(
    tmp_path,
):
    """End-to-end (defect 2): a subs job whose probe fails closes
    ``failed`` with the exception text persisted, and both read surfaces
    the user/agent can see — the ``status`` payload and ``list``'s
    history rows — carry the reason verbatim (additive keys;
    api_version untouched, pinned by the additive-keys conventions in
    monitor.py)."""
    media = tmp_path / "saturday.avi"
    media.write_bytes(b"source-bytes")
    server = _subs_probe_failure_server(tmp_path, media)
    try:
        run_payload = _call(server, "transcribe", {"paths": [str(media)]})
        job_id = run_payload["jobs"][0]["job_id"]
        deadline = time.time() + 15
        status = None
        while time.time() < deadline:
            status = _call(server, "status", {"job_id": job_id}, 2)
            if status["state"] in ("failed", "cancelled", "succeeded"):
                break
            time.sleep(0.02)
        # the probe failure fails the JOB (Mode B discipline)
        assert status is not None and status["state"] == "failed"
        assert _PROBE_FAILURE in (status["error"] or "")
        assert "Permission denied" in (status["error"] or "")
        # the active set drains like Eric's /status showed
        listing = _call(server, "list", {"include_history": False}, 3)
        assert listing["running"] == [] and listing["queued"] == []
        # ...and /history (the tool + client shape) names WHY now
        listing = _call(server, "list", {"include_history": True}, 4)
        rows = [r for r in listing["history"] if r["job_id"] == job_id]
        assert rows and rows[0]["state"] == "failed"
        assert _PROBE_FAILURE in (rows[0]["error"] or "")
        # successful/unfailed rows keep the additive key honest-null
        assert all(
            r.get("error") is not None or r["state"] != "failed"
            for r in listing["history"]
        )
    finally:
        server.executor.stop()


def test_client_history_rows_carry_error_additive(tmp_path, monkeypatch):
    """The GUI's bridge read (``LocalBridgeClient.history``) serializes
    the record's own ``error`` — the key the HTTP monitor /history
    serves verbatim (pass-through; no second serializer). A terminal
    failed record round-trips jobs.json -> rows with the reason; a
    succeeded row (no error) carries honest ``None`` — additive key,
    shape unchanged otherwise."""
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    registry = Registry(tmp_path)
    source = tmp_path / "movie.avi"
    source.write_bytes(b"x")
    failed = registry.enqueue(kind="subs", paths=[str(source)], options={})
    registry.transition(failed, "running")
    failed.error = "ffprobe failed on /m/movie.avi: Permission denied"
    registry.transition(failed, "failed")
    good_source = tmp_path / "good.avi"
    good_source.write_bytes(b"x")
    succeeded = registry.enqueue(kind="subs", paths=[str(good_source)], options={})
    registry.transition(succeeded, "running")
    registry.transition(succeeded, "succeeded")
    # the registry file on disk is the truth the client re-loads
    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    rows = {row["job_id"]: row for row in client.history()}
    assert rows[failed.job_id]["state"] == "failed"
    assert (
        rows[failed.job_id]["error"]
        == "ffprobe failed on /m/movie.avi: Permission denied"
    )
    assert rows[succeeded.job_id]["state"] == "succeeded"
    assert rows[succeeded.job_id]["error"] is None
    client.stop()


def test_monitor_history_serves_failure_reason(tmp_path, monkeypatch):
    """The HTTP monitor's /history (Eric's inspection surface) shows the
    reason: the route is a pass-through of client.history(), so the
    additive ``error`` key lands without touching the route, the
    api_version, or any serializer twice."""
    pytest.importorskip("vidlift.monitor")
    from vidlift import monitor as monitor_mod

    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    registry = Registry(tmp_path)
    source = tmp_path / "saturday.avi"
    source.write_bytes(b"x")
    failed = registry.enqueue(kind="subs", paths=[str(source)], options={})
    registry.transition(failed, "running")
    failed.error = "ffprobe failed on /m/saturday.avi: invalid data"
    registry.transition(failed, "failed")

    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    server, base_url = monitor_mod.start_server(
        client=client, host="127.0.0.1", port=None or 0,
        token="tok-test-failpath",
    )
    try:
        request = urllib.request.Request(
            base_url + "/history",
            headers={"Authorization": "Bearer tok-test-failpath"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        rows = payload["jobs"]
        assert rows and rows[0]["job_id"] == failed.job_id
        assert rows[0]["state"] == "failed"
        assert (
            rows[0]["error"]
            == "ffprobe failed on /m/saturday.avi: invalid data"
        )
    finally:
        monitor_mod.stop_server()
        client.stop()

# ---- GUI-level regression (the three stale surfaces) -------------------------

pytest.importorskip("PyQt6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from vidlift.gui import MainWindow  # noqa: E402

from test_v0414_mode_b_gui import (  # noqa: E402  (D-2: house harness)
    _ScriptedClient,
    _window,
)


_PROBE_ERROR = "probe command failed (exit 1): ffprobe: Permission denied"


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


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


def _row_state(win, filename: str) -> str:
    for row in range(win.file_table.rowCount()):
        cell = win.file_table.item(row, 1)
        if cell is not None and cell.text().endswith(filename):
            status_cell = win.file_table.item(row, 0)
            return status_cell.text() if status_cell is not None else ""
    raise AssertionError(f"no row displays {filename!r}")


def test_subs_probe_failure_lands_failed_on_details_overlay_and_bars(
    qapp, monkeypatch, tmp_path
):
    """PRIMARY (Eric's repro, defect 1 + the GUI half of defect 2): the
    Mode-B run fails at probe. The batch bar + row landed failed on
    v0.4.16.0 already; this pins the FULL landing on every surface —
    the details body says Verdict: failed, the live overlay says
    State: failed (no stale running line, no terminal stutter) with the
    probe error verbatim, and both bars stop animating (busy pulse
    ended, stale 'active work' labels cleared) instead of pulsing over
    a finished failed batch."""
    path = tmp_path / "saturday.avi"
    path.write_bytes(b"x")
    client = _ScriptedClient()
    client.make_job(
        str(path),
        [
            {"state": "queued"},
            {"state": "running", "current_phase": "probe"},
            {
                "state": "failed",
                "current_phase": "probe",
                "error": _PROBE_ERROR,
            },
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
    wait_for_checks(win)
    win.start_subtitles()  # staged Add is submitted only on explicit Start
    assert win.job_ids == ["VJ1"]

    # Tick 1: queued — the slate pulses like a real run start.
    win._tick_elapsed()
    assert _row_state(win, "saturday.avi") == "queued"
    assert win.stage_bar._busy and win.overall_bar._busy

    # Tick 2: running at probe — the stage line names the phase and the
    # pulse labels name it too (the states the pre-fix code froze).
    win._tick_elapsed()
    assert "probe" in win.stage_label.text()
    assert win.stage_bar.eta_label.text() == "probing ..."
    assert win.overall_bar._busy

    # Tick 3: the probe fails; the batch drains.
    win._tick_elapsed()
    assert win.job_ids == []
    assert _row_state(win, "saturday.avi") == "failed"

    # the bars STOP: no busy pulse, no stale active-work label —
    # determinate idle instead of an infinite "probing ..."/"queue
    # active ..." animation over a finished batch
    assert win.stage_bar._busy is False
    assert win.overall_bar._busy is False
    assert win.stage_bar.eta_label.text() == ""
    assert win.overall_bar.eta_label.text() == ""
    assert win.stage_bar.bar.maximum() != 0  # out of the 0..0 pulse
    assert win.overall_bar.bar.maximum() != 0
    # the rest of the landing (already correct pre-fix, pinned here so
    # the whole terminal truth story lives in one test)
    assert win.progress.format() == "0/1 succeeded, 1 failed"
    assert win.activity_dots._timer.isActive() is False
    assert win._run_started_at is None
    assert "Batch finished with failures" in win.summary_count.text()

    # the DETAILS panel landed failed too: body re-rendered from the
    # failed verdict cell, live overlay terminal + the failure reason
    details = win.details.toPlainText()
    assert "Verdict: failed" in details
    assert "Verdict: running" not in details
    assert "State: failed" in details
    assert "running" not in details.split("── LIVE")[1].splitlines()[1], (
        f"stale running text in the live overlay:\n{details}"
    )
    assert "failed — failed" not in details  # no terminal stutter
    assert f"Error: {_PROBE_ERROR}" in details
    next(gen, None)


def test_upscale_probe_failure_details_surface_error(qapp, monkeypatch, tmp_path):
    """Mode A variant (defect 2): a plan-batch run job failing at probe
    lands the same overlay truth — State: failed + the verbatim error —
    so the failure reason is visible on the classic upscale path too."""
    plan = _plan_for(tmp_path, "movie.avi")
    client = _ScriptedClient()
    client.make_job(
        plan.report.path,
        [
            {"state": "queued"},
            {"state": "running", "current_phase": "probe"},
            {
                "state": "failed",
                "current_phase": "probe",
                "error": _PROBE_ERROR,
            },
        ],
        kind="run",
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.start_upscale()
    assert win.job_ids == ["VJ1"]

    win._tick_elapsed()  # queued
    win._tick_elapsed()  # running at probe (follow selects the row)
    win._tick_elapsed()  # terminal failed

    assert win.job_ids == []
    assert _row_state(win, "movie.avi") == "failed"
    details = win.details.toPlainText()
    assert "State: failed" in details
    assert f"Error: {_PROBE_ERROR}" in details
    # the bars settle on the upscale path the same way
    assert win.stage_bar._busy is False
    assert win.overall_bar._busy is False
    next(gen, None)


def test_batch_end_keeps_measured_truth_but_ends_the_pulse(
    qapp, monkeypatch, tmp_path
):
    """The settle is honest, not destructive: a failure AFTER a measured
    fraction (an upscale that died mid-encode) ends the pulse but KEEPS
    the last-measured values on both bars (F-4: never recede, never
    fabricate) — only the pulse/stale labels end; nothing resets to a
    fabricated zero."""
    plan = _plan_for(tmp_path, "measured.avi")
    client = _ScriptedClient()
    client.make_job(
        plan.report.path,
        [
            {"state": "queued"},
            {
                "state": "running", "current_phase": "upscale",
                "progress": {"frames_done": 100, "frames_total": 800},
            },
            {
                "state": "failed", "current_phase": "encode",
                "error": _PROBE_ERROR,
            },
        ],
        kind="run",
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.batch = BatchPlan([plan])
    win.render_batch(win.batch)
    win.start_upscale()
    win._tick_elapsed()  # queued
    win._tick_elapsed()  # measured upscale fraction lands
    measured_stage = win.stage_bar.bar.value()
    measured_overall = win.overall_bar.bar.value()
    assert measured_stage == 125  # 100/800 of the 0..1000 range
    win._tick_elapsed()  # failure lands, batch drains
    assert win.stage_bar._busy is False
    assert win.overall_bar._busy is False
    # the measured truth did NOT recede at batch end
    assert win.stage_bar.bar.value() >= measured_stage
    assert win.overall_bar.bar.value() >= measured_overall
    assert "Verdict: failed" not in win.details.toPlainText() or (
        "Error:" in win.details.toPlainText()
    )  # error surfaces over the plan body when the row is selected
    next(gen, None)


def test_queued_cancel_batch_settles_bars_too(qapp, monkeypatch, tmp_path):
    """The cancel path (v0.4.16.0's fix) stays intact AND settles: a
    queued-only batch cancelled before any claim ends with all rows
    'cancelled', the batch bar honest, and the bars OUT of the pulse
    they had been in since the slate start ('starting ...'/'queue
    active ...' labels cleared — no finished cancel keeps animating)."""
    first = tmp_path / "queued-one.mkv"
    second = tmp_path / "queued-two.mkv"
    for path in (first, second):
        path.write_bytes(b"x")
    client = _ScriptedClient()
    client.make_job(str(first), [{"state": "queued"}], kind="subs")
    client.make_job(str(second), [{"state": "queued"}], kind="subs")

    import vidlift.gui as gui_mod

    monkeypatch.setattr(
        gui_mod.QFileDialog, "getOpenFileNames",
        staticmethod(lambda *a, **k: ([str(first), str(second)], "")),
    )

    gen = _window(qapp, monkeypatch, tmp_path, client)
    win = next(gen)
    win.add_subs_button.click()
    wait_for_checks(win)
    win.start_subtitles()  # staged Add is submitted only on explicit Start
    assert win.cancel_batch_button.isEnabled()
    win.cancel_batch_button.click()
    for _ in range(4):
        win._tick_elapsed()
        if not win.job_ids:
            break
    assert win.job_ids == []
    assert _row_state(win, "queued-one.mkv") == "cancelled"
    assert _row_state(win, "queued-two.mkv") == "cancelled"
    assert win.progress.format() == "0/2 succeeded, 2 cancelled"
    # the cancelled batch stopped animating as well — the same settle
    # contract the failed batch now carries (pulse-only batch ends)
    assert win.stage_bar._busy is False
    assert win.overall_bar._busy is False
    assert win.stage_bar.eta_label.text() == ""
    assert win.overall_bar.eta_label.text() == ""
    next(gen, None)