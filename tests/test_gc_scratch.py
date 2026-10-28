"""v0.4.9 scratch-GC tests (design §12 G1-G5)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from vidlift.engine_ncnn import CHUNK_STATE_FILENAME, _workdir_for
from vidlift.run import gc_stale_scratch, prune_plan

from tests.test_resume_claim import build_retained_scratch, make_media


def test_g1_keep_newest_two_per_source(tmp_path):
    """G1: three retained sets for one source — the OLDEST is GC'd
    (beyond keep-2), and only when older than the age floor."""
    source = make_media(tmp_path, "Show.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    old = time.time() - 30 * 86400  # 30 days old: past the floor
    for run_id in (
        "Show.20261020-080000",
        "Show.20261024-090000",
        "Show.20261027-201001",
    ):
        build_retained_scratch(log_root, run_id=run_id, source=source)
        stamp_file = log_root / f"{run_id}.prepared.mkv"
        aged = old if run_id != "Show.20261027-201001" else time.time()
        stamp = time.localtime(aged)
        import os

        os.utime(
            stamp_file,
            (aged, aged),
        )
        workdir = _workdir_for(str(stamp_file))
        # age the workdir too (the GC reads the prepared file's mtime,
        # but consistent fixtures avoid confusion)
        os.utime(workdir, (aged, aged))
    removed = gc_stale_scratch(log_root)
    ids = [r["run_id"] for r in removed]
    assert ids == ["Show.20261020-080000"]
    assert (log_root / "Show.20261024-090000.prepared.mkv").exists()
    assert (log_root / "Show.20261027-201001.prepared.mkv").exists()
    assert not (log_root / "Show.20261020-080000.prepared.mkv").exists()


def test_g2_seven_day_floor_protects_fresh_sets(tmp_path):
    """G2: three sets, but the older two are FRESH (< 7 days) — nothing
    is GC'd regardless of count."""
    source = make_media(tmp_path, "Show.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    fresh = time.time() - 2 * 86400
    for run_id in (
        "Show.20261020-080000",
        "Show.20261024-090000",
        "Show.20261027-201001",
    ):
        build_retained_scratch(log_root, run_id=run_id, source=source)
        import os

        prepared = log_root / f"{run_id}.prepared.mkv"
        os.utime(prepared, (fresh, fresh))
    removed = gc_stale_scratch(log_root)
    assert removed == []
    assert (log_root / "Show.20261020-080000.prepared.mkv").exists()


def test_g3_zero_completed_chunks_gc_eligible_immediately(tmp_path):
    """G3: a set with NO completed chunks is pure triage debris —
    GC-eligible immediately regardless of age."""
    source = make_media(tmp_path, "Show.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    fresh = time.time()
    build_retained_scratch(
        log_root,
        run_id="Show.20261027-201001",
        source=source,
        completed=0,
    )
    import os

    prepared = log_root / "Show.20261027-201001.prepared.mkv"
    os.utime(prepared, (fresh, fresh))
    removed = gc_stale_scratch(log_root)
    assert [r["run_id"] for r in removed] == ["Show.20261027-201001"]
    assert removed[0]["reason"] == "zero_completed_chunks"
    assert not (log_root / "Show.20261027-201001.prepared.mkv").exists()


def test_g4_rejected_mezzanine_never_matched(tmp_path):
    """G4: the rejected-mezzanine retention path
    (`.prepared.rejected-<ts>.mkv`) is diagnostics — NEVER a GC key."""
    source = make_media(tmp_path, "Show.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    build_retained_scratch(
        log_root, run_id="Show.20261020-080000", source=source
    )
    rejected = (
        log_root
        / "Show.20261020-080000.prepared.rejected-20261020T090000.mkv"
    )
    rejected.write_bytes(b"rejected-diagnostic")
    old = time.time() - 30 * 86400
    import os

    os.utime(rejected, (old, old))
    for run_id in ("Show.20261024-090000", "Show.20261027-201001"):
        build_retained_scratch(log_root, run_id=run_id, source=source)
        os.utime(
            log_root / f"{run_id}.prepared.mkv", (old, old)
        )
        os.utime(_workdir_for(str(log_root / f"{run_id}.prepared.mkv")), (old, old))
    gc_stale_scratch(log_root)
    assert rejected.exists()  # diagnostics survive every GC pass


def test_g5_dry_run_reports_and_deletes_nothing(tmp_path):
    """G5: prune_plan reports the decision; deletes nothing."""
    source = make_media(tmp_path, "Show.avi")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    old = time.time() - 30 * 86400
    import os

    for run_id in (
        "Show.20261020-080000",
        "Show.20261024-090000",
        "Show.20261027-201001",
    ):
        build_retained_scratch(log_root, run_id=run_id, source=source)
        os.utime(log_root / f"{run_id}.prepared.mkv", (old, old))
    decisions = prune_plan(log_root)
    assert [d["run_id"] for d in decisions] == ["Show.20261020-080000"]
    # NOTHING was deleted
    assert (log_root / "Show.20261020-080000.prepared.mkv").exists()
    assert (
        log_root / "Show.20261020-080000.engine-work"
    ).exists()