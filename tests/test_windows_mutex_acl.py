"""Native security regression tests, isolated from the production mutex name."""
import ctypes as c
import os
import threading
import uuid

import pytest

from vidlift import gpu_arbitration as arbitration

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows ACL APIs")


@pytest.fixture
def mutex_name(monkeypatch):
    name = "Global\\VidLift.Test.MutexAcl." + uuid.uuid4().hex
    monkeypatch.setattr(arbitration, "WINDOWS_NAME", name)
    return name


@pytest.fixture
def seed_mutex(mutex_name):
    from ctypes import wintypes as w
    k = c.WinDLL("kernel32", use_last_error=True)
    a = c.WinDLL("advapi32", use_last_error=True)

    class SA(c.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]

    k.CreateMutexExW.argtypes = [c.POINTER(SA), w.LPCWSTR, w.DWORD, w.DWORD]
    k.CreateMutexExW.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.CloseHandle.restype = w.BOOL
    k.LocalFree.argtypes = [c.c_void_p]
    k.LocalFree.restype = c.c_void_p
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.c_void_p]
    a.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    handles = []

    def seed(sddl):
        descriptor = c.c_void_p()
        assert a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None)
        try:
            handle = k.CreateMutexExW(c.byref(SA(c.sizeof(SA), descriptor, False)), mutex_name, 0, 0x00120001)
            assert handle, c.WinError(c.get_last_error())
            handles.append(handle)
        finally:
            k.LocalFree(descriptor)

    yield seed
    for handle in handles:
        assert k.CloseHandle(handle)


def test_secure_mutex_create_reopen_contention_release(mutex_name):
    first = arbitration.MachineGpuMutex()
    assert first.acquire(.2).acquired
    results = []

    def contend():
        second = arbitration.MachineGpuMutex()
        results.append(second.acquire(.05))
        second.release()

    try:
        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert len(results) == 1
        assert results[0].reason_code == "deadline_exceeded_live_owner"
    finally:
        first.release()
    later = arbitration.MachineGpuMutex()
    assert later.acquire(.2).acquired
    later.release()


@pytest.mark.parametrize("mask", ["GA", "0x001f0001"])
def test_generic_all_and_specific_all_are_equivalent(seed_mutex, mask):
    seed_mutex(f"D:P(A;;{mask};;;SY)(A;;{mask};;;BA)(A;;0x00120001;;;AU)")
    mutex = arbitration.MachineGpuMutex()
    try:
        assert mutex.acquire(.1).acquired
    finally:
        mutex.release()


@pytest.mark.parametrize("sddl", [
    "D:NO_ACCESS_CONTROL",  # NULL DACL
    "D:P",  # empty DACL
    "D:P(A;;GA;;;WD)",  # everyone, wrong trustee
    "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;AU)",  # excessive user rights
    "D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00120001;;;AU)(A;;GR;;;WD)",
    "D:P(D;;0x00040000;;;AU)(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x00120001;;;AU)",
    "D:P(A;ID;GA;;;SY)(A;;GA;;;BA)(A;;0x00120001;;;AU)",
])
def test_hostile_preexisting_acl_fails_closed(seed_mutex, sddl):
    seed_mutex(sddl)
    mutex = arbitration.MachineGpuMutex()
    outcome = mutex.acquire(.1)
    assert not outcome.acquired
    assert outcome.reason_code == "namespace_unavailable"
    assert mutex._handle is None
    mutex.release()


@pytest.mark.parametrize(("generic", "specific"), [
    ("GR", "0x00020001"), ("GW", "0x00020000"),
    ("GX", "0x00120000"), ("GA", "0x001f0001"),
])
def test_native_generic_mapping(seed_mutex, monkeypatch, generic, specific):
    # The third ACE grants the access needed to inspect/wait. The first
    # ACE exercises each generic mapping against the kernel's mapping.
    seed_mutex(f"D:P(A;;{generic};;;SY)(A;;GA;;;BA)(A;;0x00120001;;;AU)")
    monkeypatch.setattr(arbitration, "WINDOWS_DACL",
                        f"D:P(A;;{specific};;;SY)(A;;GA;;;BA)(A;;0x00120001;;;AU)")
    mutex = arbitration.MachineGpuMutex()
    try:
        assert mutex.acquire(.1).acquired
    finally:
        mutex.release()
