"""Regression checks for observed-ledger loss and claim ownership."""
import pytest

from vidlift.mcp_bridge.registry import JobRecord, Registry, RegistryError
from vidlift.mcp_bridge.registry_transactions import canonical_payload


def test_observed_ledger_disappearance_blocks_lookup_and_enqueue(tmp_path):
    writer = Registry(tmp_path)
    key = "a" * 32
    payload = canonical_payload("subs", tmp_path / "source.mkv", {"device": "cpu"})
    writer.prepare_submission(key, payload)
    receipt = writer.enqueue_idempotent(key, payload)
    reader = Registry(tmp_path)
    assert reader.lookup_submission(key)["job_ids"] == receipt["job_ids"]
    (tmp_path / "jobs.json").unlink()
    for registry in (writer, reader):
        with pytest.raises(RegistryError, match="disappeared"):
            registry.lookup_submission(key)
        with pytest.raises(RegistryError, match="disappeared"):
            registry.enqueue(kind="subs", paths=["other"], options={})
        assert not (tmp_path / "jobs.json").exists()


def test_terminal_transition_requires_original_claim_token(tmp_path):
    registry = Registry(tmp_path)
    job = registry.enqueue(kind="run", paths=["movie.avi"], options={})
    registry.transition(job, "running")
    stale = JobRecord.from_dict(job.to_dict())
    stale._extra["owner"]["claim_token"] = "stale-token"
    with pytest.raises(RegistryError, match="stale owner"):
        registry.transition(stale, "succeeded")
    assert registry.require(job.job_id).state == "running"
    registry.transition(job, "succeeded")
    assert registry.require(job.job_id).state == "succeeded"
