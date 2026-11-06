"""Spawned-process regressions for the registry transaction protocol."""
import multiprocessing as mp
import os
from pathlib import Path


def _enqueue_worker(state, gate, output):
    from vidlift.mcp_bridge.registry import Registry
    gate.wait(10)
    record = Registry(state).enqueue(kind="run", paths=[f"/tmp/{os.getpid()}.mp4"], options={})
    output.put(record.job_id)


def _claim_worker(state, gate, output):
    from vidlift.mcp_bridge.registry import Registry
    gate.wait(10)
    record = Registry(state).claim_next()
    output.put(record.job_id if record else None)


def _cancel_observer(state, gate, output, job_id):
    from vidlift.mcp_bridge.registry import Registry
    gate.wait(10)
    output.put(Registry(state).cancel(job_id))


def _foreign_finish(state, job_id, output):
    from vidlift.mcp_bridge.registry import Registry, RegistryError
    try:
        Registry(state).finish_job(job_id, "cancelled")
    except RegistryError as exc:
        output.put(str(exc))
    else:
        output.put("closed")


def _ctx():
    return mp.get_context("spawn")


def _run_one(target, state, *args):
    ctx = _ctx()
    output = ctx.Queue()
    worker = ctx.Process(target=target, args=(str(state), *args, output))
    worker.start()
    result = output.get(timeout=20)
    worker.join(20)
    assert worker.exitcode == 0
    return result


def _run_pair(target, state, *args):
    ctx = _ctx()
    gate, output = ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=target, args=(str(state), gate, output, *args)) for _ in range(2)]
    for worker in workers:
        worker.start()
    gate.set()
    results = [output.get(timeout=20) for _ in workers]
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0
    return results


def test_spawned_enqueue_transactions_keep_both_rows(tmp_path):
    state = tmp_path / "state"
    ids = _run_pair(_enqueue_worker, state)
    from vidlift.mcp_bridge.registry import Registry
    persisted = Registry(state).queued()
    assert len(set(ids)) == 2
    assert {job.job_id for job in persisted} == set(ids)


def test_spawned_claims_are_unique_and_durable(tmp_path):
    state = tmp_path / "state"
    from vidlift.mcp_bridge.registry import Registry
    registry = Registry(state)
    expected = {registry.enqueue(kind="run", paths=[f"/{i}.mp4"], options={}).job_id for i in range(2)}
    claimed = _run_pair(_claim_worker, state)
    assert len([job_id for job_id in claimed if job_id]) == 1
    assert len(set(job_id for job_id in claimed if job_id)) == 1
    assert set(claimed) - {None} <= expected
    assert len(Registry(state).running()) == 1
    assert len(Registry(state).queued()) == 1


def test_foreign_cancel_requests_but_does_not_close_live_owner(tmp_path):
    state = tmp_path / "state"
    from vidlift.mcp_bridge.registry import Registry
    owner = Registry(state)
    job = owner.enqueue(kind="run", paths=["/synthetic.mp4"], options={})
    claimed = owner.claim_next()
    gate = _ctx().Event()
    gate.set()
    ctx = _ctx()
    # Concurrent duplicate cancel is permitted to report either request or
    # already-requested conflict, but neither may close the owner's job.
    gate.clear()
    out = ctx.Queue()
    workers = [ctx.Process(target=_cancel_observer, args=(str(state), gate, out, claimed.job_id)) for _ in range(2)]
    for worker in workers:
        worker.start()
    gate.set()
    results = [out.get(timeout=20) for _ in workers]
    for worker in workers:
        worker.join(20)
        assert worker.exitcode == 0
    assert sorted(results) == ["cancelling", "conflict"]
    observer = Registry(state)
    assert observer.require(job.job_id).state == "running"
    assert observer.require(job.job_id).to_dict().get("cancel_requested") is True
    owner.poll_cancellation()
    assert owner.get_cancel_flag(job.job_id).is_set()


def test_stale_process_cannot_close_recovered_claim(tmp_path):
    state = tmp_path / "state"
    from vidlift.mcp_bridge.registry import Registry, RegistryError
    owner = Registry(state)
    job = owner.enqueue(kind="run", paths=["/synthetic.mp4"], options={})
    claimed = owner.claim_next()
    result = _run_one(_foreign_finish, state, job.job_id)
    assert "stale owner" in result
    assert Registry(state).require(job.job_id).state == "running"
