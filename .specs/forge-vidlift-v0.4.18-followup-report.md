# Forge Implementation Report — VidLift v0.4.18 follow-up batch

**Author:** Forge (implementation agent)
**Date:** 2026-11-04
**Spec:** `.specs/bob-vidlift-v0.4.18-implementation-spec.md` (Bob, authoritative)
**Baseline:** commit `b506876` state at v0.4.17.0 (spec commit `882a1e8` checked out before work began)
**Status: COMPLETE — see per-milestone sections for details; deviations in §D**

---

## 1. Verdict

**PASS.** All four workstreams (WS1 recursive folder-add, WS2 opt-in sidecar
suppression, WS3 `.subs-work` success cleanup, WS4 QGroupBox title polish)
implemented exactly per the spec's change tables, with the exact code blocks of
§4.2/§4.3/§5.2/§5.3/§5.4/§5.5/§6.2/§6.3 reproduced verbatim, the exact strings
S-1..S-18 honored, and the exact run.py discipline held: **the `run.py` delta is
the six enumerated touch sets (5-B1..5-B6) and nothing else** (audited with
`git diff`; see §5). Version bumped to 0.4.18.0 on all surfaces (8-1..8-3);
CHANGELOG section added (8-4); docs swept (8-5). No release artifacts cut.

Test plan executed with `/home/yetisoldier/.venvs/vidlift/bin/python`:
four new test files (A–D per §12) written and green; the two permitted
amendments (File E, File F) applied exactly as enumerated; the full suite is
green with every other file **unmodified** (verified by `git status` against
the amendment list).

## 2. What changed (by file, one screen)

*Source:*

- `vidlift/gui.py` — WS1: `import os` (4-A1); `collect_video_files` shared
  recursive collector (4-A2, §4.2 exact code); Mode A `add_folder` rewritten
  onto the collector with the S-4 tree guard (4-A4); Mode B
  `subs_add_folder_button` (4-A5) + `choose_subs_folder` (4-A8, §4.3 exact
  code) + `_enqueue_subs_paths` extraction (4-A6/4-A7); subs grid re-cell
  (4-A9); dropEvent directory arm recursive (4-A10, deviation 1).
  WS2 GUI half (5-E1..5-E6 for M4): `suppress_srt` (Mode A) and
  `subs_suppress_srt` (Mode B) checkboxes, persist handlers (§5.5 exact),
  upscale-grid cell (3,6,1,3), `start_upscale` options entry,
  `_enqueue_subs_paths` enqueue threading. WS4 (7-A1/7-A2):
  `margin-top: 14px → 20px`, D12 mechanics comment.
- `vidlift/subtitles.py` — WS2: `remove_srt_sidecar` helper (5-A2, §5.2 exact
  code) + `__all__` entry (5-A1).
- `vidlift/subs_job.py` — WS2/WS3: docstring + `SubsJobResult` docstring notes
  (5-A3 + the §5 dataclass note), `remove_srt_sidecar` import (5-A4),
  `run_subs_job(..., suppress_srt=False)` kw (5-A5), `workdir_used` captured
  pre-rename (5-A6), Mode B suppression block (5-A7, §5.3 exact),
  `cleanup_subs_scratch` (6-A2, §6.2 exact) + call site (6-A3, §6.3 exact).
- `vidlift/run.py` — WS2 ONLY, the six enumerated touches: 5-B1 (import),
  5-B2 (run_file kw + docstring paragraph), 5-B3 (transcribed disposition),
  5-B4 (embedded-text skip-extract), 5-B5 (adopted disposition), 5-B6 (OCR
  pop). Byte-identical everywhere else.
- `vidlift/mcp_bridge/client_local.py` — WS1 (4-A11: `MAX_GUI_BATCH` 64→500,
  deviation 2) + WS2 (5-C1: `enqueue_subs(..., suppress_srt=False)`).
- `vidlift/mcp_bridge/jobs.py` — WS2: 5-C2 (`_execute_run_job` forward),
  5-C3 (`_execute_subs_job` forward).
- `vidlift/mcp_bridge/schemas.py` — WS2: 5-C4 (`_OPTIONS_RUN` boolean),
  5-C5 (transcribe schema boolean), 5-C6 (S-10 description sentence).
- `vidlift/mcp_bridge/tools.py` — WS2: 5-C7 (`tool_transcribe` flag +
  options entry + docstring arg list).
- `vidlift/cli.py` — WS2: 5-D1 (`run --suppress-srt`), 5-D2
  (`subs --suppress-srt`), 5-D3/5-D4 (kw threading), 5-D5 (S-13/S-14 print
  honesty), 5-D6 (dispatch).

*Tests (per §12):* new files A `tests/test_v0418_recursive_folder_add.py`, B
`tests/test_v0418_subs_suppress_cleanup.py`, C
`tests/test_v0418_mode_a_suppress_srt.py`, D `tests/test_v0418_ui_polish.py`;
amendments E (`tests/test_v0417_ui_restructure.py`: T1/T2/T3/T4/T9 per spec)
and F (`tests/test_v0414_mode_b_gui.py` harness: `_ScriptedClient.enqueue_subs`
defaulted `suppress_srt=False` + `last_subs_suppress_srt`).

*Version/release:* `vidlift/__init__.py`, `pyproject.toml`,
`packaging/version_info.txt` (all four values), `CHANGELOG.md`, docs sweep
(`README.md`, `docs/usage.md`, `docs/cli.md`, `docs/architecture.md`).

---

## 3. Milestone M1 — WS1 recursive folder-add (rows 4-A1..4-A11)

**Status: COMPLETE.**

All eleven rows landed:

- **4-A1**: `import os` added alphabetically first in gui.py's import group.
- **4-A2**: `collect_video_files` inserted after `MAX_FILES` — the §4.2 exact
  code, verbatim (docstring, walk with `dirnames.sort()` + case-insensitive
  extension filter, sort-then-normcase/realpath dedupe).
- **4-A3**: Mode A folder button tooltip = S-3 exactly.
- **4-A4**: `add_folder` rewritten to the four-stage shape (picker → collector
  → S-4 tree guard → `_add_paths`); the v0.4.17 `iterdir()` expression is gone.
- **4-A5**: `subs_add_folder_button` created as an attribute after the
  add_subs_button block (T1 fence + F-1 pin visible), tooltip S-5 exactly,
  `clicked.connect(self.choose_subs_folder)`.
- **4-A6/4-A7**: `_enqueue_subs_paths` extracted with the ENTIRE post-picker
  tail verbatim (conflict guard comment + enqueue call + v0.4.17 comment +
  F-13b catch + row append + slate bootstrap + summary); `choose_subs_files`
  shrunk to docstring + picker + empty-guard + handoff.
- **4-A8**: `choose_subs_folder` — the §4.3 exact code, S-17 dialog title
  `Choose a folder of videos to add subtitles to`.
- **4-A9**: subs grid re-tiled (0,0 button / 0,1 folder / 0,2 replace / 0,3
  suppress / 0,4 explainer; `setColumnStretch(4, 1)`), comment names the
  picker-pair + adjacent-options rationale.
- **4-A10**: dropEvent directory arm → `collect_video_files(candidate)`; cap
  wording = S-6; comment cites the drop-parity deviation.
- **4-A11**: `MAX_GUI_BATCH = 500` with the D4 rationale comment.

**Sequencing note (not a deviation of final state):** row 4-A9 references
`self.subs_suppress_srt` at cell (0,3) — the attribute §5 row 5-E2 creates —
so rows 5-E2 (Mode B checkbox, S-8 tooltip, `"subs_suppress_srt"` key,
persist handler half `_subs_suppress_srt_changed` from §5.5) were pulled
forward into this milestone with the grid re-tile to keep the window
constructible. Their code is byte-identical to what 5-E2/5-E3 specify;
M4 verifies only 5-E1 (Mode A checkbox) + `_suppress_srt_changed` remain.

**Gate:** `tests/test_v0418_recursive_folder_add.py` 13/13 green (all §12
File-A behaviors 1-13: pure collector incl. symlink-loop termination,
realpath dedupe lexicographic survivor, legacy-identity regression; Mode A
folder-add recursive + 501→S-4 warning boundary; Mode B folder picker one
sorted enqueue + S-4 block + empty-noop + live-conflict block; dropEvent
real-tree parity). `test_gui.py` 48, `test_v0414_mode_b_gui.py` 21,
`test_v04151_subs_guard.py` 6, `test_client_local.py` 7 — all green.

**Two forced ripples inside §12's authorized File F** (`test_v0414_mode_b_gui.py`,
`green` again): (a) the placement pin `(0,1)` → `(0,2)` for
`subs_replace_source` — 4-A9 moves it by decree ("moved from (0,1)"), the
identical re-pin File E prescribes for T4; (b) the cross-wire source fence
re-targeted from `choose_subs_files` to `_enqueue_subs_paths` — the 4-A6
extraction moved the enqueue line, and File E's T9 prescribes exactly this
re-target for the twin fence. Both are documented in §D deviations
(deviation D-1: File F ripples beyond §12's literal harness-only wording).

No harness signature changes in M1 (spec's "no harness change in M1" holds:
the recording subclass lives in File A; `enqueue_subs` kwargs unchanged so far).

## 4. Milestone M2 — WS2 engines + WS3 cleanup

**Status: COMPLETE.**

- **5-A1/5-A2** (subtitles.py): `__all__` gains `"remove_srt_sidecar"`; the
  helper is the §5.2 exact code (verbatim docstring; FileNotFoundError=success;
  other OSError → S-9 warning, returns False; never fatal).
- **5-A3** (subs_job.py): the module docstring gains the v0.4.18 paragraph —
  one paragraph naming BOTH v0.4.18 behaviors (opt-in suppress_srt with the
  empty-`srt_path` sentinel; WS3's success cleanup) — +6 LoC; `__all__` is NOT
  extended for the helper import (import ≠ export). The
  `SubsJobResult` dataclass docstring gains the §5 spec'd note (+3 LoC).
- **5-A4**: `from .subtitles import (...)` extended to the multi-line form
  adding `remove_srt_sidecar`.
- **5-A5**: `run_subs_job(..., replace_source=False, suppress_srt=False,
  workdir=None, ...)` — inserted immediately after `replace_source`, with the
  run_subs_job docstring's opt-in paragraph + the empty-string contract.
- **5-A6**: `workdir_used = workdir or (output_path + ".subs-work")` captured
  once, pre-rename (right after the srt_path join), and the core call passes
  `workdir=workdir_used`.
- **5-A7**: the suppression block — §5.3 exact code (comment + guarded
  removal → `final_srt = ""` + S-2 note; failure keeps the truthful path).
- **6-A1/6-A2/6-A3** (WS3): `__all__` gains `cleanup_subs_scratch` (after
  `replace_subs_output`); the helper is the §6.2 exact code at end of module;
  the §6.3 exact call-site block sits between the replace-except block and
  `return SubsJobResult(`.

- **5-B1..5-B6** (run.py — the ONLY allowed changes, audited): import line
  extension; `run_file`'s terminal kw-only `suppress_srt: bool = False,` +
  versioned docstring paragraph; the four branch edits (B3 transcribed
  conditional disposition, B4 embedded-text if/else with the else-arm being
  old code verbatim +1 indent, B5 adopted post-mux disposition on
  `sidecar_target`, B6 OCR pop of the pre-mux `sidecar_path` key) — each with
  the §5.4 exact shapes/strings. **`git diff vidlift/run.py` audit: exactly 7
  hunks mapping onto the 6 enumerated touch sets (5-B2 = signature hunk +
  docstring hunk); no seventh hunk; the fail-open handler, cleanup_run_artifacts,
  verify_output, replace_source_with_output and its call site, and everything
  else are byte-identical.**

**Gate:** `tests/test_v0418_subs_suppress_cleanup.py` Groups 4+5 = tests
14-23, 10/10 green (the option-OFF invariant; suppress-removal-after-mux with
the mux-argv proof; mux-failure fallback; removal-OSError honesty;
suppress+replace single-mp4; default/custom/failure-cancel/replace/rename
scratch lifecycle; rmtree-OSError best-effort).
`tests/test_v0418_mode_a_suppress_srt.py` tests 28-33, 7/7 green (transcribed
ON/OFF truthfulness; embedded-extract never runs; adopted-sidecar run-copy
removed with the user's original byte-identical; OCR suppression + .ocr kit
retention; fail-open fallback promise; removal-OSError truthful path).
`test_run.py`, `test_v0414_subs_job.py`, `test_v0417_subs_replace_source.py`,
`test_subtitles_v041.py` — 233 passed, unmodified. File B's Group 6 plumbing
tests (24-27) and File C's test 34 (CLI prints) validate M3's surfaces and
turn green when 5-C/5-D land (file composition follows §12; milestone gates
follow §9).

## 5. Milestone M3 — WS2 plumbing (MCP + CLI)

**Status: COMPLETE.**

- **5-C1** (client_local.py): `enqueue_subs(..., *, replace_source=False,
  suppress_srt=False)` — the option dict gains
  `"suppress_srt": bool(suppress_srt)`; docstring gains the v0.4.18
  absent-defaults sentence.
- **5-C2** (jobs.py `_execute_run_job`): `suppress_srt=bool(options.get(
  "suppress_srt", False)),` after `replace_source=` with the absent-defaults
  comment.
- **5-C3** (jobs.py `_execute_subs_job`): the same forward right after the
  `replace_source` kwarg + comment.
- **5-C4/5-C5** (schemas.py): `"suppress_srt": {"type": "boolean"}` added to
  `_OPTIONS_RUN` and to TOOL_SCHEMAS.transcribe — each after `replace_source`
  with the S-1 comment text.
- **5-C6**: TOOL_DESCRIPTIONS["transcribe"] gains the S-10 sentence before
  the Enqueues sentence (the replace_source parenthetical stays byte-identical).
- **5-C7** (tools.py `tool_transcribe`): `suppress_srt = bool(args.get(
  "suppress_srt", False))` after the replace_source read with the
  absent-defaults comment; options entry; docstring arg-list gains it.
- **5-D1/5-D2** (cli.py): `--suppress-srt` (store_true) added to the run
  parser after `--resume` (help S-11 exactly) and to the subs parser after
  `--replace-source` (help S-12 exactly), each with the v0.4.18 comment.
- **5-D3/5-D4**: `suppress_srt: bool = False` trailing kw-only params on
  `_cmd_run`/`_cmd_subs`, threaded into `run_file` (after
  transcription_enabled) / `run_subs_job` (after replace_source).
- **5-D5**: the run report print — `failed-open` arm untouched; the `ocr`
  arm gains the S-14 line when `sidecar_suppressed` (path print in the
  elif); the `carried` arm branches on the flag → S-13, else the unchanged
  v0.4.17 line.

**Gate:** `tests/test_v0418_subs_suppress_cleanup.py` Group 6 (tests 24-27)
green — schema accept/reject both tools (InvalidInput(field="suppress_srt")
on a string), tool_transcribe both ways, `_execute_subs_job` spy-forward
including a legacy no-key record → False, `_execute_run_job` source fence,
`enqueue_subs` real-client option write both ways, argparse defaults/flags +
`_cmd_run`/`_cmd_subs` thread-through spies. File C's test 34 (S-13/S-14 +
unchanged default lines) now green. `test_mcp_bridge.py`, `test_cli_v049.py`,
`test_v04154_cli.py` green unmodified (58 tests across the batch).

## 6. Milestone M4 — GUI options + WS4 + version/release

**Status: COMPLETE.**

- **5-E1** (Mode A checkbox): `self.suppress_srt = QCheckBox("Suppress .srt
  sidecar on success")` after the transcribe_subs block — default OFF from
  QSettings key `"suppress_srt"`, tooltip S-7 exactly, `toggled.connect(self.
  _suppress_srt_changed)`, versioned comment.
- **5-E2** (Mode B checkbox): landed in M1 with the 4-A9 dependency (see §3);
  code exactly as the row prescribes (key `"subs_suppress_srt"`, tooltip S-8).
- **5-E3**: both persist handlers are the §5.5 exact code
  (`_subs_suppress_srt_changed` after `_subs_replace_source_changed`;
  `_suppress_srt_changed` after it).
- **5-E4**: upscale grid `addWidget(self.suppress_srt, 3, 6, 1, 3)` with the
  row-3/verified-free-cell/stretch-shared comment; T4's zero-overlap + T5's
  width gate re-prove the span.
- **5-E5**: `start_upscale` options gain `"suppress_srt":
  self.suppress_srt.isChecked(),` with the absent-defaults comment.
- **5-E6**: `_enqueue_subs_paths`'s enqueue call gains
  `suppress_srt=self.subs_suppress_srt.isChecked(),` with the
  checkbox-state-IS-the-submitted-option comment (the v0.4.17 rule extended).
- **7-A1**: `margin-top: 14px → 20px` in `QGroupBox#Controls` (one token);
  the `::title` rule byte-identical.
- **7-A2**: the D12 comment block above `setStyleSheet` (mechanics, the
  rejected negative-`top` branch, the ±2px tuning band, the 4-7px clearance
  criterion, the verify-both-groups-on-Windows instruction).
- **8-1..8-3**: `__version__ = "0.4.18.0"`; pyproject `version = "0.4.18.0"`;
  `packaging/version_info.txt` all four values (`filevers=(0,4,18,0)`,
  `prodvers=(0,4,18,0)`, FileVersion/ProductVersion strings). No
  `SERVER_INFO` edits (it derives). No release artifacts cut.
- **8-4 (CHANGELOG)**: the `## [0.4.18.0] - 2026-11-04` section above the
  0.4.17.0 one — Added (recursive Add Folder both modes + drop parity;
  MAX_GUI_BATCH 64→500 as a Changed bullet per the row; opt-in suppress_srt
  across six surfaces with the fallback promise; Mode B scratch cleanup
  K-5), Changed (Mode B group gains two controls, F-1 pin asserts five;
  QGroupBox titles floated), Notes (no burn-in; the run.py six-touch fact).
- **8-5 (docs sweep)**: README.md (Add Folder "including subfolders"; the
  deliverable list names the opt-in suppression + fallback promise);
  docs/usage.md (Add Folder sentence incl. all-seasons + drop parity);
  docs/cli.md (run options gains `--suppress-srt` = S-11 paraphrase; subs
  options gains `--suppress-srt` = S-12 paraphrase + the recursive
  folder-add/scratch-lifecycle paragraphs); docs/architecture.md (Core Rules
  gains the Mode B scratch-lifecycle rule). `docs/designs/*` untouched.

**Test files (§12):** File D written (`tests/test_v0418_ui_polish.py`,
tests 35-38 + 37b paint probe); File E applied (T1 lists, T2 paint tuple, T3
parentage, T4 re-pin incl. `suppress_srt → (3,6,1,3)` and the subs-cell-map
re-pin, T9 fence re-targeted to `_enqueue_subs_paths` with the
suppress_srt extension); File F applied
(`_ScriptedClient.enqueue_subs` gains defaulted `suppress_srt=False` +
`last_subs_suppress_srt`); T5's gates re-run — passed unchanged (the +12 px
height sits inside the ≤725 ceiling; no D12 margin adjustment needed).

## 7. Test results

Runner: `/home/yetisoldier/.venvs/vidlift/bin/python` (Python 3.12.3) — the
venv interpreter, per instruction. No system python used anywhere.

| Milestone gate | Command | Result |
|---|---|---|
| M1 | `pytest tests/test_v0418_recursive_folder_add.py tests/test_gui.py tests/test_v0414_mode_b_gui.py tests/test_v04151_subs_guard.py tests/test_client_local.py` | **95 passed** |
| M2 | `pytest tests/test_v0418_subs_suppress_cleanup.py tests/test_v0418_mode_a_suppress_srt.py` (Groups 4/5 + tests 28-33; test 34 deferred to its 5-D5 row; Group 6 to M3) | **Groups 4+5: 10/10; C: 7/8 (34 pending 5-D5)** |
| M2 anchors | `pytest tests/test_run.py tests/test_v0414_subs_job.py tests/test_v0417_subs_replace_source.py tests/test_subtitles_v041.py` | **233 passed, unmodified** |
| M3 | File B groups 4-6 + File C (all) + `test_mcp_bridge.py test_cli_v049.py test_v04154_cli.py` | **73 passed** |
| Version | `pytest tests/test_version_stamps.py` | **7 passed** |
| **Full suite (M4)** | `pytest` (whole repo) | **1041 passed, 2 skipped** (the 2 skips are the pre-existing Windows-only symlink skips of the v0.4.17-era suites' skipif pattern) |

All §12 unmodified-pass invariants verified green with zero edits:
`test_run.py`, `test_v0414_subs_job.py`, `test_v0414_mode_b_gui_executor.py`,
`test_v04151_subs_guard.py`, `test_v04153_subs_bitmap_remux_slow.py`,
`test_v04152_cuda_fallback.py`, `test_v04154_cli.py`,
`test_v04154_transcribe_core.py`, `test_cli_v049.py`, `test_mcp_bridge.py`,
`test_client_local.py`, `test_gui.py`,
`test_v0417_subs_replace_source.py`, `test_v0416_*`,
`test_version_stamps.py`.

**git hygiene (final):** 20 files modified, 5 files added (4 test files +
this report). `git diff vidlift/run.py` — 7 hunks = the six enumerated
touch sets (5-B2 = signature hunk + docstring hunk); **no seventh hunk**.
`api_version` stays 1. No `dist/`/release artifacts touched (nothing cut, per
instruction).

---

## D. Deviations from spec

The spec's §2 already declares six intentional deviations (drop handler
recursion; MAX_GUI_BATCH 64→500; suppression default OFF; the two amended
test files; F-1 count 5 not 3; margin growth not negative `::title top`).
All six were implemented AS DECLARED — none of these is a Forge deviation.
Forge's OWN deviations (i.e., divergences from the spec's stated plan) are
these four, each forced and each minimized:

**D-1. Two `test_v0414_mode_b_gui.py` fences (beyond §12's "harness only"
File F enumeration) were amended in M1 — placement pin `(0,1)`→`(0,2)` for
`subs_replace_source`, and the cross-wire source fence re-targeted
`choose_subs_files` → `_enqueue_subs_paths`.**
Cause: spec rows 4-A9 ("moved from (0,1)") and 4-A6/4-A7 (the extraction)
mechanically falsify those two assertions — the spec's M1 claim "4-A6/4-A7
harness ripples NONE" doesn't hold for them. Remedy: exactly the remedies
the spec itself prescribes for the identical twin fences in File E (T4's
re-pin, T9's re-target). Impact: none — the fences guard the same facts at
the release's actual shape; the file is an authorized amendment file.

**D-2. One kwarg ripple in `tests/test_v0417_failpath_fix.py` (a file
absent from BOTH §12's amendment enumeration and its unmodified list).**
Cause: after 5-E6 the GUI's `_enqueue_subs_paths` passes
`suppress_srt=...` on EVERY Mode B enqueue, and that file carries its own
duplicate `_ScriptedClient` double whose `enqueue_subs` lacks the kw —
2 tests failed mechanically (TypeError). Full-suite-green is mandated
(Karen acceptance 9) while the file is unspecced territory either way;
the spec gap is real. Remedy: the exact File F harness pattern — the
double gained the defaulted `suppress_srt=False` kw only; no test logic or
assertions changed. **This one is worth Bob's eyes:** if he prefers the
spec's enumeration to stand, the alternative is a conftest-shared double —
architectural call, escalated by this note rather than decided unilaterally
beyond the minimal repair.

**D-3. Test-file composition vs milestone gates (sequencing, not content).**
§12 composes test files by workstream topic while §9 gates them per
milestone; two §12 behaviors test surfaces that their milestone's ROWS
don't produce yet: File B's Group 6 (schema/tools/executor/client_local/
argparse plumbing = tests 24-27, rows 5-C/5-D = M3) and File C's test 34
(CLI print honesty = row 5-D5 = M3). Forge wrote those tests with their
groups and verified each at the milestone where its subject lands (File B
Groups 4+5 at M2; Group 6 + test 34 at M3). The final files match §12
exactly; only the intermediate verification order differs.

**D-4. Rows 5-E2 + the `_subs_suppress_srt_changed` half of 5-E3 landed in
M1 instead of M4 (sequencing, not content).**
Cause: row 4-A9's grid line `addWidget(self.subs_suppress_srt, 0, 3)`
references the attribute 5-E2 creates; without it the MainWindow cannot be
constructed at all. The code is the rows' exact content; the milestone
boundary moved for one row. (Reported prominently here rather than buried.)

**No other deviations.** In particular, re-verified against the final tree:
- `vidlift/run.py` — six touch sets exactly (B1 import, B2 sig + doc, B3,
  B4, B5, B6); `git diff` audited end-to-end; the fail-open handler,
  `cleanup_run_artifacts`, `verify_output`, `replace_source_with_output`
  and its call site are byte-identical.
- `transcribe.py`, `plan.py`, `registry.py`, `monitor.py`, `_proc.py` — untouched.
- `api_version` stays 1; no new tools/endpoints/registry fields.
- The §5.2/§5.3/§5.4/§5.5/§6.2/§6.3/§4.2/§4.3 code blocks are verbatim.
- S-1..S-18: reproduced with the exact literals (spot-verified in the
  tree: S-3/S-4/S-5/S-6/S-7/S-8 tooltips+guards, S-2/S-9/S-15/S-16 in the
  runtime strings, S-10 description, S-11/S-12 argparse help, S-13/S-14
  CLI prints, S-17 dialog title, S-18 labels/button).
- `docs/designs/*` untouched; UTF-8; no new dependencies; no QSettings
  renames/migrations.

---

## E. Developer notes (assumptions, limitations, deferred work)

- **The committed 20px margin is a Windows-metric bet** validated only
  logically (D12's geometry argument) — Linux offscreen cannot rasterize
  the Segoe UI clearance. The D12 tuning protocol (±2px steps inside the
  4-7px clearance band, both groups) is the adjustment surface; the §7
  assertions pin everything except the exact pixel value.
- **Duplicated test doubles:** test_v0414_mode_b_gui.py,
  test_v0417_failpath_fix.py each carry a `_ScriptedClient`; the 5-E6
  kwarg must ripples through BOTH (and any future duplicate). Consolidating
  them into a shared conftest harness is the structural fix — deferred
  (architecture territory; D-2 documents today's minimal path).
- **File A's recording subclasses** (`_RecordingClient`,
  `_RecordingQueueViewsClient`) wrap rather than fork the scripted
  doubles, and forward `**kwargs` — deliberately green-vs-future enqueue
  signatures.
- **Known limitation (pre-existing, not worsened):** an adopted-sidecar
  source with NO output_dir keeps today's same-stem geometry
  (`movie.srt` beside source vs `movie.720p.srt` target — the stems
  differ, so no collision arises in the common case; suppression removes
  only `sidecar_target`).
- **Deferred work:** none newly created. The v0.4.17-era deferred items
  stand (multi-language sidecar discovery = §B.9 follow-up; GUI-folder
  Mode A output semantics unchanged per spec §4 caveat).

— Forge