# Karen Focused Re-validation — VidLift v0.4.16.2 after the D-1 fix

You are **Karen**. You validated `096e171` and returned **FAIL** on **D-1 (Critical)** plus four
informational findings. Forge has now applied fixes at commit **17b2b78**. This is a **focused
re-validation of the delta**, not a full re-audit: do not re-prove what you already proved,
except where a change could have invalidated it.

Working directory: `/home/yetisoldier/.karen/v04162-tree` — reset it to the new commit before you
start (`cd` there and `git checkout 17b2b78`). It is disposable; write freely inside it. Do not
write into `/home/yetisoldier/projects/vidlift` except the report path below.

**Write the report incrementally** to
`/home/yetisoldier/projects/vidlift/.specs/karen-vidlift-v0.4.16.2-recheck-report.md`,
starting with verdict `IN PROGRESS` and appending each item as you prove it.

## The delta

`git diff 096e171..17b2b78`. Forge reports: suite **959 passed, 2 skipped** (five new tests, all
inside `tests/test_v04162_vad_packaging.py`); both gates now derive `-Filter *.onnx` on both
sides; F-2 guard moved inside the try; F-3 injected resolver wrapped in `OSError -> unknown`;
F-4 `onnx_files` filtered to regular files; F-5 trailing newline. Treat all of that as **claims**.

## What you must prove (and nothing more)

1. **D-1 is genuinely closed.** The gate must now PASS a healthy payload. Construct the real
   condition: a source assets dir containing both the 0-byte `__init__.py` and the ONNX, and a
   frozen/installed dir containing only the ONNX. Show both insertions green. Then show the
   **old** derivation still fails on that same healthy payload, so the fix is what changed the
   outcome and not the fixture.
2. **Detection is NOT weakened.** Re-run your own falsification cases against the NEW gate text:
   missing ONNX, wrong-named ONNX, truncated ONNX, absent assets dir, empty set, selftest failure.
   Each must still throw, with the asset named. If any case now passes, that is a FAIL.
3. **The new test file cannot silently rot.** Forge says the workflow-shape tests now pin
   `-Filter *.onnx` on both sides **and** assert the unfiltered shape is absent. Verify that
   assertion is real (mutate the workflow back to the old shape and confirm red) rather than
   reading the test and trusting it.
4. **F-2.** A raising guard must yield JSON + exit 1 with no traceback. Attack it: make the guard
   raise at import-adjacent time, and confirm the never-raises contract holds on the real CLI path.
5. **F-3.** Injected resolver raising `OSError` must become `unknown`/`null`, not propagate. Confirm
   **non-`OSError` still propagates** (loud), since that boundary is deliberate.
6. **F-4.** A directory named `*.onnx` must not appear in `checks["onnx_files"]`.
7. **No regression, no scope creep.** Full suite twice, exact counts. `git diff 88a7116 -- tests/`
   must still name exactly one file. `api_version` still 1. Version still 0.4.16.2. The
   deferred-import rule still holds (use your own instrument, not Forge's).
8. **Bob's spec amendment is sound.** `bob-vidlift-v0.4.16.2-implementation-spec.md` was amended by
   the orchestrator to `-Filter *.onnx` with a correction note. Confirm the amended sketch would
   now produce a passing gate if implemented literally, i.e. the amendment actually fixes the
   sketch rather than merely describing the fix.

## Environment

`pwsh` is not installed; replay the gate logic in Python as you did before, and state that
plainly. You cannot run the Windows build. Do not modify source. Do not edit any test.

## Deliverable

`.specs/karen-vidlift-v0.4.16.2-recheck-report.md` with verdict (PASS / PASS-WITH-FINDINGS /
FAIL), risk, confidence, one row per item with method + evidence, and anything you could not prove.
Return a concise summary with your verdict and a clear **go/no-go for release**.
