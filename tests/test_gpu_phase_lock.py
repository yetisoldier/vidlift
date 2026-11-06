"""v0.4.9 GPU phase-lock tests (design §12 T1-T4; T-gpu-3 real-J-drive
stays MANUAL per the design) + v0.4.20 ADR-3/S2 hardening arms."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

# Historical migration fixtures, NOT production v3 exclusion guarantees.
from vidlift.engine_ncnn import _LegacyGpuPhaseLock as GpuPhaseLock


@pytest.fixture()
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / ".gpu-phase.lock"


def _v2_record(pid: int, token: str = "a" * 32, created_at: float | None = None,
               creation_time: dict | None = None) -> str:
    now = time.time()
    return json.dumps({
        "schema_version": 2,
        "pid": pid,
        "created_at": created_at if created_at is not None else now,
        "heartbeat_at": now,
        "owner_token": token,
        "creation_time": creation_time or {"kind": "unavailable"},
    })


def _seed_v2(path: Path, record: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record, encoding="utf-8")


class FakeRacingProcess:
    """A 'dead holder' simulation: writes a pid file for a pid that
    does not exist."""

    @staticmethod
    def seed_dead_holder(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Reaped synthetic child, portable to the Windows packaging runner.
        proc = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                              check=True, capture_output=True, text=True)
        pid = int(proc.stdout.strip())
        path.write_text(f"{pid}\n{time.time()}\n", encoding="utf-8")


def test_t1_lock_serializes_two_acquirers(lock_path: Path):
    """T1: holder A holds; B's acquire waits (on_wait fires with the
    holder's pid) until A releases; then B acquires."""
    lock_a = GpuPhaseLock(lock_path)
    assert lock_a.acquire() is True
    assert lock_path.exists()
    # v0.4.20 (ADR-3): the record is schema-v2 JSON carrying our pid
    record = json.loads(lock_path.read_text(encoding="utf-8"))
    assert record["schema_version"] == 2
    assert record["pid"] == os.getpid()

    waits: list[tuple[int | None, float]] = []
    lock_b = GpuPhaseLock(lock_path)

    def claim_b() -> None:
        ok = lock_b.acquire(
            on_wait=lambda pid, held: waits.append((pid, held)),
            deadline_seconds=1.0,
        )
        assert ok is False  # deadline elapsed while A still held

    thread = threading.Thread(target=claim_b)
    thread.start()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    # the wait label seam fired with OUR pid as the holder
    assert waits, "on_wait never fired during B's wait"
    holder_pid, held = waits[0]
    assert holder_pid == os.getpid()
    # still locked by A (B did not steal it)
    assert lock_path.exists()
    lock_a.release()
    assert not lock_path.exists()
    # B can now take it
    assert lock_b.acquire(deadline_seconds=1.0) is True
    lock_b.release()


def test_t2_stale_holder_swept_by_next_acquire(lock_path: Path):
    """T2: a lock file whose holder pid is DEAD is swept by the next
    acquire (machine-wide liveness: a crashed GUI must not wedge the
    GPU forever)."""
    FakeRacingProcess.seed_dead_holder(lock_path)
    lock = GpuPhaseLock(lock_path)
    assert lock.acquire(deadline_seconds=1.0) is True
    lock.release()


def test_t3_release_idempotent_and_after_crash_swept(lock_path: Path):
    """T3a: release twice is a no-op the second time; T3b: a lock left
    by a dead process (the crash shape) is swept — the 'success /
    cancel / fail release paths' rule is belt-and-braces on top."""
    lock = GpuPhaseLock(lock_path)
    lock.acquire()
    lock.release()
    lock.release()  # idempotent
    assert not lock_path.exists()
    # crash shape: recreate a dead-holder lock, sweep via acquire
    FakeRacingProcess.seed_dead_holder(lock_path)
    lock2 = GpuPhaseLock(lock_path)
    assert lock2.acquire(deadline_seconds=1.0) is True
    lock2.release()


def test_t4_unreadable_holder_never_swept(lock_path: Path):
    """T4: a lock file with NO parseable pid is conservatively LEFT
    (the wait loop times out; never a destructive guess)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("not-a-pid\n", encoding="utf-8")
    lock = GpuPhaseLock(lock_path)
    ok = lock.acquire(deadline_seconds=0.2)
    assert ok is False
    assert lock_path.exists()  # conservative: not swept, not stolen
    # the wait-loop deadline returns False — the production caller's
    # degrade-open WARNING arm (the engine's honest unlock path)
    assert lock._fd is None


def test_production_wait_has_finite_default(lock_path: Path, monkeypatch):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("not-a-pid\\n", encoding="utf-8")
    monkeypatch.setattr(GpuPhaseLock, "DEFAULT_DEADLINE_SECONDS", 0.02)
    lock = GpuPhaseLock(lock_path)
    started = time.monotonic()
    assert lock.acquire() is False
    assert time.monotonic() - started < 0.5
    assert lock_path.exists()


def test_t5_wait_label_format(lock_path: Path):
    """The visible wait label reads 'waiting for GPU — held by pid N
    for Xs' (the anti-freeze promise: never a silent stall)."""
    lock_a = GpuPhaseLock(lock_path)
    lock_a.acquire()
    labels: list[str] = []
    lock_b = GpuPhaseLock(lock_path)

    def claim_b() -> None:
        lock_b.acquire(
            on_wait=lambda pid, held: labels.append(
                f"waiting for GPU — held by pid {pid if pid else '?'} "
                f"for {held:.0f}s"
            ),
            deadline_seconds=0.3,
        )

    thread = threading.Thread(target=claim_b)
    thread.start()
    thread.join(timeout=5.0)
    lock_a.release()
    assert labels and labels[0].startswith("waiting for GPU — held by pid ")
    assert "for 0s" in labels[0]


# ---- v0.4.20 (ADR-3 / S2): hardening arms ------------------------------

def test_f1_posix_permission_error_maps_to_unknown(monkeypatch):
    """F1 mapping: EPERM on kill(0) means the process EXISTS but is not
    ours — unknown, NEVER dead."""
    import vidlift.engine_ncnn as eng

    def _eperm(pid, sig):
        raise PermissionError(13, "Operation not permitted")

    monkeypatch.setattr(eng.os, "kill", _eperm)
    assert eng._posix_liveness_probe(os.getpid()) is None


def test_f1_access_denied_live_owner_never_reclaimed(lock_path: Path):
    """F1 POSIX arm: a live foreign pid whose probe raises PermissionError
    (tri-state None) must NOT be swept — acquire waits to the deadline and
    reports ``deadline_exceeded_live_owner``."""
    _seed_v2(lock_path, _v2_record(os.getpid(), token="c" * 32))
    lock = GpuPhaseLock(lock_path)
    lock._liveness_probe = lambda pid: None  # EPERM tri-state
    unavailable: list[str] = []
    assert lock.acquire(
        deadline_seconds=0.3, on_unavailable=unavailable.append
    ) is False
    assert lock_path.exists()
    assert json.loads(lock_path.read_text())["owner_token"] == "c" * 32
    assert unavailable == ["deadline_exceeded_live_owner"]


def test_f1_windows_access_denied_semantics(lock_path: Path):
    """F1 Windows-semantics arm (synthetic): the OpenProcess access-denied
    tri-state None drives the same non-reclaim behaviour; and on a host
    without WinDLL the probe degrades to unknown, never dead."""
    from vidlift.engine_ncnn import _windows_liveness_probe

    assert _windows_liveness_probe(os.getpid()) is not False  # never dead
    _seed_v2(lock_path, _v2_record(os.getpid(), token="d" * 32))
    lock = GpuPhaseLock(lock_path)
    lock._liveness_probe = lambda pid: None  # access-denied tri-state
    unavailable: list[str] = []
    assert lock.acquire(
        deadline_seconds=0.3, on_unavailable=unavailable.append
    ) is False
    assert lock_path.exists()
    assert json.loads(lock_path.read_text())["owner_token"] == "d" * 32
    assert unavailable == ["deadline_exceeded_live_owner"]


def test_pid_reuse_mismatch_is_reclaimed(lock_path: Path):
    """A live pid whose creation time MISMATCHES the recorded value is a
    recycled PID — provably not the original holder → reclaimed."""
    _seed_v2(
        lock_path,
        _v2_record(os.getpid(), token="a" * 32,
                   creation_time={"kind": "windows_filetime", "value": 111}),
    )
    lock = GpuPhaseLock(lock_path)
    lock._liveness_probe = lambda pid: True
    lock._creation_time_fn = (
        lambda pid: {"kind": "windows_filetime", "value": 222}
    )
    assert lock.acquire(deadline_seconds=0.5) is True
    # we now own the path — the recycled-pid record was quarantined
    assert json.loads(lock_path.read_text())["owner_token"] != "a" * 32
    lock.release()


def test_pid_reuse_matching_creation_time_retained(lock_path: Path):
    """A live pid whose creation time MATCHES is the original holder —
    retained until the deadline, then ``lock_unavailable``."""
    _seed_v2(
        lock_path,
        _v2_record(os.getpid(), token="a" * 32,
                   creation_time={"kind": "windows_filetime", "value": 111}),
    )
    lock = GpuPhaseLock(lock_path)
    lock._liveness_probe = lambda pid: True
    lock._creation_time_fn = (
        lambda pid: {"kind": "windows_filetime", "value": 111}
    )
    unavailable: list[str] = []
    assert lock.acquire(
        deadline_seconds=0.3, on_unavailable=unavailable.append
    ) is False
    assert lock_path.exists()
    assert json.loads(lock_path.read_text())["owner_token"] == "a" * 32
    assert unavailable == ["deadline_exceeded_live_owner"]


def test_unverifiable_creation_time_live_pid_retained(lock_path: Path):
    """Live pid + ``kind: unavailable`` creation time (the POSIX/dev shape)
    → never reclaimed before the deadline; honest reason at the bound."""
    _seed_v2(lock_path, _v2_record(os.getpid(), token="b" * 32))
    lock = GpuPhaseLock(lock_path)
    lock._liveness_probe = lambda pid: True
    unavailable: list[str] = []
    assert lock.acquire(
        deadline_seconds=0.3, on_unavailable=unavailable.append
    ) is False
    assert lock_path.exists()
    assert json.loads(lock_path.read_text())["owner_token"] == "b" * 32
    assert unavailable == ["deadline_exceeded_live_owner"]


def test_race_safe_sweep_does_not_delete_new_owner(
    lock_path: Path, monkeypatch
):
    """Synthetic interleaving: between the liveness check and the rename,
    our record is removed externally and a NEW owner acquires. The
    quarantine must not delete the new lock — post-rename identity
    verification restores it."""
    _seed_v2(lock_path, _v2_record(os.getpid(), token="a" * 32))
    lock = GpuPhaseLock(lock_path)
    # dead holder so the sweep proceeds all the way to the rename
    lock._liveness_probe = lambda pid: False

    real_rename = Path.rename
    new_owner_token = "e" * 32

    def _interleaved_rename(self, target):
        if self == lock_path:
            # external interleaving: our record vanishes, a new owner's
            # O_EXCL create lands at the path — then OUR rename runs and
            # moves the NEW inode to quarantine
            self.unlink()
            _seed_v2(lock_path, _v2_record(os.getpid(), token=new_owner_token))
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", _interleaved_rename)
    assert lock._sweep_stale() is False
    # the new owner's lock is intact at the path; nothing of theirs left
    # in a quarantine name
    assert json.loads(lock_path.read_text())["owner_token"] == new_owner_token
    quarantined = list(lock_path.parent.glob(".gpu-phase.lock.quarantine-*"))
    assert not any(
        new_owner_token in p.read_text() for p in quarantined
    )


def test_release_after_replacement_survives_new_owner(lock_path: Path):
    """Acquire A, externally replace the record with B's token, then
    A.release() — B's lock must survive (token-checked removal)."""
    lock_a = GpuPhaseLock(lock_path)
    assert lock_a.acquire(deadline_seconds=1.0) is True
    _seed_v2(lock_path, _v2_record(os.getpid(), token="f" * 32))
    lock_a.release()
    assert lock_path.exists()
    assert json.loads(lock_path.read_text())["owner_token"] == "f" * 32


def test_legacy_v1_live_pid_retained_dead_reclaimed(
    lock_path: Path, monkeypatch
):
    """Legacy v1 text format stays readable: live pid retained (deadline),
    dead pid reclaimed — the T4 malformed-text invariant is unchanged."""
    # live pid, no token (the 2-line legacy shape)
    _seed_v2(lock_path, f"{os.getpid()}\n{time.time()}\n")
    lock = GpuPhaseLock(lock_path)
    unavailable: list[str] = []
    assert lock.acquire(
        deadline_seconds=0.3, on_unavailable=unavailable.append
    ) is False
    assert lock_path.exists()  # live holder — never swept
    assert unavailable == ["deadline_exceeded_live_owner"]

    # dead pid (the T2 seed shape) — reclaimed by the next acquire
    FakeRacingProcess.seed_dead_holder(lock_path)
    lock2 = GpuPhaseLock(lock_path)
    assert lock2.acquire(deadline_seconds=1.0) is True
    lock2.release()


def test_heartbeat_renews_while_holding(lock_path: Path, monkeypatch):
    """The owner renews ``heartbeat_at`` at the heartbeat cadence while
    holding; release stops and joins the thread."""
    monkeypatch.setattr(GpuPhaseLock, "HEARTBEAT_SECONDS", 0.05)
    lock = GpuPhaseLock(lock_path)
    assert lock.acquire(deadline_seconds=1.0) is True
    first = json.loads(lock_path.read_text())["heartbeat_at"]
    time.sleep(0.2)
    second = json.loads(lock_path.read_text())["heartbeat_at"]
    assert second > first  # renewed, not stale
    lock.release()
    assert lock._heartbeat_thread is None


def test_create_refused_reports_reason(lock_path: Path, monkeypatch):
    """A filesystem that refuses atomic create degrades OPEN and reports
    ``create_refused`` — the bool-only contract is preserved."""
    import vidlift.engine_ncnn as eng

    def _refuse(*args, **kwargs):
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(eng.os, "open", _refuse)
    unavailable: list[str] = []
    lock = GpuPhaseLock(lock_path)
    assert lock.acquire(on_unavailable=unavailable.append) is False
    assert unavailable == ["create_refused"]


@pytest.mark.parametrize("action", ["sweep", "release"])
def test_adversarial_replacement_then_third_owner_never_clobbered(lock_path, monkeypatch, action):
    lock = GpuPhaseLock(lock_path)
    if action == "release":
        assert lock.acquire(deadline_seconds=1)
    else:
        _seed_v2(lock_path, _v2_record(os.getpid(), token="a" * 32))
        lock._liveness_probe = lambda pid: False
    original_rename = Path.rename
    def race(source, target):
        if source != lock_path:
            return original_rename(source, target)
        source.unlink()
        _seed_v2(lock_path, _v2_record(os.getpid(), token="b" * 32))
        result = original_rename(source, target)
        _seed_v2(lock_path, _v2_record(os.getpid(), token="c" * 32))
        return result
    monkeypatch.setattr(Path, "rename", race)
    if action == "release":
        lock.release()
    else:
        assert not lock._sweep_stale()
    assert json.loads(lock_path.read_text())["owner_token"] == "c" * 32
    displaced = list(lock_path.parent.glob(lock_path.name + ".quarantine-*"))
    assert len(displaced) == 1
    assert json.loads(displaced[0].read_text())["owner_token"] == "b" * 32
    # Unknown displaced ownership is reported, never silently acquired over.
    contender = GpuPhaseLock(lock_path)
    reasons = []
    assert not contender.acquire(deadline_seconds=0.01, on_unavailable=reasons.append)
    assert reasons == ["unreadable_lock"]


def test_permission_error_from_injected_probe_is_conservative(lock_path):
    _seed_v2(lock_path, _v2_record(os.getpid()))
    lock = GpuPhaseLock(lock_path)
    def denied(pid):
        raise PermissionError()
    lock._liveness_probe = denied
    for _ in range(2):
        reasons = []
        assert not lock.acquire(deadline_seconds=0.01, on_unavailable=reasons.append)
        assert reasons == ["deadline_exceeded_live_owner"]
        lock.release()
        assert lock_path.exists()


@pytest.mark.parametrize("raw", [b"\xff\xfe", b'{"schema_version":"oops","pid":42,"owner_token":"a"}', b"-1\n0\n"])
def test_malformed_records_retained_without_traceback(lock_path, raw):
    lock_path.parent.mkdir(parents=True)
    lock_path.write_bytes(raw)
    reasons = []
    lock = GpuPhaseLock(lock_path)
    assert not lock.acquire(deadline_seconds=0.01, on_unavailable=reasons.append)
    lock.release()
    assert lock_path.read_bytes() == raw
    assert reasons == ["unreadable_lock"]


def test_legacy_dead_owner_with_token_is_reclaimable(lock_path):
    _seed_v2(lock_path, "99999999\n0\nlegacy-token\n")
    lock = GpuPhaseLock(lock_path)
    lock._liveness_probe = lambda pid: False
    assert lock.acquire(deadline_seconds=1)
    lock.release()
    assert not lock_path.exists()


@pytest.mark.parametrize("replacement", ["legacy", "v2"])
def test_tokenless_legacy_replacement_with_same_pid_is_not_deleted(lock_path, monkeypatch, replacement):
    _seed_v2(lock_path, "99999999\n0\n")
    lock = GpuPhaseLock(lock_path)
    lock._liveness_probe = lambda pid: False
    original = Path.rename
    new = "99999999\n1\n" if replacement == "legacy" else _v2_record(99999999, token="b" * 32)
    def race(source, target):
        if source == lock_path:
            source.unlink()
            _seed_v2(lock_path, new)
        return original(source, target)
    monkeypatch.setattr(Path, "rename", race)
    assert not lock._sweep_stale()
    assert lock_path.read_text() == new