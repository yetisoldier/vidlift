"""Seed crash-recovery fixtures with a real process that has exited."""
import json
import subprocess
import sys

from vidlift.mcp_bridge.registry import Registry
from vidlift.mcp_bridge.registry_transactions import owner_alive


_SEED = """
import json
import sys
from vidlift.mcp_bridge.registry import Registry
registry = Registry(sys.argv[1])
for spec in json.loads(sys.argv[2]):
    record = registry.enqueue(kind='run', paths=[spec['path']], options=spec.get('options', {}))
    state = spec['state']
    if state != 'queued':
        registry.transition(record, 'running')
    if state not in {'queued', 'running'}:
        record.error = spec.get('error')
        registry.transition(record, state)
"""


def seed_exited_registry(state_dir, jobs):
    """Create only synthetic records; never start an executor or open media."""
    subprocess.run([sys.executable, "-c", _SEED, str(state_dir), json.dumps(jobs)],
                   check=True, capture_output=True, text=True, timeout=15)
    registry = Registry(state_dir)
    for record in [*registry.queued(), *registry.running()]:
        row = record.to_dict()
        fact = row["owner" if record.state == "running" else "origin"]
        assert owner_alive(fact) is False, "fixture must prove its owner has exited"
    return registry
