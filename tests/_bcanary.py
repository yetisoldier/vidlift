"""Shared b-marker execution registry (the canary-span fix, D1 companion).

Imported by every b-marker test file so the suite-wide canary
(tests/test_z_b_canary.py) counts every b-marker across the suite,
not just one file's own set.
"""

EXECUTED_B: set = set()


def register_b(name: str) -> None:
    EXECUTED_B.add(name)
