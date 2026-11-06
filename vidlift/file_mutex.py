"""Small OS-held mutation mutex. Its persistent inode must never be unlinked.

Unlike a PID lock, the kernel releases this lock when the process dies. Keeping
one sidecar inode prevents two contenders from locking different replacements.
This coordinates VidLift writers; it is not a security boundary against actors
that deliberately ignore the protocol or replace the mutex inode itself.
"""
from __future__ import annotations

import os
from pathlib import Path
import time


class FileMutex:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._file = None
        self._unlock = None

    def acquire(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"\0")
                    stream.flush()
                def lock():
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                def unlock():
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def lock():
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                def unlock():
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            while True:
                try:
                    lock()
                    self._file = stream
                    self._unlock = unlock
                    return True
                except (BlockingIOError, PermissionError):
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
        finally:
            if self._file is not stream:
                stream.close()

    def release(self) -> None:
        stream, self._file = self._file, None
        unlock, self._unlock = self._unlock, None
        if stream is None:
            return
        try:
            assert unlock is not None
            unlock()
        finally:
            stream.close()
