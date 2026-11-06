"""Synthetic group identity from submission to executor and watchdog policy."""
import json
from types import SimpleNamespace

import pytest

from vidlift import transcribe, subs_job, run
from vidlift.mcp_bridge.client_local import LocalBridgeClient
from vidlift.mcp_bridge import jobs, tools
from vidlift.mcp_bridge.jobs import CancelFlag
from vidlift.transcription_watchdog import WorkerTimeout, WorkerCancelled


@pytest.fixture
def bridge(tmp_path):
    return LocalBridgeClient(state_dir=tmp_path / "state", start_executor=False)


@pytest.fixture(autouse=True)
def latch():
    transcribe.reset_cuda_failure_cache()
    yield
    transcribe.reset_cuda_failure_cache()


def sources(tmp_path):
    paths = [tmp_path / name for name in ("a.mkv", "b.mkv")]
    for p in paths:
        p.write_bytes(b"synthetic source")
    return list(map(str, paths))


@pytest.mark.parametrize("kind", ["subs", "run"])
def test_multi_path_enqueue_shares_identity_and_new_call_refreshes(bridge, tmp_path, kind):
    enqueue = getattr(bridge, "enqueue_" + kind)
    paths = sources(tmp_path)
    ids = enqueue(paths)
    records = [bridge.registry.require(i) for i in ids]
    batch = records[0].options["batch_id"]
    assert batch and all(r.options["batch_id"] == batch for r in records)
    fresh = enqueue([paths[0]])[0]
    assert bridge.registry.require(fresh).options["batch_id"] != batch
    persisted = json.loads(bridge.registry.path.read_text())["jobs"]
    assert [r["options"]["batch_id"] for r in persisted[:2]] == [batch, batch]


@pytest.mark.parametrize("kind", ["subs", "run"])
def test_gui_receipts_preserve_explicit_identity(bridge, tmp_path, kind):
    for path in sources(tmp_path):
        receipt = bridge.submit_gui_job(kind, path, options={"batch_id": "start-click", "device": "cuda"})
        assert receipt.state == "accepted"
        record = bridge.registry.require(receipt.job_ids[0])
        assert record.options["batch_id"] == "start-click"
        if kind == "subs":
            assert record.options["device"] == "cuda"
    assert "batch_id" not in bridge._subs_options("auto", False, False)


@pytest.mark.parametrize("kind", ["subs", "run"])
@pytest.mark.parametrize("batch", [None, "shared-batch"])
def test_executor_reads_options_not_per_file_identity(bridge, tmp_path, monkeypatch, kind, batch):
    paths = sources(tmp_path)
    received = []
    options = {} if batch is None else {"batch_id": batch}
    def capture(path, **kwargs):
        received.append(kwargs)
        if kind == "subs":
            raise subs_job.SubsJobError("synthetic stop")
        raise run.RunError("synthetic stop")
    monkeypatch.setattr(subs_job if kind == "subs" else run,
                        "run_subs_job" if kind == "subs" else "run_file", capture)
    monkeypatch.setattr(run, "find_tools", lambda **kw: SimpleNamespace(ffmpeg="fake", ffprobe="fake"))
    for path in paths:
        record = bridge.registry.enqueue(kind=kind, paths=[path], options=options)
        outcome = getattr(jobs, "_execute_" + kind + "_job")(record, bridge.services, CancelFlag())
        assert not outcome.ok
    key = "batch_id" if kind == "subs" else "transcription_batch_id"
    assert [kw[key] for kw in received] == [batch, batch]


@pytest.mark.parametrize("kind", ["transcribe", "run"])
def test_mcp_group_and_explicit_override(bridge, tmp_path, monkeypatch, kind):
    paths = sources(tmp_path)
    monkeypatch.setattr(subs_job, "subs_plan_verdict", lambda *a, **kw: "ready")
    call = getattr(tools, "tool_" + kind)
    result = call(bridge.services, {"paths": paths})
    batches = [bridge.registry.require(r["job_id"]).options["batch_id"] for r in result["jobs"]]
    assert batches[0] == batches[1] and batches[0]
    args = {"paths": paths, "batch_id": "explicit"} if kind == "transcribe" else {
        "paths": paths, "options": {"batch_id": "explicit"}}
    explicit = call(bridge.services, args)
    assert all(bridge.registry.require(r["job_id"]).options["batch_id"] == "explicit" for r in explicit["jobs"])


def test_invalid_device_is_pre_write_boundary(bridge, tmp_path, monkeypatch):
    notifications = []
    monkeypatch.setattr(bridge.services.executor, "notify_new_work", lambda: notifications.append(True))
    with pytest.raises(ValueError, match="device"):
        bridge.enqueue_subs(sources(tmp_path), device="bogus")
    assert not bridge.registry._jobs and not bridge.registry.path.exists() and not notifications


def invoke(batch, requested="cuda"):
    return transcribe._isolated_transcription("model", "missing.wav", requested, "en", False,
                                             None, None, None, None, batch_id=batch)


def test_same_group_cached_new_explicit_cuda_retries_and_success_clears(monkeypatch):
    from vidlift import transcription_watchdog as wd
    attempts = []
    success = False
    def worker(target, args, **kwargs):
        attempts.append(args[0])
        if args[0] != "cpu" and not success:
            raise WorkerTimeout("init")
        return "ok", "cuda" if success else "cpu", "en", None, []
    monkeypatch.setattr(wd, "supervise_worker", worker)
    invoke("A")
    second = invoke("A")
    assert attempts == ["cuda", "cpu", "cpu"]
    assert any("cached" in note for note in second[-1])
    invoke("B")
    assert attempts[-2:] == ["cuda", "cpu"]
    success = True
    invoke("C")
    assert transcribe._cached_cuda_failure("B") is None
    invoke("B")
    assert attempts[-1] == "cuda"


def test_strict_cpu_never_reads_or_writes_slot(monkeypatch):
    from vidlift import transcription_watchdog as wd
    transcribe._remember_cuda_failure("A", "init_timeout", "old evidence")
    def forbidden(*a, **kw):
        pytest.fail("strict cpu accessed CUDA latch")
    monkeypatch.setattr(transcribe, "_cached_cuda_failure", forbidden)
    monkeypatch.setattr(transcribe, "_remember_cuda_failure", forbidden)
    monkeypatch.setattr(wd, "supervise_worker", lambda target, args, **kw: ("ok", "cpu", "en", None, []))
    invoke("A", "cpu")
    assert transcribe._cuda_failure_batch == "A"
    assert transcribe._cuda_failure_reason == ("init_timeout", "old evidence")


@pytest.mark.parametrize("outcome", ["cancel", "post-progress", "post-progress-error"])
def test_cancellation_and_post_progress_never_latch(monkeypatch, outcome):
    from vidlift import transcription_watchdog as wd
    def worker(target, args, on_message, **kwargs):
        if outcome == "cancel":
            raise WorkerCancelled()
        on_message("resolved", "cuda")
        on_message("progress", object())
        if outcome == "post-progress-error":
            return "error", "decode", 1, "RuntimeError"
        raise WorkerTimeout("decode")
    monkeypatch.setattr(wd, "supervise_worker", worker)
    with pytest.raises((run.EngineCancelled, transcribe.TranscriptionError)):
        invoke("A")
    assert transcribe._cached_cuda_failure("A") is None


def test_cli_group_identity_and_explicit_cuda(monkeypatch, tmp_path):
    from vidlift import cli
    paths = sources(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "find_tools", lambda **kw: SimpleNamespace(ffmpeg="fake", ffprobe="fake"))
    monkeypatch.setattr(subs_job, "subs_plan_verdict", lambda *a, **kw: "ready")
    def fake_subs(path, **kw):
        calls.append(kw)
        return SimpleNamespace(output_path="synthetic", language="en", language_confidence=1,
                               cue_count=1, model="fake", device="cuda", notes=[])
    monkeypatch.setattr(subs_job, "run_subs_job", fake_subs)
    for _ in range(2):
        assert cli.main(["subs", *paths, "--device", "cuda"]) == 0
    first = calls[0]["batch_id"]
    second = calls[2]["batch_id"]
    assert first and second and first != second
    assert [c["batch_id"] for c in calls] == [first, first, second, second]
    assert all(c["device_pin"] == "cuda" for c in calls)
    received = []
    def fake_run(path, **kw):
        received.append(kw["transcription_batch_id"])
        raise run.RunError("synthetic stop")
    monkeypatch.setattr(cli, "run_file", fake_run)
    for _ in range(2):
        assert cli.main(["run", paths[0]]) == 1
    assert received[0] and received[1] and received[0] != received[1]


def test_registry_multifile_group_reaches_spawned_core_latch(bridge, monkeypatch, tmp_path):
    from test_transcription_process_integration import prepare
    from vidlift import engine_ncnn
    first, runner, lock = prepare(monkeypatch, tmp_path)
    monkeypatch.setattr(engine_ncnn, "_gpu_phase_lock", lock)
    monkeypatch.setattr(run, "find_tools", lambda **kw: SimpleNamespace(ffmpeg="fake", ffprobe="fake"))
    bridge.services.command_runner = runner
    second = tmp_path / "second.mkv"
    second.write_bytes(b"second sentinel")
    attempts = tmp_path / "attempts"
    monkeypatch.setenv("VIDLIFT_TEST_ATTEMPT_LOG", str(attempts))
    ids = bridge.enqueue_subs([str(first), str(second)], device="cuda")
    outcomes = [jobs._execute_subs_job(bridge.registry.require(i), bridge.services, CancelFlag()) for i in ids]
    assert all(o.ok and o.result is not None and o.result["device"] == "cpu" for o in outcomes)
    assert attempts.read_text().splitlines() == ["cuda", "cpu", "cpu"]
    assert outcomes[1].result is not None
    assert any("cached" in note for note in outcomes[1].result["notes"])
    live = bridge.registry.require(ids[1]).get_live("state_box")["state"]
    assert "cached" in live.progress["transcription_fallback_reason"]
    third = tmp_path / "third.mkv"
    third.write_bytes(b"third sentinel")
    fresh = bridge.enqueue_subs([str(third)], device="cuda")[0]
    outcome = jobs._execute_subs_job(bridge.registry.require(fresh), bridge.services, CancelFlag())
    assert outcome.ok
    assert attempts.read_text().splitlines() == ["cuda", "cpu", "cpu", "cuda", "cpu"]
    assert first.read_bytes() == b"source sentinel"
    assert second.read_bytes() == b"second sentinel"
    assert third.read_bytes() == b"third sentinel"


def test_empty_string_is_an_opaque_identity_not_none(monkeypatch):
    from vidlift import transcription_watchdog as wd
    calls = []
    def worker(target, args, **kwargs):
        calls.append(args[0])
        if args[0] == "cuda":
            raise WorkerTimeout("init")
        return "ok", "cpu", "en", None, []
    monkeypatch.setattr(wd, "supervise_worker", worker)
    invoke("")
    invoke("")
    assert calls == ["cuda", "cpu", "cpu"]
    assert transcribe._cached_cuda_failure("") is not None
