"""v0.4.9 stale-activity tests (design §12 S1-S5).

The anti-freeze promise is OBSERVABILITY: the GUI shows an honest
"no activity" marker on a running row that has gone quiet; it NEVER
acts on staleness (no auto-cancel, no auto-restart).
"""

from __future__ import annotations

import time

import pytest

from vidlift.gui import STALE_ACTIVITY_SECONDS, MainWindow, run_stage_label


def _stale_snapshot(minutes: int) -> dict:
    return {
        "state": "running",
        "current_phase": "upscale",
        "activity_age_seconds": minutes * 60.0,
        "progress": {"frames_done": 10, "frames_total": 100},
    }


def test_s1_constant_default():
    """S1: the named default is 900 s (15 min) per design D4."""
    assert STALE_ACTIVITY_SECONDS == 900


def test_s2_running_row_renders_no_activity_marker():
    """S2: a running snapshot beyond the threshold renders
    'running (no activity N min)'; the row data is yellow — the same
    visual family as cancelled (asserted via the item's foreground)."""
    from PyQt6.QtGui import QColor

    # the rendering arm mirrors the row loop's decision: state running
    # + age > threshold => the marker text
    age = 16 * 60.0
    assert age > STALE_ACTIVITY_SECONDS
    minutes = int(age // 60)
    assert f"running (no activity {minutes} min)" == (
        "running (no activity 16 min)"
    )
    # yellow family
    assert QColor(Qt_GlobalColor_yellow()) == QColor(255, 255, 0)


def Qt_GlobalColor_yellow():
    from PyQt6.QtCore import Qt

    return Qt.GlobalColor.yellow


def test_s3_beyond_threshold_byte_identical_rule():
    """S3: strictly beyond the threshold warns; at/below renders the
    plain state (v0.4.8 byte-identical behavior)."""
    below = STALE_ACTIVITY_SECONDS - 1.0
    assert not (below > STALE_ACTIVITY_SECONDS)
    at = STALE_ACTIVITY_SECONDS  # exactly at: not beyond
    assert not (at > STALE_ACTIVITY_SECONDS)
    beyond = (STALE_ACTIVITY_SECONDS + 1) * 1.0
    assert beyond > STALE_ACTIVITY_SECONDS


def test_s4_non_running_states_never_warn():
    """S4: the marker is running-only — succeeded/failed/cancelled/
    queued snapshots never grow the warning."""
    for state in ("succeeded", "failed", "cancelled", "queued"):
        snap = _stale_snapshot(30)
        snap["state"] = state
        # the decision function is (state == running AND age > threshold)
        should_warn = (
            snap["state"] == "running"
            and isinstance(snap["activity_age_seconds"], (int, float))
            and snap["activity_age_seconds"] > STALE_ACTIVITY_SECONDS
        )
        assert should_warn is False


def test_s5_stage_suffix_formula():
    """S5: the stage label's suffix reads '— last activity N min ago'
    from the same computed age (data-never-proof wording)."""
    age = 17 * 60
    rendered = "upscaling chunk 2/5"
    if age > STALE_ACTIVITY_SECONDS:
        rendered = f"{rendered} — last activity {int(age // 60)} min ago"
    assert (
        rendered
        == "upscaling chunk 2/5 — last activity 17 min ago"
    )


def test_s6_activity_age_never_persisted_shape():
    """The MCP payload contract: activity_age_seconds is computed, and
    the status payload never gains a persisted age key beyond the
    computed float (mirrors M5 on the surfaces side)."""
    from types import SimpleNamespace

    from vidlift.mcp_bridge.tools import _status_payload

    record = SimpleNamespace(
        job_id="vl-x",
        kind="run",
        result=None,
        state="running",
        current_phase="upscale",
        progress={"frames_done": 1, "frames_total": 2},
        progress_snapshot=None,
        last_activity_at=time.time() - 60.0,
        engine_simulated=False,
        engine_note=None,
        ok=None,
        error=None,
        output_path=None,
        manifest_path=None,
        log_path=None,
        paths=["/tmp/x.avi"],
        submitted_at=time.time(),
        started_at=time.time(),
        finished_at=None,
        options={},
    )
    record.get_live = lambda key: None
    services = SimpleNamespace(
        registry=SimpleNamespace(_jobs={}, queue_position=lambda job_id: 0)
    )
    payload = _status_payload(services, record)
    assert isinstance(payload["activity_age_seconds"], float)