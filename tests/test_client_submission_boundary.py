"""Real temporary registry, injected persistence faults, no executor threads."""
import json

import pytest

from vidlift.mcp_bridge.client_local import LocalBridgeClient
from vidlift.mcp_bridge.tools import BridgeError


@pytest.fixture
def bridge(tmp_path):
    client = LocalBridgeClient(state_dir=tmp_path / "registry", start_executor=False)
    yield client
    executor = client.services.executor
    assert executor is not None
    assert executor._threads == []
    client.stop()


@pytest.mark.parametrize("kind", ["subs", "run"])
@pytest.mark.parametrize("fault", ["before-enqueue", "before-save", "after-save", "after-enqueue",
                                  "before-notify", "after-notify", "fast-terminal"])
@pytest.mark.parametrize("exception", [BridgeError, FileNotFoundError])
def test_real_submission_boundary(bridge, tmp_path, monkeypatch, kind, fault, exception):
    path = tmp_path / "source.mkv"
    path.write_bytes(b"source sentinel")
    sidecar = path.with_suffix(".srt")
    sidecar.write_bytes(b"sidecar sentinel")
    options = {"replace_source": True, "suppress_srt": True, "device": "cpu"}
    registry = bridge.registry
    original_enqueue = registry.enqueue_idempotent
    original_save = registry._save_unlocked
    original_notify = bridge.services.executor.notify_new_work
    notifications = []

    def fail():
        if exception is BridgeError:
            raise BridgeError("io_error", "synthetic boundary failure")
        raise exception("synthetic boundary failure")

    def enqueue(*args, **kwargs):
        if fault == "before-enqueue":
            fail()
        result = original_enqueue(*args, **kwargs)
        if fault == "after-enqueue":
            fail()
        return result

    def save():
        if fault == "before-save":
            fail()
        original_save()
        if fault == "after-save":
            fail()

    def notify():
        if fault == "before-notify":
            fail()
        original_notify()
        notifications.append(True)
        if fault == "fast-terminal":
            record = registry.queued()[0]
            registry.transition(record, "running")
            registry.transition(record, "failed")
            assert bridge.active_job_count() == 0
            fail()
        if fault == "after-notify":
            fail()

    monkeypatch.setattr(registry, "enqueue_idempotent", enqueue)
    monkeypatch.setattr(registry, "_save_unlocked", save)
    monkeypatch.setattr(bridge.services.executor, "notify_new_work", notify)
    result = bridge.submit_gui_job(kind, str(path), options=options, engine="stub")
    if fault == "after-save":
        assert result.error is None  # committed image resolves post-replace failure
    else:
        assert isinstance(result.error, exception)
    accepted = fault in {"after-save", "after-enqueue", "before-notify", "after-notify", "fast-terminal"}
    assert result.state == ("accepted" if accepted else "unknown")
    assert len(result.job_ids) == int(accepted)
    assert len(registry._jobs) == int(accepted)  # pre-save ghosts are forbidden
    if registry.path.exists():
        persisted = json.loads(registry.path.read_text())["jobs"]
        assert len(persisted) == int(accepted)
        if accepted:
            assert persisted[0]["kind"] == kind
            assert persisted[0]["options"]["replace_source"] is True
            assert persisted[0]["options"]["suppress_srt"] is True
            assert persisted[0]["job_id"] == result.job_ids[0]
    assert bool(notifications) == (fault in {"after-save", "after-notify", "fast-terminal"})
    assert path.read_bytes() == b"source sentinel"
    assert sidecar.read_bytes() == b"sidecar sentinel"


@pytest.mark.parametrize("kind", ["subs", "run"])
def test_proven_validation_failure_is_safe_to_retry(bridge, tmp_path, monkeypatch, kind):
    path = tmp_path / "initially-missing.mkv"
    notifications = []
    monkeypatch.setattr(bridge.services.executor, "notify_new_work", lambda: notifications.append(True))
    result = bridge.submit_gui_job(kind, str(path), options={"replace_source": True, "suppress_srt": True})
    assert result.state == "rejected"
    assert isinstance(result.error, FileNotFoundError)
    assert result.job_ids == ()
    assert not bridge.registry._jobs
    assert not bridge.registry.path.exists()
    assert not notifications
    path.write_bytes(b"synthetic source")
    retry = bridge.submit_gui_job(kind, str(path))
    assert retry.state == "accepted"
    assert len(retry.job_ids) == 1
    assert notifications == [True]
    assert bridge.registry.require(retry.job_ids[0]).paths == [str(path)]


def test_invalid_device_rejected_before_registry(bridge, tmp_path):
    path = tmp_path / "source.mkv"
    path.write_bytes(b"sentinel")
    result = bridge.submit_gui_job("subs", str(path), options={"device": "invalid"})
    assert result.state == "rejected"
    assert isinstance(result.error, ValueError)
    assert not bridge.registry._jobs


@pytest.mark.parametrize("kind", ["subs", "run"])
def test_public_enqueue_preserves_original_exception_and_list_contract(bridge, tmp_path, monkeypatch, kind):
    path = tmp_path / "source.mkv"
    path.write_bytes(b"sentinel")
    enqueue = bridge.enqueue_subs if kind == "subs" else bridge.enqueue_run
    error = BridgeError("io_error", "synthetic notifier failure")

    def fail():
        raise error

    monkeypatch.setattr(bridge.services.executor, "notify_new_work", fail)
    with pytest.raises(BridgeError) as caught:
        enqueue([str(path)])
    assert caught.value is error
    assert len(bridge.registry.queued()) == 1
    monkeypatch.setattr(bridge.services.executor, "notify_new_work", lambda: None)
    other = tmp_path / "other.mkv"
    other.write_bytes(b"sentinel")
    ids = enqueue([str(other)])
    assert isinstance(ids, list)
    assert len(ids) == 1
    assert bridge.registry.require(ids[0]).paths == [str(other)]
