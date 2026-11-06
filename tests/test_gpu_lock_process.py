"""Real process contention against an isolated GPU-phase-lock path."""
import json
from pathlib import Path
import subprocess
import sys

from vidlift.engine_ncnn import GpuPhaseLock


SCRIPT = '''
import json,sys,time
from pathlib import Path
from vidlift.engine_ncnn import GpuPhaseLock
from vidlift.gpu_arbitration import MachineGpuMutex
lock = GpuPhaseLock(Path(sys.argv[2]), backend=MachineGpuMutex(test_path=Path(sys.argv[1])))
spans=[]
for _ in range(3):
    assert lock.acquire(deadline_seconds=5)
    start=time.monotonic()
    time.sleep(0.04)
    end=time.monotonic()
    lock.release()
    spans.append([start,end])
print(json.dumps(spans),flush=True)
'''


def test_four_processes_never_overlap_held_intervals(tmp_path):
    path = tmp_path / ".gpu-phase.lock"
    shared = tmp_path / "kernel-v3"
    shared.touch()
    paths = [tmp_path / str(i) / "record" for i in range(4)]
    processes = [subprocess.Popen([sys.executable, "-c", SCRIPT, str(shared), str(private)],
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for private in paths]
    try:
        results = [p.communicate(timeout=15) for p in processes]
        assert all(p.returncode == 0 for p in processes), results
        spans = sorted(span for out, err in results for span in json.loads(out))
        assert len(spans) == 12
        assert all(left[1] <= right[0] for left, right in zip(spans, spans[1:])), spans
        assert all(not private.exists() for private in paths)
        # Persistent arbitration inode is intentional, not a stale PID lock.
        assert shared.exists()
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
                p.communicate(timeout=5)
