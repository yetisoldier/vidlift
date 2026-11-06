"""Durable identity and committed-only failure/restart boundary regressions."""
import copy
import json
from pathlib import Path
import uuid

import pytest

from vidlift.mcp_bridge.registry import Registry, RegistryError, STARTUP_SWEEP_ERROR
from vidlift.mcp_bridge.registry_transactions import canonical_payload
from vidlift.mcp_bridge.client_local import LocalBridgeClient


def payload(tmp_path, kind="subs"):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source sentinel")
    return canonical_payload(kind, source, {"device": "cuda", "replace_source": True,
        "suppress_srt": False, "batch_id": "frozen-batch", "engine": "stub"})


@pytest.mark.parametrize("kind", ["subs", "run"])
def test_prepared_nonexecutable_accepted_replay_and_conflict(tmp_path, kind):
    registry = Registry(tmp_path / "state")
    key = uuid.uuid4().hex
    data = payload(tmp_path, kind)
    prepared = registry.prepare_submission(key, data)
    assert prepared["state"] == "prepared" and not registry.queued()
    registry = Registry(registry.path.parent)
    assert registry.lookup_submission(key) == prepared
    accepted = registry.enqueue_idempotent(key, data)
    original = registry.path.read_bytes()
    assert len(accepted["job_ids"]) == 1
    assert registry.enqueue_idempotent(key, data) == accepted
    assert registry.path.read_bytes() == original
    changed = copy.deepcopy(data)
    changed["options"]["device"] = "cpu"
    with pytest.raises(RegistryError, match="idempotency_conflict"):
        registry.enqueue_idempotent(key, changed)
    assert registry.path.read_bytes() == original
    record = registry.require(accepted["job_ids"][0])
    registry.transition(record, "running")
    token = (record._extra.get("owner") or {}).get("claim_token")
    registry.finish_job(accepted["job_ids"][0], "failed", owner_token=token, error="synthetic failure", result={"notes": ["retained"]})
    terminal = Registry(registry.path.parent)
    assert terminal.enqueue_idempotent(key, data) == accepted
    assert terminal.require(accepted["job_ids"][0]).state == "failed"
    assert Path(data["source"]).read_bytes() == b"source sentinel"


@pytest.mark.parametrize("stage", ["prepare", "commit"])
@pytest.mark.parametrize("after", [False, True])
def test_write_failure_never_exposes_executable_ghost(tmp_path, monkeypatch, stage, after):
    registry = Registry(tmp_path / "state")
    key, data = uuid.uuid4().hex, payload(tmp_path)
    if stage == "commit":
        registry.prepare_submission(key, data)
    save = registry._save_unlocked
    def broken():
        if after:
            save()
        raise OSError("injected save boundary")
    monkeypatch.setattr(registry, "_save_unlocked", broken)
    operation = registry.prepare_submission if stage == "prepare" else registry.enqueue_idempotent
    if after:
        receipt = operation(key, data)
        assert receipt["state"] == ("prepared" if stage == "prepare" else "accepted")
    else:
        with pytest.raises(OSError):
            operation(key, data)
    durable = Registry(registry.path.parent)
    assert len(registry._jobs) == len(durable._jobs) == int(stage == "commit" and after)
    assert not list(registry.path.parent.glob(".jobs-*.tmp"))


def test_gui_sweep_preserves_receipt_and_is_byte_noop_twice(tmp_path):
    registry = Registry(tmp_path / "state")
    key, data = uuid.uuid4().hex, payload(tmp_path)
    registry.prepare_submission(key, data)
    receipt = registry.enqueue_idempotent(key, data)
    # Prove previous submitter dead using an impossible PID fact in scratch.
    image = json.loads(registry.path.read_text())
    image["jobs"][0]["origin"] = {"pid": 99999999, "creation_time": {"kind": "unavailable"}}
    registry.path.write_text(json.dumps(image))
    restarted = Registry(registry.path.parent)
    assert len(restarted.sweep_orphans(include_queued=True, error=STARTUP_SWEEP_ERROR)) == 1
    record = restarted.require(receipt["job_ids"][0])
    assert record.state == "failed" and record.started_at is None
    assert record.error == STARTUP_SWEEP_ERROR
    before = restarted.path.read_bytes()
    assert restarted.sweep_orphans(include_queued=True) == []
    assert restarted.enqueue_idempotent(key, data) == receipt
    assert restarted.path.read_bytes() == before


def test_live_claim_not_swept_and_foreign_cancel_is_request(tmp_path):
    owner = Registry(tmp_path)
    job = owner.enqueue(kind="subs", paths=["synthetic"], options={})
    claimed = owner.claim_next()
    token = claimed._extra["owner"]["claim_token"]
    flag = owner.get_cancel_flag(job.job_id)
    other = Registry(tmp_path)
    assert other.sweep_orphans(include_queued=True) == []
    assert other.cancel(job.job_id) == "cancelling"
    assert other.require(job.job_id).state == "running"
    owner.poll_cancellation()
    assert flag.is_set()
    owner.finish_job(job.job_id, "cancelled", owner_token=token, ok=False)
    assert other.require(job.job_id).state == "cancelled"


def test_abandoned_corruption_unknown_fields_and_result_boundary(tmp_path):
    registry = Registry(tmp_path / "state")
    key, data = uuid.uuid4().hex, payload(tmp_path)
    registry.prepare_submission(key, data)
    registry.abandon_submission(key)
    before = registry.path.read_bytes()
    assert registry.enqueue_idempotent(key, data)["state"] == "abandoned"
    assert not registry.queued() and registry.path.read_bytes() == before
    key = uuid.uuid4().hex
    registry.prepare_submission(key, data)
    accepted = registry.enqueue_idempotent(key, data)
    image = json.loads(registry.path.read_text())
    image["future"] = {"keep": True}
    image["jobs"][0]["future_job"] = 99
    image["submissions"][key]["future_receipt"] = 100
    registry.path.write_text(json.dumps(image))
    registry = Registry(registry.path.parent)
    claimed = registry.claim_next()
    token = claimed._extra["owner"]["claim_token"]
    registry.finish_job(accepted["job_ids"][0], "succeeded", owner_token=token, ok=True, result={"output_path": "synthetic"})
    image = json.loads(registry.path.read_text())
    assert image["future"] == {"keep": True} and image["jobs"][0]["future_job"] == 99
    assert image["submissions"][key]["future_receipt"] == 100
    assert image["jobs"][0]["result"] == {"output_path": "synthetic"}
    image["jobs"] = []
    registry.path.write_text(json.dumps(image))
    with pytest.raises(RegistryError, match="missing job"):
        Registry(registry.path.parent)


@pytest.mark.parametrize("kind", ["run", "subs"])
def test_client_notification_failure_and_terminal_reply_reconcile(tmp_path, monkeypatch, kind):
    source = tmp_path / "source.mkv"
    source.write_bytes(b"source")
    client = LocalBridgeClient(state_dir=tmp_path / "state", start_executor=False)
    key = uuid.uuid4().hex
    opts = {"device": "cpu", "batch_id": "frozen", "replace_source": False}
    notices = []
    def notify():
        notices.append(True)
        job = client.registry.claim_next()
        token = job._extra["owner"]["claim_token"]
        client.registry.finish_job(job.job_id, "failed", owner_token=token, error="fast terminal")
        raise OSError("reply lost")
    monkeypatch.setattr(client.services.executor, "notify_new_work", notify)
    result = client.submit_gui_job(kind, str(source), options=opts, engine="stub", submission_id=key)
    assert result.state == "accepted" and isinstance(result.error, OSError)
    assert client.registry.require(result.job_ids[0]).state == "failed"
    replay = client.submit_gui_job(kind, str(source), options=opts, engine="stub", submission_id=key)
    assert replay.job_ids == result.job_ids and len(notices) == 1
    assert len(client.submission_recovery()) == 1
