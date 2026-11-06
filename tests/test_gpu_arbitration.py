"""v3 kernel lifetime authority, diagnostic failure and cancellation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from vidlift.engine_ncnn import GpuPhaseLock, EngineCancelled, _LegacyGpuPhaseLock
from vidlift.gpu_arbitration import MachineGpuMutex


def backend(tmp_path):
    shared = tmp_path / "kernel"
    shared.touch(exist_ok=True)
    return MachineGpuMutex(test_path=shared)


def test_foreign_metadata_cannot_grant_or_steal_kernel_lock(tmp_path):
    first = GpuPhaseLock(tmp_path / "a" / "record", backend=backend(tmp_path))
    second = GpuPhaseLock(tmp_path / "b" / "record", backend=backend(tmp_path))
    assert first.acquire(deadline_seconds=.2)
    # B's pathname is not authority. Unknown and live foreign records stay put.
    first.path.write_bytes(b"foreign replacement B")
    quarantine = first.path.with_name(first.path.name + ".quarantine-C")
    quarantine.write_bytes(b"foreign C")
    assert not second.acquire(deadline_seconds=.05)
    first.release()
    assert first.path.read_bytes() == b"foreign replacement B"
    assert quarantine.read_bytes() == b"foreign C"
    assert second.acquire(deadline_seconds=.2)
    second.release()


@pytest.mark.parametrize("failure", ["write", "partial", "fsync"])
def test_metadata_failure_keeps_exclusion_and_future_acquisition(tmp_path, monkeypatch, failure):
    import vidlift.engine_ncnn as engine
    first = GpuPhaseLock(tmp_path / "state" / "record", backend=backend(tmp_path))
    second = GpuPhaseLock(tmp_path / "other" / "record", backend=backend(tmp_path))
    real_write = os.write
    calls = []
    def broken(fd, data):
        if failure == "partial" and not calls:
            calls.append(True)
            return real_write(fd, data[:4])
        raise OSError("synthetic diagnostic failure")
    with monkeypatch.context() as patch:
        if failure == "fsync":
            patch.setattr(engine.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync")))
        else:
            patch.setattr(engine.os, "write", broken)
        assert first.acquire(deadline_seconds=.2)
    assert "metadata_unavailable" in first.diagnostics
    assert not first.path.exists()
    assert not second.acquire(deadline_seconds=.02)
    first.release()
    assert second.acquire(deadline_seconds=.2)
    second.release()


def test_short_write_eintr_and_failed_heartbeat_preserve_complete_metadata(tmp_path, monkeypatch):
    lock = GpuPhaseLock(tmp_path / "record", backend=backend(tmp_path))
    real = os.write
    calls = []
    def short(fd, data):
        if not calls:
            calls.append(True)
            raise InterruptedError()
        return real(fd, data[:3])
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", short)
        assert lock.acquire(deadline_seconds=.2)
    before = lock.path.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync")))
        lock._touch_heartbeat()
    assert lock.path.read_bytes() == before
    assert json.loads(before)["owner_token"] == lock._owner_token
    lock.release()


@pytest.mark.parametrize("during", [False, True])
def test_cancel_wait_never_falls_open_or_releases_owner(tmp_path, during):
    first = GpuPhaseLock(tmp_path / "a", backend=backend(tmp_path))
    second = GpuPhaseLock(tmp_path / "b", backend=backend(tmp_path))
    assert first.acquire(deadline_seconds=.2)
    cancelled = threading.Event()
    if not during:
        cancelled.set()
    timer = threading.Timer(.05, cancelled.set)
    timer.start()
    try:
        started = time.monotonic()
        with pytest.raises(EngineCancelled):
            second.acquire(deadline_seconds=3, should_cancel=cancelled.is_set)
        assert time.monotonic() - started < .5
        second.release()
        contender = backend(tmp_path)
        assert not contender.acquire(.01).acquired
    finally:
        timer.join()
        first.release()


def test_missing_production_namespace_does_not_create_peruser_fallback(tmp_path, monkeypatch):
    import vidlift.gpu_arbitration as arbitration
    monkeypatch.setattr(arbitration, "POSIX_PATH", tmp_path / "missing" / "global")
    mutex = MachineGpuMutex()
    assert mutex.acquire(.01).reason_code == "namespace_unavailable"
    assert not (tmp_path / "missing").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX provisioning check")
def test_default_phase_lock_uses_shared_production_backend(tmp_path, monkeypatch):
    import vidlift.gpu_arbitration as arbitration
    shared = tmp_path / "missing" / "global"
    monkeypatch.setattr(arbitration, "POSIX_PATH", shared)
    lock = GpuPhaseLock(tmp_path / "state" / "record")
    assert lock.scope == "machine_cooperating_v3_only"
    assert not lock.acquire(deadline_seconds=.2)
    assert lock.last_unavailable == "namespace_unavailable"
    assert lock._backend._test_path is None
    assert not shared.exists()
    assert not (tmp_path / "state" / "record.kernel").exists()
    lock.release()


@pytest.mark.parametrize("replacement", [False, True])
def test_retained_legacy_failed_initial_write_cleans_only_own_inode(tmp_path, monkeypatch, replacement):
    path = tmp_path / "legacy"
    lock = _LegacyGpuPhaseLock(path)
    def fail(token):
        if replacement:
            path.unlink()
            path.write_bytes(b"foreign replacement")
        raise OSError("first write failed")
    monkeypatch.setattr(lock, "_write_record", fail)
    assert not lock.acquire(deadline_seconds=.1)
    lock.release()
    if replacement:
        assert path.read_bytes() == b"foreign replacement"
    else:
        assert not path.exists()
        later = _LegacyGpuPhaseLock(path)
        assert later.acquire(deadline_seconds=.1)
        later.release()


def test_kernel_owner_death_releases_fd_without_claiming_gpu_idle(tmp_path):
    shared = tmp_path / "kernel"
    shared.touch()
    script = "from pathlib import Path; import sys,time; from vidlift.gpu_arbitration import MachineGpuMutex; m=MachineGpuMutex(test_path=Path(sys.argv[1])); assert m.acquire(1).acquired; print('held',flush=True); time.sleep(30)"
    process = subprocess.Popen([sys.executable, "-c", script, str(shared)], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "held"
        assert not MachineGpuMutex(test_path=shared).acquire(.02).acquired
        process.kill()
        process.wait(timeout=3)
        later = MachineGpuMutex(test_path=shared)
        assert later.acquire(.5).acquired
        later.release()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
