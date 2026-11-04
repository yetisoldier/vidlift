# Karen Validation Report — VidLift v0.4.16.2 (VAD packaging hotfix)

**Validator:** Karen (adversarial validation)
**Date:** 2026-11-03
**Checkpoint under test:** commit `096e171` (parent `88a7116` = v0.4.16.1 release)
**Worktree:** `/home/yetisoldier/.karen/v04162-tree` (isolated; HEAD verified `096e171`, clean)
**Gate:** `/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3, pytest 9.1.1)
**Method note:** every claim below was attacked with independent instruments (mutation
copies in scratch dirs, a C-level import audit hook, a real PyInstaller collector run,
a Python transliteration of the pwsh gate — `pwsh` is NOT installed on this box, stated
plainly). No source file was modified; all mutations ran against scratch copies.

---

## Verdict: FAIL (one Critical defect — release-pipeline blocker; everything else verified sound)

**Risk level:** High — fail-closed (the defect class still cannot ship), but the
v0.4.16.2 workflow as committed **cannot produce an installer at all**.
**Confidence:** High — the Critical finding rests on three independently established
facts (wheel RECORD, real collector output, pwsh semantics), not on inference.

**The one-sentence story:** the bundling fix, the runtime honesty layer, the selftest,
the status key, the regression net, and the suite are all *proven* correct — but the CI
gate that this hotfix exists to add **false-positives on a healthy build** (it demands
`assets\__init__.py`, which `collect_data_files` never ships), so the first workflow
dispatch will go red on a *correct* payload and the release is blocked by its own gate.

---

## Proof matrix

| # | Claim | Method | Result | Evidence |
|---|-------|--------|--------|----------|
| 1 | **Central claim: the gate would have caught the original defect** — collector removed (`datas=[]`, the exact v0.4.16.1 shape) | Mutation: scratch copy of the repo with 88a7116's specs + 096e171's tests/workflow; contract tests run against it (no source touched) | **PROVEN — suite layer fires** | 3 tests RED: `test_gui_spec_collects_faster_whisper_data`, `test_cli_spec_collects_faster_whisper_data`, `test_both_specs_parity` (`AssertionError: assert 'faster_whisper' in 'datas=[], '`); the 3 workflow-contract tests stayed green (independent layer) |
| 1b | Wrong-name bundling caught at the suite layer | Mutation: spec doctored to `datas=[("silero_vad_v5.onnx", "faster_whisper/assets")]` | **PROVEN** | `test_gui_spec_collects_faster_whisper_data` RED (collector assertion AND the `silero`-leaf assertion), `test_both_specs_parity` RED |
| 1c | CI parity gate fires on missing / wrong-name / truncated / selftest-fail; self-heals on upstream rename | Python transliteration of the exact pwsh block (pwsh not installed — stated); 9 scenarios replayed against fabricated venv/dist trees | **PROVEN for defect-catching** — but the same replay exposed **Defect D-1 (Critical)**: the gate also fires on a HEALTHY payload | S2 no-dir → `frozen GUI tree missing faster_whisper assets dir`; S3 empty dir → `frozen tree missing asset files: silero_vad_v6.onnx`; **S4 wrong leaf → same throw, naming `silero_vad_v6.onnx`** (the task's required proof); S5 truncated → size throw; S6 selftest exit 1 → `frozen CLI --selftest failed`; S7 rename (venv+dist both move) → GREEN (self-heal proven); S8 venv renamed/dist stale → RED naming `silero_vad_v7.onnx`; S9 extras tolerated (documented property) |
| 2 | **Pinned asset record is live, not decorative** | Computed name/size/sha256 of the installed wheel's asset with my own instrument; compared to `EXPECTED_VAD_ASSETS` | **PROVEN — record matches reality exactly** | Installed: `silero_vad_v6.onnx`, 1,245,151 bytes, sha256 `4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2` — identical to the pin. Tripwire logic run live: PASSES on the real wheel, TRIPS on simulated rename (refresh message) and on content drift |
| 2b | Parity check self-heals if faster-whisper renames the asset | Replay S7/S8 + tripwire simulation | **PROVEN** | S7: both sides derive live → GREEN under rename. S8: stale dist vs renamed venv → RED naming the new leaf. The suite tripwire fails loudly with refresh instructions beside the constant; `test_record_has_no_stale_placeholder` guards the empty-record "fix" |
| 3 | **Deferred-import rule survives (N3)** — `faster_whisper` never enters `sys.modules` when the suite runs; `vad_asset_report`/`selftest_report` import-free | My OWN instruments: (a) C-level audit hook via `sitecustomize` logging every `faster_whisper` import event (validated with a positive control first); (b) zero-overhead `pytest_sessionfinish` plugin dumping `sys.modules` at session end. NOT the test's own assertions | **PROVEN** | (b) Full clean run: `SESSIONFINISH exitstatus=0 faster_whisper_loaded=NO`. (a) Two runs logged ZERO import events (both aborted at ~67% in Qt tests — instrument artifact, see row 12 — but covered everything up to abort, clean at both process exits). Static: `vad_asset_report` uses only `importlib.util`/`os.scandir`/`pathlib`/`time`; the only `faster_whisper` imports are `WhisperEngine.__init__`'s deferred block (lines 195–196) and `selftest_report`'s guarded body (1010, 1029). **Caveat recorded:** `-X importtime` under pytest is a VACUOUS instrument (pytest's fd capture swallows it after startup — only 380 startup lines captured; not even `vidlift` appears). Forge's importtime-based N3 evidence in his run log is worthless; the claim itself is true and independently proven here |
| 4 | **`--selftest` is honest** — exit non-zero when the asset is genuinely unavailable, zero when available; tried to make it lie | Live runs (dev box, both guard states) + 6 attacks: fake-package shadow (symlink farm over the real package, controlled assets dir), lying report monkeypatch, wrong-path resolver, raising guard, empty dir, directory-named-onnx, corrupt onnx | **PROVEN — could not make it lie** | Unguarded: exit 0, `"ok": true`, `"vad_load_smoke": "ok"`, `"report_agrees": true`. Guarded: exit 1, skip shape naming the env var. Attacks: empty assets dir → `ok:false`, exit 1, genuine NoSuchFile in errors; directory-named-`silero_vad_v6.onnx` → glob LISTS it but the real load smoke fails (InvalidProtobuf) → `ok:false` (presence alone never passes); corrupt onnx → `ok:false` (proves LOAD, not existence); lying `vad_asset_report` (claims present while asset missing) → `ok:false`; resolver disagreement → `report_agrees:false` → `ok:false`; CLI dispatch: `--selftest` outranks subcommands, `--version` (argparse action) unchanged, flag-not-subcommand pinned |
| 5 | **Preflight refuses rather than degrades** | Independent live probe (not the suite's tests): missing report injected, production env shape; plus a source scan for silent `vad_on` flips | **PROVEN** | `VadAssetMissingError` raised with: dir named ✓, remedy ("reinstall") ✓, expectation ("*.onnx") ✓; runner recorded ZERO calls; workdir NOT created (refusal precedes `os.makedirs`). No code path assigns `vad_on` outside constructor plumbing — nothing silently degrades to `vad_on=False`. `vad_on=False` + missing asset proceeds BY DESIGN (a no-VAD run never touches the asset — documented minimal-dependency truth, verified live: run completes) |
| 6 | **ADR-7 carve-out correctly scoped** — honest `TranscriptionError` never mislabelled as CUDA fault, never triggers CPU rebuild; construction-arm fallback unchanged | Independent probes through `make_whisper_engine_with_fallback` with fault-injecting fakes (both decode arms + both construction cases) + Forge's §3.4 audit re-verified | **PROVEN** | `VadAssetMissingError` from first decode (transcribe AND detect arms): raises through, `built=['cuda']` (NO rebuild), no note, no stderr WARNING. Negative control: `RuntimeError` from first decode → exactly one CPU rebuild + honest note (unchanged). Construction arm: `TranscriptionError` at CUDA construction → raises through (pre-existing, unchanged); `RuntimeError` at construction → one rebuild + note (pre-existing, unchanged). §3.4 audit: all `TranscriptionError` references in `test_v04152_cuda_fallback.py` are construction-arm; none expects a rebuild from a decode-arm refusal — Forge's audit claim verified |
| 7 | **Status contract is additive** — `api_version` stays 1; `transcription_engine` additive; pre-existing consumers parse; 500 attempted | Live `status_payload` probes (real checker, raising probe, missing shape) + key-set diff vs 88a7116 + full diff review | **PROVEN** | `api_version == 1 == API_VERSION`. Envelope keys = 88a7116's nine keys + `transcription_engine` (slotted after `transcription_model`); the ONLY removed line in monitor.py is the old import (replaced by the multi-import) — no existing key touched. 500 attempt: probe raises → envelope still serves, `vad_asset:"unknown"`, `ready_for_transcription: null`, detail names the fault. Missing → `ready:false`. Present → `ready:true`. JSON-serializable end to end. `/health` untouched (no diff) |
| 8 | **No pre-existing test modified** | `git diff 88a7116 -- tests/` (the task's exact command) and against the checkpoint | **PROVEN** | Both show EXACTLY one file: `tests/test_v04162_vad_packaging.py` (new, 884 lines). `run.py`, `subs_job.py`, `mcp_bridge/*`, `gui.py`, `conftest.py` untouched (verified by name-only diff). Source changes confined to `transcribe.py`, `cli.py`, `monitor.py`, `__init__.py` + the 4 release-stanza files |
| 9 | **Full suite determinism** — run at least twice, exact counts | Three full runs from the worktree at 096e171 | **PROVEN — 954 passed / 2 skipped, three identical clean runs** | Run 1: `954 passed, 2 skipped in 105.67s`; Run 2: `954 passed, 2 skipped in 103.44s`; Run 3 (with N3 plugin): `954 passed, 2 skipped in 103.56s`, exit 0. Matches Forge's claim (921 baseline + 33 new; baseline 921/2 confirmed in his log at 88a7116). Adjacency battery (version-stamps, windows-packaging, v0414-transcribe, v04152-cuda-fallback, monitor, new file): 115 passed |
| 10 | **Workflow gate logic verified by replaying the script blocks** | Python transliteration of both insertions (pwsh NOT installed — stated plainly); the load-bearing facts independently proven: wheel RECORD, real `collect_data_files` run (PyInstaller 6.22.3, the unpinned version CI resolves), `Get-ChildItem -File`/`Compare-Object` semantics | **PROVEN with one Critical exception (D-1)** | See rows 1c and D-1. The gate's defect-catching semantics are correct (S2–S6, S8 all RED with the right messages); the gate's expected-set derivation is wrong (includes `__init__.py`, which the collector excludes) → false positive on healthy builds, both insertions |
| 11 | **Declared deviations verified (all four)** | (1) installed-signature check; (2) grep for `state` readers; (3) version-literal regex on real + doctored workflow; (4) live report probes | **ALL FOUR CONFIRMED, all justified** | (1) `get_speech_timestamps(audio, vad_options=None, sampling_rate=16000, **kwargs)` feeds `VadOptions(**kwargs)` — the spec sketch's `vad_parameters={...}` raises `TypeError: VadOptions.__init__() got an unexpected keyword argument 'vad_parameters'` (reproduced); the implemented `min_silence_duration_ms=300` works. As sketched, the CI gate could NEVER reach `ok:true` — the deviation was mandatory. (2) The binding contract's `vad_asset` key is implemented everywhere; the only `"state"` readers are the pre-existing model-download machinery — nothing reads `state` from a VAD report. (3) `test_workflow_has_no_version_literals` is NOT vacuous: a doctored comment containing `v0.4.16.2` IS caught by its regex (proven); the real workflow carries zero 4-part literals and zero vidlift-named shapes (proven with the test's own regex). (4) `unknown` detail preserves the resolver's inner source behind the `injected` label (verified live: `detail: "assets dir unresolvable (resolver source: unavailable)"`, `resolver_source: "injected"`) |
| 12 | Instrument side-observation (not a product defect) | Two full-suite runs with a C-level audit hook attached | Noted | Both aborted at ~67% inside PyQt6 tests (`Fatal Python error: Aborted`, core dump) — the hook fires on every import event and perturbs timing-sensitive Qt probe tests. Every non-instrumented run is clean. Worth knowing: the suite has a timing-sensitive Qt-test class that a heavy import hook can knock over. NOT a v0.4.16.2 regression (runs 1/2/6 clean; the same hook would likely abort the 88a7116 suite too) |

---

## Defects

### D-1 — CRITICAL (release blocker): the CI VAD gate false-positives on a HEALTHY build

**The gate demands `assets\__init__.py`, which `collect_data_files` never ships.**

- **Evidence (each fact independently established on this box):**
  1. The faster-whisper 1.2.1 wheel RECORD contains `faster_whisper/assets/__init__.py`
     (0 bytes, sha256 `47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU`) — so the CI build
     venv's `faster_whisper\assets` holds `__init__.py` + `silero_vad_v6.onnx`.
  2. Running the REAL collector — PyInstaller 6.22.3 (latest; the workflow installs
     `pyinstaller` UNPINNED, so this is what CI resolves) —
     `collect_data_files("faster_whisper")` returns EXACTLY ONE entry:
     `…/faster_whisper/assets/silero_vad_v6.onnx → faster_whisper/assets`.
     `__init__.py` is excluded (PyInstaller's default: `.py` files are modules, not data).
  3. Both workflow insertions derive `$expected = Get-ChildItem $fwAssets -File` — which
     INCLUDES `__init__.py` — and compare against the frozen tree, which contains ONLY
     the `.onnx`. `Compare-Object` flags `__init__.py` (`<=`) →
     `throw "frozen tree missing asset files: __init__.py"`.
- **Impact:** the v0.4.16.2 `build-windows-installer` workflow goes RED at the "Build GUI
  and CLI executables" step on a CORRECT payload. Insertion B fails identically
  ("installed payload missing asset files: __init__.py" — replayed). Fail-closed (the
  defect class still cannot ship — S2–S6 prove the gate catches real omissions too),
  but the artifact cannot be produced at all; validation-spec acceptance (V-10: gate
  lines green) is unreachable; impl-spec acceptance criterion 4 is unreachable.
- **Likely root cause:** the impl-spec §5.1/§5.2 sketch (Bob) derives the expected set
  from the venv dir with `Get-ChildItem -File` and no `.py` filter; Forge implemented it
  verbatim. The design assumed `assets/` holds only data files; upstream ships an
  (empty) `__init__.py`, making it a subpackage.
- **Reproduction:** `.karen-scratch/gatereplay/replay_gate.py` (scenario "B-healthy"):
  expected={__init__.py, silero_vad_v6.onnx} vs shipped={silero_vad_v6.onnx} →
  `frozen tree missing asset files: __init__.py`.
- **Recommendation:** filter both sides to the asset class the gate exists for —
  `$expected = Get-ChildItem $fwAssets -File -Exclude *.py | Sort-Object Name` in BOTH
  insertions (keeps name+size parity for genuine data files), or compare `*.onnx` only
  (matching the design's "non-empty ONNX set" language and the suite tripwire's glob).
  One line per insertion. The impl-spec §5.1/§5.2 sketch needs the same amendment.
- **Severity: CRITICAL** for the release (the installer cannot be built; go/no-go = NO-GO
  until fixed). NOT a user-safety issue (fail-closed).

### D-2 — Informational: `selftest_report` propagates an exception if the env guard itself raises

The guard call `_engine_disabled_by_env()` sits OUTSIDE the try block, so a raising
guard escapes `selftest_report` (violating impl-spec §3.2 rule 2 "never raises"), and
`_cmd_selftest` would traceback instead of printing JSON + exit 1. No realistic trigger
exists (the guard is a one-line `os.environ.get(...) == "0"` that cannot fault), and the
spec sketch has the same shape — Forge implemented it faithfully. Reproduced by
monkeypatching the guard to raise. No action required; note for the next spec revision.

### D-3 — Informational: `vad_asset_report` propagates an exception if an INJECTED resolver raises

The injected-resolver call is outside any try (the DEFAULT resolver is internally
guarded and cannot raise). Test-seam-only surface; the monitor's try/except would
serve `unknown` if it ever happened in production. Reproduced with a raising resolver.

### D-4 — Informational: `selftest_report`'s glob lists a directory named `*.onnx`

`Path.glob` does not filter directories (unlike `vad_asset_report`'s
`os.scandir`+`is_file()`), so a directory named `silero_vad_v6.onnx` appears in
`checks["onnx_files"]`. The real load smoke catches it (proven: `ok:false`,
InvalidProtobuf in errors) — no dishonest outcome is possible; the asymmetry is
cosmetic.

### D-5 — Informational (style): the new test file lacks a trailing newline

`\ No newline at end of file` on `tests/test_v04162_vad_packaging.py`. Trivial.

---

## Observations (no action required)

1. **The CI "Test" step does not run the new contract tests** — it runs only
   `test_windows_packaging.py` + `test_gui.py`. The spec-contract tests and the pinned
   tripwire run dev-side only (consistent with ADR-6's "the suite screams on the dev
   box"; the CI parity + selftest gates are the release-path authority). Defense in
   depth holds: a spec regression between dev-gate and dispatch is still caught by the
   CI payload gates.
2. **Forge's importtime N3 evidence was vacuous** (see row 3) — his conclusion was
   correct, his instrument was not. His §3.4 audit claim, suite counts, pinned-record
   claim, and deviation list all verified TRUE against my own instruments.
3. **The suite has a timing-sensitive Qt-test class** that a heavy import hook can
   abort (row 12) — relevant to anyone instrumenting the suite in the future.

---

## What I could NOT prove (and why)

| Item | Why unproven |
|------|-------------|
| The actual Windows build: asset lands at `_internal\faster_whisper\assets\silero_vad_v6.onnx` in the real frozen tree (V-1/V-2/V-9) | Cannot run the Windows build from Linux. The collector's OUTPUT LIST is proven with the real PyInstaller on the real package (one entry: the onnx → `faster_whisper/assets`); the actual PyInstaller/Inno execution remains CI-side. V-9 (innoextract audit of the CI artifact) requires the artifact, which D-1 currently prevents from existing |
| The literal pwsh gate execution | `pwsh` is not installed on this box. The gate LOGIC is replayed faithfully in Python; the load-bearing facts (collector output, wheel content, `Get-ChildItem -File`/`Compare-Object` semantics) are each independently established. Residual risk: an exotic pwsh parsing quirk — low |
| Frozen `find_spec`/`_MEIPASS` resolution (FrozenImporter behavior under PyInstaller) | Windows-only. Dev-side `find_spec` verified; the `_MEIPASS` fallback and the selftest's real-loader cross-check are the designed covers; the CI selftest gate is the authoritative proof (blocked by D-1 until fixed) |
| V-8 (the running-job prediction on `vl-20261103-3ef5b3a8`), V-2b, V-6b, V-4, V-11 (box-side runtime: selftest on the box, /status envelope, doctored-install refusal, four-file clean sweep) | The Windows box's monitor is UNREACHABLE from this box (both `100.99.94.125:8766` and `192.168.1.53:8766` time out — same state as at the end of Bob's review; GUI stopped / host asleep / link down, cause unknown from here). These remain box-side items for the release owner; the unit-level equivalents are all proven above |
| `test_gui.py`-class Qt behaviors under the new code | Out of diff scope (gui.py untouched); suite green covers it |

---

## Release recommendation

**Not Ready for Release — Requires Significant Fixes (one surgical fix).**

The v0.4.16.2 *code* (bundling, runtime honesty, selftest, status key, regression net)
is verified correct end to end at every layer reachable from Linux, and the central
claim — the gate catches the original defect — is proven by mutation. But the CI gate
as committed false-positives on a healthy build (D-1), so the first workflow dispatch
will fail and no installer can be produced. Fix D-1 (one `-Exclude *.py` per insertion,
or compare `*.onnx` only), amend impl-spec §5.1/§5.2 to match, re-run the workflow-shape
tests, then dispatch. After a green CI run, the remaining unproven items (frozen-tree
audit V-9, gate-output audit V-10, box-side V-2b/V-6b/V-4/V-11, and the V-8 prediction
record) must close before publish — per the validation spec's own go/no-go list.

**Go/no-go: NO-GO until D-1 is fixed. Everything else is green.**
