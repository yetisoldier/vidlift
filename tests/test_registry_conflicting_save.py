"""F-3 (v0.4.15.3) — registry converge-before-write pins.

Karen's 2026-11-02 validation evidence, promoted to regression pins:
two ``Registry`` instances (two "processes") over one state dir. The
old ``save()`` wrote the in-memory view WITHOUT converging, so a
stale-view save wholesale-Reverted another process's newer rows —
``succeeded → running`` resurrected, freshly enqueued rows dropped
(the v0.4.15.1 "false active job" wedge at runtime).

The v0.4.15.3 rule (Bob's design): converge (the v0.4.5 D2 reload) at
the WRITE boundary under the same lock hold — ``save``, ``enqueue``
and ``transition`` all land ON TOP of the file's converged truth, so a
save can only ever ADD/UPDATE, never revert.

Staleness is forced deterministically (``_loaded_mtime = -1.0``, the
same seam test_registry_reload.py uses): filesystem timestamp
granularity must never decide whether the converge fires.
"""

from __future__ import annotations

import json
import threading

import pytest

from vidlift.mcp_bridge.registry import Registry


@pytest.fixture
def state_dir(tmp_path):
    path = tmp_path / "state"
    path.mkdir(parents=True)
    return path


def _file_rows(state_dir) -> dict[str, dict]:
    data = json.loads(
        (state_dir / "jobs.json").read_text(encoding="utf-8")
    )
    return {row["job_id"]: row for row in data["jobs"]}


def _stale(registry: Registry) -> None:
    """Mark this view stale regardless of fs mtime granularity."""
    registry._loaded_mtime = -1.0


def test_stale_view_save_never_drops_another_processs_new_rows(state_dir):
    """F-3a pin: B loaded EARLIER; A closed rec1 (succeeded) and
    enqueued rec2 since; B's plain ``save()`` must NOT revert the file
    — rec2 survives and rec1 stays terminal-truthful."""
    a = Registry(state_dir)
    rec1 = a.enqueue(kind="run", paths=["/x/movie.avi"], options={})
    b = Registry(state_dir)  # B's view: rec1 queued, nothing else
    a.transition(rec1, "running")
    a.transition(rec1, "succeeded")
    rec2 = a.enqueue(kind="run", paths=["/x/other.avi"], options={})
    _stale(b)  # B predates both of A's writes (deterministic)
    b.save()
    rows = _file_rows(state_dir)
    assert rows[rec1.job_id]["state"] == "succeeded", (
        "the terminal truth must not resurrect to the stale view's "
        "'running'-era state"
    )
    assert rec2.job_id in rows, "the fresh row must survive B's save"


def test_stale_running_view_save_does_not_resurrect_running(state_dir):
    """F-3b pin: B holds a stale ``running`` view; A closed the job
    succeeded; B's ``save()`` must keep the file's terminal truth."""
    a = Registry(state_dir)
    rec = a.enqueue(kind="run", paths=["/x/movie.avi"], options={})
    a.transition(rec, "running")
    b = Registry(state_dir)
    # B's stale loaded view, read WITHOUT the get() read path (a read
    # would legitimately converge before the save — the pin here is the
    # WRITE boundary).
    assert b._jobs[rec.job_id].state == "running"
    a.transition(rec, "succeeded")
    _stale(b)  # B's baseline predates A's close (deterministic)
    b.save()
    rows = _file_rows(state_dir)
    assert rows[rec.job_id]["state"] == "succeeded"


def test_converge_keeps_both_writers_rows(state_dir):
    """B enqueues its own row and saves: the file holds A's rows AND
    B's row (converge-then-insert under one hold) — neither side's
    writes are lost."""
    a = Registry(state_dir)
    rec_a1 = a.enqueue(kind="run", paths=["/x/a1.avi"], options={})
    b = Registry(state_dir)  # B's view: rec_a1 only
    rec_a2 = a.enqueue(kind="run", paths=["/x/a2.avi"], options={})
    _stale(b)  # B predates A's second enqueue (deterministic)
    rec_b = b.enqueue(kind="run", paths=["/x/b.avi"], options={})
    rows = _file_rows(state_dir)
    assert rec_a1.job_id in rows
    assert rec_a2.job_id in rows, "A's insert survives B's enqueue-save"
    assert rec_b.job_id in rows


def test_transition_mutates_canonical_record_after_foreign_write(state_dir):
    """A foreign write lands between A's load and A's ``transition()``
    — A's transition still lands in the file, applied ATOP converged
    truth: the foreign row survives AND A's row mutates from the file's
    state (the canonical re-parsed record, not A's stale handle)."""
    a = Registry(state_dir)
    rec = a.enqueue(kind="run", paths=["/x/movie.avi"], options={})
    b = Registry(state_dir)
    foreign = b.enqueue(kind="run", paths=["/x/foreign.avi"], options={})
    _stale(a)  # A's baseline predates B's foreign write (deterministic)
    a.transition(rec, "running")
    rows = _file_rows(state_dir)
    assert rows[rec.job_id]["state"] == "running"
    assert rows[foreign.job_id]["state"] == "queued", (
        "the foreign row must survive A's transition-save"
    )
    # the registry's own view agrees with the file afterwards.
    assert a.require(rec.job_id).state == "running"


def test_concurrent_save_and_enqueue_never_corrupt(state_dir):
    """Threads through the same registry: converged writes stay correct
    under interleaving — the final file must contain every enqueued
    row, every state must parse, and the file must be valid JSON."""
    registry = Registry(state_dir)
    ids: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        for n in range(4):
            rec = registry.enqueue(
                kind="run", paths=[f"/x/w{index}-{n}.avi"], options={}
            )
            with lock:
                ids.append(rec.job_id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ids) == 16
    rows = _file_rows(state_dir)
    assert set(ids) <= set(rows)
    # one of the rows was transitioned; it stays terminal in the file.
    rec = registry.require(ids[0])
    registry.transition(rec, "cancelled")
    assert _file_rows(state_dir)[ids[0]]["state"] == "cancelled"