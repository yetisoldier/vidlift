"""Real local IPC, no MainWindow, isolated endpoints and synthetic subprocesses."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

import pytest
from PyQt6.QtCore import QCoreApplication
from PyQt6.QtNetwork import QLocalSocket

from _gui_checks import gui_application
from vidlift.ui.single_instance import SingleInstanceGuard, endpoint_name


@pytest.fixture
def instance_root():
    # AF_UNIX paths have a small byte limit; pytest's basetemp can be long.
    base = Path(__file__).resolve().parents[1] / ".forge-t517"
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ip-", dir=base) as name:
        root = Path(name)
        if sys.platform == "linux":
            assert len(os.fsencode(root / "vidlift-gui-v3.sock")) < 108
        yield root


@pytest.fixture
def guard(instance_root):
    app = gui_application()
    instance = SingleInstanceGuard(name="VidLift-test-" + uuid.uuid4().hex,
                                   lock_dir=instance_root, timeout_ms=500)
    assert instance.start(app)
    yield app, instance
    instance.close()
    app.processEvents()


def test_first_owner_second_signals_without_second_server(guard, instance_root):
    app, first = guard
    raised = []
    first.on_raise = lambda: raised.append(True)
    second = SingleInstanceGuard(name=first.name, lock_dir=instance_root, timeout_ms=500)
    assert not second.start(app)
    assert second.status == "signalled"
    assert second.server is None
    assert raised == [True]


@pytest.mark.parametrize("command", [b'garbage\n', b'{"cmd":"delete"}\n',
    b'{"cmd":"raise","path":"bad"}\n', b'{"cmd":"raise","cmd":"raise"}\n', b'x' * 300])
def test_invalid_commands_rejected_owner_survives(guard, command):
    app, owner = guard
    raised = []
    owner.on_raise = lambda: raised.append(True)
    client = QLocalSocket()
    client.connectToServer(owner.name)
    assert client.waitForConnected(500)
    client.write(command)
    client.flush()
    response = bytearray()
    deadline = time.monotonic() + 2
    while b"\n" not in response and time.monotonic() < deadline:
        app.processEvents()
        response.extend(client.readAll().data())
        time.sleep(0.005)
    assert json.loads(response) == {"ok": False, "error": "invalid_command"}
    assert not raised and owner.server.isListening()
    client.abort()


_SCRIPT = '''
import sys
from PyQt6.QtCore import QCoreApplication,QTimer
from vidlift.ui.single_instance import SingleInstanceGuard
app = QCoreApplication([])
g = SingleInstanceGuard(name=sys.argv[1], lock_dir=sys.argv[2], timeout_ms=2000)
owner = g.start(app)
print(g.status, flush=True)
if owner:
    QTimer.singleShot(2500, app.quit)
    app.exec()
    g.close()
sys.exit(0 if owner or g.status == "signalled" else 1)
'''


def process(name, root):
    return subprocess.Popen([sys.executable, "-c", _SCRIPT, name, str(root)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_stale_endpoint_recovered_after_owner_kill(instance_root):
    name = "VidLift-test-" + uuid.uuid4().hex
    owner = process(name, instance_root)
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == "owner"
        owner.kill()  # synthetic fixture process only
        owner.wait(timeout=5)
        app = gui_application()
        replacement = SingleInstanceGuard(name=name, lock_dir=instance_root)
        try:
            assert replacement.start(app)
            assert replacement.status == "owner"
        finally:
            replacement.close()
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)


def test_concurrent_start_exactly_one_owner(instance_root):
    name = "VidLift-test-" + uuid.uuid4().hex
    processes = [process(name, instance_root) for _ in range(4)]
    try:
        results = [p.communicate(timeout=12) for p in processes]
        statuses = [out.strip() for out, err in results]
        assert statuses.count("owner") == 1, results
        assert statuses.count("signalled") == 3, results
        assert all(p.returncode == 0 for p in processes), results
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
                p.communicate(timeout=5)


def test_endpoint_scope_and_cli_exemption():
    facts = {"platform": "win32", "sid": "S-1-5-21-123", "session_id": 1}
    first = endpoint_name(facts_fn=lambda: facts)
    assert first == endpoint_name(facts_fn=lambda: dict(facts))
    assert first != endpoint_name(facts_fn=lambda: dict(facts, session_id=2))
    assert first != endpoint_name(facts_fn=lambda: dict(facts, sid="S-1-5-21-456"))
    assert endpoint_name(facts_fn=lambda: {"platform": "posix", "uid": 42}) == "VidLift-42"
    root = Path(__file__).resolve().parents[1]
    assert "single_instance" not in (root / "run_cli.py").read_text()
    assert "single_instance" not in (root / "vidlift/cli.py").read_text()


def test_unknown_ownership_fails_closed(guard, instance_root, monkeypatch):
    app, owner = guard
    other = SingleInstanceGuard(name=owner.name, lock_dir=instance_root)
    class Denied:
        def error(self):
            return QLocalSocket.LocalSocketError.SocketAccessError
        def abort(self):
            pass
    monkeypatch.setattr(other, "_connect", lambda: (Denied(), False))
    assert not other.start(app)
    assert other.status == "error" and other.error is not None and "will not start" in other.error
    assert owner.server.isListening()
