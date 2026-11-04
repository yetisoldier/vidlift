# Forge Task — VidLift v0.4.16.2 FIX pass (Karen FAIL: D-1 + F-2..F-5)

You are **Forge**. You implemented Bob's spec at commit `096e171`. Karen has now validated it and
returned **FAIL** on one Critical defect plus four informational findings. This is a narrow fix
pass, not a redesign. Do not re-litigate the design.

Working directory: `/home/yetisoldier/.karen/v04162-tree` is Karen's read-only worktree; **ignore
it**. Work in `/home/yetisoldier/projects/vidlift` (the real repo, currently at `096e171`, clean).

## Read first

- `.specs/karen-vidlift-v0.4.16.2-validation-report.md` (full evidence)
- `.specs/karen-vidlift-v0.4.16.2-findings.md` (the five findings, F-1 is now D-1 below)

## D-1 (CRITICAL, release blocker) — the CI gate false-positives on a HEALTHY build

Karen proved, with three independent facts:

1. faster-whisper 1.2.1's wheel ships `faster_whisper/assets/__init__.py` (0 bytes), so the
   build venv's `assets/` directory contains `__init__.py` **and** `silero_vad_v6.onnx`.
2. The real `collect_data_files("faster_whisper")` under PyInstaller 6.22.3 returns **only the
   `.onnx`** — PyInstaller excludes `.py` files from data collection by default.
3. The gate derives `$expected` with `Get-ChildItem ... -File`, which therefore includes
   `__init__.py`; `Compare-Object` flags it missing; the build throws
   `frozen tree missing asset files: __init__.py` (and in the installed-payload step,
   `installed payload missing asset files: __init__.py`) **on a perfectly correct payload**.

Result: the workflow can never go green, so the hotfix blocks its own release.

**Fix:** in BOTH insertions of `.github/workflows/build-windows-installer.yml`, restrict the
comparison to `*.onnx` on both sides. Prefer the explicit ONNX-only form over `-Exclude *.py`:

```powershell
$expected = Get-ChildItem $fwAssets -File -Filter *.onnx | Sort-Object Name
```

and mirror the same `-Filter *.onnx` (or equivalent glob) on the frozen-tree side, so the two
sides are compared on a like-for-like basis. This also matches the design language ("non-empty
ONNX set") and the suite tripwire's existing glob.

**After fixing, you MUST verify the workflow-shape tests still pass.** The tests in
`tests/test_v04162_vad_packaging.py` assert on the gate's text. If a test asserts the literal
`Get-ChildItem $fwAssets -File | Sort-Object Name` shape, update **only that assertion inside
the new test file** to match the corrected gate — the new file is yours to edit. Do NOT modify
any pre-existing test.

**Prove the gate would still catch the real defect after your change.** Replay the gate logic
(mirror what Karen did — a Python replay is acceptable since `pwsh` is not installed) and show:
- missing ONNX  -> gate throws naming `silero_vad_v6.onnx`
- wrong-named ONNX -> gate throws
- correct payload (onnx only, with `__init__.py` present in the source assets dir) -> gate PASSES
That last case is the whole point of this fix and must be demonstrated, not asserted.

## F-2 (low) — `selftest_report` guard call sits outside the try

`_engine_disabled_by_env()` is called before the `try` block, so a raising guard escapes the
"never raises" contract and `_cmd_selftest` would traceback instead of printing JSON + exit 1.
Move the guard call inside the `try`, or wrap it, so the never-raises contract actually holds.
Keep the existing behaviour for the normal path identical.

## F-3 (low) — injected resolver raise propagates out of `vad_asset_report`

The injected `resolver()` call is unguarded. Wrap it in the same `OSError -> unknown` discipline
the default resolver already has. Test-seam only, but the discipline should be uniform.

## F-4 (informational) — `selftest_report`'s `onnx_files` glob lists directories named `*.onnx`

`Path.glob` does not filter directories, unlike `vad_asset_report`'s `scandir` + `is_file()`.
Make them consistent: filter to regular files. The load smoke already prevents a dishonest `ok`,
so this is symmetry, not safety.

## F-5 (trivial) — missing trailing newline

`tests/test_v04162_vad_packaging.py` has `\ No newline at end of file`. Add it.

## Constraints (unchanged from the original task)

- `api_version` stays 1. Additive keys only. No new endpoints, no new MCP tools, no new deps.
- **The deferred-import rule must survive**: the suite must never import `faster_whisper`.
- **No pre-existing test may be modified.** Only `tests/test_v04162_vad_packaging.py` is yours.
- Gate env: `/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3, pytest 9.1.1).
- Do not touch: the `cublas64_12.dll` concern, K-1 stale-overlay edge.
- Do not change the version number; it stays 0.4.16.2.

## Done when

- Full suite green, run it yourself, report the real number (expect 954 passed, 2 skipped).
- Every fix above is demonstrated with evidence, especially the healthy-payload-passes gate case.
- Report: what changed per finding, how you proved each, anything deliberately not done.
