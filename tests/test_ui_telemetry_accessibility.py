"""Accessible textual equivalents for progress and GPU telemetry."""

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication

from vidlift.ui.gpu_meter import GpuMeterWidget, GpuSample
from vidlift.ui.widgets import EtaProgressBar


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_eta_accessible_busy_measured_and_unknown_states(qapp):
    parent = EtaProgressBar()
    bar = EtaProgressBar(parent)
    assert bar.parent() is parent
    assert bar.accessibleName() == "Progress"
    assert "unknown" in bar.accessibleDescription()
    assert "0%" not in bar.accessibleDescription()

    bar.set_busy(True, "checking source ...")
    assert bar.bar.maximum() == 0
    assert "checking source" in bar.bar.accessibleDescription()
    assert "0%" not in bar.accessibleDescription()

    bar.set_value(0.42, 31)
    assert bar.bar.maximum() == 1000
    assert "Measured progress: 42 percent" in bar.accessibleDescription()
    assert "ETA 0:31" in bar.accessibleDescription()

    bar.reset()
    assert "unknown" in bar.accessibleDescription()
    assert "0%" not in bar.accessibleDescription()


def test_gpu_accessible_description_without_sampler_or_events(qapp, monkeypatch):
    from vidlift.ui import gpu_meter

    monkeypatch.setattr(gpu_meter.shutil, "which", lambda _name: None)
    meter = GpuMeterWidget()
    meter.start = lambda: None  # prohibit starting a real sampler
    assert meter.accessibleName() == "NVIDIA GPU utilization"
    assert meter.accessibleDescription() == "NVIDIA GPU utilization unavailable"

    meter._accept_sample(GpuSample(73, 2, True))
    assert meter.accessibleDescription() == "NVIDIA GPU utilization 73 percent, 2 devices"
    meter._accept_sample(GpuSample(None, 0, False))
    assert meter.accessibleDescription() == "NVIDIA GPU utilization unavailable"
    meter.stop()
