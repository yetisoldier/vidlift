# Karen Focused Re-validation — VidLift v0.4.16.2 after the D-1 fix

**Validator:** Karen (adversarial validation)
**Date:** 2026-11-03
**Checkpoint under test:** commit `17b2b78` (parent `096e171` — the commit Karen FAILED on D-1)
**Scope:** focused re-validation of the fix delta ONLY (`git diff 096e171..17b2b78`); previously
proven facts are not re-proven except where the delta could have invalidated them.
**Worktree:** `/home/yetisoldier/.karen/v04162-tree` (HEAD verified detached at `17b2b78`, clean
except Karen's own untracked `.karen-scratch/`)
**Gate:** `/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3, pytest 9.1.1)
**Method note:** `pwsh` is NOT installed on this box (verified: `which pwsh` → nothing) — the gate
logic is replayed in Python exactly as in the first validation pass. The Windows build itself
cannot be run from Linux; that residual is unchanged and restated at the end.

## Verdict: PASS  (final — see the end of this report for risk, confidence, and go/no-go)

Items were appended below as they were proven; each carries its own method and evidence.

---

## The delta under test (claims, to be verified)

Forge reports, for `git diff 096e171..17b2b78`:
- Suite **959 passed, 2 skipped** (five new tests, all inside `tests/test_v04162_vad_packaging.py`).
- Both workflow gates now derive `-Filter *.onnx` on both sides (D-1 fix).
- F-2: the `_engine_disabled_by_env()` guard call moved inside the try.
- F-3: the injected resolver is wrapped in `OSError -> unknown`.
- F-4: `selftest_report`'s `onnx_files` glob is filtered to regular files.
- F-5: trailing newline added to the test file.
- Bob's impl-spec §5.1/§5.2 sketches amended to `-Filter *.onnx` with a correction note.

Diff stat confirmed (8 files: workflow, transcribe.py, the one test file, and 5 `.specs/`
documents — no other source touched; `git diff 096e171..17b2b78 --name-only` shows exactly:
`.github/workflows/build-windows-installer.yml`, `vidlift/transcribe.py`,
`tests/test_v04162_vad_packaging.py`, and 5 `.specs/` documents — the Bob impl-spec amendment,
the Forge fix task, and 3 Karen validation documents).

---

## Item 1 — D-1 is genuinely closed

**Method.** `pwsh` is not installed (verified again: `which pwsh` → nothing), so the gate logic
was replayed in Python exactly as in the first pass — but this time the replay script
**self-checks against the committed text**: it asserts the workflow file at HEAD contains the
exact `-Filter *.onnx` lines in BOTH insertions and that NO unfiltered `Get-ChildItem ... -File |
Sort-Object` derivation remains, before replaying anything. Instrument:
`.karen-scratch/gatereplay-v2/replay_gate_v2.py`, log `replay-v2.log` (exit 0, all machine
assertions passed).

**The real condition, not a fabricated fixture.** The healthy payload was constructed from
real instruments, re-run for this delta:
- **Venv side:** the workflow's OWN derivation command
  (`python -c "import faster_whisper, os; print(os.path.join(os.path.dirname(faster_whisper.__file__), 'assets'))"`)
  run with the gate venv's Python → `…/site-packages/faster_whisper/assets`, whose real file set
  is `['__init__.py' (0 bytes), 'silero_vad_v6.onnx' (1,245,151 bytes)]` (fact 1 of D-1, still true).
- **Dist side:** the REAL collector re-run — PyInstaller 6.22.3 (the UNPINNED version the
  workflow installs, so this is what CI resolves) `collect_data_files('faster_whisper')` returns
  EXACTLY ONE entry: `…/assets/silero_vad_v6.onnx → faster_whisper/assets` (fact 2 of D-1, still
  true). The healthy payload dir contains exactly that one file.

**Result — D-1 is closed:**

| Scenario | Gate | Outcome |
|---|---|---|
| H1 healthy payload, NEW gate, insertion A (dist tree) | `-Filter *.onnx` both sides | **GREEN** — `vad assets ok: silero_vad_v6.onnx` |
| H2 healthy payload, NEW gate, insertion B (installed tree) | `-Filter *.onnx` both sides | **GREEN** — `vad assets ok: silero_vad_v6.onnx` |
| H1-old SAME healthy payload, OLD (096e171) derivation, insertion A | unfiltered `-File` | **RED** — `frozen tree missing asset files: __init__.py` |
| H2-old SAME healthy payload, OLD derivation, insertion B | unfiltered `-File` | **RED** — `installed payload missing asset files: __init__.py` |

The old derivation still fails on the *same* payload the new gate passes — the fix, not the
fixture, changed the outcome. Both insertions green on the healthy payload. **PROVEN.**

## Item 2 — Detection is NOT weakened (falsification battery vs the NEW gate text)

All previously-proven defect-catching cases were re-replayed against the NEW gate text, plus
new cases attacking the `-Filter`/`-File` change itself. Every case that must throw still
throws, with the asset named:

| Case | Outcome | Throw text (abridged) |
|---|---|---|
| Missing ONNX — empty dist dir | **RED** | `frozen tree missing asset files: silero_vad_v6.onnx` |
| Missing ONNX — `__init__.py` shipped, onnx not (collector-minus-onnx) | **RED** | `frozen tree missing asset files: silero_vad_v6.onnx` |
| Wrong-named ONNX (v5 shipped, v6 expected) | **RED** | `frozen tree missing asset files: silero_vad_v6.onnx` |
| Truncated ONNX (right name, half size) | **RED** | `asset size mismatch: silero_vad_v6.onnx` |
| Absent assets dir | **RED** | `frozen GUI tree missing faster_whisper assets dir: …` |
| Empty set — venv ships NO onnx, insertion A | **RED** | `frozen tree ships no *.onnx under …` (the non-empty check) |
| Empty set — insertion B (no non-empty check there; selftest is the backstop) | **RED** | `installed CLI --selftest failed` |
| Selftest failure (healthy payload, exit 1) | **RED** | `frozen CLI --selftest failed — see dist\selftest-cli.json` |
| **NEW attack:** directory NAMED `silero_vad_v6.onnx`, no real file | **RED** | `frozen tree missing asset files: silero_vad_v6.onnx` — `-File` excludes the decoy dir; a directory is never counted as an ONNX |
| **NEW attack:** ONNX renamed `silero_vad_v6.onnx.bak` in dist | **RED** | `frozen tree missing asset files: silero_vad_v6.onnx` — the filter is not fooled by a suffix |
| Upstream rename rot (venv v7, dist stale v6) | **RED** | `frozen tree missing asset files: silero_vad_v7.onnx` |

Correct properties preserved (GREEN, as before): decoy `*.onnx` **directory** alongside the real
ONNX (dir ignored, file counted); upstream rename self-heal (v7 both sides); extra non-onnx
files tolerated. **PROVEN — no case that used to throw now passes; the scoping only removed the
`__init__.py` false positive.**

## Item 3 — The new test file cannot silently rot (mutation-verified, not read-and-trusted)

**Method.** Scratch copy of the repo (`.karen-scratch/mutation3/` — no source in the real tree
touched), then four mutations of `.github/workflows/build-windows-installer.yml`, each followed
by a run of exactly the two workflow-shape tests
(`test_workflow_dist_tree_vad_gate`, `test_workflow_installed_vad_gate`).

| Mutation | Shape | Result |
|---|---|---|
| Control (pristine copy) | committed text | **2 passed** (green baseline) |
| M1 — full revert: ` -Filter *.onnx` removed everywhere (the exact 096e171 D-1 shape) | old gate | **2 failed** — both tests RED |
| M2 — only insertion A reverted; insertion B untouched | mixed across insertions | **test A RED, test B green** — each test pins its own insertion |
| M3 — only the `$shipped` side of insertion A reverted (`$expected` still filtered) | one-side rot | **test A RED** (positive pin at line 994 fires) |
| M4 — ALL `-Filter *.onnx` lines kept intact, but a stray unfiltered derivation (`$debugAll = Get-ChildItem $distAssets -File | Sort-Object Name`) ADDED to insertion A | pins satisfied, D-1 shape reintroduced alongside | **test A RED** — the *absent*-assertion fires: `assert "$distAssets -File | Sort-Object" not in block` FAILED |

M4 is the decisive one: it satisfies every positive pin and still fails, proving the
"unfiltered shape is absent" assertions are load-bearing rather than decorative — the D-1 shape
cannot sneak back in even as an *extra* line while the filtered lines remain. **PROVEN — the
anti-rot claim is real, verified by mutation, not by reading the test.**

## Item 4 — F-2: a raising guard yields JSON + exit 1, no traceback, on the REAL CLI path

**Method (own instrument, not the suite's tests).** The guard `_engine_disabled_by_env` was
patched to raise at INTERPRETER STARTUP via `sitecustomize.py` on `PYTHONPATH`
(`.karen-scratch/f2-attack/`) — import-adjacent time, before the CLI runs — and the REAL CLI
path was driven in a subprocess: `python run_cli.py --selftest` from the repo root (real argv →
`cli.main` → `_cmd_selftest` → `selftest_report` → JSON print → exit code). Nothing else was
faked. An `atexit` hook recorded whether the REAL `faster_whisper` ever entered `sys.modules`.

| Attack | Exit | stdout | stderr | faster_whisper imported? |
|---|---|---|---|---|
| Guard raises `RuntimeError("guard exploded")` | **1** | `vidlift-selftest {"checks": {}, "errors": ["RuntimeError: guard exploded (Karen F-2 attack)"], "ok": false}` — valid JSON (parsed), `ok:false`, fault in `errors[]` | **0 bytes** — no traceback | **no** (the guard fault precedes the deferred import) |
| Guard raises `OSError("guard IO fault")` | **1** | same shape: `"errors": ["OSError: guard IO fault"]`, `ok:false` | **0 bytes** | **no** |
| Guard raises `KeyboardInterrupt` (BaseException) | 130 | empty | traceback | — |

The `KeyboardInterrupt` row is the **deliberate boundary**, not a defect: the never-raises
envelope is `except Exception`; a cancellation signal propagating is honest Python behavior
(swallowing it would be worse). The new test pins `RuntimeError`; the contract holds for every
`Exception` subclass. Accidental control from a broken first attempt at the OSError variant
(unpatched guard): exit 0, healthy `ok:true` selftest — the normal path is untouched by the
restructure. **PROVEN — the never-raises contract now covers a faulting guard, on the real CLI
path, with the JSON artifact intact (the CI gate keeps its failure channel).**

## Item 5 — F-3: injected resolver `OSError` → `unknown`/`null`; non-`OSError` still LOUD

**Method (own instrument).** Direct calls to `vad_asset_report` with raising resolvers
(`.karen-scratch/f3-probe.py`, all assertions machine-checked):

- **OSError family → unknown, never propagates:** `OSError`, `PermissionError`,
  `FileNotFoundError`, `NotADirectoryError` each produce the full uniform report shape (all 9
  keys) with `vad_asset:"unknown"`, `assets_dir:null`, `onnx_files:null`, `onnx_count:null`,
  `resolver_source:"injected"`, `detail` starting `"probe failed:"` and naming the exception
  class and message (e.g. `"probe failed: PermissionError: denied"`).
- **Non-OSError → still LOUD (the deliberate boundary):** `ValueError`, `TypeError`,
  `RuntimeError`, `KeyError`, `AssertionError` all PROPAGATE — verified each raises out of
  `vad_asset_report` (a swallow there would have been a boundary-widening defect; none found).
- `KeyboardInterrupt` propagates (BaseException — same boundary as F-2).
- **Control:** a healthy injected resolver still returns `present` with
  `onnx_files=["silero_vad_v6.onnx"]` — the wrap does not touch the normal path.

**PROVEN — seam faults degrade to the honest `unknown` shape; the loud boundary is intact.**

## Item 6 — F-4: a directory named `*.onnx` never appears in `checks["onnx_files"]`

**Method (own instrument — the REAL loader path, not the suite's fakes).** A shadow copy of the
real `faster_whisper` package (verified: `faster_whisper.__file__` resolves inside the shadow)
with controlled assets dirs, driven through the real `selftest_report()` — real
`get_assets_path()`, real glob, real ONNX load smoke (onnxruntime actually loads the model):

- **Variant A** — assets = real `silero_vad_v6.onnx` (file) + `decoy-dir.onnx` (DIRECTORY) +
  `readme.txt`: `checks["onnx_files"] == ["silero_vad_v6.onnx"]` — the decoy directory is
  **not** listed; `vad_load_smoke:"ok"` (the real model loads); `report_agrees:true`;
  `ok:true`. The listing now agrees with `vad_asset_report`'s `scandir`+`is_file()` discipline.
- **Variant B** — assets = ONLY a directory named `silero_vad_v6.onnx`: the fixed expression
  yields `onnx_files == []` while the OLD (096e171) expression
  `sorted(p.name for p in assets.glob(VAD_ASSET_GLOB))` yields `['silero_vad_v6.onnx']` —
  proven side-by-side, so the fix changed real behavior, not just style. The selftest is
  honestly `ok:false` with `InvalidProtobuf` in `errors[]` (the load smoke catches the decoy,
  exactly as before the fix — no dishonest outcome was possible, and still is not).

**PROVEN — directories named `*.onnx` are excluded from the listing; the safety property (load
smoke catches the decoy) is unchanged.**

## Item 7 — No regression, no scope creep

**Full suite, twice (plus one instrumented third run), exact counts:**

| Run | Command | Result |
|---|---|---|
| 1 | `python -m pytest -q` (worktree at `17b2b78`) | **959 passed, 2 skipped** in 103.45s |
| 2 | same | **959 passed, 2 skipped** in 104.31s |
| 3 | same + Karen's sessionfinish plugin | **959 passed, 2 skipped**, exitstatus 0 |

Matches Forge's claim (959/2) exactly; deterministic across runs; delta vs the first-pass counts
is exactly +5 (954/2 → 959/2), matching the five new tests in the one file.

**Scope checks:**
- `git diff 88a7116 --name-only -- tests/` → **exactly one file**:
  `tests/test_v04162_vad_packaging.py` (the task's required proof, unchanged result).
  `git diff 096e171..17b2b78 --name-only -- tests/` → the same single file (no pre-existing
  test touched by the fix pass).
- Whole-delta file list: `.github/workflows/build-windows-installer.yml`,
  `vidlift/transcribe.py`, `tests/test_v04162_vad_packaging.py`, and 5 `.specs/` documents —
  **nothing else**. No other source file (cli.py, monitor.py, gui.py, specs, pyproject) touched;
  no new dependency (pyproject/requirements untouched by the delta).
- `vidlift/transcribe.py` delta adds **no import lines** (grep of the diff: zero `+import`/`+from`)
  and is confined to the three fix sites (F-2 guard move, F-3 resolver wrap, F-4 is_file filter).

**`api_version` still 1 (live probe, own instrument):** `API_VERSION = 1` const; a live
`status_payload` probe serves `api_version: 1` with the additive envelope intact —
`transcription_engine` is the VAD report verbatim plus the one derived `ready_for_transcription`
key (live values: `vad_asset:"present"`, `ready_for_transcription:true`), JSON-serializable end
to end. `/health` untouched (monitor.py not in the delta).

**Version still 0.4.16.2:** `pyproject.toml` `version = "0.4.16.2"`;
`vidlift/__init__.py` `__version__ = "0.4.16.2"`; `CHANGELOG.md` `[0.4.16.2] - 2026-11-03`.

**Deferred-import rule (N3) — own instruments, not Forge's and not the suite's own asserts:**
1. **Session-finish plugin, full clean run** (zero per-import overhead; modules persist once
   imported, so end-state is proof for real imports): `SESSIONFINISH exitstatus=0
   faster_whisper_loaded=NO` on the full 959/2 run.
2. **C-level audit hook** (fires below pytest's capture; logs every import event naming
   faster_whisper or any submodule): **zero import events** through 67% of the suite, both
   process exits clean (`SESSION-END: ... NO` ×2). The run then aborted (exit 134, core dump)
   inside the timing-sensitive Qt test class — the SAME instrument artifact documented in the
   first validation pass (row 12 there): the hook perturbs Qt probe timing; every
   non-instrumented run is clean. Partial event coverage + full end-state coverage, as before.
3. **Static:** the only `faster_whisper` imports in `vidlift/` remain the two deferred sites
   (`transcribe.py:195-196` engine constructor, `1032`/`1060` selftest guarded body) — unchanged
   by the delta. The new F-4 test seeds FAKE modules via `monkeypatch.setitem` (auto-restored;
   never loaded from disk — the audit hook's zero events confirm no real load), and the
   module-level `find_spec` probe imports nothing.

**PROVEN — no regression, no scope creep, all invariants hold.**

## Item 8 — Bob's spec amendment is sound (the sketch itself now passes, proven by extraction + replay)

**Method (not read-and-trusted).** The two powershell code fences were EXTRACTED from the
amended `.specs/bob-vidlift-v0.4.16.2-implementation-spec.md` §5.1/§5.2
(`.karen-scratch/spec-amendment-probe.py`), then:

1. **Byte-comparison:** every load-bearing gate line in both sketches is **byte-identical** to
   the committed workflow's lines — the `$fwAssets` derivation, both `-Filter *.onnx`
   derivations (`$expected`/`$shipped` in §5.1, `$installed` in §5.2), the `Test-Path` throws,
   the `Compare-Object` parity rule, the size loop, the non-empty-ONNX check, and the selftest
   invocations. The D-1 (unfiltered) shape is absent from BOTH sketches. The `CORRECTION
   (v0.4.16.2 fix pass, Karen D-1)` note is present and **prescriptive** ("Derive BOTH sides as
   `-Filter *.onnx`"), not merely descriptive.
2. **Replay:** the sketch-implemented gate (same proven engine as items 1–2) passes the healthy
   payload on BOTH insertions (GREEN) and stays RED on wrong-named ONNX, missing ONNX, and
   selftest failure — the amendment fixes the sketch, it does not just narrate the fix.

§5.2's sketch inherits `$expected` from §5.1 via the "copy the `$fwAssets`-derivation lines
into the Validate step header" note — since §5.1's lines are now filtered, a literal
implementation inherits the fix (and the committed insertion B confirms: its two re-derivation
lines match §5.1's amended text). **PROVEN.**

*Informational nit (no action required):* the §5.2 note says "copy the **three**
`$fwAssets`-derivation lines"; only two lines in §5.1 mention `$fwAssets` (the derivation and
the `$expected` line), and the committed insertion B uses exactly those two. "Three" is a
loose count — any literal reading still copies the filtered lines, so the gate is correct
either way.

## F-5 — trailing newline

`tests/test_v04162_vad_packaging.py` now ends with `\n` (verified: final byte `0x0a`; the
096e171→17b2b78 diff shows the no-newline marker gone). Trivial; closed.

## Forge's claims vs reality (all five claims verified TRUE)

| Claim | Verified |
|---|---|
| Suite 959 passed, 2 skipped | **TRUE** — three full runs (two plain + one instrumented), identical counts; 954+5 arithmetic exact |
| Five new tests, all inside `tests/test_v04162_vad_packaging.py` | **TRUE** — exactly 5 new `def test_` in the delta, all in that file, all pass individually |
| Both gates derive `-Filter *.onnx` on both sides | **TRUE** — replay self-check + byte-inspection of both insertions |
| F-2 guard inside the try | **TRUE** — proven behaviorally on the real CLI path |
| F-3 injected resolver wrapped `OSError -> unknown` | **TRUE** — proven with own probe incl. subclasses and the loud boundary |
| F-4 `onnx_files` filtered to regular files | **TRUE** — proven with the REAL loader against a shadow package |
| F-5 trailing newline | **TRUE** |

---

## Summary matrix (one row per required item)

| # | Item | Method | Verdict |
|---|---|---|---|
| 1 | D-1 genuinely closed | Python replay of the committed gate (self-checked against HEAD text); healthy payload built from the REAL wheel assets + REAL PyInstaller 6.22.3 collector output; old-derivation control on the same payload | **PROVEN** — GREEN both insertions; old gate RED on same payload naming `__init__.py` |
| 2 | Detection not weakened | 13-scenario falsification battery vs the NEW gate text (incl. new attacks: dir-named-onnx, `.onnx.bak`) | **PROVEN** — every must-throw case still throws, asset named |
| 3 | Test file cannot silently rot | 4 mutations of the workflow in a scratch copy (full revert, per-insertion, one-side, stray-line) | **PROVEN** — all red, incl. the absent-assertion catching a stray unfiltered line with all pins intact |
| 4 | F-2 raising guard → JSON + exit 1 | sitecustomize injection at interpreter startup + real `run_cli.py --selftest` subprocess; RuntimeError, OSError, KeyboardInterrupt | **PROVEN** — exit 1, valid JSON, 0-byte stderr, no fw import; BaseException boundary deliberate |
| 5 | F-3 OSError→unknown, non-OSError loud | own direct probe: 4 OSError-family classes, 5 non-OSError classes, BaseException, healthy control | **PROVEN** — unknown/null shape; loud boundary intact |
| 6 | F-4 dir named `*.onnx` excluded | REAL loader via shadow faster_whisper package; two variants; old-vs-new expression side-by-side | **PROVEN** — dir never listed; honest ok:false preserved |
| 7 | No regression, no scope creep | suite ×2 (+1 instrumented); git diffs; live status_payload probe; version stamps; N3 via sessionfinish plugin + audit hook + static | **PROVEN** — 959/2 ×2; one test file; api_version 1; 0.4.16.2; N3 holds |
| 8 | Bob's spec amendment sound | extraction + byte-compare + replay of the amended sketches | **PROVEN** — sketch implemented literally IS the committed gate |

## Observations (no action required)

1. The never-raises envelope is `except Exception` on both new wraps (guard, injected resolver):
   `KeyboardInterrupt`/`BaseException` propagates (traceback, exit 130). This is the correct,
   honest boundary — swallowing cancellation would be worse — and it is consistent across both
   sites. Pinned as deliberate by the new tests' docstrings.
2. The audit-hook N3 run aborts at 67% in the timing-sensitive Qt test class — the SAME
   instrument artifact documented in the first pass; every non-instrumented run is clean
   (three more clean full runs this pass).
3. Insertion B has no non-empty-ONNX check of its own (unlike insertion A) — by design, the
   installed CLI's `--selftest` is the backstop there (proven RED via that channel, F5b).
   Unchanged by the delta.

## What I could NOT prove (and why)

| Item | Why unproven |
|---|---|
| The literal pwsh gate execution / the actual Windows CI dispatch (V-10: gate lines green in a real run) | `pwsh` is not installed on this box (stated plainly); the Windows build cannot run from Linux. The gate LOGIC is replayed faithfully and self-checked against the committed text; the load-bearing facts (wheel content, real collector output, `Get-ChildItem -File`/`-Filter`/`Compare-Object` semantics) are each independently established. Residual: an exotic pwsh parsing quirk — low. The proof that the workflow goes green end-to-end requires dispatching it |
| Box-side items (V-8 running-job prediction, V-2b/V-6b/V-4/V-11 runtime checks) | The Windows box's monitor is UNREACHABLE from this box (both `100.99.94.125:8766` and `192.168.1.53:8766` time out — re-verified this pass, same state as at the end of the first validation). Release-owner items per the validation spec's go/no-go list |
| Frozen `_MEIPASS`/FrozenImporter resolution in the real frozen tree | Windows-only, unchanged residual; the CI selftest gate is the designed cover — now REACHABLE since D-1 no longer blocks the artifact from existing |

---

## Verdict: PASS

**Risk level: Low** (for the delta under test; the release-path residual is the CI dispatch
itself, which is now unblocked and expected green).
**Confidence: High** — every required item proven with independent instruments; the one
Critical blocker (D-1) is closed by a fix that is proven necessary AND sufficient (old
derivation still red on the same healthy payload the new gate passes); detection strength is
re-proven case by case; the anti-rot assertions are mutation-verified; no new defects found in
the delta.

## Release recommendation

**Ready for Production — GO for release dispatch.**

The D-1 release blocker is closed and could not be re-broken by any falsification attempt; the
four informational findings (F-2..F-5) are all addressed and behaviorally proven; the suite is
deterministic at 959/2; scope is exactly the mandated fix (one workflow, one source file, one
test file, spec docs); every invariant (api_version 1, version 0.4.16.2, N3 deferred-import,
no pre-existing test touched) holds. From Karen's side there is no remaining objection to
dispatching `build-windows-installer`.

**Go/no-go: GO** — conditional on the release owner closing the standing (unchanged,
non-delta) residuals after dispatch: a green CI run (V-10 gate lines + V-9 installer audit)
and the box-side checklist (V-8, V-2b/V-6b/V-4/V-11), per the validation spec's own go/no-go
list. Those were unprovable from this box before this fix pass and remain so for
environmental, not code, reasons.
