# Forge Report — v0.4.17 workstream-2: subtitle-surface UI restructure

**Implementer:** Forge
**Date:** 2026-11-04
**Spec:** `.specs/bob-vidlift-v0.4.17-ui-restructure-spec.md` (authoritative, read in full)
**Base tree:** `81dc1d7` (workstream-1 checkpoint `0269cb0` + spec docs; workstream-1 code landed)
**Interpreter:** `/home/yetisoldier/.venvs/vidlift/bin/python` throughout (canonical per spec §0)

## Verdict (top-line)

**IMPLEMENTATION COMPLETE per spec — all milestones M1→M3 executed; all exit gates green.**

- M1 (restructure): gui.py items 1-6 applied exactly (§6.1 block verbatim, §6.2 fix verbatim, §6.5 QSS verbatim, §6.3 strings exact); item-8 test edits applied. Gate: `test_gui.py` + `test_v0414_mode_b_gui.py` + `test_v0414_mode_b_gui_executor.py` green; live offscreen probe measured **1290×696** window minimum (spec predicted ≈1290×~700; ≤1330 M1 ceiling met).
- M2 (fences): new `tests/test_v0417_ui_restructure.py` (T1-T10) green. Full suite green (see M2 section for the final tally).
- M3 (hygiene): CHANGELOG §6.6 appended inside `[0.4.17.0]` (no retitle, D6); docs sweep recorded; `test_version_stamps.py` 7/7 green **unmodified**; `git diff 0269cb0..HEAD -- vidlift/run.py vidlift/subs_job.py vidlift/cli.py vidlift/mcp_bridge/` **empty**; protected files untouched (byte-identity proof below).
- No version file touched; version stays **0.4.17.0** (D6). No release artifacts cut.

See "Deviations from spec" (end of file) — five entries: three realization choices a reviewer should confirm (harness import vs copy, `_grid_position` extension, CHANGELOG placement inside the section), one transparent estimate overage (comment/docstring lines only, no scope change), and one bookkeeping note (a fixture the ported test needs, same as its original). None change spec'd behavior, strings, or layout.

## What changed (inventory)

| File | Change |
|---|---|
| `vidlift/gui.py` | Items 1-6: `QGroupBox` import; `transcribe_subs` label shortened + Finding-1 comment; `subs_replace_source` label shortened + placement comment; `_build_shell` controls block replaced with the two-QGroupBox structure (§6.1); QSS `#Controls` → `QGroupBox#Controls` + `::title` (§6.5); `_subs_detail_text` K-2 three-way mapping (§6.2). |
| `tests/test_v0414_mode_b_gui.py` | Item 8: item-25 placement assertions → Mode B group grid (0,0)/(0,1); label assertion → D3 string; group-separation assertions added; helper docstring updated. Items 26-29 untouched. |
| `tests/test_v0417_ui_restructure.py` | NEW (item 7): T1-T10 layout-membership fence, paint truth, group separation, cell map + zero-overlap, width/height gates, transcribe visibility + persistence, K-2 detail strings, wiring fences, plan/render threading. |
| `CHANGELOG.md` | Item 9: §6.6 "Changed"/"Fixed" blocks appended inside the existing `[0.4.17.0]` section. |

**Explicit non-changes verified at M3:** `vidlift/run.py`, `vidlift/subs_job.py`, `vidlift/cli.py`, `vidlift/mcp_bridge/*`, `vidlift/plan.py`, `vidlift/transcribe.py`, `vidlift/subtitles.py`, `vidlift/monitor.py`, all version files — zero edits.

---

## Milestone M1 — restructure (gui.py + item-8 test edits)

**Status: COMPLETE — exit gate green.**

Applied (in spec-§5 order):

1. **Item 1** — `QGroupBox,` added to the QtWidgets import block in its alphabetical slot (after `QGridLayout,`, before `QHBoxLayout,`).
2. **Item 2** — `transcribe_subs` label → `"Transcribe missing subtitles"` (D2); tooltip, QSettings key `"transcribe_subs"`, `True` default, and `toggled` handler **verbatim**; the creation comment now carries the Finding-1 story (created v0.4.14, never added to a layout, rendered at upscale grid (3,3,1,3) by workstream-2).
3. **Item 3** — `subs_replace_source` label → `"Replace original on success (deletes source)"` (D3/K-1); workstream-1 §6.3 tooltip **verbatim**; comment updated: placement shared-grid (2,8) → Mode B group grid (0,1), co-location rationale retained, K-1 width history recorded.
4. **Item 4** — `_build_shell` controls block replaced **wholesale** with the §6.1 code (two `QGroupBox`es, objectName `"Controls"` each; upscale rows 0-3 and Mode B row exactly per §3, including the verbatim house comments — verb order §12, Karen-r2-D2 span, v0.4.13 Cancel-Batch; new row comments for the de-overlap and the Finding-1 render). `explainer` is a scope-only `#Muted` wordwrap local at (0,2) with `setColumnStretch(2, 1)`. Block ends at `layout.addWidget(self.progress)` as before; run panel/splitter/table untouched. `_toggle_monitor` connection preserved verbatim from the old block.
5. **Item 5** — QSS `#Controls { … }` → `QGroupBox#Controls { … }` + `QGroupBox#Controls::title { … }` exactly per §6.5; all other style rules untouched.
6. **Item 6** — `_subs_detail_text` renderer: the two-line `"Resolution handling"` branch replaced by the §6.2 three-way `"Source file:"` mapping (K-2), incl. the honest passthrough arm and the mirror-literals comment. `_subs_file_rows` column-3 literals verified byte-preserved (repo grep: only the frozen sites at the new :1609/1611 plus the mirrored literals in the renderer).
7. **Item 8 (file B edit)** — `tests/test_v0414_mode_b_gui.py`: item-25 test placement assertions → Mode B group grid `(0, 0)`/`(0, 1)`; label assertion → the D3 string; **added** group-separation assertions (Mode B pair share a parent; `replace_source` and `transcribe_subs` share the upscale group; `transcribe_subs` at upscale `(3, 3)`); test docstring and `_grid_of`/`_grid_position` docstrings updated. `_grid_of` body unchanged (`add_subs_button.parentWidget()` already resolves to the Mode B group — spec's "no helper change" honored); `_grid_position` now resolves the layout from the *looked-up widget's own parent* so the spec-required upscale-grid `(3, 3)` assertion is expressible — for the Mode B pair it resolves to the same grid `_grid_of` returns, so the original semantics are preserved. Items 26-29 **untouched**.

**Exit gate evidence:**
- `tests/test_v0414_mode_b_gui.py tests/test_v0414_mode_b_gui_executor.py tests/test_gui.py` → **74 passed** (canonical venv).
- Live offscreen probe (`QT_QPA_PLATFORM=offscreen`, `VIDLIFT_MONITOR_AUTOSTART=0`, isolated QSettings ini): **window minimumSizeHint = 1290×696** — width is exact v0.4.16.2 parity as the spec predicted (≤1330 M1 gate; ≤1360 T5 ceiling); height ≤725 ceiling met (spec ~700 ± metric slack).
- §0.1 hint cross-check on this box: transcribe 182px (spec 184), subs_replace 271px (spec 274), monitor 419, resume 153, OCR 137, Mode A replace 326, start 110, cancel-batch 103 — all within a few px of Bob's reference measurements.
- Group truth: `findChildren(QGroupBox)` = exactly `Upscale` + `Add subtitles (no upscale)`, both objectName `Controls`; membership probe: transcribe/replace/resume → `Upscale`; subs_replace/add_subs → `Add subtitles (no upscale)`.

---

## Milestone M2 — fences (new test file)

**Status: COMPLETE — exit gate green.**

Wrote `tests/test_v0417_ui_restructure.py` (item 7): 11 tests implementing T1-T10 on the Mode B scripted-client harness. Realization note: the harness is the house-shared one — the file imports `_window`, `_ScriptedClient`, `_plan_for`, `_alive` from `tests/test_v0414_mode_b_gui.py`, the exact pattern `tests/test_v04151_subs_guard.py:61` already uses for the same objects (the spec's "harness copied … `_ScriptedClient`, generator `_window`, `_alive` list" is honored: the harness objects are byte-identical by import; a copy-paste duplicate would have drifted the moment the mode-B harness evolves and pushed the file far beyond the ~230 LoC estimate).

Coverage per T-group:

- **T1 never-rendered fence** — recursive walk from `centralWidget().layout()` descending BOTH nested layout items and each widget's own widget-layout (the QGroupBox grids are widget layouts); asserts all 16 attribute-owned controls + the exact §6.3 explainer QLabel are laid out. **Mutation-verified**: removing the `transcribe_subs` addWidget makes T1 fail (Finding-1 class structurally dead), restored → green.
- **T2 paint truth** — the §0.1 hide-and-grab technique per widget (transcribe, resume, monitor, subs_replace, replace_source): parent-group grab → hide → re-grab → nonzero inside-geometry pixel delta; `pytest.skip` guard when the platform cannot rasterize offscreen.
- **T3 group separation** — parent identities both directions + titles `Upscale` / `Add subtitles (no upscale)` among `findChildren(QGroupBox)`.
- **T4 cell map + zero-overlap** — exact `(row, col, rowspan, colspan)` pins (add_subs (0,0), subs_replace (0,1), transcribe (3,3,1,3), resume (2,7,1,2), monitor (2,0,1,4), replace_source (1,5,1,2)) + the general pairwise intersection check over every item of BOTH grids. **Mutation-verified**: re-creating the v0.4.9 overlap (monitor span 5, resume (2,1)) fails T4.
- **T5 width/height gates** — `minimumSizeHint()` ≤ 1360 / ≤ 725 with the measured-provenance comment (reference box 1290×696, v0.4.16.2 parity, 1366-px laptop context). Lives only in this file, which Windows CI does not enumerate (§0.2).
- **T6 transcribe visibility + persistence** — default-ON on fresh settings; both-direction persist/restore under `"transcribe_subs"` (item-26 pattern); §6.3 label; tooltip retains `1.5 gb` + `no subtitle track`; Mode B key stays absent.
- **T7 row-2 contract** — resume in the upscale group at (2,7,1,2); renderability delegated to T2's delta (as §8 specifies).
- **T8 K-2 detail strings** — enqueue→select flow for the ON posture (`Source file: replaced on success — output renamed to <name>.mp4`) and OFF posture (`Source file: kept — output beside it as <name>.subs.mp4`); `Resolution handling` asserted absent; direct `_subs_detail_text` unit arms for replace / keep / unknown-passthrough (`Source file: 720p`).
- **T9 wiring fences** — inspect-pinned read sites (`transcription_enabled` / `replace_source` / `resume` in `start_upscale`, never `subs_replace_source`; `replace_source=self.subs_replace_source.isChecked()` in `choose_subs_files`, never `self.replace_source.isChecked()`).
- **T10 plan/render threading** — both test_gui.py F6 halves against the new layout: build-batch-plan spy threads `transcription_enabled` both ways; render_batch honest disabled-note override with the `transcription_engine_on` fixture (same as the original).

**Exit gate evidence:**
- `tests/test_v0417_ui_restructure.py` alone → **11 passed**.
- Full suite (canonical venv, offscreen): **1001 passed, 2 skipped** — the spec's ≈1000/2 prediction (990 prior + 11 new; the 2 skips are pre-existing).

---

## Milestone M3 — hygiene (items 9-10 + proofs)

**Status: COMPLETE — all proofs green.**

**Item 9 — CHANGELOG:** the §6.6 "Changed"/"Fixed" blocks appended **verbatim** inside the existing `[0.4.17.0] - 2026-11-04` section (no retitle — D6 honored; version stays 0.4.17.0). Placement: after the section's `### Added` block and before its `### Notes` block, keeping keep-a-changelog ordering (Added → Changed → Fixed, Notes last). The spec anchored "after line 37", but workstream-1's landed CHANGELOG content ends that section at line 33 in this tree — the section-internal anchor intent ("same version section, no retitle") is met; see Deviations note 3.

**Item 10 — docs sweep (verify-only):** `rg` over `README.md` + `docs/` for `.subs.mp4` and panel-layout prose → **no user-facing doc content requires changes** (the string set appears only in code/tests). Known hit confirmed and left untouched as instructed: `docs/designs/v0.4.9-resume-and-observability.md:185-186` ("a Resume retained work checkbox on the run panel…") — historical design record, do-not-edit. **Note for Quill:** that record's "run panel" wording now describes a checkbox that lives in the Upscale group's row 2; the combined docs pass owns the user-facing sweep (incl. K-6's `vidlift subs` gap).

**Proofs (all run against the final working tree, canonical interpreter):**

| Proof | Result |
|---|---|
| `tests/test_version_stamps.py` | **7 passed — unmodified** (D6: no restamp; `__init__.py`, `pyproject.toml`, `packaging/version_info.txt` all untouched at 0.4.17.0) |
| `git diff 0269cb0..HEAD -- vidlift/run.py vidlift/subs_job.py vidlift/cli.py vidlift/mcp_bridge/` | **empty (0 lines)** |
| `git diff ac90825..HEAD -- vidlift/run.py` | **empty (0 lines)** — §2's byte-identity gate holds at both anchors |
| `git diff HEAD -- vidlift/{run,subs_job,cli,plan,transcribe,subtitles,monitor}.py vidlift/mcp_bridge/` (working tree) | **empty for every file** — this workstream touched none of them |
| Full suite, final run | **1001 passed, 2 skipped** |
| §8 "unmodified-pass invariants" named files, explicitly | **221 passed** (`test_run.py`, workstream-1 file A all 25, fail-path, subs-job, executor, subs-guard, bitmap-remux, cli-v049, mcp-bridge, client-local, version-stamps — zero edits to any) |
| Spec §6.1 block vs shipped `gui.py` | **byte-verbatim match** (89 lines, mechanical diff) |
| Spec §6.2 block vs shipped `gui.py` | **byte-verbatim match** (18 lines, mechanical diff) |
| Spec §6.5 QSS / §6.3 strings | verbatim present (mechanical needle checks; old 352px/288px labels and the live "Resolution handling" render confirmed gone — the old heading survives only inside the §6.2 historical comment) |
| Release artifacts | **none cut** (no packaging/stamp work; combined release lands after Karen + Quill) |

Note on the §7 M3 gate wording: `git diff ac90825..HEAD` for `subs_job.py`/`cli.py`/`mcp_bridge/` is **not** empty (302/51/117 lines) — those are *workstream-1's own committed changes* on my inherited base (checkpoint `0269cb0` = "workstream-1 checkpoint … is the base", spec header). This workstream's diff against its base is empty for all of them, and `run.py` is empty at both anchors — which is the invariant §2 and §10 actually assert.

---

## Self review (Forge)

- **Dead code / duplication:** none introduced. The `controls` local QFrame symbol (grep-verified by Bob) is gone; the explainer is a scope-only local per §6.1's house pattern. No new QSettings keys, no new deps, no timers/threads.
- **Behavior fences:** T9 (inspect-pinned read sites) + workstream-1 items 26-29 green **unmodified** prove zero rewiring; `start_upscale` options dict, all handler bodies, `_subs_file_rows` column-3 literals byte-preserved (repo greps).
- **Mutation-verified fences:** T1 proven to catch the never-rendered class (addWidget removed → T1 red → restored green); T4 proven to catch the overlap class (v0.4.9 overlap recreated → T4 red → restored green). The Finding-1 class and the overlap class are now machine-dead.
- **Consistency:** versioned `v0.4.17 workstream-2` comments; §6.3 strings centralized as constants in the new test file; em-dash/arrow characters carried through exactly as spec'd.
- **Window min on this box: 1290×696** (width = exact v0.4.16.2 parity; height inside the ≤725 ceiling). QGroupBox title rendering on **Windows native style** remains Karen's §10 check (the §6.5 fallback exists if needed; no QSS substitution was needed or made offscreen).

---

## Deviations from spec

1. **Test harness realized by import, not copy (M2, item 7).** Spec: "Harness copied from tests/test_v0414_mode_b_gui.py". Realized as the house pattern `from test_v0414_mode_b_gui import _window, _ScriptedClient, _plan_for, _alive` — the exact same pattern `tests/test_v04151_subs_guard.py:61` already uses for the same objects. The harness objects are identical (by construction), which satisfies the intent (no real ffmpeg/whisper/network; per-run ini; offscreen); a literal copy-paste would have created a second drifting copy of 170+ LoC and blown the ~230 LoC estimate. No harness code was changed.
2. **`_grid_position` resolves the widget's own parent layout (M1, item 8).** Spec: "the helper … already resolves to the new parent — no helper change". Kept: `_grid_of` is byte-identical in body (only its docstring grew, as item 8 directs). Narrowly extended: `_grid_position` now looks the widget up in `widget.parentWidget().layout()` instead of always `_grid_of(win)` — required to assert the spec-mandated `transcribe_subs == (3, 3)` in the *upscale* grid, which the old signature could not express (it resolves only the Mode B grid). For the item-25 Mode B assertions the resolution is identical (both widgets share the Mode B parent), so every original assertion's semantics are preserved. Flagged in case Karen counts this as "helper change"; it is the minimal change that makes item 8's required additions executable.
3. **CHANGELOG block placement inside the section (M3, item 9).** Spec anchored "after line 37" of a pre-workstream-1 file state; in this tree the `[0.4.17.0]` section ends at line 33. The §6.6 blocks are appended inside the section (after `### Added`, before `### Notes`) rather than after the last line of the section — same version section, no retitle, keep-a-changelog group order.
4. **LoC estimates exceeded (transparently).** Spec estimates: file A ≈230, item 8 ±14, gui.py ≈ +84/−57. Actual: file A = 511 LoC; item 8 = +37/−12; gui.py = +97/−46. The excess is documentation (module/test docstrings and house-style comments — e.g. T1's walk must descend widget-layouts, a subtlety worth its own comment; item 8's mandated docstring/cell-reference rewording is more verbose than the ±14 guess). Where the spec gives *exact code* (§6.1/§6.2/§6.5) the shipped bytes are verbatim, so those items are LoC-exact by definition; content scope (T1-T10, item 8 assertions) is fully covered. No functional scope was added or dropped.
5. **T10b requires the `transcription_engine_on` fixture**, exactly as its `test_gui.py` original does (suite-wide autouse guard keeps transcription off). Not a deviation from §8 (the spec's T10 says "port the two test_gui.py F6 assertions" — the original carries this marker); recorded so nobody "removes" it thinking it's excess.

Nothing else deviates: items 1-4, 5, 6, 9, 10 verbatim; §6.3 strings exact; D2-D6 honored (no version file touched, no release artifacts cut, no protected module edited).