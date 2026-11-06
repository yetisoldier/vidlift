"""Kernel arbitration for cooperating v3 participants.

Production uses the global Windows mutex or an administrator-provisioned
POSIX inode. Legacy executables, unlocked callers and orphaned native
children are not constrained by this protocol.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
import threading
import time

WINDOWS_NAME = r"Global\YetiWurks.VidLift.GpuPhase.v3"
WINDOWS_DACL = "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00120001;;;AU)"
POSIX_PATH = Path("/run/lock/vidlift/gpu-phase.v3")


@dataclass(frozen=True)
class LockOutcome:
    acquired: bool
    scope: str = "machine_cooperating_v3_only"
    protocol: str = "v3"
    reason_code: str | None = None
    abandoned: bool = False


class MachineGpuMutex:
    def __init__(self, *, test_path: Path | None = None):
        self._test_path = test_path
        self._fd = None
        self._handle = None
        self._owner_thread = None
        self._kernel = None

    def acquire(self, deadline_seconds=900.0, on_wait=None, should_cancel=None):
        if self._owner_thread is not None:
            raise RuntimeError("recursive GPU mutex acquisition is unsupported")
        deadline = time.monotonic() + max(0.0, deadline_seconds)
        started = time.monotonic()
        try:
            if os.name == "nt" and self._test_path is None:
                self._open_windows()
            else:
                self._open_posix()
            while True:
                if should_cancel is not None and should_cancel():
                    return LockOutcome(False, reason_code="cancelled")
                status = self._try_wait()
                if status in ("acquired", "abandoned"):
                    self._owner_thread = threading.get_ident()
                    return LockOutcome(True, abandoned=status == "abandoned",
                                       reason_code=("abandoned_owner_child_state_unknown"
                                                    if status == "abandoned" else None))
                if on_wait:
                    on_wait(None, time.monotonic() - started)
                if time.monotonic() >= deadline:
                    return LockOutcome(False, reason_code="deadline_exceeded_live_owner")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        except (OSError, ValueError, AttributeError):
            return LockOutcome(False, reason_code="namespace_unavailable")
        finally:
            if self._owner_thread is None:
                self._close()

    def _open_posix(self):
        import fcntl  # noqa: F401, reject unsupported platforms
        path = Path(self._test_path) if self._test_path is not None else POSIX_PATH
        parent = path.parent.lstat()
        if not stat.S_ISDIR(parent.st_mode) or (self._test_path is None and
                (parent.st_uid != 0 or stat.S_IMODE(parent.st_mode) != 0o755)):
            raise OSError("untrusted arbitration directory")
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if self._test_path is not None:
            flags |= os.O_CREAT
        fd = os.open(path, flags, 0o600)
        self._fd = fd
        opened, named = os.fstat(fd), path.lstat()
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise OSError("untrusted arbitration inode")
        if self._test_path is None and (opened.st_uid != 0 or stat.S_IMODE(opened.st_mode) != 0o666):
            raise OSError("unprovisioned arbitration file")

    def _open_windows(self):
        import ctypes as c
        from ctypes import wintypes as w
        k = c.WinDLL("kernel32", use_last_error=True)
        a = c.WinDLL("advapi32", use_last_error=True)
        class SA(c.Structure):
            _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]
        k.CreateMutexExW.argtypes = [c.POINTER(SA), w.LPCWSTR, w.DWORD, w.DWORD]
        k.CreateMutexExW.restype = w.HANDLE
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.WaitForSingleObject.restype = w.DWORD
        k.ReleaseMutex.argtypes = [w.HANDLE]
        k.ReleaseMutex.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.LocalFree.argtypes = [c.c_void_p]
        k.LocalFree.restype = c.c_void_p
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p]
        a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
        a.GetSecurityInfo.argtypes = [w.HANDLE, c.c_int, w.DWORD] + [c.POINTER(c.c_void_p)] * 5
        a.GetSecurityInfo.restype = w.DWORD
        a.GetSecurityDescriptorDacl.argtypes = [c.c_void_p, c.POINTER(w.BOOL), c.POINTER(c.c_void_p), c.POINTER(w.BOOL)]
        a.GetSecurityDescriptorDacl.restype = w.BOOL
        self._kernel = k
        expected, actual_sd, actual_acl = c.c_void_p(), c.c_void_p(), c.c_void_p()
        try:
            if not a.ConvertStringSecurityDescriptorToSecurityDescriptorW(WINDOWS_DACL, 1, c.byref(expected), None):
                raise c.WinError(c.get_last_error())
            sa = SA(c.sizeof(SA), expected, False)
            self._handle = k.CreateMutexExW(c.byref(sa), WINDOWS_NAME, 0, 0x00120001)
            if not self._handle:
                raise c.WinError(c.get_last_error())
            if a.GetSecurityInfo(self._handle, 6, 4, None, None, c.byref(actual_acl), None, c.byref(actual_sd)):
                raise OSError("mutex DACL unavailable")
            present, defaulted, expected_acl = w.BOOL(), w.BOOL(), c.c_void_p()
            if not a.GetSecurityDescriptorDacl(expected, c.byref(present), c.byref(expected_acl), c.byref(defaulted)):
                raise OSError("expected DACL invalid")
            def acl_bytes(pointer):
                if not pointer.value:
                    raise OSError("NULL DACL refused")
                size = c.c_ushort.from_address(pointer.value + 2).value
                return c.string_at(pointer, size)
            if acl_bytes(actual_acl) != acl_bytes(expected_acl):
                raise OSError("existing mutex DACL incompatible")
        finally:
            for allocation in (actual_sd, expected):
                if allocation.value:
                    k.LocalFree(allocation)

    def _try_wait(self):
        if self._handle is not None:
            status = self._kernel.WaitForSingleObject(self._handle, 0)
            if status == 0:
                return "acquired"
            if status == 0x80:
                return "abandoned"
            if status == 0x102:
                return "waiting"
            raise OSError("WaitForSingleObject failed")
        import fcntl
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return "acquired"
        except BlockingIOError:
            return "waiting"

    def _close(self):
        if self._fd is not None:
            fd, self._fd = self._fd, None
            os.close(fd)
        if self._handle is not None:
            handle, self._handle = self._handle, None
            if not self._kernel.CloseHandle(handle):
                raise OSError("CloseHandle failed")

    def release(self):
        if self._owner_thread is None:
            return
        if self._owner_thread != threading.get_ident():
            raise RuntimeError("GPU mutex must be released on its acquiring thread")
        try:
            if self._handle is not None:
                if not self._kernel.ReleaseMutex(self._handle):
                    raise OSError("ReleaseMutex failed")
            else:
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            self._owner_thread = None
            self._close()
