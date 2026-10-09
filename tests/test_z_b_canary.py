"""Suite-wide b-series execution canary (the canary-span fix, D1 companion).

Karen's validation finding: the b0 canary in test_engine_ncnn.py could
only count its own file\'s markers, so b6 (test_benchmark.py) and
b7/b8/b12 (test_run.py) executed without any canary proving them.
This test runs last in a full-suite pass and enforces exactly the
markers whose test files were collected this session: the full suite
proves every b-marker; a subset run enforces only the subset\'s own.
"""

from __future__ import annotations

from pathlib import Path

from _bcanary import EXECUTED_B

_FILE_MARKERS = {
    "test_engine_ncnn.py": {"b0", "b1", "b2", "b3", "b4", "b5",
                            "b9", "b10", "b11"},
    "test_benchmark.py": {"b6"},
    "test_run.py": {"b7", "b8", "b12"},
}


def test_every_collected_b_marker_provably_executed(request):
    collected = {Path(str(item.fspath)).name
                 for item in request.session.items}
    missing: set = set()
    for fname, markers in _FILE_MARKERS.items():
        if fname in collected:
            missing |= markers - EXECUTED_B
    assert not missing, (
        f"b-series boundary tests did not execute: {sorted(missing)} "
        f"(collected b-files: {sorted(collected & set(_FILE_MARKERS))})"
    )
