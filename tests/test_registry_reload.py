# v0.4.5 Slice 3 (D2) — registry mtime-based reload.

# Design docs/designs/v0.4.5-progress-honesty.md §4/§8 (tests 14-19):
# reload on mtime change, no-reload unchanged, cancel-flag identity
# preservation, state_box preservation, soft-fail corrupt file, and
# save-updates-mtime. Two Registry instances over one state dir play
# "two processes"; the _stat_mtime_or_none seam plays time for the
# deterministic no-parse spy.

from __future__ import annotations

import json

import pytest

from vidlift.mcp_bridge.jobs import CancelFlag
from vidlift.mcp_bridge.registry import Registry


@pytest.fixture
def state_dir(tmp_path):
    path = tmp_path / "state"
    path.mkdir(parents=True)
    return path


def _enqueue(registry: Registry, job_id: str | None = None):
    record = registry.enqueue(
        kind="run", paths=["/x/movie.avi"], options={}
    )
    if job_id:
        record.job_id = job_id
    return record


def test_reload_on_mtime_change(state_dir):
    """Test 14: registry B (a 'second process') enqueues; registry A
    sees the new job on its next read without restarting."""
    a = Registry(state_dir)
    b = Registry(state_dir)
    record = b.enqueue(kind="run", paths=["/x/other.avi"], options={})
    # a's mtime baseline predates b's write (same-instance save would
    # have refreshed it; a fresh Registry's stat does not)
    a._loaded_mtime = -1.0  # any later stat reads as "changed"
    seen = a.get(record.job_id)
    assert seen is not None
    assert seen.paths == ["/x/other.avi"]
    assert seen.state == "queued"


def test_no_reload_when_mtime_unchanged(state_dir, monkeypatch):
    """Test 15: repeated reads with an unchanged mtime parse at most
    once — the common case is one stat per call, zero parses."""
    registry = Registry(state_dir)
    registry.enqueue(kind="run", paths=["/x/movie.avi"], options={})
    registry.get("whatever")  # first read may parse (mtime baseline)
    # Force the recorded mtime so _maybe_reload's stat compares equal,
    # then count parses: none may happen while the file is untouched.
    registry._loaded_mtime = registry._stat_mtime_or_none()
    parses: list[int] = []
    original_parse = registry._parse_rows

    def spy_parse():
        parses.append(1)
        return original_parse()

    monkeypatch.setattr(registry, "_parse_rows", spy_parse)
    registry._loaded_mtime = registry._stat_mtime_or_none()
    for _ in range(5):
        assert registry.get("nonexistent") is None
    assert parses == []


def test_reload_preserves_cancel_flags(state_dir):
    """Test 16: a running job's live CancelFlag survives another
    writer's enqueue — same object identity after the reload."""
    a = Registry(state_dir)
    record = a.enqueue(kind="run", paths=["/x/movie.avi"], options={})
    a.transition(record, "running")
    flag = a.get_cancel_flag(record.job_id)
    assert flag is not None
    b = Registry(state_dir)
    assert b.get(record.job_id).state == "running"
    b.enqueue(kind="run", paths=["/x/other.avi"], options={})
    seen = a.get(record.job_id)
    assert seen.state == "running"
    # A running job in the FILE has no cancel flag in the file (the
    # flag is process-local); THIS server's flag must survive verbatim.
    assert a.get_cancel_flag(record.job_id) is flag
    assert a.get_cancel_flag(record.job_id).is_set() is False


def test_reload_preserves_live_state_box(state_dir):
    """Test 17: a live job's _live state_box survives the reload by
    reference — the executor's status seam never drops."""
    a = Registry(state_dir)
    record = a.enqueue(kind="run", paths=["/x/movie.avi"], options={})
    box: dict = {"state": "sentinel"}
    record.set_live("state_box", box)
    a.transition(record, "running")
    b = Registry(state_dir)
    b.enqueue(kind="run", paths=["/x/other.avi"], options={})
    seen = a.get(record.job_id)
    assert seen.get_live("state_box") is box
    assert seen.get_live("state_box")["state"] == "sentinel"


def test_reload_failure_is_soft(state_dir, capsys):
    """Test 18: a corrupt file between an mtime bump and a read is a
    soft failure — the reader keeps the in-memory view and logs; no
    raise (the hard raise stays at startup load())."""
    a = Registry(state_dir)
    record = a.enqueue(kind="run", paths=["/x/movie.avi"], options={})
    baseline = a._loaded_mtime
    # corrupt the file WITHOUT changing its mtime (deterministic: the
    # seam bumps the recorded mtime; the content is garbage)
    registry_path = state_dir / "jobs.json"
    registry_path.write_text("{corrupt json", encoding="utf-8")
    a._loaded_mtime = -12345.0  # guarantees a "changed" mtime branch
    seen = a.get(record.job_id)
    assert seen is not None  # in-memory view preserved
    assert seen.paths == ["/x/movie.avi"]
    out = capsys.readouterr().out
    assert "registry reload failed" in out
    assert baseline is not None  # (sanity: the fixture had a real mtime)


def test_save_updates_loaded_mtime(state_dir, monkeypatch):
    """Test 19: save() stamps _loaded_mtime from the post-write stat —
    a save->read sequence does not re-parse our own write."""
    registry = Registry(state_dir)
    registry.enqueue(kind="run", paths=["/x/movie.avi"], options={})
    registry._loaded_mtime = registry._stat_mtime_or_none()
    parses: list[int] = []
    original_parse = registry._parse_rows

    def spy_parse():
        parses.append(1)
        return original_parse()

    monkeypatch.setattr(registry, "_parse_rows", spy_parse)
    registry.transition(
        registry.require(
            [r.job_id for r in registry.queued()][0]
        ),
        "cancelled",
    )  # a state transition saves internally
    # the save bumped its own mtime; reads now see no change at all
    assert registry.get("whatever") is None
    assert parses == []
    # and the recorded mtime matches the file
    assert registry._loaded_mtime == registry._stat_mtime_or_none()