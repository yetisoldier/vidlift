# v0.4.11 regression — the GPU meter's nvidia-smi probe must never
# open a console window on Windows.

# The 2026-10-29 live verdict (Eric, NINJABASE2, minutes after
# upgrading to v0.4.10): the meter's new child process flashed a
# visible conhost window twice per second, because GpuProbeRunnable's
# raw subprocess.Popen bypassed the v0.4.4 rule (vidlift/_proc.py: any
# new external-command call MUST route its subprocess kwargs through
# popen_kwargs()). Two regression layers live here so this cannot
# recur quietly:

#   1. A source-contract test (runs on EVERY platform, no nvidia-smi
#      needed): gpu_meter.py must reference popen_kwargs and the
#      _probe body's Popen call must carry a ** unpacked kwargs (the
#      popen_kwargs spread), asserted via AST so it survives
#      reformatting.
#   2. A Windows-only behavioral test (skipped elsewhere): running the
#      real _probe end-to-end with a monkeypatched Popen capturer, the
#      kwargs handed to subprocess.Popen must carry a STARTUPINFO with
#      STARTF_USESHOWWINDOW (SW_HIDE) and CREATE_NO_WINDOW — the exact
#      suppression _proc.popen_kwargs() produces.

from __future__ import annotations

import ast
import os
import sys

import pytest

import vidlift._proc as _proc
from vidlift.ui import gpu_meter


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METER_SOURCE_PATH = os.path.join(REPO_ROOT, "vidlift", "ui", "gpu_meter.py")


def _meter_source() -> str:
    with open(METER_SOURCE_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


def _popen_calls_in_function(tree: ast.AST, name: str) -> list[ast.Call]:
    """Every subprocess.Popen(...) call inside the named function of
    the meter module (AST-level, formatting-independent)."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                is_popen = (
                    isinstance(func, ast.Attribute) and func.attr == "Popen"
                ) or (isinstance(func, ast.Name) and func.id == "Popen")
                if is_popen:
                    calls.append(call)
    return calls


# ---------------------------------------------------------------------------
# Layer 1: source contract (platform-independent)
# ---------------------------------------------------------------------------


def test_meter_module_routes_probe_through_popen_kwargs() -> None:
    """The meter module must import and route through popen_kwargs at
    all — the v0.4.10 defect was a module that simply forgot the
    _proc convention existed."""
    source = _meter_source()
    assert "popen_kwargs" in source, (
        "vidlift/ui/gpu_meter.py no longer references popen_kwargs; the "
        "GPU probe has drifted from the _proc console-suppression "
        "convention and will re-flash console windows on Windows."
    )


def test_probe_popen_call_unpacks_popen_kwargs() -> None:
    """GpuProbeRunnable._probe's Popen call must carry a ** unpacked
    kwargs (the popen_kwargs spread) — not merely an unused
    reference, and no second bare Popen call alongside it."""
    tree = ast.parse(_meter_source(), filename=METER_SOURCE_PATH)
    calls = _popen_calls_in_function(tree, "_probe")
    assert calls, "GpuProbeRunnable._probe lost its Popen call entirely"
    for call in calls:
        # a keyword whose arg is None is a **spread in AST terms
        spreads = [kw for kw in call.keywords if kw.arg is None]
        assert spreads, (
            "A subprocess.Popen call in GpuProbeRunnable._probe no longer "
            "spreads **popen_kwargs(); the Windows console suppression "
            "was dropped and the v0.4.10 conhost-flash regression returns."
        )


def test_run_body_has_no_other_bare_popen() -> None:
    """The runnable's run() must not spawn anything itself either —
    every spawn path of the meter goes through the suppressed _probe."""
    tree = ast.parse(_meter_source(), filename=METER_SOURCE_PATH)
    assert _popen_calls_in_function(tree, "run") == [], (
        "GpuProbeRunnable.run() spawned a child directly instead of "
        "routing through the suppressed _probe()."
    )


def test_popen_kwargs_is_noop_off_windows() -> None:
    """The convention must stay a pure additive no-op here on Linux so
    the fixed probe's call is byte-identical to the pre-fix one."""
    if os.name == "nt":
        pytest.skip("non-Windows-only contract check")
    assert _proc.popen_kwargs() == {}


# ---------------------------------------------------------------------------
# Layer 2: Windows-only behavioral proof (skipped on other platforms)
# ---------------------------------------------------------------------------


def _capture_popen(monkeypatch, real_popen) -> dict:
    """Swap subprocess.Popen for a pass-through recorder returning the
    captured kwargs dict (filled only when the meter is run)."""
    captured: dict = {}

    def recording_popen(*args, **kwargs):
        captured.update(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    return captured


@pytest.mark.skipif(os.name != "nt", reason="Windows-only behavior")
def test_windows_probe_popen_kwargs_suppress_console(monkeypatch) -> None:
    """On Windows, run the REAL GpuProbeRunnable._probe end-to-end
    while recording the Popen kwargs: startupinfo must carry
    STARTF_USESHOWWINDOW (SW_HIDE) and creationflags must include
    CREATE_NO_WINDOW. The command itself may fail (nvidia-smi is not
    faked here) — the suppression contract is about the SPAWN."""
    import subprocess  # local import so the capture target is the same module object

    real_popen = subprocess.Popen
    captured = _capture_popen(monkeypatch, real_popen)

    # sys.executable is a present, spawnable binary on every Windows
    # box; the query args make it exit nonzero fast (usage error), so
    # the probe resolves to the honest n/a sample — but the SPAWN
    # kwargs are still recorded before that.
    runnable = gpu_meter.GpuProbeRunnable(sys.executable)
    sample = runnable._probe()

    assert "startupinfo" in captured, (
        "GPU probe passed no startupinfo: the child console window "
        "flashes on Windows (the v0.4.10 regression path)."
    )
    si = captured["startupinfo"]
    assert isinstance(si, subprocess.STARTUPINFO)
    assert si.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert si.wShowWindow == subprocess.SW_HIDE
    assert captured.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW
    # the probe still resolves honestly either way (n/a or a value)
    assert isinstance(sample, gpu_meter.GpuSample)


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows-only proof against the real nvidia-smi binary",
)
def test_windows_real_nvidia_smi_probe_suppresses(monkeypatch) -> None:
    """Strongest proof when nvidia-smi exists on the Windows box: the
    REAL binary is probed end-to-end and the recorded kwargs still
    carry the full console suppression."""
    import shutil
    import subprocess

    binary = shutil.which("nvidia-smi")
    if binary is None:
        pytest.skip("nvidia-smi not installed on this Windows box")

    real_popen = subprocess.Popen
    captured = _capture_popen(monkeypatch, real_popen)

    runnable = gpu_meter.GpuProbeRunnable(binary)
    sample = runnable._probe()

    si = captured["startupinfo"]
    assert si.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert si.wShowWindow == subprocess.SW_HIDE
    assert captured["creationflags"] & subprocess.CREATE_NO_WINDOW
    # a real, working nvidia-smi answers with a usable sample
    assert sample.tool_available