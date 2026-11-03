"""v0.4.16 hermeticity: the suite's DEFAULT state-root resolution never
lands on the real user profile.

The forge task's scope B: the suite must never touch ``~/.vidlift`` —
no discovery-card writes, no machine-lock churn on the real root, no
warm-cache reads of the dev session's model cache. conftest's
``_hermetic_state_root`` autouse fixture reroutes
``benchmark.state_dir()``'s default arm per test; these tests pin the
mechanism:

1. with ``$VIDLIFT_STATE_DIR`` unset, the resolved state root lives
   under pytest's tmp base, NEVER ``~/.vidlift``;
2. an explicit ``$VIDLIFT_STATE_DIR`` still wins (the env-resolved
   tests keep passing);
3. the machine GPU-phase lock (the only real-root churn the old suite
   produced — 50 create/unlink cycles per run) rides the SAME
   resolution, so an acquire+release leaves nothing under the user
   profile and the lock file lives (and is cleaned) under the tmp
   root.
"""

from __future__ import annotations

import os
from pathlib import Path

import vidlift.benchmark as benchmark_mod
from vidlift.engine_ncnn import _gpu_phase_lock


def test_default_state_root_is_hermetic_never_the_user_profile():
    """The autouse fixture is active: with NO env pin, state_dir()
    resolves to a per-test tmp root — never ``~/.vidlift``."""
    os.environ.pop("VIDLIFT_STATE_DIR", None)
    resolved = benchmark_mod.state_dir()
    assert resolved != Path.home() / ".vidlift"
    # pytest's tmp base (or the fixture root) — never the user profile
    assert ".vidlift" != resolved.name
    assert "vidlift-state" in str(resolved) or "pytest" in str(resolved)


def test_env_still_wins_over_the_isolated_default(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDLIFT_STATE_DIR", str(tmp_path / "envstate"))
    assert benchmark_mod.state_dir() == tmp_path / "envstate"


def test_explicit_argument_still_wins(tmp_path):
    assert benchmark_mod.state_dir(str(tmp_path / "explicit")) == (
        tmp_path / "explicit"
    )


def test_gpu_phase_lock_never_touches_the_user_profile(tmp_path):
    """The old suite's 50 create/unlink cycles on
    ``~/.vidlift/.gpu-phase.lock`` were its ONLY real-root writes; the
    lock now rides the isolated root: acquire + release leaves nothing
    behind and the lock file lives under tmp."""
    os.environ.pop("VIDLIFT_STATE_DIR", None)
    lock = _gpu_phase_lock()
    assert ".gpu-phase.lock" in lock.path.name
    assert lock.path.parent != Path.home() / ".vidlift"
    acquired = lock.acquire()
    try:
        assert acquired
        assert lock.path.is_file()
        assert not (Path.home() / ".vidlift" / ".gpu-phase.lock").exists()
    finally:
        lock.release()
    assert not lock.path.exists()