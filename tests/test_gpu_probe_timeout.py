# v0.4.5 slice 2b — the GPU probe frame's hard timeout (D-probe-hang).

# The 2026-10-20 live-run verdict: a hung ncnn engine probe kept the
# packaged GUI silent for ~3 hours (GPU resident-but-idle, no log line,
# no step — the probe emits neither by design). The bound must live at
# the SUBPROCESS level (_probe_run's terminate -> kill reaping, every
# wait timed — bare subprocess.run does an untimed communicate() after
# its kill, which is how the hang slipped past timeout=180), the
# timeout must be named honestly ("probe frame timed out after Ns" +
# the stderr line), and select_engine's EXISTING auto path lands on
# the stub — the run ships fail-open instead of hanging forever.

from __future__ import annotations

import subprocess
import sys
import time

import pytest

import vidlift.engine_ncnn as engine_ncnn


def test_gpu_probe_timeout_reaps_falls_back_and_logs(
    monkeypatch, capsys
):
    """2b: a probe past its bound (1) is reaped at the subprocess level
    within the bound — not at the child's leisure, (2) reads as honest
    data: (False, 'probe frame timed out after Ns') plus the stderr
    line 'engine probe timed out after Ns', cached so later auto
    selections in the same process skip the dead probe, and (3) sends
    select_engine's auto path to the stub with that exact note."""
    # ---- (1) the real reaper: bounded even against a sleeping child
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        engine_ncnn._probe_run(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=0.5,
        )
    elapsed = time.monotonic() - started
    # the child wanted 30 s; the bound is 0.5 s + the bounded escalation
    assert elapsed < 20

    # ---- (2) gpu_probe_frame names the timeout honestly
    monkeypatch.setattr(engine_ncnn, "_PROBE_CACHE", {})
    calls: list[int] = []

    def hanging_run(cmd, *, capture_output, text, timeout, **kwargs):
        calls.append(1)
        raise subprocess.TimeoutExpired(cmd, timeout)

    ok, reason = engine_ncnn.gpu_probe_frame(
        "/nonexistent-ncnn/engine",
        "/nonexistent/models",
        run_fn=hanging_run,
        timeout_s=120.0,
    )
    assert ok is False
    assert reason == "probe frame timed out after 120s"
    out = capsys.readouterr()
    assert "engine probe timed out after 120s" in out.err
    # the timeout result is cached per (binary, models_dir) — the SAME
    # process never re-pays the dead probe (pre-existing cache rule)
    ok2, reason2 = engine_ncnn.gpu_probe_frame(
        "/nonexistent-ncnn/engine",
        "/nonexistent/models",
        run_fn=hanging_run,
        timeout_s=120.0,
    )
    assert (ok2, reason2) == (ok, reason) and len(calls) == 1

    # ---- (3) select_engine's auto path falls to the stub on timeout
    # (resolution seams faked so the ONLY failure is the probe timeout)
    monkeypatch.setattr(
        engine_ncnn, "resolve_realesrgan", lambda binary=None: "/fake/ncnn"
    )
    monkeypatch.setattr(
        engine_ncnn,
        "resolve_realesrgan_models",
        lambda binary, models_dir=None: "/fake/models",
    )
    monkeypatch.setattr(
        engine_ncnn, "verify_model_artifacts", lambda *a, **k: None
    )
    monkeypatch.setattr(
        engine_ncnn,
        "gpu_probe_frame",
        lambda binary, models, **k: (False, "probe frame timed out after 120s"),
    )
    engine, note = engine_ncnn.select_engine("auto")
    from vidlift.run import StubEngine

    assert isinstance(engine, StubEngine)
    assert note == "auto: stub engine (probe frame timed out after 120s)"