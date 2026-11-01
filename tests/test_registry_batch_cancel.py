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