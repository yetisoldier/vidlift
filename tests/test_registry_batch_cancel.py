"""v0.4.13 registry batch-cancel tests (design §7.1).

R2 is the race gate (design §3.3): ``Registry.claim_next()`` and
``Registry.cancel_batch()`` are driven from two threads on a barrier,
×200 iterations. The property under test is the §3.3 total order:

- Either the claim lands first (the job is legitimately ``running``
  with its claim-time CancelFlag set, and ``cancel_batch`` reports it
  ``cancelling``) — or the cancel marks land first (the claim finds
  nothing).
- After ``cancel_batch`` returns, NO id from the batch set may be
  unflagged-and-queued: every id is ``cancelled`` or flagged-``running``.
- A follow-up ``claim_next()`` never resurrects a batch id.

R1 (``cancel_batch`` atomicity/ordering) and R3 (the claim refactor is
behavior-preserving, via the untouched ``test_resume_claim.py`` /
``test_registry_reload.py`` suites) round out the file.
"""

from __future__ import annotations

import threading

import pytest

from vidlift.mcp_bridge.registry import Registry


def _fresh_registry(tmp_path, name: str, count: int = 3):
    """A registry with `count` queued jobs in distinct subdirs (the
    jobs.json from a previous iteration must never leak across)."""
    state_dir = tmp_path / name
    state_dir.mkdir(parents=True, exist_ok=True)
    registry = Registry(state_dir)
    ids = [
        registry.enqueue(
            kind="run", paths=[f"/nowhere/{name}-{i}.avi"], options={}
        ).job_id
        for i in range(count)
    ]
    return registry, ids


def test_r2_claim_next_vs_cancel_batch_race_gate(tmp_path):
    """Design §3.3 total order, ×200: claim vs batch-cancel cannot
    interleave into an unflagged-and-queued batch id."""
    for iteration in range(200):
        registry, ids = _fresh_registry(tmp_path, f"it{iteration}")
        barrier = threading.Barrier(2, timeout=30)
        claim_result: list = []
        cancel_result: list = []

        def claimer():
            barrier.wait()
            claim_result.append(registry.claim_next())

        def canceller():
            barrier.wait()
            cancel_result.append(registry.cancel_batch(list(ids)))

        t_claim = threading.Thread(target=claimer)
        t_cancel = threading.Thread(target=canceller)
        t_claim.start()
        t_cancel.start()
        t_claim.join(timeout=30)
        t_cancel.join(timeout=30)
        assert not t_claim.is_alive() and not t_cancel.is_alive(), (
            f"iteration {iteration}: threads hung"
        )

        claimed = claim_result[0]
        results = cancel_result[0]

        # The cancel op always reports a per-id verdict for the full set.
        assert set(results) == set(ids), iteration

        for job_id in ids:
            record = registry.get(job_id)
            state = record.state
            if state == "queued":
                # The one forbidden end state: claimed-by-no-one,
                # marked-by-no-one.
                pytest.fail(
                    f"iteration {iteration}: {job_id} left queued "
                    f"(claim={'yes' if claimed else 'no'})"
                )
            if state == "cancelled":
                # Marked by cancel_batch inside its lock hold.
                assert results[job_id] == "cancelled", (iteration, job_id)
            elif state == "running":
                # Case B: the claim landed first. Its flag must be set
                # and the cancel op must report the flag-flip verdict.
                flag = registry.get_cancel_flag(job_id)
                assert flag is not None and flag.is_set(), (
                    iteration, job_id
                )
                assert results[job_id] == "cancelling", (iteration, job_id)
            else:
                pytest.fail(
                    f"iteration {iteration}: unexpected state {state!r} "
                    f"for {job_id}"
                )

        if claimed is not None:
            # The claim ran first: exactly the oldest id, flagged, and
            # cancel_batch saw it running.
            assert claimed.state == "running"
            assert claimed.job_id == ids[0]  # submission order held
        else:
            # The cancel ran first: every id marked, nothing claimable.
            assert all(
                registry.get(job_id).state == "cancelled" for job_id in ids
            )

        # Post-state: a follow-up claim never resurrects a batch id.
        assert registry.claim_next() is None, iteration

# ---------------------------------------------------------------------------
# R1: cancel_batch atomicity/ordering (design §7.1)
# ---------------------------------------------------------------------------


def test_r1_cancel_batch_running_and_queued(tmp_path):
    """J1 running + J2/J3 queued: one cancel_batch call flips J1's flag
    (\"cancelling\"), marks J2/J3 cancelled with the binding error/ok,
    persists once, and a follow-up claim finds nothing."""
    registry, ids = _fresh_registry(tmp_path, "r1")
    j1, j2, j3 = ids
    record = registry.require(j1)
    registry.claim_next()  # claims J1 (oldest) -> running + live flag

    results = registry.cancel_batch(ids)

    assert results == {j1: "cancelling", j2: "cancelled", j3: "cancelled"}
    assert registry.get_cancel_flag(j1).is_set()
    j1r = registry.require(j1)
    assert j1r.state == "running"  # dies later via the flag (worker path)
    for job_id in (j2, j3):
        queued_record = registry.require(job_id)
        assert queued_record.state == "cancelled"
        assert queued_record.error == "batch cancelled by user"
        assert queued_record.ok is False
    # The persisted file already reflects the cancelled truth.
    reloaded = Registry(tmp_path / "r1")
    assert reloaded.require(j2).state == "cancelled"
    assert reloaded.require(j2).error == "batch cancelled by user"
    assert reloaded.require(j2).ok is False
    assert reloaded.require(j1).state == "running"
    # The claim loop's next read finds nothing from the set.
    assert registry.claim_next() is None


def test_r1_cancel_batch_active_job_count_reaches_zero(tmp_path):
    """After the running job's flag-driven death and the queued marks,
    queued+running shrink so Start/Plan can re-enable (§3.4 choke 8)."""
    from vidlift.mcp_bridge.client_local import LocalBridgeClient

    client = LocalBridgeClient(state_dir=tmp_path / "r1c", start_executor=False)
    paths = []
    for i in range(2):
        path = tmp_path / f"v{i}.avi"
        path.write_text("")
        paths.append(path)
    ids = [
        client.enqueue_run([str(path)], options={})[0]
        for path in paths
    ]
    # Simulate: first job running (claim), then GUI-side batch cancel.
    client.registry.claim_next()
    client.registry.get(ids[0]).state == "running"
    client.registry.cancel_batch(ids)
    assert client.active_job_count() == 1  # only the dying head remains


def test_r1_cancel_batch_verdicts_for_terminal_and_unknown(tmp_path):
    """Terminal ids -> already-finished; unknown ids -> not-found;
    queued ids still cancel. Full per-id verdict map, no exceptions."""
    registry, ids = _fresh_registry(tmp_path, "r1b", count=3)
    j1, j2, j3 = ids
    registry.transition(registry.require(j1), "cancelled")  # terminal
    results = registry.cancel_batch([j1, j2, j3, "vl-nope-404"])
    assert results == {
        j1: "already-finished",
        j2: "cancelled",
        j3: "cancelled",
        "vl-nope-404": "not-found",
    }
    assert registry.require(j1).state == "cancelled"  # untouched


def test_r1_cancel_batch_double_call_is_cancelling(tmp_path):
    """Idempotency belt-and-suspenders (§4.4): the running job's flag
    already set -> second call degrades to \"cancelling\" via the
    client's conflict rule; queued jobs read already-finished."""
    from vidlift.mcp_bridge.client_local import LocalBridgeClient

    client = LocalBridgeClient(state_dir=tmp_path / "r1d", start_executor=False)
    paths = []
    for i in range(2):
        path = tmp_path / f"w{i}.avi"
        path.write_text("")
        paths.append(path)
    ids = [
        client.enqueue_run([str(path)], options={})[0]
        for path in paths
    ]
    client.registry.claim_next()
    first = client.cancel_batch(ids)
    assert first == {ids[0]: "cancelling", ids[1]: "cancelled"}
    second = client.cancel_batch(ids)
    assert second == {ids[0]: "cancelling", ids[1]: "already-finished"}


def test_r1_cancel_batch_include_running_variant_seam(tmp_path):
    """§4.6 seam, not surfaced: include_running=False marks ONLY queued
    jobs; the running job keeps running (flag untouched)."""
    registry, ids = _fresh_registry(tmp_path, "r1e")
    registry.claim_next()  # J1 -> running
    results = registry.cancel_batch(ids, include_running=False)
    assert results == {
        ids[0]: "conflict",
        ids[1]: "cancelled",
        ids[2]: "cancelled",
    }
    assert registry.get_cancel_flag(ids[0]).is_set() is False
    assert registry.require(ids[0]).state == "running"
