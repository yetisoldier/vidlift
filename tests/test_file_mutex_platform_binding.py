"""The unlock operation must match the backend used to acquire the lock."""
import os

import pytest

from vidlift.file_mutex import FileMutex


@pytest.mark.skipif(os.name != "posix", reason="POSIX backend regression")
def test_release_uses_acquisition_backend_when_platform_probe_changes(tmp_path, monkeypatch):
    lock = FileMutex(tmp_path / "lock")
    contender = FileMutex(tmp_path / "lock")
    assert lock.acquire()
    assert not contender.acquire(timeout=0)
    with monkeypatch.context() as patch:
        patch.setattr(os, "name", "nt")
        lock.release()
    assert contender.acquire(timeout=0)
    contender.release()
    lock.release()
