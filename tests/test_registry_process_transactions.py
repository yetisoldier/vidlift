"""Real spawned-process registry transaction, claim and lease probes."""
import json
import os
from pathlib import Path
import subprocess
import sys

SCRIPT = r'''
import json, sys, time
from pathlib import Path
from vidlift.mcp_bridge.registry import Registry
from vidlift.mcp_bridge.registry_transactions import canonical_payload
root, mode, key = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
registry = Registry(root)
Path(sys.argv[4]).write_text('ready')
while not Path(sys.argv[5]).exists(): time.sleep(.01)
if mode == 'receipt':
    payload = canonical_payload('subs', root/'synthetic.mkv', {'device':'cpu','batch_id':'same'})
    registry.prepare_submission(key, payload)
    result = registry.enqueue_idempotent(key, payload)['job_ids']
elif mode == 'enqueue':
    result = [registry.enqueue(kind='subs', paths=[key], options={}).job_id]
else:
    record = registry.claim_next()
    result = [record.job_id] if record else []
print(json.dumps(result), flush=True)
'''


def run_pair(tmp_path, mode, keys):
    root = tmp_path / "state"
    root.mkdir(exist_ok=True)
    gate = tmp_path / "go"
    processes = [subprocess.Popen([sys.executable, "-c", SCRIPT, str(root), mode, key,
                 str(tmp_path / f"ready-{i}"), str(gate)], stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, text=True) for i, key in enumerate(keys)]
    try:
        import time
        deadline = time.monotonic() + 5
        while not all((tmp_path / f"ready-{i}").exists() for i in range(2)):
            assert time.monotonic() < deadline
            time.sleep(.01)
        gate.write_text("go")
        outputs = [p.communicate(timeout=10) for p in processes]
        assert all(p.returncode == 0 for p in processes), outputs
        return [json.loads(out) for out, err in outputs], json.loads((root / "jobs.json").read_text())
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
                p.communicate()


def test_two_processes_same_key_one_job(tmp_path):
    outputs, image = run_pair(tmp_path, "receipt", ["a" * 32] * 2)
    assert outputs[0] == outputs[1] and len(image["jobs"]) == len(image["submissions"]) == 1


def test_two_processes_independent_enqueues_retained(tmp_path):
    outputs, image = run_pair(tmp_path, "enqueue", ["a", "b"])
    assert len({ids[0] for ids in outputs}) == len(image["jobs"]) == 2


def test_two_processes_only_one_claim(tmp_path):
    from vidlift.mcp_bridge.registry import Registry
    root = tmp_path / "state"
    registry = Registry(root)
    record = registry.enqueue(kind="subs", paths=["synthetic"], options={})
    outputs, image = run_pair(tmp_path, "claim", ["a", "b"])
    assert sorted(map(len, outputs)) == [0, 1]
    assert [ids[0] for ids in outputs if ids] == [record.job_id]
    assert image["jobs"][0]["state"] == "running"


def test_live_executor_lease_startup_and_foreign_cancel(tmp_path):
    script = r'''
import sys,time
from pathlib import Path
from vidlift.mcp_bridge.server import build_services
from vidlift.mcp_bridge.jobs import JobExecutor, JobOutcome
root = Path(sys.argv[1])
services = build_services(root)
def execute(record, services, flag):
    (root/'running').write_text(record.job_id)
    deadline=time.monotonic()+8
    while not flag() and time.monotonic()<deadline: time.sleep(.02)
    (root/'observed').write_text(str(flag()))
    return JobOutcome(ok=False, cancelled=flag())
services.executor=JobExecutor(services, executor_fn=execute)
services.executor.start()
while not (root/'observed').exists(): time.sleep(.02)
services.executor.stop()
'''
    from vidlift.mcp_bridge.registry import Registry
    from vidlift.mcp_bridge.server import build_services
    import time
    root = tmp_path / "state"
    registry = Registry(root)
    record = registry.enqueue(kind="subs", paths=["synthetic"], options={})
    process = subprocess.Popen([sys.executable, "-c", script, str(root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not (root / "running").exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        observer = build_services(root, gui_startup=True)
        assert observer.registry.require(record.job_id).state == "running"
        assert observer.registry.cancel(record.job_id) == "cancelling"
        out, err = process.communicate(timeout=10)
        assert process.returncode == 0, (out, err)
        assert (root / "observed").read_text() == "True"
        assert registry.require(record.job_id).state == "cancelled"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
