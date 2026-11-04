# Follow-up (next cycle): non-blocking test-coverage items from Karen's v0.4.18 validation

**Author:** Hermes (orchestrator) — **Date:** 2026-11-04
**Source:** Karen v0.4.18 validation report (`karen-vidlift-v0.4.18-validation-report.md`),
findings F-1, F-7, D-2 — recommended, non-blocking. Queued so they don't slip.

## F-1 — path-guard test gap (Low)
`cleanup_subs_scratch`'s unique protection — refusing to delete a STALE default-named
`{output}.subs-work` (retained from a prior failed run) when the current run used a
caller-supplied workdir — is untested. Mutation 3b (guard removed) left all 14 tests green,
so a future refactor could silently drop the guard.
- **Fix:** add one test — seed a stale `{output}.subs-work`, run with `workdir=custom`, assert
  the stale dir survives (and `[]` is returned).

## F-7 — unpinned 100-file enqueue acceptance (Low)
The `MAX_GUI_BATCH` 64→500 flagship case ("a 100-file Mode B folder add enqueues", §13.1) has
no pinned automated test; only the symbolic `MAX_GUI_BATCH + 1` cap test exists.
- **Fix:** add one test — `enqueue_subs` with 100 paths against a real registry → 100 records,
  no `BatchTooLarge`.

## D-2 — `test_v0417_failpath_fix.py` kwarg ripple (Bob's-eyes note standing)
Forge edited `tests/test_v0417_failpath_fix.py` (a file in NEITHER §12 list) to add a defaulted
`suppress_srt=False` kwarg to a client double — the minimal File-F pattern, no assertion changes.
Accepted by Karen with a "Bob's-eyes note": the alternative (a conftest-shared double) is an
architecture call that should be decided deliberately, not left as a one-off ripple.
- **Action:** Bob to confirm whether the shared double belongs in conftest (dedupe) or stays as
  the per-file defaulted-kwarg pattern.
