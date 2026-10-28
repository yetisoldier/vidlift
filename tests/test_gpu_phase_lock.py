"""v0.4.9 GPU phase-lock tests (design §12 T1-T4; T-gpu-3 real-J-drive
stays MANUAL per the design)."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from vidlift.engine_ncnn import GpuPhaseLock


@pytest.fixture()
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / ".gpu-phase.lock"


class FakeRacingProcess:
    """A 'dead holder' simulation: writes a pid file for a pid that
    does not exist."""

    @staticmethod
    def seed_dead_holder(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # find a surely-dead pid: spawn and reap one
        proc = os.fork()
        if proc == 0:  # child exits immediately
            os._exit(0)
        os.waitpid(proc, 0)
        path.write_text(f"{proc}\n{time.time()}\n", encoding="utf-8")


def test_t1_lock_serializes_two_acquirers(lock_path: Path):
    """T1: holder A holds; B's acquire waits (on_wait fires with the
    holder's pid) until A releases; then B acquires."""
    lock_a = GpuPhaseLock(lock_path)
    assert lock_a.acquire() is True
    assert lock_path.exists()
    assert lock_path.read_text().startswith(f"{os.getpid()}\n")

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