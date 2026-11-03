"""v0.4.16 (Karen remediation D-5/D-6/D-7) — the visibility spec's
D-1..D-6 discovery-card matrix: ``<state_dir>/monitor.json`` (ADR-2's
provisioning artifact, written by ``MainWindow._write_monitor_discovery``).

Karen's validation found ZERO suite tests for this matrix — a
secret-bearing provisioning artifact shipped untested, and her probe 4
had to execute the matrix itself (it found D-5: the card written
world/group-readable) plus the fixed ``monitor.json.tmp`` staging name
(D-6, two GUI instances can interleave). This file lands the matrix on
the REAL window wiring (test_monitor_gui's harness: real
LocalBridgeClient onto a tmp state dir, QSettings isolated per test,
loopback-only host) and adds the D-5/D-6 regression pins:

  D-1 default start  -> fields + atomic staging + owner-only 0o600
  D-2 port fallback  -> card carries the ACTUAL bound port, matching
                        the persisted QSettings port
  D-3 toggled off    -> enabled false, port/lan_base_url null
  D-4 token override -> card carries the override, source "qsettings"
  D-5 empty token    -> card "" + source "empty-open"; open bind
  D-6 card-write failure -> swallowed, never takes the monitor down
"""

from __future__ import annotations

import json
import os
import socket
import stat
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

import vidlift.config as config
from vidlift import __version__
from vidlift.mcp_bridge.client_local import LocalBridgeClient
from vidlift.monitor import MONITOR_TOKEN_DEFAULT, stop_server

QK = config.QSETTINGS_MONITOR_KEYS


@pytest.fixture(autouse=True)
def _no_leaked_listener():
    yield
    stop_server()


@pytest.fixture(autouse=True)
def _restore_autostart(monkeypatch):
    """Monitor tests run the REAL wiring: unset the conftest default
    off-switch (VIDLIFT_MONITOR_AUTOSTART=0), so default-on behavior is
    actually exercised here."""
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


# ---- helpers -------------------------------------------------------------


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
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_window(
    qapp, real_client, tmp_path, monkeypatch, *, extra=None
):
    """A REAL MainWindow on the isolated ini (host pinned to loopback)
    — construction starts the monitor per the ini's enabled flag."""
    from vidlift.gui import MainWindow

    ini = _isolate_settings(monkeypatch, tmp_path)
    ini.setValue(QK["port"], _free_port())
    ini.setValue(QK["enabled"], True)
    ini.setValue(QK["host"], "127.0.0.1")
    for key, value in (extra or {}).items():
        ini.setValue(key, value)
    ini.sync()
    return MainWindow(client=real_client)


def _close(window, qapp):
    if window is None:
        return
    window.job_ids = []
    window.close()
    qapp.processEvents()


def _card(tmp_path) -> dict | None:
    path = tmp_path / "monitor.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_no_staging_residue(tmp_path) -> None:
    assert not (tmp_path / "monitor.json.tmp").exists()
    assert list(tmp_path.glob("monitor.json.*.tmp")) == []


def _get(url: str, token: str | None = None) -> tuple[int, str]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=5
        ) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


# ---- the matrix -----------------------------------------------------------


def test_d1_default_start_card_fields_atomic_and_owner_only(
    qapp, real_client, tmp_path, monkeypatch
):
    """D-1 (+Karen's D-5): the default start writes the card with the
    effective default token, the ACTUAL bound port, the LAN base URL,
    and the version stamp; the staging is atomic (no residue of either
    staging shape) and the token-bearing artifact is owner-only 0o600 —
    never group/world-readable (Karen probe 4 saw 0o664 pre-fix)."""
    from vidlift import monitor as monitor_mod

    window = _make_window(qapp, real_client, tmp_path, monkeypatch)
    try:
        card = _card(tmp_path)
        assert card is not None, "no card written at default start"
        served = monitor_mod.active_server.server_address[1]
        assert card["schema"] == config.MONITOR_DISCOVERY_SCHEMA
        assert card["enabled"] is True
        assert card["port"] == served
        assert card["lan_base_url"].endswith(f":{served}")
        assert card["token"] == MONITOR_TOKEN_DEFAULT
        assert card["token_source"] == "default"
        assert card["version"] == __version__
        assert card["bind_host"] == "127.0.0.1"
        _assert_no_staging_residue(tmp_path)
        assert (
            stat.S_IMODE(os.stat(tmp_path / "monitor.json").st_mode) == 0o600
        ), "the card carries the monitor token — it must be owner-only"
        # the served truth gates on the card's own token (auth parity)
        status, _ = _get(f"http://127.0.0.1:{served}/status")
        assert status == 401
        status, body = _get(
            f"http://127.0.0.1:{served}/status", token=card["token"]
        )
        assert status == 200 and "transcription_model" in json.loads(body)
    finally:
        _close(window, qapp)


def test_d2_port_fallback_card_carries_actual_port(
    qapp, real_client, tmp_path, monkeypatch
):
    """D-2: pre-bind the configured port, restart the monitor — the
    card records the ACTUAL fallback port and the QSettings port is
    persisted to match (the next launch is clean)."""
    from vidlift import monitor as monitor_mod

    window = _make_window(qapp, real_client, tmp_path, monkeypatch)
    blocker = None
    try:
        first = _card(tmp_path)
        configured = first["port"]
        assert window.monitor_port_reported == configured
        monitor_mod.stop_server()
        blocker = socket.socket()
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", configured))
        blocker.listen(1)
        window._start_monitor()
        card = _card(tmp_path)
        served = monitor_mod.active_server.server_address[1]
        assert card["port"] == served, "the card carries the ACTUAL port"
        assert served != configured, "the fallback actually shifted"
        assert window.monitor_port_reported == served
        assert int(_ini(tmp_path).value(QK["port"])) == served, (
            "the shifted port is persisted so the next launch is clean"
        )
        _assert_no_staging_residue(tmp_path)
    finally:
        if blocker is not None:
            blocker.close()
        _close(window, qapp)


def test_d3_toggle_off_rewrites_card_and_keeps_owner_only(
    qapp, real_client, tmp_path, monkeypatch
):
    """D-3: toggling the monitor OFF rewrites the card to enabled False
    with null port/LAN URL (a paired watcher never pairs from a stale
    "on" card); the rewritten card stays owner-only."""
    window = _make_window(qapp, real_client, tmp_path, monkeypatch)
    try:
        assert _card(tmp_path)["enabled"] is True
        window.monitor_check.setChecked(False)
        qapp.processEvents()
        card = _card(tmp_path)
        assert card["enabled"] is False
        assert card["port"] is None
        assert card["lan_base_url"] is None
        assert (
            stat.S_IMODE(os.stat(tmp_path / "monitor.json").st_mode) == 0o600
        )
        _assert_no_staging_residue(tmp_path)
    finally:
        _close(window, qapp)


def test_d4_qsettings_token_override_rides_the_card(
    qapp, real_client, tmp_path, monkeypatch
):
    """D-4: a QSettings token override lands in the card with
    ``token_source == "qsettings"`` — and the served monitor accepts
    the override token."""
    override = "d4-override-token"
    window = _make_window(
        qapp, real_client, tmp_path, monkeypatch, extra={QK["token"]: override}
    )
    try:
        card = _card(tmp_path)
        assert card["token"] == override
        assert card["token_source"] == "qsettings"
        # auth parity: the override serves, the default does not
        status, _ = _get(
            f"http://127.0.0.1:{card['port']}/status",
            token=MONITOR_TOKEN_DEFAULT,
        )
        assert status == 401
        status, _ = _get(f"http://127.0.0.1:{card['port']}/status", token=override)
        assert status == 200
    finally:
        _close(window, qapp)


def test_d5_empty_token_open_bind_card_and_canary(
    qapp, real_client, tmp_path, monkeypatch
):
    """D-5 (matrix row; T-7 behavior): a stored EMPTY token opens the
    bind — the card carries "" with ``token_source == "empty-open"`` and
    /status answers WITHOUT auth (behavior unchanged)."""
    window = _make_window(
        qapp, real_client, tmp_path, monkeypatch, extra={QK["token"]: ""}
    )
    try:
        card = _card(tmp_path)
        assert card["token"] == ""
        assert card["token_source"] == "empty-open"
        status, body = _get(f"http://127.0.0.1:{card['port']}/status")
        assert status == 200
        assert "transcription_model" in json.loads(body)
    finally:
        _close(window, qapp)


def test_d6_card_write_failure_never_takes_monitor_down(
    qapp, real_client, tmp_path, monkeypatch
):
    """D-6 (matrix row): a degraded state dir (the injected creation
    failure is the same OSError a read-only dir raises) must NEVER take
    the monitor down — the toggle still starts the server; the card
    write swallows its own failure; the pill reflects serving truth."""
    from vidlift import monitor as monitor_mod

    real_open = os.open

    def failing_card_open(path, flags, *args, **kwargs):
        name = os.fspath(path)
        if "monitor.json" in name and name.endswith(".tmp"):
            raise OSError(13, "unwritable state dir (test injection)")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_card_open)
    window = _make_window(qapp, real_client, tmp_path, monkeypatch)
    try:
        # construction-time card write failed silently; the monitor is ON
        assert monitor_mod.active_server is not None
        assert _card(tmp_path) is None
        # the pill says serving (truth surface unaffected), no crash
        assert window.monitor_pill.text().startswith("Monitor on: ")
        status, _ = _get(
            f"http://127.0.0.1:{window.monitor_port_reported}/status",
            token=MONITOR_TOKEN_DEFAULT,
        )
        assert status == 200
    finally:
        _close(window, qapp)


def test_d6b_staging_name_pid_unique_no_torn_write(
    qapp, real_client, tmp_path, monkeypatch
):
    """Karen's D-6: the staging name is pid-unique — two GUI instances
    sharing one state dir (the registry explicitly supports
    multi-writer convergence) can no longer interleave into ONE fixed
    ``monitor.json.tmp``; ``os.replace`` moves only THIS writer's own
    completed card, so the final artifact is always fully-written JSON.
    The pre-fix fixed name is gone from the staging vocabulary."""
    real_replace = os.replace
    replaces: list[tuple[str, str]] = []

    def recording_replace(src, dst):
        replaces.append((os.fspath(src), os.fspath(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    window = _make_window(qapp, real_client, tmp_path, monkeypatch)
    try:
        replaces.clear()
        monkeypatch.setattr(os, "getpid", lambda: 4321)
        window._write_monitor_discovery(
            enabled=True, port=7001, token="writer-a",
            token_source="default",
        )
        assert Path(replaces[-1][0]).name == "monitor.json.4321.tmp"
        monkeypatch.setattr(os, "getpid", lambda: 8765)
        window._write_monitor_discovery(
            enabled=True, port=7001, token="writer-b",
            token_source="default",
        )
        assert Path(replaces[-1][0]).name == "monitor.json.8765.tmp"
        # the final card is a COMPLETE write from the LAST writer (never
        # a torn interleave), and no staging residue survives
        card = _card(tmp_path)
        assert card["token"] == "writer-b"
        assert card["port"] == 7001
        _assert_no_staging_residue(tmp_path)
    finally:
        _close(window, qapp)