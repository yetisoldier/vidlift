"""Isolated Linux IPC tests using real independent Qt processes."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

import pytest
from PyQt6.QtCore import QCoreApplication

from vidlift.ui.single_instance import SingleInstanceGuard, resolve_posix_endpoint

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux runtime namespace')


@pytest.fixture
def root():
    base = Path(__file__).resolve().parents[1] / '.forge-t517'
    base.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ip-', dir=base) as name:
        yield Path(name)


SCRIPT = r'''
import os, sys
from PyQt6.QtCore import QCoreApplication, QTimer, QStandardPaths
from vidlift.ui.single_instance import SingleInstanceGuard
# Force Qt to cache each process's distinct temporary directory before startup.
QStandardPaths.writableLocation(QStandardPaths.StandardLocation.TempLocation)
a = QCoreApplication([])
g = SingleInstanceGuard(runtime_root_provider=lambda uid: sys.argv[1], timeout_ms=1200,
                        on_raise=lambda: print('activated', flush=True))
owner = g.start(a)
print(g.status, flush=True)
if owner:
    QTimer.singleShot(6000, a.quit)
    a.exec()
    g.close()
'''


def launch(root, n, shared_temp=None):
    base = root.parent / ('launch-' + root.name + '-' + str(n))
    base.mkdir(mode=0o700, exist_ok=True)
    temp = base / ('t' * 90)
    temp.mkdir(exist_ok=True)
    state = base / 'state'
    state.mkdir(exist_ok=True)
    env = dict(os.environ, TMPDIR=str(shared_temp or temp), VIDLIFT_STATE_DIR=str(state))
    return subprocess.Popen([sys.executable, '-c', SCRIPT, str(root)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def stop(p):
    if p.poll() is None:
        p.kill()
    return p.communicate(timeout=8)


def test_four_processes_different_cached_tmpdirs_and_state(root):
    processes = [launch(root, i) for i in range(4)]
    try:
        results = [p.communicate(timeout=12) for p in processes]
        lines = [out.splitlines() for out, _ in results]
        assert sum('owner' in x for x in lines) == 1, results
        assert sum('signalled' in x for x in lines) == 3, results
        assert sum(x.count('activated') for x in lines) == 3, results
        assert all(p.returncode == 0 for p in processes)
    finally:
        for p in processes:
            stop(p)


def test_stopped_owner_cannot_be_replaced_then_killed_owner_recovers(root):
    owner = launch(root, 1)
    try:
        assert owner.stdout.readline().strip() == 'owner'
        os.kill(owner.pid, signal.SIGSTOP)
        contender = launch(root, 2)
        out, err = contender.communicate(timeout=6)
        assert out.strip() == 'error', (out, err)
        endpoint = root / 'vidlift-gui-v3.sock'
        old_fd = os.open(endpoint, os.O_PATH | os.O_NOFOLLOW)
        os.kill(owner.pid, signal.SIGKILL)
        owner.wait(timeout=5)
        replacement = launch(root, 3)
        try:
            assert replacement.stdout.readline().strip() == 'owner'
            assert endpoint.lstat().st_ino != os.fstat(old_fd).st_ino
        finally:
            stop(replacement)
            os.close(old_fd)
    finally:
        stop(owner)


def test_unsafe_endpoint_and_lock_are_never_removed(root):
    app = QCoreApplication.instance() or QCoreApplication([])
    path = root / 'vidlift-gui-v3.sock'
    path.write_bytes(b'foreign')
    guard = SingleInstanceGuard(runtime_root_provider=lambda uid: root, timeout_ms=200)
    assert not guard.start(app) and guard.status == 'error'
    assert path.read_bytes() == b'foreign'
    path.unlink()
    lock = root / 'vidlift-gui-v3.lease'
    if lock.exists():
        lock.unlink()
    lock.symlink_to(path)
    assert not SingleInstanceGuard(runtime_root_provider=lambda uid: root, timeout_ms=200).start(app)
    assert lock.is_symlink()


def test_shutdown_preserves_replacement_path(root):
    app = QCoreApplication.instance() or QCoreApplication([])
    guard = SingleInstanceGuard(runtime_root_provider=lambda uid: root, timeout_ms=200)
    assert guard.start(app), guard.error
    path = root / 'vidlift-gui-v3.sock'
    path.unlink()
    path.write_bytes(b'replacement')
    guard.close()
    assert path.read_bytes() == b'replacement'


def test_stale_endpoint_swapped_during_probe_is_preserved(root, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    path = root / 'vidlift-gui-v3.sock'
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    guard = SingleInstanceGuard(runtime_root_provider=lambda uid: root, timeout_ms=200)
    original = guard._connect
    def swap():
        result = original()
        path.unlink()
        path.write_bytes(b'foreign replacement')
        return result
    monkeypatch.setattr(guard, '_connect', swap)
    assert not guard.start(app)
    assert path.read_bytes() == b'foreign replacement'


def test_replaced_lease_blocks_shutdown_cleanup(root):
    app = QCoreApplication.instance() or QCoreApplication([])
    guard = SingleInstanceGuard(runtime_root_provider=lambda uid: root, timeout_ms=200)
    assert guard.start(app), guard.error
    lease = root / 'vidlift-gui-v3.lease'
    lease.unlink()
    lease.write_bytes(b'foreign')
    guard.close()
    assert lease.read_bytes() == b'foreign'
    assert (root / 'vidlift-gui-v3.sock').exists()


@pytest.mark.parametrize('legacy_first', [True, False])
def test_legacy_coexistence_is_diagnostic_only(root, legacy_first):
    shared = root / 'temp'
    shared.mkdir(mode=0o700)
    legacy_script = r'''
from PyQt6.QtCore import QCoreApplication, QTimer
from PyQt6.QtNetwork import QLocalServer
from vidlift.ui.single_instance import endpoint_name
a = QCoreApplication([])
s = QLocalServer(a)
print('legacy' if s.listen(endpoint_name()) else 'error', flush=True)
QTimer.singleShot(8000, a.quit)
a.exec()
'''
    def legacy():
        return subprocess.Popen([sys.executable, '-c', legacy_script],
                                env=dict(os.environ, TMPDIR=str(shared)),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    older = newer = None
    try:
        if legacy_first:
            older = legacy()
            assert older.stdout.readline().strip() == 'legacy'
        newer = launch(root, 8, shared)
        assert newer.stdout.readline().strip() == 'owner'
        if not legacy_first:
            older = legacy()
            assert older.stdout.readline().strip() == 'legacy'
        _, err = stop(newer)
        assert 'mixed-version exclusion is unverified' in err
        if legacy_first:
            assert 'legacy VidLift endpoint is reachable' in err
    finally:
        if older is not None:
            stop(older)
        if newer is not None:
            stop(newer)


def test_runtime_validation_and_length(root):
    facts = {'platform': 'linux', 'uid': os.getuid()}
    endpoint = resolve_posix_endpoint(facts, lambda uid: root)
    assert endpoint.socket.is_absolute()
    root.chmod(0o755)
    with pytest.raises(OSError):
        resolve_posix_endpoint(facts, lambda uid: root)
    root.chmod(0o700)
    link = root.parent / (root.name + '-link')
    link.symlink_to(root, target_is_directory=True)
    try:
        with pytest.raises(OSError):
            resolve_posix_endpoint(facts, lambda uid: link)
    finally:
        link.unlink()
    longroot = root / ('a' * 100)
    longroot.mkdir(mode=0o700)
    with pytest.raises(OSError, match='Runtime socket path too long'):
        resolve_posix_endpoint(facts, lambda uid: longroot)
    app = QCoreApplication.instance() or QCoreApplication([])
    guard = SingleInstanceGuard(runtime_root_provider=lambda uid: longroot)
    assert not guard.start(app)
    assert guard.status == 'error' and guard.error is not None
    assert 'Runtime socket path too long' in guard.error
    with pytest.raises(OSError):
        resolve_posix_endpoint({'platform': 'darwin', 'uid': os.getuid()}, lambda uid: root)
