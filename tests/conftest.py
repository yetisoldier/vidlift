"""Shared test fixtures.

One autouse guard: auto engine selection must resolve to the STUB in
every test on every box. Without this, a machine with a bundled
tools/realesrgan/ layout (Eric's Windows checkout after the packaging
milestone) would make ``run_file(engine_request="auto")`` launch REAL
engine probe frames inside unit tests. The guard pins
VIDLIFT_REALESRGAN_NCNN to a nonexistent path so ``select_engine``
deterministically falls to the stub with its named reason; tests that
exercise real resolution set their own env/binary explicitly and
override this per-test.
"""

from __future__ import annotations

import pytest

_GUARD_PATH = "/nonexistent/vidlift-test-guard/realesrgan-ncnn-vulkan"


@pytest.fixture(autouse=True)
def _auto_engine_guard(monkeypatch):
    monkeypatch.setenv("VIDLIFT_REALESRGAN_NCNN", _GUARD_PATH)


@pytest.fixture(autouse=True)
def _no_monitor_autostart(monkeypatch):
    # v0.4.6: the GUI's LAN monitor ships ON by default; tests isolate
    # QSettings but a construction-time listener must never open in a
    # suite run (nor write the port fallback back to real user
    # QSettings). Monitor-specific tests re-enable per-test.
    monkeypatch.setenv("VIDLIFT_MONITOR_AUTOSTART", "0")