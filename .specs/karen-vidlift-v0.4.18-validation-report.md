# Karen Validation Report — VidLift v0.4.18 follow-up batch (4 workstreams)

**Author:** Karen (validation agent)
**Date:** 2026-11-04
**Checkpoint under review:** `9100d74` (working tree clean, HEAD == 9100d74)
**Inputs:** Bob's spec `.specs/bob-vidlift-v0.4.18-implementation-spec.md` (§13 baseline), Forge's report `.specs/forge-vidlift-v0.4.18-followup-report.md`, Hermes brief `.specs/hermes-brief-v0.4.18-followup-batch.md`
**Bases:** `vidlift/run.py` diffed against `ac90825` (last pre-v0.4.18 run.py touch = `20dfe61`, v0.4.16.0 — verified via `git log --follow`); tree diffed against `b506876` (v0.4.17.0)
**Runner:** `/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3) exclusively
**Method:** full-suite run · git-diff audits (hunk-by-hunk vs the spec's change tables) · mutation testing in a scratch copy (defects recreated, gates proven to FIRE) · live probes with REAL ffmpeg and a REAL LocalBridgeClient/registry (offscreen Qt) · direct helper probes

---

## 1. Executive summary

**Overall status: WARN — CODE-CLEARED, release clearance CONDITIONAL.**

| Dimension | Verdict |
|---|---|
| **Code-cleared** | **YES.** All four workstreams implemented exactly per the spec's change tables; every functional gate proven live; the three load-bearing defect classes (recursion-less, suppression-fallback, cleanup-leak) recreated as mutations and **caught by the new tests**; zero Critical/High code defects. |
| **Release-cleared** | **CONDITIONAL** on two items neither of which is a code defect: (1) §13.6's Windows visual acceptance (D12 band, 4–7 px clearance at 100 %/125 % on Segoe UI) — **cannot be executed on Linux offscreen** (the spec itself says so); (2) **F-2**, a false verification claim in the CHANGELOG that must be corrected (or made true) before the release notes ship. |

**Overall risk: Low.** **Confidence: High** (functional claims), **Medium** (WS4's pixel-level outcome, pending the Windows check).

Full suite: **1041 passed, 2 skipped** (both skips pre-existing Windows-only GPU-meter tests — see F-4). All §12 unmodified-pass invariants green with zero edits outside the three authorized/escalated files.

---

## 2. Findings

| # | Severity | Finding | Impact | Evidence | Recommendation |
|---|---|---|---|---|---|
| F-1 | **Low** (test-coverage gap) | `test_20_custom_workdir_is_never_touched` cannot detect removal of `cleanup_subs_scratch`'s path guard. The guard's *unique* protection — refusing to delete a **stale default-named** `{output}.subs-work` (e.g. retained from a prior failed run) when the current run used a caller-supplied workdir — is untested. Mutation 3b (guard deleted) left all 14 tests in the file green. | The shipped code is correct and the guard is live (direct probe below); the gap is that a future refactor could silently drop the guard with no red test. | Mutation run: `14 passed` with guard removed. Direct probe: guard present → stale scratch survives, `[]` returned; guard removed → stale scratch rmtree'd. | Add one test: seed a stale `{output}.subs-work`, run with `workdir=custom`, assert the stale dir survives. |
| F-2 | **Medium** (docs honesty) | CHANGELOG `[0.4.18.0]` states the QGroupBox margin was "**Verified visually on the Windows build**" — no Windows build exists or was tested. Forge's own report §E says the 20 px value is "a Windows-metric bet validated only logically" and "No release artifacts cut." | A false verification claim in release-facing notes, in a project whose discipline is honesty-first. Misleads Eric into skipping the D12 tuning check. | `CHANGELOG.md` diff vs `b506876` (the Changed bullet); Forge report §E bullet 1 + §6 "No release artifacts cut." | Reword to "pending visual verification on the Windows build (D12 tuning band: ±2 px steps, 4–7 px clearance)" — or perform the verification and keep the claim. |
| F-3 | **Low** (docs nit) | CHANGELOG garbles the manifest key name: "`sidecar_subtitles`-style honesty" — the actual key is `sidecar_suppressed`. | Cosmetic; the same sentence then states the correct key. | CHANGELOG diff, Added bullet 3. | One-token fix. |
| F-4 | **Informational** (report accuracy) | Forge's report attributes the 2 suite skips to "Windows-only symlink skips"; they are actually the pre-existing Windows-only **GPU-meter** tests (`test_gpu_meter_no_window.py:132,164`, `os.name != "nt"`). The v0.4.18 symlink test's skipif is the correct opposite direction (`os.name == "nt"` — runs on Linux, skips on Windows). | None (count and pre-existing-ness are correct; only the attribution is wrong). | grep of skipif markers + targeted run (47 passed, 0 skipped on the four new files + version stamps). | None. |
| F-5 | **Informational** | Comment anchor drift: run.py OCR-branch comment cites "sidecar_path was set PRE-mux at :2883" — the line is now :2948 (the spec's §0 itself warns anchors drift). | None (comment-only). | run.py:2971 vs :2948. | None, or fix opportunistically. |
| F-6 | **Informational** | `subs_job.py` ends without a trailing newline (pre-existing; the new helper inherits it). | None (style). | `git diff` tail: "\ No newline at end of file" on both sides. | None. |
| F-7 | **Low** (test-coverage suggestion) | The MAX_GUI_BATCH alignment's flagship case — "a 100-file Mode B folder add enqueues" (§13.1) — has no pinned automated test. `test_client_local.py:112` tests the cap raise symbolically (`MAX_GUI_BATCH + 1`); nothing pins the 100-file pass. | A future cap regression (e.g. re-tightening to 64) would only be caught by the symbolic test if it tracked the constant — which it does — but the *acceptance* case itself is unpinned. Proven live in this validation (100/100 enqueued). | Live probe §5.4 below; grep of the four new test files. | Add one test: `enqueue_subs` with 100 paths against a real registry → 100 records, no `BatchTooLarge`. |

**No Critical or High severity defects.** No functional defect found in any workstream.

---

## 3. Test plan — planned, executed, added

| Suite / probe | Status |
|---|---|
| Full suite `pytest tests/ -q` (venv python) | **1041 passed, 2 skipped** (105.6 s) — matches Forge's claim exactly |
| Gate (e): `git diff ac90825..9100d74 -- vidlift/run.py` hunk enumeration | Executed — 7 hunks = the six §5-B touch sets (see §4.1) |
| Gate (f): version stamps ×4 + `api_version` | Executed — all 0.4.18.0; `API_VERSION = 1` (monitor.py:52, untouched) |
| WS1 code review (gui.py 16 hunks vs spec rows 4-A1..4-A11, 5-E*) | Executed — 1:1 mapping, no unexpected hunk |
| WS2 code review (subtitles.py, subs_job.py, run.py branches, cli.py, mcp_bridge ×5) | Executed — matches §5 rows exactly |
| WS3 code review (cleanup_subs_scratch + call site + path guard) | Executed — matches §6 exactly |
| WS4 code review (QSS + D12 comment + F-1 pin) | Executed — matches §7 exactly |
| **Added — mutation testing** (6 mutations in a scratch copy) | Executed — 5 FIRE, 1 does not (F-1) |
| **Added — live real-ffmpeg Mode B E2E** (4 cases: suppress-ON, default-OFF, mux-failure fallback, suppress+replace) | Executed — all pass |
| **Added — live real-ffmpeg Mode A E2E** (StubEngine upscale + real encode/mux + fake transcription engine; suppress ON/OFF + manifest honesty) | Executed — all pass |
| **Added — live GUI probes** (real LocalBridgeClient + registry: both Add Folder buttons, drag-drop, 100-file enqueue, 501 warning, symlink loop, QSettings isolation, F-1 grid pin, QSS) | Executed — all pass |
| **Added — MCP/CLI surface probes** (catalog booleans, schema accept/reject, absent-defaults, `--suppress-srt` help ×2) | Executed — all pass |
| **Added — edge probes** (.mp4-source replace under suppression, permission-denied subtree, second-add conflict guard, verbatim-move AST check) | Executed — all pass |
| §13.6 Windows visual (D12 band) | **NOT EXECUTABLE in this environment** (Linux offscreen) — release-gate item, tied to F-2 |
| UNC-root walk (`\\server\share`) | Not executable on Linux — noted, not a defect |

---

## 4. Gate-by-gate evidence

### 4.1 Gate (e) — run.py delta is ONLY the six §5-B touch sets

`git diff ac90825 9100d74 -- vidlift/run.py` (base valid: run.py's last pre-v0.4.18 touch was `20dfe61`, v0.4.16.0): **exactly 7 hunks**, `git diff -w` also 7 (no hidden whitespace hunks), mapping:

| Hunk | Lines | Touch set | Verbatim vs §5.4? |
|---|---|---|---|
| 1 | @@ -57,6 +57,10 | 5-B1 import `remove_srt_sidecar` (after `mux_srt_into_mp4,`) | ✓ (+4 comment lines beyond the spec's +1 LoC estimate — content identical) |
| 2 | @@ -1918,6 +1922,10 | 5-B2 signature `suppress_srt: bool = False,` terminal kw-only after `denoise` | ✓ |
| 3 | @@ -1962,6 +1970,19 | 5-B2 docstring paragraph (opt-in, fail-open contract, XOR bookkeeping) | ✓ |
| 4 | @@ -2775,7 +2796,20 | 5-B3 transcribed disposition | ✓ exact §5.4-B3 code |
| 5 | @@ -2797,31 +2831,49 | 5-B4 embedded-text skip-extract if/else | ✓ exact shape; else-arm code statements verbatim +1 indent (comment re-wrapped only) |
| 6 | @@ -2836,7 +2888,20 | 5-B5 adopted disposition on `sidecar_target` | ✓ exact §5.4-B5 |
| 7 | @@ -2900,6 +2965,22 | 5-B6 OCR pop of pre-mux `sidecar_path` | ✓ exact §5.4-B6 |

**No seventh touch set.** The fail-open handler (run.py:2987-3003), `cleanup_run_artifacts`, `verify_output`, `replace_source_with_output` + call site, and every non-subtitle line are byte-identical. Forge's "7 hunks = 6 touch sets" audit is confirmed.

### 4.2 Gate (f) — version + api_version

- `vidlift/__init__.py:9` `__version__ = "0.4.18.0"` ✓
- `pyproject.toml:3` `version = "0.4.18.0"` ✓
- `packaging/version_info.txt`: `filevers=(0, 4, 18, 0)`, `prodvers=(0, 4, 18, 0)`, `FileVersion '0.4.18.0'`, `ProductVersion '0.4.18.0'` — **all four** ✓
- `vidlift/monitor.py:52` `API_VERSION = 1`; monitor.py untouched by the diff ✓
- `tests/test_version_stamps.py` green (in the full suite) ✓
- CHANGELOG `## [0.4.18.0] - 2026-11-04` present at line 5 ✓ (but see F-2/F-3)

### 4.3 Gate (a) — WS1 recursion (§13.1)

**Code:** `collect_video_files` (gui.py:110-142) is the §4.2 exact code — `os.walk(followlinks=False)`, `dirnames.sort()`, case-insensitive extension filter, sort-then-`normcase(realpath)` dedupe. `add_folder` (1704-1721), `choose_subs_folder` (1845-1876, §4.3 exact incl. S-17 title), `_enqueue_subs_paths` (1878+, the old `choose_subs_files` tail moved **verbatim** — AST comparison: 16/16 statements identical except the authorized 5-E6 `suppress_srt=` kwarg), `dropEvent` dir arm (3710), `MAX_GUI_BATCH = 500` (client_local.py:59).

**Live (real client + registry, offscreen):**
- Nested tree `My Show/Season 1/{épisode un.avi, e 2.mkv}`, `Season 2/e3.mp4`, `pilot.TS` (unicode + spaces; `.txt`/`.nfo` skipped):
  - Mode A **Add Folder** → `win.files` == the fully sorted 4-path list ✓
  - Mode B **Add Folder** → 4 real registry `subs` jobs, paths == sorted list, options carry `replace_source: False, suppress_srt: False`; table Source column == sorted list; **every output path in its source's own directory** (`Season 1/e 2.subs.mp4`, `Season 2/e3.subs.mp4`, …) — nested structure preserved end-to-end ✓
  - **Drag-drop** the same dir → recursive walk + `_add_paths` dedupe (0 duplicate rows) ✓
- **100-file Mode B folder add → 100 jobs enqueued** (would have died on the 64-cap; no warning fired) ✓
- **501-file add → warning fired with the exact S-4 text** ("That folder tree holds 501 videos (subfolders included); add at most 500 at once."), nothing added ✓
- **Symlink loop**: real `alias -> real` dir symlink (never descended), `real/loop -> ..` self-loop (terminated), duplicate file symlink deduped to ONE entry with the lexicographically-first name surviving — exactly D3 ✓
- **Permission-denied subtree**: `chmod 000` dir skipped silently, siblings queued ✓
- **Second Mode B add while the first is queued**: v0.4.15.1 conflict guard fires ("Wait for the current batch…"), 0 new jobs ✓
- Shallow folder == pre-0.4.18 result: test_06 (byte-for-byte `iterdir` identity) green ✓

**Mutation proof (gate FIRES):** reverting the collector to one-level `iterdir` → **5 failures**: test_01 (pure collector), test_04 (symlinked dir), test_07 (Mode A button), test_09 (Mode B button), test_13 (drag-drop). All three ingestion surfaces are guarded.

### 4.4 Gate (b) — WS2 suppression (§13.2, §13.3, §13.4)

**Code:** `remove_srt_sidecar` (subtitles.py:466-491) is §5.2 exact (FileNotFoundError = success; other OSError → S-9 WARNING, returns False, never fatal). Mode B block (subs_job.py:294-297) is §5.3 exact — after `mplex`, before replace. All four Mode A branches are the §5.4 exact shapes (§4.1 above). `SubsJobResult.srt_path` `""` sentinel documented. CLI S-13/S-14 prints, MCP schemas/tools/executors, GUI checkboxes (S-7/S-8 tooltips, keys `suppress_srt`/`subs_suppress_srt`), `start_upscale` + `_enqueue_subs_paths` threading — all verified against the §5 rows.

**Live, REAL ffmpeg, Mode B** (real ffprobe/wav-extract/mov_text mux; only the transcription engine faked):
- **Suppress ON**: `movie.subs.mp4` exists, ffprobe shows `('subtitle', 'mov_text')`, **no `.srt` beside it**, `.subs-work` gone, `result.srt_path == ""`, S-2 note present ✓
- **Default OFF**: sidecar **KEPT** with real SRT content, `srt_path` truthful, `.subs-work` gone, no suppression note — the v0.4.17 deliverable set ✓
- **Fallback promise (§13.4, load-bearing)**: forced real-mux failure under suppression → `SubsJobError` raised, **the `.srt` is on disk afterward**, scratch retained ✓
- **Suppress + replace**: single `movie.mp4`, source gone, no `.srt` anywhere, `srt_path == ""`, mov_text stream in the replaced file ✓
- **`.mp4` source + suppress + replace** (the strongest same-path atomicity arm): single `movie.mp4` remains, honest notes ✓

**Live, REAL ffmpeg, Mode A** (StubEngine passthrough upscale + real prepare/encode/verify/mux + fake transcription engine):
- **Suppress ON**: verified `movie.720p.mp4` with `('subtitle', 'mov_text')`, **no sidecar on disk**, manifest `run.subtitles` = `{status: carried, sidecar_suppressed: true, …}` with **no block-level `sidecar_path`**, S-15 log line present ✓
- **Default OFF**: sidecar KEPT, manifest carries `sidecar_path` (v0.4.17 shape), no suppressed key ✓
- Embedded branch skips the extract entirely: test_29 asserts **no `-c:s srt` command ran** ✓; adopted branch removes only the run's COPY (test_30: user's original byte-identical) ✓; OCR branch pops the pre-mux key and retains the `.ocr/` kit (test_31) ✓
- **XOR honesty holds in every branch** (code-verified: if/else in B3/B5, single-arm B4, pop+set B6, fail-open untouched)

**Mutation proof (gates FIRE):**
- Mode B suppression moved pre-mux → **test_16 fails** (`suppress_on_mux_failure_keeps_sidecar_and_fails` — sidecar missing after mux failure) ✓
- Mode A transcribed suppression moved pre-mux → **test_32 fails** (fail-open fallback: `KeyError: 'sidecar_kept'` — the sidecar was already gone) ✓

**Surfaces parity (§13.7):** MCP catalog `run` + `transcribe` entries both carry `suppress_srt` (server tools/list composes TOOL_SCHEMAS + TOOL_DESCRIPTIONS); schema accepts `True`, rejects `"yes"` with `InvalidInput(field='suppress_srt')`, absent validates, **unknown fields still rejected**; `_execute_run_job`/`_execute_subs_job` forward `bool(options.get("suppress_srt", False))` (a legacy pre-0.4.18 record reads False → v0.4.17-identical); CLI `run --suppress-srt` (S-11) and `subs --suppress-srt` (S-12) present with exact help ✓

**Persistence/isolation (§13.8):** live round-trip — Mode A toggle writes only `suppress_srt`; fresh window reads Mode A=True, Mode B=False; Mode B toggle writes only `subs_suppress_srt`; `replace_source`/`subs_replace_source` untouched ✓

### 4.5 Gate (c) — WS3 cleanup (§13.5)

**Code:** `cleanup_subs_scratch` (subs_job.py:583-625) is §6.2 exact — path guard (`normcase/abspath` identity against `output_path + ".subs-work"`), `is_dir` check, best-effort `rmtree`, S-16 notes both ways. `workdir_used` captured once pre-rename (line 235) and threaded to the core call (line 255). Sole call site (line 328) sits after the replace block, immediately before `return` — reachable only past a successful mux.

**Live:** every real-ffmpeg success case above left **no `.subs-work`** (default, suppress, and replace postures); the mux-failure case **retained** it with the sidecar ✓

**Mutation proof (gates FIRE):**
- Cleanup call removed (the K-5 defect recreated) → **3 failures**: test_19 (scratch gone after success), test_22 (replace posture), test_23 (rmtree-OSError warning) ✓
- Cleanup moved pre-mux (success-only broken) → **test_21 fails** (failure/cancel retention) ✓
- **Path guard removed → 0 failures** (F-1). Direct probe proves the guard is live and uniquely protective: with a stale `{output}.subs-work` present and a custom workdir, the real guard returns `[]` and the stale scratch **survives**; guard-removed, it is **deleted**. The shipped code is correct; the test cannot see the difference.

### 4.6 Gate (d) — WS4 + F-1 (§13.6)

**Code + live:** `QGroupBox#Controls { … margin-top: 20px; … }` (gui.py:1633); the `::title` rule byte-identical (`subcontrol-origin: margin; subcontrol-position: top left; left: 12px`); **no negative `top:` anywhere** in the stylesheet (live string check); the D12 comment block (1609-1622) captures the mechanics, the rejected negative-top branch, the ±2 px tuning band, and the 4-7 px clearance criterion. T5 size gates re-run green in the full suite.

**F-1:** live `subs_grid.count() == 5`; members exactly `[QPushButton, QPushButton, QCheckBox, QCheckBox, QLabel]`; cells `(0,0),(0,1),(0,2),(0,3),(0,4)` all 1×1 — the §7/D13 five-item pin ✓

**§13.6's Windows visual acceptance is NOT executable on Linux offscreen** (the spec's own D12 acknowledges this). The committed 20 px is a logically-argued Windows-metric bet. **Release-gate item** — see F-2.

### 4.7 §13.2 defaults byte-identical

Full suite green with every pre-v0.4.18 test file **unmodified** (only the three authorized/escalated files changed — verified via the b506876 diff stat); test_14/test_28b assert the OFF invariant directly; live Mode B + Mode A default runs produce the exact v0.4.17 deliverable set (sidecar kept, truthful paths, no new notes); every new option reads absent → False on all surfaces (GUI writes explicit `False` = absent semantics; registry/MCP/CLI absent-defaults verified).

### 4.8 §13.9 hygiene

- Full suite green (1041/2 skips) ✓
- run.py delta = the six touch sets (§4.1) ✓
- Version stamps ×4 + api_version 1 (§4.2) ✓
- CHANGELOG present (F-2/F-3 caveats) ✓
- Docs sweep landed: README (Add Folder "including every subfolder"; deliverable list names the opt-in + fallback), usage.md (all-seasons + drop parity), cli.md (`--suppress-srt` ×2 + recursive folder-add + scratch lifecycle), architecture.md (Mode B scratch lifecycle rule); `docs/designs/*` untouched ✓
- `transcribe.py`, `plan.py`, `registry.py`, `monitor.py`, `_proc.py` untouched (diff stat) ✓
- No new dependencies; UTF-8; no QSettings renames ✓

### 4.9 Deviations audit (Forge §D vs spec §2)

- Spec's six declared deviations: all implemented as declared (drop recursion, MAX_GUI_BATCH 500, default OFF, the two amended test files, F-1 count 5, margin growth).
- **D-1** (two `test_v0414_mode_b_gui.py` fence amendments): verified minimal and exactly the remedies the spec itself prescribes for the twin fences in File E (T4 re-pin, T9 re-target). Accepted.
- **D-2** (`test_v0417_failpath_fix.py` kwarg ripple — a file in NEITHER §12 list): verified the edit is the minimal File-F pattern (defaulted kw only, no assertion changes); the alternative (conftest-shared double) is an architecture call correctly escalated, not decided unilaterally. **Accepted with Bob's-eyes note standing.**
- D-3/D-4 (sequencing): content-neutral, verified against the final tree.

### 4.10 Security & performance (§13)

- Deletion confinement: `remove_srt_sidecar` is called only on this-run-created paths (`transcribed.srt_path`, the adopted COPY at `sidecar_target`, `ocr_result.srt_path` — all derived from the job's own output); the user's original sidecar is never a candidate (test_30: original byte-identical). Cleanup deletes only the default-derived scratch (guard live). No globbing anywhere. Collector never follows symlinked dirs (live-proven). Schema boundary still rejects unknown fields (live-proven). No privilege changes. ✓
- Performance: one `os.walk` + one `realpath` per video — the 501-file probe and the 100-file enqueue were instantaneous; no steady-state cost with options OFF (all guards are `if suppress_srt:` / absent-reads). ✓

---

## 5. Mutation-testing ledger (scratch copy, restored green after each)

| Mutation | Defect class recreated | Expected sentinel | Result |
|---|---|---|---|
| M1: collector → one-level `iterdir` | recursion-less (v0.4.17 defect) | tests 01/04/07/09/13 | **FIRED — 5 failures** |
| M2: Mode B suppression moved pre-mux | suppression-fallback broken | test_16 | **FIRED** |
| M2b: Mode A transcribed suppression moved pre-mux | Mode A fallback broken | test_32 | **FIRED** |
| M3: cleanup call removed | cleanup-leak (K-5) | tests 19/22/23 | **FIRED — 3 failures** |
| M3b: path guard removed | caller-workdir deletion | test_20 | **DID NOT FIRE** → F-1 |
| M3c: cleanup moved pre-mux | success-only broken | test_21 | **FIRED** |

The three gates the brief named (recursion-less, suppression-fallback, cleanup-leak) all FIRE. The one blind spot is F-1's stale-scratch scenario.

---

## 6. Defect summary

- **Critical: 0. High: 0.**
- **Medium: 1** — F-2 (CHANGELOG false "verified visually on the Windows build" claim).
- **Low: 3** — F-1 (path-guard test gap), F-3 (changelog key-name typo), F-7 (unpinned 100-file enqueue acceptance).
- **Informational: 3** — F-4 (skip attribution), F-5 (comment anchor), F-6 (trailing newline).

---

## 7. Release recommendation

**Ready with Minor Fixes — code-cleared NOW; release-cleared after two conditions.**

1. **Fix F-2** (one line): correct or make true the CHANGELOG's Windows-verification claim. This is the only Medium defect and it is a docs-honesty issue, squarely against the project's own discipline.
2. **Execute §13.6 on the real Windows build** (the D12 protocol): both group titles, 4–7 px clearance at 100 % and 125 %, no clipped pixels, T5 width gate — the spec's own committed plan. Linux offscreen cannot substitute for it.
3. Recommended (non-blocking): F-1's stale-scratch test, F-7's 100-file pin, F-3's token fix, Bob's eyes on D-2.

The four workstreams themselves are implemented exactly per spec, are guarded by tests that demonstrably catch the three named defect classes, and behave correctly under real-ffmpeg end-to-end runs in both modes, including the load-bearing fallback promise. No code change is required for release.

— Karen
