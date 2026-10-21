"""v0.4.6 GUI monitor-wiring smoke tests (design §4).

Uses the REAL LocalBridgeClient (tmp state dir, executor stopped) —
the monitor serves through StatusView, so health/status need the real
client read surface; the FakeClient seam has none. QSettings is
isolated to a tmp ini per test (the test_gui.py R3 pattern), so the
default-on start and toggle persistence never touch real user state.
"""

from __future__ import annotations

import socket
import urllib.request

import pytest

pytest.importorskip("PyQt6")

from vidlift.mcp_bridge.client_local import LocalBridgeClient
from vidlift.monitor import MONITOR_TOKEN_DEFAULT, MonitorServer, stop_server


@pytest.fixture(autouse=True)
def _no_leaked_listener():
    yield
    stop_server()


@pytest.fixture(autouse=True)
def _restore_autostart(monkeypatch):
    """Monitor tests run the REAL wiring: unset the conftest default
    off-switch (VIDLIFT_MONITOR_AUTOSTART=0), so default-on behavior
    is actually exercised here."""
    monkeypatch.delenv("VIDLIFT_MONITOR_AUTOSTART", raising=False)


@pytest.fixture
def qapp():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def real_client(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    yield client
    client.stop()


def _ini(tmp_path):
    from PyQt6.QtCore import QSettings

    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


def _isolate_settings(monkeypatch, tmp_path):
    from vidlift.gui import MainWindow

    ini = _ini(tmp_path)

    def _settings(self):
        return ini

    monkeypatch.setattr(MainWindow, "_settings", _settings)
    return ini


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _window(qapp, real_client, tmp_path, monkeypatch, enabled=True):
    from vidlift.gui import MainWindow

    ini = _isolate_settings(monkeypatch, tmp_path)
    ini.setValue("monitor/port", _free_port())
    ini.setValue("monitor/enabled", enabled)
    ini.setValue("monitor/host", "127.0.0.1")
    ini.sync()
    window = MainWindow(client=real_client)
    yield window
    window.job_ids = []
    window.close()
    qapp.processEvents()


def test_default_on_starts_server_and_close_stops_it(qapp, real_client, tmp_path, monkeypatch):
    from vidlift import monitor as monitor_mod

    window = next(_window(qapp, real_client, tmp_path, monkeypatch, enabled=True))
    assert monitor_mod.active_server is not None
    assert window.monitor_check.isChecked() is True
    assert window.monitor_pill.text().startswith("Monitor on: ")
    # token-free liveness through the real socket
    host, port = monitor_mod.active_server.server_address
    with urllib.request.urlopen(
        f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}/health", timeout=5
    ) as response:
        assert response.status == 200
    window.close()
    qapp.processEvents()
    assert monitor_mod.active_server is None
    assert window.monitor_pill.text() == "Monitor off"


def test_disabled_setting_leaves_monitor_off(qapp, real_client, tmp_path, monkeypatch):
    from vidlift import monitor as monitor_mod

    window = next(_window(qapp, real_client, tmp_path, monkeypatch, enabled=False))
    assert monitor_mod.active_server is None
    assert window.monitor_check.isChecked() is False
    assert window.monitor_pill.text() == "Monitor off"


def test_conftest_default_blocks_autostart_in_plain_gui_tests(
    qapp, real_client, tmp_path, monkeypatch
):
    """The suite-wide off-switch: outside the monitor tests, GUI tests
    never open a listener (explicit env wins over the on-default)."""
    monkeypatch.setenv("VIDLIFT_MONITOR_AUTOSTART", "0")
    from vidlift import monitor as monitor_mod

    window = next(_window(qapp, real_client, tmp_path, monkeypatch, enabled=True))
    assert monitor_mod.active_server is None
    assert window.monitor_check.isChecked() is True
    assert window.monitor_pill.text() == "Monitor off"


def test_checkbox_toggle_persists_and_drives_server(qapp, real_client, tmp_path, monkeypatch):
    from PyQt6.QtCore import QSettings
    from vidlift import monitor as monitor_mod

    window = next(_window(qapp, real_client, tmp_path, monkeypatch, enabled=False))
    window.monitor_check.setChecked(True)
    qapp.processEvents()
    assert monitor_mod.active_server is not None
    ini = _ini(tmp_path)
    assert ini.value("monitor/enabled") in (True, "true")
    window.monitor_check.setChecked(False)
    qapp.processEvents()
    assert monitor_mod.active_server is None
    assert ini.value("monitor/enabled") in (False, "false")
    assert window.monitor_pill.text() == "Monitor off"