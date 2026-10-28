"""v0.4.9 MCP-surface tests (design §12 M-arms for the resume option
and the computed activity_age_seconds; the v0.4.8 absent-defaults
rule must hold for every caller that never sends resume)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vidlift.mcp_bridge.schemas import _OPTIONS_RUN as RUN_OPTIONS_SCHEMA


def test_mcp1_resume_in_schema_with_description():
    """The run tool's options schema names `resume` (boolean) — the
    MCP client's discoverability arm (design tri-surface UX)."""
    assert "resume" in RUN_OPTIONS_SCHEMA
    assert RUN_OPTIONS_SCHEMA["resume"]["type"] == "boolean"


def test_mcp2_resume_absent_reads_false_in_worker(monkeypatch, tmp_path):
    """Absent -> False: a v0.4.8-shaped options payload (no resume key)
    threads resume=False into run_file — behavior-identical."""
    captured: dict = {}

    def fake_run_file(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            ok=True,
            state=SimpleNamespace(
                file_path="x",
                manifest_path=None,
                output_path=None,
                error=None,
                fail_open=False,
                fail_open_reason=None,
                engine_simulated=False,
                engine_note=None,
                subtitles={"status": "none"},
                phases=[],
                log=SimpleNamespace(path="log"),
            ),
            error=None,
            plan=SimpleNamespace(output_path="out", expectation="e"),
            verification=SimpleNamespace(ok=True),
            manifest_path=None,
        )

    from vidlift import run as run_mod

    monkeypatch.setattr(run_mod, "run_file", fake_run_file)
    from vidlift.mcp_bridge.jobs import _execute_run_job
    from vidlift.mcp_bridge.registry import JobRecord

    record = JobRecord(job_id="vl-r1", kind="run",
                       paths=["/tmp/x.avi"],
                       options={"log_dir": str(tmp_path / "runs")})
    services = SimpleNamespace(
        engine="stub",
        realesrgan=None,
        realesrgan_models=None,
        command_runner=None,
        probe_runner=None,
        registry=SimpleNamespace(_jobs={}),
    )
    outcome = _execute_run_job(record, services, SimpleNamespace(stopped=False))
    assert captured["resume"] is False


def test_mcp3_resume_true_threads_into_run_file(monkeypatch, tmp_path):
    """options["resume"]=True -> run_file(resume=True): the claim layer
    runs for this job."""
    captured: dict = {}

    def fake_run_file(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            ok=True,
            state=SimpleNamespace(
                file_path="x",
                manifest_path=None,
                output_path=None,
                error=None,
                fail_open=False,
                fail_open_reason=None,
                engine_simulated=False,
                engine_note=None,
                subtitles={"status": "none"},
                phases=[],
                log=SimpleNamespace(path="log"),
            ),
            error=None,
            plan=SimpleNamespace(output_path="out", expectation="e"),
            verification=SimpleNamespace(ok=True),
            manifest_path=None,
        )

    from vidlift import run as run_mod

    monkeypatch.setattr(run_mod, "run_file", fake_run_file)
    from vidlift.mcp_bridge.jobs import _execute_run_job
    from vidlift.mcp_bridge.registry import JobRecord

    record = JobRecord(job_id="vl-r2", kind="run",
                       paths=["/tmp/x.avi"],
                       options={"log_dir": str(tmp_path / "runs"),
                                "resume": True})
    services = SimpleNamespace(
        engine="stub",
        realesrgan=None,
        realesrgan_models=None,
        command_runner=None,
        probe_runner=None,
        registry=SimpleNamespace(_jobs={}),
    )
    outcome = _execute_run_job(record, services, SimpleNamespace(stopped=False))
    assert captured["resume"] is True


def test_mcp4_activity_age_computed_and_absent_for_fresh_jobs():
    """A running job ~0 s old gets a small computed age; a terminal
    job's payload carries NO activity age (the field is computed for
    in-flight observability, never persisted)."""
    from vidlift.mcp_bridge.tools import _activity_age_seconds

    running = SimpleNamespace(
        state="running", last_activity_at=time.time() - 5.0
    )
    age = _activity_age_seconds(running)
    assert age is not None and 4.0 <= age <= 6.0
    # terminal job: the field is None (the payload omits/nulls it)
    done = SimpleNamespace(
        state="succeeded", last_activity_at=time.time() - 900.0
    )
    assert _activity_age_seconds(done) is None


def test_mcp5_status_payload_activity_age_fresh_small():
    """The status payload's computed age rides for RUNNING jobs (the
    GUI's source of the yellow marker)."""
    from vidlift.mcp_bridge.tools import _status_payload

    record = SimpleNamespace(
        job_id="vl-age",
        kind="run",
        result=None,
        state="running",
        current_phase="upscale",
        progress={"frames_done": 1, "frames_total": 2},
        progress_snapshot=None,
        last_activity_at=time.time() - 30.0,
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
    assert 29.0 <= payload["activity_age_seconds"] <= 31.0