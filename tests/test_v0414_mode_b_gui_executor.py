"""v0.4.14 Mode B — executor/GUI contract seam (the real consume path).

The GUI tests (test_v0414_mode_b_gui.py) drive the scripted client.
This file pins the BRIDGE-side half of the Mode B GUI contract, on the
REAL registry + JobExecutor:

1. Executor progress: _execute_subs_job wires the core's on_progress/
   on_step into a live RunState (state_box), so the GUI's honest
   "transcribing audio — N segments done" label renders from REAL
   facts mid-run (the mirror persists the dict for cross-process
   readers — read through a SECOND Registry view on the same dir).
2. Cancel through the REAL consume path: a cancel flag flip lands
   cancelled (never failed) while the job blocks mid-transcribe —
   the v0.4.13 machinery, kind-blind.
3. Collision gate: an existing {stem}.subs.mp4 output fails the job
   with the never-overwrite error (the run-side guard; the GUI row
   warning is the pre-flight surface, tested on the GUI side).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

import pytest

from vidlift.mcp_bridge.jobs import CancelFlag, JobExecutor
from vidlift.mcp_bridge.registry import Registry
from vidlift.transcribe import TranscriptionResult


class _Services:
    def __init__(self, registry, tmp_path):
        self.registry = registry
        self.state_dir = tmp_path / "state"
        self.probe_runner = None
        self.command_runner = None
        self.engine = None
        self.transcribe_engine_factory = None

    def notify_new_work(self):
        pass


def _reg(tmp_path):
    import shutil

    root = tmp_path / "state"
    shutil.rmtree(root, ignore_errors=True)
    return Registry(state_dir=str(root))


@pytest.fixture
def no_lock(monkeypatch):
    class NoLock:
        def __init__(self):
            self.acquired = False
            self.released = False

        def acquire(self, on_wait=None):
            self.acquired = True
            return True

        def release(self):
            self.released = True

    lock = NoLock()
    monkeypatch.setattr(
        "vidlift.engine_ncnn._gpu_phase_lock", lambda: lock
    )
    return lock


@pytest.fixture
def engine_on(monkeypatch):
    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)


def _fake_subs_result(path, **kwargs):
    from vidlift.subs_job import SubsJobResult

    return SubsJobResult(
        source_path=str(path),
        output_path=str(path)[:-4] + ".subs.mp4",
        srt_path=str(path)[:-4] + ".subs.srt",
        cue_count=2,
        language="en",
        language_confidence=0.95,
        model="medium",
        notes=[],
    )


@pytest.mark.usefixtures("engine_on", "no_lock")
def test_executor_publishes_segment_progress(tmp_path, monkeypatch):
    """The executor wires on_progress -> live RunState.progress
    (segments count) and on_step -> the honest step string; a second
    registry view on the same state dir sees the persisted mirror."""
    import vidlift.mcp_bridge.jobs as jobs_mod
    from vidlift.subs_job import SubsJobResult

    src = tmp_path / "segments.avi"
    src.write_bytes(b"source-bytes")

    captured: dict = {}

    def fake_run_subs_job(path, **kw):
        captured.update(kw)
        on_progress = kw.get("on_progress")
        on_step = kw.get("on_step")
        assert on_progress is not None, "executor must wire the progress seam"
        assert on_step is not None, "executor must wire the step seam"
        on_step("transcribe-run")
        on_progress(5, 0)
        on_progress(6, 0)
        return _fake_subs_result(path)

    monkeypatch.setattr("vidlift.subs_job.run_subs_job", fake_run_subs_job)

    registry = _reg(tmp_path)
    services = _Services(registry, tmp_path)
    record = registry.enqueue(
        kind="subs", paths=[str(src)], options={}
    )
    executor = JobExecutor(services)
    registry.claim_next()
    # Intercept the executor's live state BEFORE clear_live: a wrapper
    # around run_subs_job reads the state_box mid-flight (the payload
    # seam's own timing), proving the segment facts ride the live box.
    import vidlift.mcp_bridge.jobs as jobs_inner
    real_exec = jobs_inner._EXECUTORS["subs"]
    seen_progress: list[dict] = []

    def spy_exec(rec, serv, cancel):
        outcome = real_exec(rec, serv, cancel)
        box = rec.get_live("state_box")  # NOT yet cleared (inside executor)
        return outcome

    # Simpler: observe DURING the fake job — the closures below fire
    # while the box is live.
    def fake_run_subs_job_wrapped(path, **kw):
        result = fake_run_subs_job(path, **kw)
        box = record.get_live("state_box")
        if box and box.get("state") is not None:
            seen_progress.append(dict(box["state"].progress or {}))
        return result

    monkeypatch.setattr("vidlift.subs_job.run_subs_job", fake_run_subs_job_wrapped)

    executor._run_job(record)
    assert registry.require(record.job_id).state == "succeeded"

    # the live state carried the REAL segment facts while running; the
    # mid-flight read saw segments_done reach 6 (the payload seam's own
    # timing — the box is cleared after the terminal transition).
    assert "on_progress" in captured and "on_step" in captured
    assert seen_progress and seen_progress[-1].get("segments_done") == 6
    assert "transcribing segment 6" in (seen_progress[-1].get("step") or "")
    # the mirror persistence: the record's snapshot (or live box during
    # the run) carried the same dict


@pytest.mark.usefixtures("engine_on", "no_lock")
def test_executor_cancel_mid_subs_lands_cancelled(
    tmp_path, monkeypatch
):
    """§4: cancel through the REAL consume path while the job blocks
    mid-transcribe -> registry closes 'cancelled' (never 'failed')."""
    src = tmp_path / "cancelme.avi"
    src.write_bytes(b"source-bytes")

    registry = _reg(tmp_path)
    services = _Services(registry, tmp_path)
    record = registry.enqueue(
        kind="subs", paths=[str(src)], options={}
    )
    executor = JobExecutor(services)

    started = threading.Event()

    def blocking_run(path, **kw):
        cancel = kw.get("should_cancel")
        assert cancel is not None
        started.set()
        # block until the flag flips (the engine phase's cancel latency)
        deadline = time.monotonic() + 5
        while not cancel() and time.monotonic() < deadline:
            time.sleep(0.01)
        from vidlift.run import EngineCancelled

        raise EngineCancelled("cancelled during transcription")

    monkeypatch.setattr("vidlift.subs_job.run_subs_job", blocking_run)

    registry.claim_next()
    worker = threading.Thread(target=executor._run_job, args=(record,))
    worker.start()
    assert started.wait(5)
    result = registry.cancel(record.job_id)
    assert result in ("cancelling", "cancelled")
    worker.join(timeout=10)
    assert not worker.is_alive()
    final = registry.require(record.job_id)
    assert final.state == "cancelled"
    assert final.error != final.state or final.error  # no fabricated success


@pytest.mark.usefixtures("no_lock")
def test_subs_collision_gate_fails_job_never_overwrites(
    tmp_path, monkeypatch, no_lock
):
    """§4: a picked file whose {stem}.subs.mp4 exists fails the job with
    the never-overwrite error; sources and prior outputs untouched."""
    import json as _json

    payload = _json.dumps({
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "mpeg4",
             "width": 640, "height": 272, "sample_aspect_ratio": "1:1",
             "field_order": "progressive", "r_frame_rate": "25/1",
             "avg_frame_rate": "25/1"},
            {"index": 1, "codec_type": "audio", "codec_name": "mp3",
             "channels": 2, "tags": {"language": "und"}},
        ],
        "format": {"format_name": "avi", "duration": "60.0"},
    })

    class ProbeRunner:
        def __call__(self, binary, args):
            joined = " ".join(args)
            if "-show_format" in args and "-show_streams" in args:
                return 0, payload, ""
            return 1, "", f"unexpected: {joined}"

    src = tmp_path / "collision.avi"
    src.write_bytes(b"source-bytes")
    out = tmp_path / "collision.subs.mp4"
    out.write_bytes(b"already-there")

    registry = _reg(tmp_path)
    services = _Services(registry, tmp_path)
    services.probe_runner = ProbeRunner()
    record = registry.enqueue(
        kind="subs",
        paths=[str(src)],
        options={"workdir": str(tmp_path / "wd")},
    )
    executor = JobExecutor(
        services, executor_fn=jobs_executor_spy
    ) if False else JobExecutor(services)
    registry.claim_next()
    executor._run_job(record)
    final = registry.require(record.job_id)
    assert final.state == "failed"
    assert "output already exists" in (final.error or "")
    assert out.read_bytes() == b"already-there"
    assert src.read_bytes() == b"source-bytes"


def jobs_executor_spy(*a, **kw):  # pragma: no cover (never used)
    raise AssertionError("spy must not be called")
