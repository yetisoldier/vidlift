# Karen Validation Report — v0.4.17 workstream-2: subtitle-surface UI restructure

**Validator:** Karen (Testing & Validation)
**Date:** 2026-11-04
**Checkpoint under review:** `17a7c10` (workstream-1 base `0269cb0`; Mode A byte-anchor `ac90825`)
**Inputs read in full:** Bob's spec `.specs/bob-vidlift-v0.4.17-ui-restructure-spec.md` (766 lines),
Forge's report `.specs/forge-vidlift-v0.4.17-ui-restructure-report.md` (125 lines),
`.specs/hermes-release-plan-v0.4.17-combined.md` (42 lines).
**Interpreter:** `/home/yetisoldier/.venvs/vidlift/bin/python` throughout (canonical).
**Method note:** every measurement below was taken live offscreen (`QT_QPA_PLATFORM=offscreen`,
`VIDLIFT_MONITOR_AUTOSTART=0`, isolated per-run QSettings ini) by Karen's own probe scripts —
not asserted from source, not trusted from Forge's report. Mutation tests were run in a scratch
copy of the repo; the working tree was verified byte-clean after all validation (`git status` empty,
`git diff` 0 lines).

---

## Executive Summary

**Overall Status: PASS (code cleared) — release NOT cleared (by design; gate list below).**

| Dimension | Verdict |
|---|---|
| **Code (workstream-2)** | **PASS** — all 10 §10 acceptance criteria met with live evidence; all 8 key gates (a–h) green; §8 test plan fully realized and mutation-proven |
| **Release (v0.4.17 combined)** | **NOT CLEARED** — pending: Quill docs pass, Eric's version confirmation, release mechanics, Windows-side pre-release checks |

**Overall Risk: Low.** **Confidence: High.**

The restructure does exactly what Bob's spec says: window minimum **1290×696 measured** (was
1532×595 at the base — I reproduced both numbers myself), "Transcribe missing subtitles" renders
real pixels for the first time since v0.4.14, "Resume retained work" paints **5451 px** (was
**0/7314** — reproduced at `0269cb0`), the two surfaces are structurally separated with exactly
their own controls, and the K-2 "Source file:" lines render in both postures with "Resolution
handling" gone from live output. The new fences are not decorative: I recreated all three defect
classes (never-rendered, v0.4.9 overlap, width bomb) in a scratch copy and the suite caught every
one. `vidlift/run.py` is byte-identical at both anchors; no version file moved; workstream-1's
items 26-29 pass unmodified.

---

## Findings table

| # | Severity | Finding | Evidence | Disposition |
|---|---|---|---|---|
| F-1 | **Low** (test-coverage gap, not a defect in shipped code) | No single test asserts the Mode B group contains **exactly** its three controls (criterion 4's "exactly"). T3 checks direction-specific membership; T4 pins the three cells; a future Mode A control accidentally parented into the Mode B group would pass T1-T10. | Live enumeration (Karen probe): Mode B group = exactly {Add Subtitles…, replace checkbox, explainer}; all 14 Mode A controls in Upscale — correct today. | Non-blocking. Recommend a one-line `subs_grid.count() == 3`-style exhaustiveness assertion in a future pass. |
| F-2 | Informational | T2's paint-truth probe is stacking-sensitive: an overlap where the later-added widget wins stacking yields nonzero paint deltas for both widgets. Verified: my first overlap mutation (coordinates only, new add-order kept) passed T2 while the overlap existed — T4 caught it. | Mutation A (coords-only): T2 passed, T4/T7 failed. Mutation A2 (faithful v0.4.9: add-order + coords): T2+T4+T7 all failed. | No action. T4 is the authoritative overlap fence; T2 the visibility fence. Both exist; class is covered. |
| F-3 | Informational (spec wording bug) | Bob §7's M3 gate as written ("`git diff ac90825..HEAD -- vidlift/run.py vidlift/subs_job.py vidlift/cli.py vidlift/mcp_bridge/` empty") is unsatisfiable: workstream-1's own committed changes to subs_job/cli/mcp_bridge are in HEAD by design. | `git diff ac90825..HEAD -- vidlift/subs_job.py` = 302 lines (workstream-1 content); `git diff 0269cb0..HEAD` for all protected modules = 0 lines. | Forge flagged it correctly; the invariant §2/§10-6 actually asserts (run.py vs ac90825; everything else vs the workstream-1 base) holds. No code action. |
| F-4 | Informational | PyQt6 returns a bogus `columnStretch(6) == 49` for an out-of-range column query on the 3-column Mode B grid. | Karen probe: `SUBS_GRID columns: 3 items: 3 col2stretch: 1`; window min 1290 proves no inflation from any hidden column. | Binding artifact; no layout effect. No action. |
| F-5 | Informational | `.karen/prefix-base/vidlift/gui.py` holds a stale pre-change gui.py copy (git-ignored scratch from a prior Karen run) containing the old "Resolution handling" render line. | `git check-ignore` → IGNORED; not tracked; not shipped. | No action; noted so nobody greps it by accident. |
| F-6 | **Low** (carried, pre-existing) | The manual Mode-A-replace smoke (workstream-1 §10-6) remains outstanding on this box (no engine available). | Carried unchanged from workstream-1 validation; `run.py` byte-identity + the green replace block (129 passed incl. `test_run.py`) carry it. | Carried to the release-gate list, same posture as workstream-1. |

**No Critical or High severity defects found.**

---

## Key gates (a–h) — verdicts

| Gate | Verdict | Headline evidence (all measured live by Karen) |
|---|---|---|
| **(a) K-1** — window min width ≤1360 (~1290), measured offscreen | **PASS** | `minimumSizeHint()` = **1290×696** at `17a7c10`; **1532×595** reproduced at `0269cb0` (scratch-copy baseline). +242px regression fully retired; width = exact v0.4.16.2 parity. Height 696 ≤ 725. |
| **(b) Finding 1** — transcribe checkbox rendered, visible, default-ON, controllable, persisted | **PASS** | Laid out (recursive layout walk); paint delta **6233/6233 px** (indicator zone 920 + text zone 5129 — genuine painting, not layout-shift artifact); fresh settings → `isChecked() is True`; toggle persists under `transcribe_subs` both directions and fresh windows restore both ways; reaches plan (T10a spy, both directions) and start options (T9). Baseline at `0269cb0`: **not laid out at all** (no parent widget). |
| **(c) resume_claim overlap** — paints >0 px (was 0/7314 since v0.4.9) | **PASS** | Paint delta **5451/5451 px** at HEAD (indicator 920 + text 4347); **0/7314 reproduced at `0269cb0`**. Monitor still paints fully (10971/10971). Zero-overlap check green on both grids. |
| **(d) Surfaces truly separated** | **PASS** | Mode B group contains **exactly** 3 items: Add Subtitles… (0,0), replace checkbox (0,1), scope explainer (0,2, `#Muted`, wordwrap). All 14 Mode A controls in the Upscale group; zero cross-membership. Independent budgets proven: hiding any Mode B/touched control leaves window min at 1290×696. |
| **(e) K-2** — "Resolution handling" gone; "Source file:" in both postures | **PASS** | Live enqueue→select flow: ON → `Source file: replaced on success — output renamed to <name>.mp4`; OFF → `Source file: kept — output beside it as <name>.subs.mp4`; `"Resolution handling" not in detail` in both. Repo grep: the string survives only in the spec'd historical comment (gui.py:2099), test assertions, and CHANGELOG prose — not in any render path. Column-3 literals frozen at :1609/:1611 (test 28 green). |
| **(f) Protected modules byte-identical** | **PASS** | `git diff ac90825..HEAD -- vidlift/run.py` = **0 lines**; vs `0269cb0` also 0. All other protected modules (`subs_job.py`, `cli.py`, `mcp_bridge/*`, `plan.py`, `transcribe.py`, `subtitles.py`, `monitor.py`) = **0 lines vs `0269cb0`** (the correct anchor — they carry workstream-1's own committed changes vs `ac90825`, per F-3). |
| **(g) Version untouched** | **PASS** | `git diff 0269cb0..HEAD` on `vidlift/__init__.py`, `pyproject.toml`, `packaging/version_info.txt` = **0 lines**; all six stamps at **0.4.17.0** (both StringStructs + both tuples verified); `test_version_stamps.py` **7/7 green, file unmodified** (0-line diff). |
| **(h) Workstream-1 intact** | **PASS** | Items 26-29 (5 tests: persistence, key isolation, enqueue threading, column-3, cross-wire fence) **green unmodified** — the file's only diff hunk starts at :879 (helpers + item-25). Workstream-1 file A (25 tests) + `test_run.py` = **129 passed, unmodified**. Full suite **1001 passed, 2 skipped** (skips pre-existing, Windows-only GPU-meter). |

---

## §10 acceptance criteria — per-criterion evidence

### 1. K-1 resolved — PASS

- **Measured (Karen, offscreen, `minimumSizeHint`):** window minimum **1290×696** at `17a7c10`.
- **Baseline reproduced (Karen, scratch copy at `0269cb0`'s gui.py):** **1532×595** — Karen's
  original K-1 measurement, exactly. Δ = 242 = the 352px checkbox vacating column 8 (max
  reverting to the 110px Start button). The regression is fully retired; 1290 is exact
  v0.4.16.2 width parity.
- **Per-widget differential (Karen probe):** hiding `subs_replace_source`, `transcribe_subs`,
  `resume_claim`, or `monitor_check` individually leaves the window minimum at 1290×696 —
  no single control inflates the window; the Mode B group's budget is structurally
  decoupled (D1's load-bearing claim verified empirically).
- **Gate arithmetic:** 1290 ≤ 1360 ✓; height 696 ≤ 725 ✓; fits a 1366×768 laptop client
  (~728px) on both axes.
- **Fence mutation-proofed:** a 2000px `setMinimumWidth` bomb on the Mode B explainer makes
  `test_window_minimum_size_gates` (T5) **FAIL** — the K-1 class stays machine-dead (see §8).

### 2. Finding 1 resolved — PASS

- **Rendered:** recursive layout walk from `centralWidget().layout()` (descending both
  layout items and widget-layouts) finds `transcribe_subs` at upscale grid **(3,3,1,3)**.
  At `0269cb0` the same probe reports it laid out in **no** layout (and it has no parent
  widget at all — `parentWidget()` is None).
- **Visible (paint truth, Karen probe):** hide-and-grab delta **6233/6233 px**; the changed
  pixels include the indicator zone (920 px, first 40px of the rect) and the text zone
  (5129 px) — genuine painting, not a layout-shift artifact.
- **Default-ON, fresh settings:** `isChecked() is True` on a fresh isolated ini.
- **Controllable + persisted:** toggle OFF → ini records `transcribe_subs=false`; fresh
  window restores OFF; toggle ON → records true; fresh window restores ON (both directions,
  Karen probe + T6). Mode B's key `subs_replace_source` never written by these toggles.
- **Reaches the machinery:** T10a (build-batch-plan spy) threads `transcription_enabled`
  False/True both ways; T10b (render override) shows the honest disabled note when
  unchecked and restores the plan note when re-checked; T9 pins the `start_upscale` options
  dict read (`"transcription_enabled": self.transcribe_subs.isChecked()`).
- **Label/tooltip:** label is the §6.3 short string; tooltip verbatim (carries
  "no subtitle track" + "~1.5 GB model" + minutes-per-file cost — asserted case-insensitively
  and read back by Karen).

### 3. Finding-1 twin resolved (resume/monitor) — PASS

- **resume_claim paints:** delta **5451/5451 px** at HEAD (indicator 920 + text 4347).
  **0/7314 reproduced at `0269cb0`** by Karen — the "invisible and unclickable since
  v0.4.9" claim is confirmed, and its fix is confirmed.
- **monitor paints fully:** delta **10971/10971 px** (its full rect; span (2,0,1,4)).
- **General no-overlap:** pairwise intersection check over every item of BOTH grids passes
  (T4); Karen's independent cell enumeration shows row 2 = monitor (2,0,1,4), profile label
  (2,5), profile combo (2,6), resume (2,7,1,2) — no intersections.
- **Mutation-proofed:** faithfully recreating the v0.4.9 defect (resume added to the layout
  before monitor, at (2,1,1,2) under monitor's (2,0,1,5) span) makes **T2 + T4 + T7 all
  fail**. Note (F-2): coordinates-only recreation (keeping the new add-order, so resume wins
  stacking) is caught by T4/T7 while T2 still passes — the fences complement each other.

### 4. Surfaces separated (Finding 2) — PASS

- **Mode B group contains exactly** its three controls (Karen enumeration): `Add Subtitles…`
  (0,0), `Replace original on success (deletes source)` (0,1), scope explainer (0,2,
  objectName `Muted`, wordwrap on, `setColumnStretch(2, 1)`). Nothing else.
- **No cross-membership:** all 14 Mode A attribute controls' parent is the Upscale group;
  neither Mode B control is in it; `subs_replace_source.parentWidget() is not
  replace_source.parentWidget()`.
- **Titles:** `findChildren(QGroupBox)` = exactly `Upscale` + `Add subtitles (no upscale)`,
  both objectName `Controls` (the QSS chrome matches both).
- **Tooltip scope claims literally true (Karen read-back):** Mode B tooltip ends "…Applies
  to Add Subtitles only — the upscale panel has its own replace option." — structurally
  true (replace_source lives in the Upscale group). `add_subs_button` tooltip names "the
  checkbox beside this button" — literally true ((0,0)/(0,1), same row, adjacent column).
  Mode A's tooltip ("After a verified upscale…") sits in the Upscale group — true.
- **Coverage note (F-1):** the "exactly" is proven by Karen's live enumeration + T4's cell
  pins; no single test asserts group-item-count exhaustiveness. Non-blocking.

### 5. K-2 resolved — PASS

- **Live flow (Karen, scripted client, enqueue→select):** checkbox ON → column 3 renders
  `replace original → .mp4`, state-at-enqueue True, detail contains
  `Source file: replaced on success — output renamed to <name>.mp4`; checkbox OFF →
  column 3 `keep original`, state-at-enqueue False, detail contains
  `Source file: kept — output beside it as <name>.subs.mp4`. `"Resolution handling"`
  appears in **neither** posture.
- **All three renderer arms (T8 unit calls):** replace → replaced-line; keep → kept-line;
  unknown value (`720p`) → honest passthrough `Source file: 720p` (never a crash).
- **Repo grep:** "Resolution handling" survives only in the spec'd historical comment
  (`gui.py:2099`), the new tests' forbidden-string assertions, and CHANGELOG prose. Zero
  render-path occurrences.
- **Column-3 row strings unchanged:** literals at `_subs_file_rows` :1609/:1611 byte-frozen;
  workstream-1 test 28 (`test_subs_file_rows_column3_tracks_submitted_state`) green
  unmodified.

### 6. Mode A intact — PASS

- `git diff ac90825..HEAD -- vidlift/run.py` = **0 lines**; vs `0269cb0` = **0 lines**.
- `tests/test_run.py` green **unmodified** (0-line diff; 129 passed together with
  workstream-1 file A).
- Options-dict fences (T9) green: `start_upscale` still reads
  `transcription_enabled`/`replace_source`/`resume` from the Mode A widgets and never
  `subs_replace_source`; `choose_subs_files` still reads `subs_replace_source` and never
  Mode A's `replace_source`.
- gui.py diff = exactly 7 hunks (import block, transcribe creation, subs_replace creation,
  `_build_shell` ×2, QSS, detail renderer) — none touch `start_upscale`, handlers,
  `_subs_file_rows`, or any QSettings call. QSettings key literal sets old-vs-new are
  **identical** (11 literals each).
- Manual Mode-A-replace smoke: still outstanding on this box (no engine) — carried (F-6),
  same posture as workstream-1 §10-6.

### 7. Mode B intact — PASS

- Items 26-29 (persistence, key isolation, enqueue threading, column-3, cross-wire fence):
  **5/5 green, zero edits** — the file's only diff hunk begins at :879 (helpers + item-25).
- Live pick flow (Karen): state-at-enqueue == checkbox in both directions (see criterion 5
  evidence — the scripted client records `last_subs_replace_source` True/False matching the
  checkbox exactly).
- Workstream-1 file A (`test_v0417_subs_replace_source.py`, 25 tests) green unmodified.

### 8. Window chrome — PASS

- Min height **696 ≤ 725** measured.
- Both groups render titled, non-clipped frames: group grabs are non-blank (Upscale
  1318×218, 336 distinct sampled colors; Mode B 1318×77, 134 distinct colors).
- Fits 1366×768 in normal state: min 1290×696; resize probes at 1290×700 and 1366×768 keep
  every key control visible.

### 9. Settings integrity — PASS

- **No new/renamed QSettings keys:** key-literal sets identical old vs new (11 each);
  Karen's post-exercise ini census shows only keys actually toggled.
- The four pre-existing keys behave per workstream-1 criterion 7: `transcribe_subs`
  (default True, both-direction restore — criterion 2), `subs_replace_source` (default
  False, item-25/26/27 green), `replace_source`, `resume_claim` (handlers byte-preserved;
  resume restore exercised by its own green tests).
- Cross-process restore proof stands (fresh `MainWindow` instances re-reading the synced
  ini — the item-26/T6 pattern).

### 10. Hygiene — PASS

- `test_version_stamps.py` **7/7 green, unmodified**; version stays **0.4.17.0** (D6):
  `__init__.py:9`, `pyproject.toml:3`, `packaging/version_info.txt` FileVersion +
  ProductVersion strings and `filevers`/`prodvers` tuples all verified.
- CHANGELOG `[0.4.17.0]` carries §6.6 verbatim (Changed + Fixed blocks, inside the section,
  keep-a-changelog order, no retitle).
- Docs sweep verified by Karen: no user-facing doc content requires changes (`.subs.mp4`
  appears only in code/tests; `docs/` + `README.md` have a 0-line diff this workstream);
  the historical design record `docs/designs/v0.4.9-resume-and-observability.md` untouched
  as instructed (its "run panel" wording note passed to Quill).
- Suite total: **1001 passed, 2 skipped** (venv interpreter, 106.5s) — the spec's ≈1000/2
  (990 + 11 new); both skips pre-existing Windows-only GPU-meter tests, untouched by this
  workstream.
- Windows CI safety: `.github/workflows/build-windows-installer.yml:100` enumerates only
  `tests/test_windows_packaging.py tests/test_gui.py` — the px gates live in
  `test_v0417_ui_restructure.py`, which CI does not enumerate (§0.2 requirement honored).

---

## §8 test plan — validation of the fences themselves

### File A (`tests/test_v0417_ui_restructure.py`, 512 LoC, 11 tests) — all T-groups present and audited

| T-group | Present | Karen's audit |
|---|---|---|
| T1 never-rendered fence | ✓ | Recursive walk descends BOTH layout items and widget-layouts (required — the group grids are widget layouts); asserts all 16 attribute controls + the exact §6.3 explainer. **Mutation-proven:** removing the transcribe `addWidget` → T1 fails (with T2/T3/T4). |
| T2 paint truth | ✓ | Hide-and-grab per widget with a rasterization `skip` guard. **Mutation-proven:** faithful v0.4.9 recreation → T2 fails (resume back to 0 px). Stacking-sensitivity documented as F-2. |
| T3 group separation | ✓ | Parent identities both directions + both titles among `findChildren(QGroupBox)`. |
| T4 cell map + zero-overlap | ✓ | All six position pins per §3 + the general pairwise intersection check over BOTH grids. **Mutation-proven:** both overlap variants (coords-only and faithful) → T4 fails. |
| T5 width/height gates | ✓ | ≤1360/≤725 with measured-provenance comment; lives outside CI's enumerated files. **Mutation-proven:** 2000px Mode B width bomb → T5 fails. |
| T6 transcribe visibility + persistence | ✓ | Default-ON, both-direction persist/restore, §6.3 label, tooltip honesty text, Mode B key isolation. Independently reproduced by Karen's probe. |
| T7 resume row-2 contract | ✓ | Upscale-group parent + (2,7,1,2) pin; renderability delegated to T2 exactly as §8 specifies. **Mutation-proven** (fails on overlap recreation). |
| T8 K-2 detail strings | ✓ | Enqueue→select both postures + `Resolution handling` forbidden + all three unit arms (replace/keep/unknown passthrough). Independently reproduced live. |
| T9 wiring fences | ✓ | inspect-pinned read sites, both flows, cross-wire forbidden. |
| T10 plan/render threading | ✓ | Both F6 halves ported; T10b carries the `transcription_engine_on` fixture exactly like its `test_gui.py` original (Forge deviation 5 — correct). |

### File B (`tests/test_v0414_mode_b_gui.py` item-25 edit) — per spec

Placement → Mode B grid (0,0)/(0,1); label → the D3 string; tooltip assertions unchanged
(workstream-1 §6.3 verbatim, incl. the `35 Hours.avi` naming rule); group-separation
assertions added incl. transcribe at upscale (3,3). Items 26-29: zero edits (single diff
hunk at :879), 5/5 green.

### Unmodified-pass invariants — all honored

Zero-line diffs vs `0269cb0` and green for every §8-named file: `test_run.py`,
`test_v0417_subs_replace_source.py` (25), `test_v0417_failpath_fix.py`,
`test_v0414_subs_job.py`, `test_v0414_mode_b_gui_executor.py`,
`test_v04151_subs_guard.py`, `test_v04153_subs_bitmap_remux_slow.py`, `test_cli_v049.py`,
`test_mcp_bridge.py`, `test_client_local.py`, `test_version_stamps.py` (7/7),
`test_gui.py` (incl. the :517 verb-order test and both F6 transcribe tests).

### Mutation-test matrix (Karen, scratch copy — tree never touched)

| Mutation | Defect class | Fences that failed |
|---|---|---|
| Remove `grid.addWidget(self.transcribe_subs, 3, 3, 1, 3)` | Finding 1 (never-rendered) | **T1, T2, T3, T4** |
| v0.4.9 overlap, faithful (resume added before monitor, (2,1,1,2) under (2,0,1,5)) | Finding-1 twin (painted-over) | **T2, T4, T7** |
| v0.4.9 overlap, coords-only (new add-order kept — resume wins stacking) | geometric overlap | **T4, T7** (T2 passes — F-2) |
| `explainer.setMinimumWidth(2000)` | K-1 (width inflation via Mode B) | **T5** |
| (control, unmutated copy) | — | 11/11 pass |

All mutations reverted; control run green; real tree verified byte-clean afterward.

### §10 edge cases probed (beyond §8)

- **Legacy config quirk:** stored `denoise/mode=bogus-legacy` + new layout → window
  constructs, combo reads `Off`, min stays 1290. PASS.
- **Resize just above minima:** 1290×700 and 1366×768 — all key controls visible. PASS.
- **Group title rendering:** both groups render non-blank titled frames offscreen (the
  Windows native-style check remains a release-gate item — §6.5 fallback exists with Bob
  sign-off).
- **Long-label headroom:** asserted via T5's ceiling slack (1290 + 70px slack to 1360);
  no translation surface exists today.
- **Detail compare-guard** (`_set_details_text` :2068 region): unchanged by the diff
  (7-hunk audit) and exercised green by the live-overlay tests.

---

## Forge's deviations — Karen assessment

| # | Deviation | Assessment |
|---|---|---|
| 1 | Harness imported, not copied | **Accept.** House pattern (`test_v04151_subs_guard.py:61` precedent); harness objects byte-identical by construction; spec's intent (offscreen, isolated ini, no ffmpeg/whisper/network) verified live. A literal copy would drift. |
| 2 | `_grid_position` resolves the widget's own parent layout | **Accept.** Required to express the spec-mandated upscale (3,3) assertion — the old signature could only resolve the Mode B grid. `_grid_of` body byte-identical; used only by item-25's three assertions (items 26-29 don't call it); Mode B semantics preserved. Transparently flagged. |
| 3 | CHANGELOG placement inside the section | **Accept.** The spec's "after line 37" anchor referred to a pre-workstream-1 file state; the section-internal intent (same version section, no retitle, keep-a-changelog order) is met — verified in the diff. |
| 4 | LoC overage (file A 512 vs ~230; item 8 +37/−12; gui.py +97/−46) | **Accept.** Documentation-only; where the spec gives exact code (§6.1/§6.2/§6.5) Karen mechanically verified **0 missing lines** (byte-verbatim). No functional scope added or dropped. |
| 5 | T10b needs the `transcription_engine_on` fixture | **Accept.** Same marker as its `test_gui.py` original; the suite-wide autouse guard requires it. Correctly recorded. |

---

## Test plan and results (Karen's executed plan)

| # | Test | Result |
|---|---|---|
| 1 | Full suite, canonical venv (`python -m pytest tests/ -q`) | **1001 passed, 2 skipped** (106.5s) |
| 2 | `test_version_stamps.py` alone, unmodified | 7/7 green |
| 3 | `test_v0417_ui_restructure.py` alone | 11/11 green |
| 4 | Items 26-29 (5 tests) explicitly | 5/5 green |
| 5 | Workstream-1 file A + `test_run.py` | 129 passed |
| 6 | Skip identification | 2 pre-existing Windows-only GPU-meter skips |
| 7 | K-1 live measurement (HEAD) | 1290×696 |
| 8 | K-1 baseline reproduction (`0269cb0` gui.py, scratch copy) | 1532×595 |
| 9 | Paint-truth probes, 6 widgets (HEAD) | all >0 px; resume 5451/5451; transcribe 6233/6233; monitor 10971/10971 |
| 10 | Paint-truth baseline (`0269cb0`) | resume **0/7314**; transcribe not laid out |
| 11 | Group-membership exactness enumeration | Mode B exactly 3; all Mode A in Upscale |
| 12 | K-2 live flow both postures + forbidden string | PASS |
| 13 | Persistence both directions + key census | PASS; key sets identical (11=11) |
| 14 | Per-widget window-min differential (4 widgets) | no inflation |
| 15 | Mutation matrix (4 mutations + control) | all caught; control green |
| 16 | §6.1/§6.2/§6.5 byte-verbatim mechanical check | 0 missing lines |
| 17 | Protected-module + version-file diffs (3 anchors) | all 0 lines |
| 18 | Docs sweep + Windows CI enumeration + CHANGELOG audit | PASS |
| 19 | Edge cases (legacy denoise, resize, grabs) | PASS |
| 20 | Working-tree cleanliness after validation | `git status` empty |

---

## Release recommendation

**Code is cleared. Release is NOT cleared — by design, not by defect.**

### Verdicts separated

- **Code (workstream-2, checkpoint `17a7c10`): PASS.** All 10 §10 criteria met with live,
  independently reproduced evidence; all key gates (a–h) green; the fences are
  mutation-proven load-bearing; protected surfaces byte-identical; version untouched.
  No Critical or High defects. The two Low findings (F-1 coverage nicety, F-6 carried
  smoke) do not block.
- **Release (v0.4.17 combined): NOT CLEARED** — the release plan itself forbids cutting
  before Karen + Quill, and the following gate items remain open.

### Remaining release-gate items

1. **Quill docs pass (mandatory, release-plan step 5)** covering BOTH workstreams —
   including: the v0.4.9 historical design record's "run panel" wording now describes a
   checkbox living in the Upscale group's row 2 (do-not-edit record; phrase the release
   note as "fixed a layout overlap that hid the Resume retained work checkbox" per Bob D5);
   K-6's `vidlift subs` docs gap; the restructured settings surface.
2. **Version confirmation:** Bob decided **0.4.17.0** (D6, with recorded future-revisit
   criteria). Eric should confirm he does not overrule on marketing grounds before the
   release is cut; if overruled, the D6 stamp playbook (four version_info values +
   `__init__.py` + `pyproject.toml` + CHANGELOG retitle + gate re-run) is recorded in the
   spec, not executed.
3. **Release mechanics (release-plan step 6):** single combined release — commit, push to
   GitHub AND Gitea, cut the release, hash-verify the published artifact. Explicitly NOT
   done by Forge (correct — forbidden until Karen + Quill).
4. **Windows-side pre-release checks (carried from §9/§10):** on the real Windows installer
   build — (a) native-style `QGroupBox::title` rendering (the §6.5 pre-approved fallback
   exists, Bob sign-off required if invoked); (b) re-measure window minimum width/height
   under Windows font metrics (T5's ceilings carry slack, but §9 row 1 asks for the real
   build measurement before release).
5. **Carried smoke (F-6):** the manual Mode-A-replace smoke from workstream-1 §10-6 remains
   outstanding on this box (no engine); `run.py` byte-identity + the green replace block
   carry it, unchanged posture.

**Release recommendation: Ready for Production pending the gate list above** — the code
itself requires no further fixes; the remaining items are process gates (docs, version
sign-off, release mechanics, Windows build checks), exactly as the combined release plan
sequences them.

— Karen
