# Karen Findings — VidLift v0.4.16.2 (routing required)

Companion to `karen-vidlift-v0.4.16.2-validation-report.md` (full evidence there).
Verdict context: FAIL as the release gate stands — one Critical finding below; all
other proof items green.

---

## F-1 — [FIX → Forge] (with a one-line spec amendment → Bob) — CRITICAL, release blocker

**The CI VAD gate demands `assets\__init__.py`, which `collect_data_files` never ships —
the workflow false-positives on a HEALTHY build and cannot go green.**

- **Where:** `.github/workflows/build-windows-installer.yml`, BOTH insertions —
  the `$expected = Get-ChildItem $fwAssets -File | Sort-Object Name` line in the
  "Build GUI and CLI executables" step and the re-derived copy in the
  "Validate installed artifact" step. **CORRECTION (v0.4.16.2 fix pass):** this sketch is the
  origin of Karen's D-1 blocker. `faster_whisper/assets/` ships a 0-byte `__init__.py`, but
  `collect_data_files` excludes `.py`, so `-File` alone puts `__init__.py` in `$expected` and the
  gate fails a HEALTHY build. Derive BOTH sides as `-Filter *.onnx` (or `-Exclude *.py`).
  Amended so a future implementer cannot reintroduce it.
- **Why it fires on healthy builds (all three facts empirically proven):**
  1. faster-whisper 1.2.1's wheel ships `faster_whisper/assets/__init__.py` (0 bytes —
     wheel RECORD), so the build venv's assets dir holds `__init__.py` + the onnx.
  2. `collect_data_files("faster_whisper")` (real PyInstaller 6.22.3, the UNPINNED
     version the workflow installs) returns exactly one entry — the `.onnx` — because
     PyInstaller excludes `.py` files from data collection by default.
  3. `Get-ChildItem -File` includes `__init__.py` in `$expected`; `Compare-Object`
     flags it `<=`; the gate throws
     `frozen tree missing asset files: __init__.py` (insertion B:
     `installed payload missing asset files: __init__.py`).
- **Fix (pick one, one line per insertion):**
  - `$expected = Get-ChildItem $fwAssets -File -Exclude *.py | Sort-Object Name`
    (keeps name+size parity for genuine data files), or
  - compare `*.onnx` only on both sides (matches the design's "non-empty ONNX set"
    language and the suite tripwire's glob).
- **Spec amendment (→ Bob):** impl-spec §5.1/§5.2 sketches carry the same
  `Get-ChildItem -File` derivation — the origin of the bug is the sketch, faithfully
  implemented. Amend both sketch blocks so the next implementer doesn't reintroduce it.
- **After fixing:** re-run `tests/test_v04162_vad_packaging.py` (workflow-shape tests
  assert on the gate text — the `-Exclude *.py` variant keeps every asserted string
  intact; verify), then dispatch the workflow and confirm the gate lines print green
  (V-10) before publish.
- **Severity honesty:** fail-closed — the original defect class still cannot ship
  (the gate also catches every real omission: proven by replay). This blocks the
  release, it does not endanger users.

## F-2 — [FIX → Forge, low priority] — Informational: `selftest_report` guard call sits outside the try

`_engine_disabled_by_env()` is called before the try block; a raising guard escapes the
"never raises" contract (impl-spec §3.2 rule 2) and `_cmd_selftest` would traceback
instead of printing JSON + exit 1. No realistic trigger (the guard is a one-line env
read); the spec sketch has the same shape. Optional hardening: wrap the guard call or
move the check inside the try. Not a blocker.

## F-3 — [FIX → Forge, low priority] — Informational: injected-resolver raise propagates out of `vad_asset_report`

The injected `resolver()` call is unguarded (the default resolver is internally
guarded and cannot raise). Test-seam-only; the monitor's try/except serves `unknown`
if it ever happened in production. Optional: wrap the injected call in the same
OSError→unknown discipline. Not a blocker.

## F-4 — [NOTE → Forge] — Informational: `selftest_report`'s `onnx_files` glob lists directories named `*.onnx`

`Path.glob` doesn't filter dirs (unlike `vad_asset_report`'s `scandir`+`is_file()`).
The load smoke catches the dir case (proven: `ok:false`), so no dishonest outcome is
possible — cosmetic asymmetry only. Fold into F-2/F-3 hardening if touched at all.

## F-5 — [NOTE → Forge] — Trivial: `tests/test_v04162_vad_packaging.py` lacks a trailing newline

`\ No newline at end of file`. Fix opportunistically; do not re-run the suite for it
alone.

---

## Verified-true claims worth recording (no action)

- All four Forge-declared deviations are real, justified, and correctly implemented
  (the smoke-kwarg deviation was MANDATORY — the spec sketch's `vad_parameters={...}`
  shape raises `TypeError` on faster-whisper 1.2.1 and would have made the CI gate
  unpassable forever).
- Forge's §3.4 audit claim, suite counts (954/2), pinned-record values, and
  zero-modification claim all verified TRUE.
- Forge's `-X importtime` N3 evidence was VACUOUS (the instrument captures only
  startup imports under pytest) — his conclusion was right, the instrument was not;
  Karen re-proved N3 with an audit hook + a sessionfinish plugin.
