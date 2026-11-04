# VidLift v0.4.17 Implementation Spec — workstream 2: subtitle-surface UI restructure

**Author:** Bob (Principal Software Architect)
**Date:** 2026-11-04
**Source docs (read in full):** `.specs/hermes-findings-v0.4.17-subtitle-ux.md` (Findings 1+2),
`.specs/karen-vidlift-v0.4.17-validation-report.md` (K-1, K-2),
`.specs/hermes-release-plan-v0.4.17-combined.md` (combined release; workstream-1 checkpoint
`0269cb0` is the base).
**Audience:** Forge (implementation), Karen (validation baseline in §10).
**Scope:** GUI-only restructure of the main-window settings surface. `vidlift/run.py` stays
**byte-identical** (§10 verifies the diff gate). Mode A *surface* code in `gui.py` is deliberately
touched (regroup + render + de-overlap) — every touch is enumerated and flagged for Eric in §2,
per the release-plan invariant.

---

## 0. Evidence verification

Every claim below was re-measured live against the working tree (checkpoint `0269cb0`),
canonical interpreter `/home/yetisoldier/.venvs/vidlift/bin/python`,
`QT_QPA_PLATFORM=offscreen`, `VIDLIFT_MONITOR_AUTOSTART=0`.

### 0.1 Measurements (reproducing K-1, and new findings)

| Widget / cell | sizeHint width | Role |
|---|---|---|
| `subs_replace_source` (label "Replace original after adding subtitles (deletes the source)") | **352px** | K-1 cause: sole occupant driving column 8 |
| `start_button` / `cancel_batch_button` (column 8's other occupants) | 110 / 103px | pre-K-1 column-8 max |
| `transcribe_subs` (original label "Add subtitles by transcription when none exist") | 288px | Finding 1: constructed, never laid out |
| `transcribe_subs` (short label "Transcribe missing subtitles") | 184px | chosen replacement label |
| Mode B short label "Replace original on success (deletes source)" | 274px | chosen replacement label |
| `monitor_check` | 419px hint | spans row 2, cols 0–4 (span 5) |
| `resume_claim` | 153px hint | spans row 2, cols 1–2 — **inside** monitor's span |

- **K-1 math (measured):** window min-width 1532; hiding only the checkbox restores 1290;
  Δ = 242 = 352 − 110 (column-8 max swaps from the button to the checkbox). Matches Karen.
- **Column budget table (measured, non-spanning column minima):** col 0: 87 · col 1: 228
  (engine combo) · col 2: 78 · col 3: 48 ("GB/hour") · col 4: 90 (`gbph` fixed width) ·
  col 5: 109 ("Output Folder") · col 6: 124 (`output_dir` min; stretch column) ·
  col 7: 115 · col 8: 110 (352 with the checkbox). Spacing 12px; controls margins 18px side.
  A spanning widget inflates the window **only if its hint exceeds the sum of the minima of the
  columns it spans plus inner spacing** (verified empirically against every existing span:
  subtitle_ocr 137 ≤ 78+48+124+24; replace_source 326 ≤ 109+124+12... geometry dump in §0.3).
- **NEW FINDING (Finding-1 twin, measured):** the pre-existing row-2 overlap
  (`monitor_check` (2,0,1,5) over `resume_claim` (2,1,1,2), present since v0.4.9 and confirmed
  at `ac90825:gui.py:1310/1316`) is not cosmetic-neutral: **`resume_claim` paints 0 of 7314
  pixels** (hide/show pixel-diff over its rect). It is covered because Qt's stacking order
  follows *creation* order (monitor at `gui.py:524`, index 17; resume at 624, index 14), so the
  later-created monitor paints over the earlier-created resume. "Resume retained work" has been
  **invisible and unclickable in production since v0.4.9**; it "works" in the suite only
  because tests drive `setChecked()` programmatically. This is the identical defect class as
  Finding 1 (a control the user can never see or operate) and it sits inside the rows this
  workstream must re-lay anyway — fixing it here is the same finding's fix, not scope creep.
- **Winning layout variant (prototyped live, surgery-equivalent to §6.1)**: min **1290×~700**.
  Variants measured: transcribe at (3,3,1,3) short label → **1290** (exact v0.4.16.2 parity);
  same cell with the original 288px label → 1306 (+16); transcribe at (2,7,1,2) → 1340 (+50);
  Mode B label shortening buys **zero** window min-width once it lives in its own container
  (measured identical 1306 both label lengths) — it is kept anyway as defense-in-depth per K-1's
  own recommendation. Height 686 + ~14 for the upscale group's title band ≈ **700**
  (current 595; 1366×768 laptop client ≈ 728).
- **Paint-truth probe on the final variant:** transcribe 6601 px delta, resume 5451 px
  (was 0), monitor 11339 px, Mode B checkbox 2326 px — every touched control genuinely renders.

### 0.2 Source anchors verified in this tree

- `vidlift/gui.py:26-49` — QtWidgets import block (no `QGroupBox` import).
- `vidlift/gui.py:524-526` — `monitor_check` creation (creation-index evidence above).
- `vidlift/gui.py:597-602` — `subtitle_ocr` checkbox (Mode A carry policy; toggled → `_refresh_details`).
- `vidlift/gui.py:605-618` — `replace_source` (Mode A) — label/tooltip/handlers stay verbatim.
- `vidlift/gui.py:624-637` — `resume_claim` creation.
- `vidlift/gui.py:639-657` — `transcribe_subs` creation (Finding 1); default `True` from
  QSettings key `"transcribe_subs"`; tooltip carries the 1.5 GB / minutes-per-file honesty text.
- `vidlift/gui.py:743-757` — `add_subs_button` + v0.4.17 tooltip (names "the checkbox beside
  this button" — stays literally true in the new group).
- `vidlift/gui.py:758-781` — `subs_replace_source` creation; tooltip = workstream-1 §6.3
  verbatim (ends "…the upscale panel has its own replace option." — stays true structurally).
- `vidlift/gui.py:1209-1231` — `_replace_source_changed` / `_subs_replace_source_changed` /
  `_resume_claim_changed` / `_transcribe_subs_changed` handlers (QSettings house rule; all
  unchanged by this spec).
- `vidlift/gui.py:1310-1377` — the `controls` QFrame + mono-grid block being replaced.
  Cell occupancy (pre-change): row 0 = add/folder/clear/GB·hour/gbph/output/output_dir/plan/start;
  row 1 = engine label+combo / subtitle_ocr(1,2,1,3) / replace_source(1,5,1,2) / cancel(1,7) /
  cancel-batch(1,8); row 2 = monitor(2,0,1,5) + resume(2,1,1,2) [overlap, §0.1] +
  profile label(2,5)/combo(2,6) + add_subs(2,7) + subs_replace(2,8); row 3 = denoise
  label(3,1)/combo(3,2); `setColumnStretch(6, 1)`.
- `vidlift/gui.py:1438-1469` — `_apply_style`; `#Controls {…}` frame chrome at :1449
  (objectName selector — matches both new groups; no other test/code queries objectName "Controls").
- `vidlift/gui.py:1544-1589` — `_subs_file_rows` (column-3 literals `"keep original"` /
  `"replace original → .mp4"` at :1574-1577 — **frozen**: pinned by workstream-1 test 28).
- `vidlift/gui.py:1628-1761` — `choose_subs_files` (enqueue reads `subs_replace_source` at :1689).
- `vidlift/gui.py:2040-2066` — `_subs_detail_text`; the K-2 line is `:2062-2063`:
  `if target and target != "keep original": lines.append(f"Resolution handling: {target}")`.
  `"Resolution handling"` occurs nowhere else in the repo (grep-verified; zero tests pin it).
- `vidlift/gui.py:1814` (`plan_batch`), `:1846` (`render_batch` override), `:2228`
  (`start_upscale` options) — the three `transcribe_subs` read sites (all Mode A, all unchanged;
  the toggle's *wiring* is correct today — only its *rendering* was missing).
- Version stamps: `vidlift/__init__.py:9`, `pyproject.toml:3`, `packaging/version_info.txt`
  (both StringStructs + both tuples) all carry `0.4.17.0`; gate `tests/test_version_stamps.py`
  (7 tests) is green. CHANGELOG `[0.4.17.0] - 2026-11-04` section exists at `CHANGELOG.md:5-37`.
- Windows CI (`.github/workflows/build-windows-installer.yml:99-100`) runs offscreen only
  `tests/test_windows_packaging.py tests/test_gui.py` — the new pixel/size gates (§8 file A)
  must therefore NOT live in `test_gui.py` (font-metric fragility on Windows); they go in a new
  file that CI does not enumerate.
- Existing tests that interact with the settings grid: `tests/test_gui.py:517-535`
  (`test_polish_helper_line_and_verb_order` — uses `plan_button.parentWidget().layout()`, needs
  only plan < start columns + cancel present → survives); `tests/test_gui.py:1022-1053,
  1212-1233` (transcribe toggling; no placement assertions → survive);
  `tests/test_v0414_mode_b_gui.py:880-923` (`_grid_of`/`_grid_position` helper + item-25
  placement/label/tooltip assertions → **edited by this spec**, §8-file-B);
  `:926-1076` (items 26-29: persistence, key isolation, enqueue threading, column-3, cross-wire
  fence → all layout-independent → survive **unmodified**).
- Full suite at checkpoint: **990 passed, 2 skipped** (Karen, §Test plan and results).

---

## 1. Decisions

### D1 — Separation is *layout-structural*: two titled `QGroupBox` containers, each its own `QGridLayout`

**Decision.** Split the single `controls` QFrame/mono-grid into two containers stacked
vertically in the existing root VBox:

1. `QGroupBox("Upscale")` — the Mode A surface: everything the upscale batch consumes
   (file/output controls, engine, profile, denoise, GB/hour, replace-original, resume, the
   subtitle *carry* policy: OCR + transcribe-when-missing, cancel buttons).
2. `QGroupBox("Add subtitles (no upscale)")` — the Mode B surface: the `Add Subtitles…`
   button, its own replace checkbox, and a one-line scope explainer.

**Rationale (this is the load-bearing decision for K-1).** Hermes's direction asks that "each
surface shows only its own controls." A merely *visual* separation (header labels inside one
shared grid) was modeled and rejected: in a single `QGridLayout`, **column minima couple across
sections** — any wide Mode B control lands in a column shared with upscale controls and
re-inflates the window (exactly the K-1 mechanism: a 352px checkbox at (2,8) taxed column 8,
which Start/Cancel-Batch shared). Two grids = two independent column budgets; a future Mode B
control (e.g. model/language combos, Hermes's "its own model/language controls" growth slot)
can never again tax the upscale surface's width. Measured: the variant restores **1290px min
width — exact v0.4.16.2 parity**.

**Alternatives considered.**

- (b) `QTabWidget` (Upscale / Subtitles tabs): rejected — hides a primary action Eric uses
  behind a tab; the shared file-table/run-panel would visually belong to both tabs
  (ambiguity for which mode a running job is); large churn to every layout-touching test.
- (c) Shared grid + full-width section-header rows: rejected — visual-only (column coupling
  survives, K-1 class stays open); measured +27px over baseline even with maximal in-budget
  placement gymnastics; header rows cost nearly the same height as group titles anyway.
- (d) Modal/secondary dialog for Mode B options: rejected — adds a click to a one-action flow,
  moves a persisted preference into dialog scope, and Hermes explicitly said "panel."

**Height cost.** +~105px min height (686 measured, 700 with the second title band) — fits the
1366×768 constraint (≈728px client); width, not height, was K-1's fatal axis because window
*width* cannot exceed the display even maximized. Karen re-verifies both axes (§10).

**Consequence / test impact.** Parent containers change → exactly one existing test edits
(item-25 placement/label assertions, §8-file-B); persistence/behavior tests are untouched.
No QSettings key moves; tab order is creation-ordered (unchanged).

### D2 — Finding 1: RENDER `transcribe_subs` at upscale-grid (3,3,1,3), with a shorter label

**Decision.** The checkbox is added to a layout **for the first time since v0.4.14**: upscale
group, row 3 (the Mode-A processing-options row, beside denoise), spanning columns 3–5.
Label shortened `"Add subtitles by transcription when none exist"` (288px) →
`"Transcribe missing subtitles"` (184px). Tooltip **verbatim unchanged** (it carries the full
semantics: no-track condition, 1.5 GB model, minutes-per-file cost, sync-from-own-audio).
QSettings key `"transcribe_subs"` and its `True` default **unchanged** — no migration,
existing users' stored preference (if anyone toggled it blind via CLI-era settings) is honored.

**Rationale.** Placement budget: cols 3+4+5 = 48+90+109+24 = **271px** ≥ 184 → **zero**
inflation (measured: 1290). The original 288px label *also* fits-ish (measured +16 → 1306) but
costs 16px for a qualifier the tooltip already states; the short verb-first label matches the
sibling pattern (`"Enable subtitle OCR"` 137px + rich tooltip). Row-3 vs row-2 (2,7,1,2) was
measured: 1306/1290 vs 1340 — row 3 wins by 50px because columns 7–8 are the 115/110px
button columns and a 184px span over 237px of budget is tighter than 271px. Rejected
alternative: putting it beside `subtitle_ocr` on row 1 (semantically adjacent) — row 1 has no
cells left at its current budgets.

**The always-on default is now visible/controllable:** fresh settings render the checkbox
checked (default `True`), in the upscale group, two rows above the fold; the toggle already
reaches `plan_batch` (:1814), `render_batch` (:1846), and `start_upscale` (:2228) today —
no behavior wiring changes, only rendering.

### D3 — K-1: Mode B replace checkbox shortened + relocated (both halves of Karen's recommendation)

**Decision.** Label `"Replace original after adding subtitles (deletes the source)"` (352px) →
`"Replace original on success (deletes source)"` (274px); relocated from shared-grid (2,8)
into the Mode B group at (0,1), immediately right of the button at (0,0). Tooltip **verbatim**
(workstream-1 §6.3 strings survive — including the D6 naming rule, the keep-both promise, and
the "…upscale panel has its own replace option" scope sentence, which is now *structurally*
true). `add_subs_button` text/tooltip unchanged ("the checkbox beside this button" remains
literally beside it).

**Rationale.** Measured: once in its own container the label length no longer moves window min
width at all — but K-1's recommendation was "shorter label + tooltip, **or** move the control,"
and doing both is defense-in-depth: if any future layout ever re-shares columns, the 274px
label is survivable (78px cheaper). The new label keeps the two load-bearing qualifiers —
**on success** (only after a verified pass) and **deletes** (irreversibility) — and matches
Mode A's label prefix `"Replace original on success (…"` for muscle-memory parity. The group's
title now supplies the mode scope, so "after adding subtitles" in the label was pure stutter.

### D4 — K-2: the subs detail heading names the source's fate, in BOTH postures

**Decision** (renderer rewrite at `gui.py:2062-2063`, exact code §6.2). Replace the
upscale-schema holdover `"Resolution handling: {target}"` with a `"Source file:"` mapping:

- ON → `Source file: replaced on success — output renamed to <name>.mp4`
- OFF → `Source file: kept — output beside it as <name>.subs.mp4`
- unknown future value → `Source file: {target}` (honest passthrough, never a crash)

**Rationale.** K-2's root cause: the v0.4.14 renderer treated column 3 as a resolution cell
(upscale schema: `"720p"`), but workstream-1 redefined that column as an *action phrase*
(`"replace original → .mp4"` / `"keep original"`). The heading now names what the value
actually is. Deliberate sub-decision: the OFF (`"keep original"`) posture now **renders a
line too** (previously suppressed by the `!= "keep original"` guard) — silence implying the
default is exactly the invisibility class this workstream exists to kill; the detail body
already states the output contract on the preceding line, so the source's fate is the one
missing fact of the same contract. Column-3 cell strings themselves are **NOT** changed
(workstream-1 test 28 pins them verbatim); the renderer *maps* them.

### D5 — The v0.4.9 resume/monitor overlap is Finding 1's twin: fix it in the same re-lay

**Decision.** `monitor_check` span narrows (2,0,1,5) → (2,0,1,4) (its 419px hint fits the
477px four-column budget: 87+228+78+48+36) and `resume_claim` moves (2,1,1,2) → (2,7,1,2)
(the 237px tail vacated by Mode B ≥ its 153px). Zero width cost (measured: 1290 with D2-D5
all applied). Resume becomes *visible and clickable for the first time since v0.4.9*.

**Rationale / scope.** §0.1's paint probe proved resume renders zero pixels today — the same
"control the user can never see" defect as Finding 1, sharing the rows this spec re-lays.
Leaving a proven-zero-pixel control in place while fixing its twin would require explaining why
one invisibility bug deserved fixing and the other didn't. The touch is three grid-argument
edits; handlers, persistence key (`"resume_claim"`), and all read sites (:1968, :2225) are
untouched. **Flagged for Eric explicitly (release-plan invariant):** after this release the
"Resume retained work" checkbox will *appear*. It is not new functionality — it has existed,
persisted, and functioned (via QSettings) since v0.4.9; it was painted over by the monitor
checkbox's later stacking order. Quill's release notes should phrase it as "fixed a layout
overlap that hid the Resume retained work checkbox."

### D6 — VERSION: keep **0.4.17.0** (no restamp)

**Decision.** The combined release stays `0.4.17.0`. Forge touches **no** version file;
`tests/test_version_stamps.py` runs green **unmodified**.

**Alternatives considered.** (b) Bump to `0.5.0` (the "UI restructure is user-visible,
semantic-minor" argument from the release plan).

**Rationale for rejection.**

1. *The restructured surface is itself unreleased.* Workstream-1's checkbox at (2,8) has never
   shipped; reshaping it in the same cycle is finishing 0.4.17, not superseding it. You do not
   bump a major-ish number to polish your own unreleased UI.
2. *The project's own lineage.* The third component routinely ships user-visible features —
   0.4.13 (batch-cancel UX), 0.4.14 (the entire Mode B mode: GUI + CLI + MCP), 0.4.15
   (denoise), 0.4.16 (monitor visibility + model management). In this scheme second-component
   bumps (0.4→0.5) signal a milestone plateau (engine generation, public surface), not a
   settings-panel restructure.
3. *Strict semver actually agrees:* minor = backward-compatible functionality additions.
   Workstream-2 adds **no API surface** — no CLI flag, no schema field (`api_version` 1
   untouched), no new QSettings key, no new option. It is defect repair (invisible control ×2,
   incoherent heading, +242px width regression) plus relocation of unreleased chrome.
4. *Operational risk for zero benefit.* All six stamps + the CHANGELOG header are already
   consistent at 0.4.17.0 with the gate green; Hermes's invariant says do-not-double-bump.
   Restamping to 0.5.0 churns `packaging/version_info.txt` (FileVersion + ProductVersion
   strings → `"0.5.0.0"`; `filevers` + `prodvers` tuples → `(0, 5, 0, 0)`), `__init__.py`,
   `pyproject.toml`, and the CHANGELOG section title — re-opening the exact multi-stamp drift
   class `test_version_stamps.py` exists to catch, for a private tool with no version
   consumers.

**Future-revisit criteria.** If Eric overrules on marketing grounds, the stamp playbook is the
four version_info values above plus the two files, retitle CHANGELOG `[0.4.17.0]` → `[0.5.0.0]`,
suite gate re-run. Recorded, not executed.

---

## 2. Deliberate Mode A *surface* touches (flagged for Eric, per the release-plan invariant)

`vidlift/run.py`: **zero bytes** (gate: `git diff ac90825..HEAD -- vidlift/run.py` empty at
handoff). The following Mode A *GUI surface* changes are deliberate:

| # | Touch | Why |
|---|---|---|
| F-1 | All upscale controls re-containered into `QGroupBox("Upscale")` | D1; visual regroup only — same widgets, cells, handlers, keys |
| F-2 | `transcribe_subs` rendered for the first time; label shortened | D2 / Finding 1 |
| F-3 | `resume_claim` re-placed → becomes visible; `monitor_check` span 5→4 | D5 / Finding-1 twin (§0.1 measured) |
| F-4 | Mode B label shortened | D3 / K-1 |

No Mode A *behavior* changes: the `start_upscale` options dict, all persistence keys, all
handler bodies, and all five `transcribe_subs`/`replace_source`/`resume_claim` read sites are
byte-preserved. Regression fences: §8 T10 + existing items 26-29 green unmodified.

---

## 3. The GUI layout plan (target state)

```
┌─ root VBox ─────────────────────────────────────────────────────────────────┐
│ title row (pills, GPU meter) — unchanged                                    │
│ PLAN_HELPER_TEXT — unchanged                                                │
│ ┌─ QGroupBox "Upscale" (objectName Controls) ────────────────────────────┐  │
│ │ row 0: (0,0)Add Files (0,1)Add Folder (0,2)Clear (0,3)GB/hour (0,4)gbph │  │
│ │        (0,5)Output Folder (0,6)output_dir[stretch] (0,7)Plan (0,8)Start │  │
│ │ row 1: (1,0)Upscale engine (1,1)combo (1,2,1:3)Enable subtitle OCR      │  │
│ │        (1,5,1:2)Replace original on success… (1,7)Cancel (1,8)Cancel B. │  │
│ │ row 2: (2,0,1:4)LAN monitoring… (2,5)Upscale profile (2,6)combo         │  │
│ │        (2,7,1:2)Resume retained work        ← WAS INVISIBLE (overlap)   │  │
│ │ row 3: (3,1)Denoise label (3,2)combo (3,3,1:3)Transcribe missing subs   │  │
│ │                                             ← FIRST RENDER EVER         │  │
│ └─────────────────────────────────────────────────────────────────────────┘  │
│ ┌─ QGroupBox "Add subtitles (no upscale)" (objectName Controls) ─────────┐  │
│ │ (0,0)Add Subtitles… (0,1)Replace original on success (deletes source)  │  │
│ │ (0,2)[Muted, wordwrap] "Adds subtitles from the file's own audio —      │  │
│ │       video streams are copied (no upscale, no re-encode)."            │  │
│ └─────────────────────────────────────────────────────────────────────────┘  │
│ progress — run panel — splitter/table/details — unchanged                   │
└──────────────────────────────────────────────────────────────────────────────┘
```

Width/height contract: window min **1290×~700**; hard ceilings width ≤1360, height ≤725
(§10 measurement procedure). Span-vs-budget table proving no column inflation (measured
budgets from §0.2):

| Widget | Span | Hint | Budget (cols+spacing) | Δ |
|---|---|---|---|---|
| monitor_check | (2,0,1:4) | 419 | 87+228+78+48+36 = 477 | 0 |
| resume_claim | (2,7,1:2) | 153 | 115+110+12 = 237 | 0 |
| transcribe_subs | (3,3,1:3) | 184 | 48+90+109+24 = 271 | 0 |
| subtitle_ocr / replace_source (unchanged) | (1,2,1:3) / (1,5,1:2) | 137 / 326 | 274 / 364 | 0 |

---

## 4. Architecture deltas (one screen)

```
BEFORE (checkpoint 0269cb0)                    AFTER (this spec)
┌ controls (QFrame, one QGridLayout) ────────┐ ┌ QGroupBox "Upscale" ─┐ ┌ QGroupBox "Add subtitles…" ─┐
│ … engine/OCR/replaceA/cancel/cancelBatch   │ │ all Mode A controls  │ │ Add Subtitles…  [replace B] │
│ … monitor▒▒resume (invisible overlap)      │ │ resume VISIBLE       │ │ + muted scope explainer     │
│ … profile · Add Subtitles… · [352px cbx]   │ │ transcribe RENDERED  │ │ (independent column budget) │
└────────────────────────────────────────────┘ └──────────────────────┘ └─────────────────────────────┘
window min 1532×595                             window min 1290×~700
transcribe_subs: constructed, never laid out    transcribe_subs: laid out, default-ON visible
subs detail: "Resolution handling: replace…"    subs detail: "Source file: replaced on success …"
```

All data flow below the chrome is unchanged: same widgets → same handlers → same QSettings keys
→ same enqueue option dicts. Zero changes in `run.py`, `subs_job.py`, `cli.py`, `mcp_bridge/*`,
`plan.py`, `transcribe.py`, `subtitles.py`, `monitor.py`.

---

## 5. Per-module change table

Line anchors are pre-change, verified in §0.2. LoC are ±20% and include house-style comments.

| # | Module : anchor | Binding / exact text | Change | LoC |
|---|---|---|---|---|
| 1 | `vidlift/gui.py` : QtWidgets import block (26-49) | — | Add `QGroupBox,` (alphabetical slot: after `QGridLayout,`, before `QHBoxLayout,`). | +1 |
| 2 | `vidlift/gui.py` : `transcribe_subs` creation (639-657) | `QCheckBox("Transcribe missing subtitles")` | Label shortened (D2); tooltip **verbatim**; comment block gains the Finding-1 story ("created v0.4.14, never added to a layout; rendered v0.4.17 workstream-2 at upscale grid (3,3,1,3)"). No handler/key/default change. | +6/-2 |
| 3 | `vidlift/gui.py` : `subs_replace_source` creation (758-781) | `QCheckBox("Replace original on success (deletes source)")` | Label shortened (D3/K-1); tooltip **verbatim** (workstream-1 §6.3); comment updated (placement moves from shared-grid (2,8) to the Mode B group grid (0,1); the co-location rationale stands). | +6/-3 |
| 4 | `vidlift/gui.py` : `_build_shell` controls block (1310-1377) | — | **Replaced wholesale** by the §6.1 code: two `QGroupBox` containers (objectName `"Controls"` each — the existing objectName-scoped chrome QSS matches both); upscale grid rows 0-3 per §3 (rows 0/1 untouched adds; row 2: monitor span 5→4, profile pair unmoved, resume → (2,7,1,2); row 3: denoise unmoved + `grid.addWidget(self.transcribe_subs, 3, 3, 1, 3)`); Mode B grid per §3 (button (0,0), checkbox (0.1), muted wordwrap explainer (0,2), `setColumnStretch(2, 1)`). Comment upkeep: the "Canonical verb order" (§12), Karen-r2-D2 span comment, v0.4.13 Cancel-Batch comment carry over; the v0.4.9-resume/v0.4.14-placement/v0.4.15-denoise/v0.4.17-(2,8) comments are replaced by the new row comments in §6.1. | +62/-52 |
| 5 | `vidlift/gui.py` : `_apply_style` (1449) | QSS | Replace `#Controls { … }` with the §6.5 `QGroupBox#Controls { … }` + `QGroupBox#Controls::title { … }` rules. All other rules untouched. | +2 |
| 6 | `vidlift/gui.py` : `_subs_detail_text` (2062-2063) | — | K-2 fix (D4): replace the two lines with the §6.2 three-way mapping + its comment. Method signature untouched. | +13/-2 |
| 7 | `tests/test_v0417_ui_restructure.py` | NEW | §8 file A — 10 test groups (layout-membership fence, paint truth, group separation, cell map + zero-overlap, width/height gates, transcribe visibility+persistence, K-2 detail assertions, wiring fences). | +~230 |
| 8 | `tests/test_v0414_mode_b_gui.py` : item-25 test (`test_subs_replace_checkbox_exists_beside_button_default_off_tooltip`, 895-923) + helper docstring (:881-882) | — | Placement assertions → (0,0)/(0,1) of the **Mode B group's own grid** (helper `win.add_subs_button.parentWidget().layout()` already resolves to the new parent — no helper change); label assertion → the D3 string; **add** group-separation assertions (§8 file B). Items 26-29 untouched. | ±14 |
| 9 | `CHANGELOG.md` : `[0.4.17.0]` section (after line 37) | — | Append the §6.6 "Changed"/"Fixed" blocks (UI restructure, Finding-1 render fix, K-1 width restoration, K-2 heading, resume-visibility fix) — same version section, no retitle (D6). | +18 |
| 10 | Docs sweep: `README.md`, `docs/` | — | Verify-only: grep for `.subs.mp4`/panel-layout prose. Known hit `docs/designs/v0.4.9-resume-and-observability.md:185` is a *historical design record* — do not edit; note for Quill (the combined docs pass owns user-facing docs incl. K-6's `vidlift subs` gap). | ~2 |

**Explicit non-changes (regression fences):** `vidlift/run.py` byte-identical;
`vidlift/subs_job.py`, `vidlift/cli.py`, `vidlift/mcp_bridge/{jobs,tools,schemas,client_local,registry}.py`,
`vidlift/plan.py`, `vidlift/transcribe.py`, `vidlift/subtitles.py`, `vidlift/monitor.py` — zero
edits. Version files — zero edits (D6). QSettings keys — zero renames, zero new keys
(`"transcribe_subs"` default `True`, `"subs_replace_source"` default `False`, `"replace_source"`,
`"resume_claim"` all as-is). `_subs_file_rows` column-3 literals (`:1574-1577`) byte-preserved.
`start_upscale` options dict (:2213-2233) byte-preserved. All handlers (:1209-1231)
byte-preserved.

**Estimated totals:** `gui.py` ≈ +84/−57 (net +27); tests ≈ +230 new, ±14 edit; CHANGELOG +18.

---

## 6. Code-level detail

### 6.1 The `_build_shell` controls block (replaces `gui.py:1310-1377` verbatim)

```python
        # ---- v0.4.17 workstream-2 (Hermes findings 1/2, Karen K-1/K-2):
        # the settings surface splits into TWO titled groups so each mode
        # shows only its own controls. The separation is LAYOUT-STRUCTURAL —
        # two independent grids, two independent column budgets — so no
        # Mode B control can ever inflate the upscale grid's width again
        # (K-1: the 352px checkbox at shared-grid (2,8) pushed the window
        # minimum width 1290 -> 1532).
        controls_upscale = QGroupBox("Upscale")
        controls_upscale.setObjectName("Controls")
        grid = QGridLayout(controls_upscale)
        grid.setContentsMargins(18, 8, 18, 16)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        add = QPushButton("Add Files")
        add.setToolTip("Choose one or more video files")
        add.clicked.connect(self.add_files)
        folder = QPushButton("Add Folder")
        folder.setToolTip("Add recognized videos from a folder")
        folder.clicked.connect(self.add_folder)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear_files)
        browse_output = QPushButton("Output Folder")
        browse_output.clicked.connect(self.choose_output_dir)
        # Canonical verb order left-to-right: Add → Plan → Start (§12).
        grid.addWidget(add, 0, 0)
        grid.addWidget(folder, 0, 1)
        grid.addWidget(clear, 0, 2)
        grid.addWidget(QLabel("GB/hour"), 0, 3)
        grid.addWidget(self.gbph, 0, 4)
        grid.addWidget(browse_output, 0, 5)
        grid.addWidget(self.output_dir, 0, 6)
        grid.addWidget(self.plan_button, 0, 7)
        grid.addWidget(self.start_button, 0, 8)
        grid.addWidget(QLabel("Upscale engine"), 1, 0)
        grid.addWidget(self.engine_combo, 1, 1)
        grid.addWidget(self.subtitle_ocr, 1, 2, 1, 3)
        # Karen r2 D2: span 2 columns, not 3 -- a 3-column span reached
        # column 7, where cancel_button sits; the checkbox label clipped
        # under the Cancel button.
        grid.addWidget(self.replace_source, 1, 5, 1, 2)
        grid.addWidget(self.cancel_button, 1, 7)
        # v0.4.13 (design §4.1): Cancel Batch lands immediately right of
        # Cancel Current.
        grid.addWidget(self.cancel_batch_button, 1, 8)
        # v0.4.17 workstream-2: row 2 de-overlapped. The monitor span
        # narrows 5 -> 4 (its 419px hint still fits the 477px four-column
        # budget), and resume_claim moves from the invisible overlap at
        # (2,1,1,2) — painted over by the later-created monitor (Qt stacks
        # by CREATION order; measured 0/7314 painted pixels, the Finding-1
        # twin) — to the row's free tail the Mode B controls vacated.
        grid.addWidget(self.monitor_check, 2, 0, 1, 4)
        self.monitor_check.toggled.connect(self._toggle_monitor)
        # v0.4.8 (design D5): the profile combo rides row 2.
        grid.addWidget(QLabel("Upscale profile"), 2, 5)
        grid.addWidget(self.profile_combo, 2, 6)
        grid.addWidget(self.resume_claim, 2, 7, 1, 2)
        # v0.4.15 (opt-in denoise) row 3 — the Mode A processing-options
        # row. v0.4.17 (Hermes Finding 1): the transcription toggle is
        # RENDERED for the first time since v0.4.14 (created at :643 but
        # never added to a layout); the (3,3,1,3) span's 271px column
        # budget takes its 184px label at zero width cost.
        grid.addWidget(QLabel(DENOISE_COMBO_LABEL), 3, 1)
        grid.addWidget(self.denoise_combo, 3, 2)
        grid.addWidget(self.transcribe_subs, 3, 3, 1, 3)
        grid.setColumnStretch(6, 1)
        layout.addWidget(controls_upscale)

        # ---- Mode B surface (v0.4.17 workstream-2): its own group, its
        # own column budget, only its own controls (Hermes Finding 2). The
        # explainer states SCOPE only; the naming rule, keep-both promise,
        # and replace semantics live in the two tooltips (one derivation,
        # no drift surface).
        controls_subs = QGroupBox("Add subtitles (no upscale)")
        controls_subs.setObjectName("Controls")
        subs_grid = QGridLayout(controls_subs)
        subs_grid.setContentsMargins(18, 8, 18, 12)
        subs_grid.setHorizontalSpacing(12)
        subs_grid.setVerticalSpacing(10)
        subs_grid.addWidget(self.add_subs_button, 0, 0)
        subs_grid.addWidget(self.subs_replace_source, 0, 1)
        explainer = QLabel(
            "Adds subtitles from the file's own audio — video streams "
            "are copied (no upscale, no re-encode)."
        )
        explainer.setObjectName("Muted")
        explainer.setWordWrap(True)
        subs_grid.addWidget(explainer, 0, 2)
        subs_grid.setColumnStretch(2, 1)
        layout.addWidget(controls_subs)
        layout.addWidget(self.progress)
```

Notes for Forge: the block ends where the old one did (`layout.addWidget(self.progress)`);
everything after (`run panel`, splitter, table) is untouched. The `controls` symbol had no
references outside the block (grep-verified). `explainer` is intentionally a *local* (house
pattern for layout-only widgets: `add`/`folder`/`clear` are locals too); tests find it by text.

### 6.2 K-2 fix — `_subs_detail_text` (replaces `gui.py:2062-2063`)

```python
        # v0.4.17 workstream-2 (Karen K-2): column 3 of a subs row carries an
        # ACTION phrase ("replace original → .mp4" / "keep original"), not a
        # resolution — the "Resolution handling" heading was a v0.4.14
        # upscale-schema holdover and read incoherently under the subs
        # schema. The heading now names the source's fate, rendered in BOTH
        # postures (silence used to imply "kept" — the invisibility class
        # this workstream exists to kill). The literals mirror
        # _subs_file_rows (:1574-1577) — pinned by test 28 and §8-T8.
        if target == "replace original → .mp4":
            lines.append(
                "Source file: replaced on success — output renamed to <name>.mp4"
            )
        elif target == "keep original":
            lines.append(
                "Source file: kept — output beside it as <name>.subs.mp4"
            )
        elif target:
            lines.append(f"Source file: {target}")
```

### 6.3 Final user-visible texts (exact; Karen asserts these)

| Surface | Text | Status |
|---|---|---|
| Group A title | `Upscale` | new |
| Group B title | `Add subtitles (no upscale)` | new |
| transcribe label | `Transcribe missing subtitles` | shortened (was 46 chars) |
| transcribe tooltip | *verbatim existing* (:651-656) | unchanged |
| Mode B checkbox label | `Replace original on success (deletes source)` | shortened (was 56 chars) |
| Mode B checkbox tooltip | *verbatim workstream-1 §6.3* (:770-778) | unchanged |
| `add_subs_button` text + tooltip | *verbatim* (:743-757) | unchanged |
| Mode B explainer | `Adds subtitles from the file's own audio — video streams are copied (no upscale, no re-encode).` | new, scope-only |
| subs detail, replace ON | `Source file: replaced on success — output renamed to <name>.mp4` | new (K-2) |
| subs detail, keep OFF | `Source file: kept — output beside it as <name>.subs.mp4` | new (K-2) |
| Forbidden in subs detail | `Resolution handling` | must not appear |

### 6.4 Stable strings / keys for Karen

- QSettings keys (pre-existing, behavior identical): `transcribe_subs` (default `True`),
  `subs_replace_source` (default `False`), `replace_source`, `resume_claim`. **No new keys,
  no renames, no migration.**
- Detail strings per §6.3 table; column-3 row literals unchanged: `keep original`,
  `replace original → .mp4`.

### 6.5 QSS amendment (replaces `gui.py:1449`)

```
QGroupBox#Controls { background: #191d1f; border: 1px solid #2b3134; border-radius: 8px; margin-top: 14px; padding-top: 4px; font-weight: 600; }
QGroupBox#Controls::title { subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 4px; color: #98a196; }
```

Visual parity with today's frame chrome; the title color matches `#Muted` (#98a196).
**Pre-approved fallback** (only if Windows/native style renders titles badly — Forge may not
substitute freely): keep `QGroupBox` for structure but flatten via QSS
(`QGroupBox#Controls::title { color: #98a196; }` already does this; a full QFrame+header-label
re-layout is the escape hatch and would require Bob sign-off since §8-T3 asserts group titles).

### 6.6 CHANGELOG amendment (item 9) — append inside `[0.4.17.0]`

```markdown
### Changed

- **Settings surface split by mode.** The main window's controls are now two titled
  groups — **Upscale** (engine, profile, denoise, GB/hour, its own replace-original, resume,
  and the subtitle carry policy) and **Add subtitles (no upscale)** (its own
  "Replace original on success" checkbox, moved out of the upscale grid). Each surface now
  shows only the controls that mode actually consumes.
- **Main-window minimum width restored** to ~1290px (a long Mode B checkbox label had
  inflated it to 1532px on the checkpoint build).

### Fixed

- **"Transcribe missing subtitles" is now visible and controllable.** The transcription
  toggle (default on) existed since v0.4.14 but was never added to the window layout — it is
  now rendered in the Upscale group's options row. Its behavior is unchanged.
- **"Resume retained work" is now visible.** A layout overlap had painted it under the LAN
  monitoring checkbox since v0.4.9. No behavior change — the option itself already worked.
- Subs detail panel wording: the source's fate is now stated coherently in both postures
  ("Source file: kept …" / "Source file: replaced on success — output renamed to <name>.mp4")
  instead of an incoherent "Resolution handling" heading.
```

---

## 7. Implementation milestones (Forge)

| Milestone | Content (§5 items) | Exit gate |
|---|---|---|
| **M1 — restructure** | items 1-6 (gui.py only) + the item-8 test edits | `pytest tests/test_v0414_mode_b_gui.py tests/test_v0414_mode_b_gui_executor.py tests/test_gui.py -q` green; live offscreen probe: window min ≤ 1330 measured on this box |
| **M2 — fences** | item 7 (new test file) | `pytest tests/test_v0417_ui_restructure.py -q` green AND full suite green (`990 - 0 + ~10` ⇒ ≈1000 passed, 2 pre-existing skips) |
| **M3 — hygiene** | items 9-10 + proofs | `tests/test_version_stamps.py` 7/7 green **unmodified**; `git diff ac90825..HEAD -- vidlift/run.py vidlift/subs_job.py vidlift/cli.py vidlift/mcp_bridge/` **empty**; CHANGELOG in place |

Coding conventions: house style (versioned comments `v0.4.17 workstream-2`, exact strings per
§6.3, no new third-party dependencies, no new QSettings keys). Forge does **not** touch any
version file and does **not** cut any release artifacts — the combined release lands after
Karen + Quill (release-plan steps 4-6).

---

## 8. Test plan — which files to add / extend

### File A (new): `tests/test_v0417_ui_restructure.py` (~230 LoC)

Harness copied from `tests/test_v0414_mode_b_gui.py` (offscreen, `VIDLIFT_MONITOR_AUTOSTART=0`,
per-run isolated `QSettings` ini file, `_ScriptedClient`, generator `_window`, `_alive` list).
No real ffmpeg, no whisper, no network.

**T1 — the never-rendered fence (Finding 1 / its twins, permanently).** Recursive walk over
every `QLayout` reachable from `win.centralWidget().layout()` (descend into nested layouts via
`itemAt(i).layout()`); collect every `.widget()`. Assert membership of **all** attribute-owned
controls: `transcribe_subs`, `subtitle_ocr`, `replace_source`, `resume_claim`, `monitor_check`,
`subs_replace_source`, `add_subs_button`, `engine_combo`, `profile_combo`, `denoise_combo`,
`gbph`, `output_dir`, `plan_button`, `start_button`, `cancel_button`, `cancel_batch_button`.
Also: a `QLabel` with the §6.3 explainer text is present and parented. (A widget created but
never added to ANY layout fails here forever — the Finding-1 class dies structurally.)

**T2 — paint truth.** `QT_QPA offscreen` grab: for each of `transcribe_subs`, `resume_claim`,
`monitor_check`, `subs_replace_source`, `replace_source` — grab the parent frame, hide the
widget, grab again, assert a nonzero pixel delta inside the widget's geometry (the §0.1 probe
technique). Guard: `skipif` when `grab().toImage().isNull()` (a platform that cannot rasterize
offscreen must not false-fail — the structural T1/T4 still hold there).

**T3 — group separation.** `subs_replace_source.parentWidget() is not replace_source.parentWidget()`;
`add_subs_button.parentWidget() is subs_replace_source.parentWidget()`;
`transcribe_subs.parentWidget() is replace_source.parentWidget()`; group titles
`"Upscale"` and `"Add subtitles (no upscale)"` found among `findChildren(QGroupBox)`.

**T4 — cell map + zero-overlap.** `getItemPosition` assertions: within the Mode B grid,
add_subs (0,0), subs_replace (0,1); within the upscale grid, transcribe (3,3,1,3), resume
(2,7,1,2), monitor (2,0,1,4), replace_source (1,5,1,2). Plus the **general** intersection
check over both grids: iterate every item pair, assert their `(row, col, rowspan, colspan)`
rects do not intersect (Karen's programmatic method, now enforced in-repo — kills the v0.4.9
overlap class).

**T5 — width gate (K-1).** `win.show()` offscreen; `win.minimumSizeHint().width() <= 1360`
(comment: reference box measures ≈1290 = exact v0.4.16.2 parity; ceiling sits under the 1366px
laptop edge K-1 named; font-metric slack for non-reference platforms).
Height: `win.minimumSizeHint().height() <= 725` (1366×768 laptop with a 40px taskbar leaves
≈728; pre-change value 595 — growth budget for the second group). Both numbers carry the
measured-provenance comment.

**T6 — transcribe visibility + persistence (Finding 1 functional half).** Fresh settings →
checkbox `isChecked() is True` (the always-on default is *visible*); toggle writes
`"transcribe_subs"` and a fresh window restores it (both directions — the item-26 pattern);
label == §6.3; tooltip still carries `1.5 gb` and the no-track condition
(`"no subtitle track"` per the existing tooltip wording :651-656 — assert case-insensitively
against the real string). Mode B key untouched by it (`subs_replace_source` stays absent).

**T7 — resume/monitor row-2 contract (D5 fence).** `resume_claim.parentWidget()` is the upscale
group; `resume_claim.isVisibleTo(controls_upscale)`-style renderability: covered by T2 delta >
0 for both widgets (resume nonzero = the exact regression this fixes).

**T8 — K-2 detail strings.** Scripted-client enqueue with `subs_replace_source` ON → select
the row → detail text contains `"Source file: replaced on success — output renamed to
<name>.mp4"` and `assert "Resolution handling" not in detail`. OFF posture → contains
`"Source file: kept — output beside it as <name>.subs.mp4"`. Direct unit call of
`_subs_detail_text` on synthetic rows for the three arms (replace / keep / unknown-future
passthrough `f"Source file: {target}"`).

**T9 — wiring fences (inspect-pinned, the cross-wiring class).** `start_upscale` source still
contains `"transcription_enabled": self.transcribe_subs.isChecked()`,
`"replace_source": self.replace_source.isChecked()`, `"resume": self.resume_claim.isChecked()`
and never `subs_replace_source`; `choose_subs_files` still contains
`replace_source=self.subs_replace_source.isChecked()` and never `self.replace_source.isChecked()`
(existing test 29 style; re-asserted here so this file alone proves the restructure didn't
rewire reads).

**T10 — plan/render threading intact.** Port the two `test_gui.py` F6 assertions against the
restructured window (spy on `build_batch_plan` → `transcription_enabled` threads both ways;
render_batch disabled-note override when unchecked) — duplicates exist in `test_gui.py`; here
they pin the same contracts against the new layout in one file.

### File B (edit): `tests/test_v0414_mode_b_gui.py` (item-25 test, 895-923; ±14 LoC)

- Placement: `_grid_position(win, win.add_subs_button) == (0, 0)` and
  `_grid_position(win, win.subs_replace_source) == (0, 1)` (the helper already resolves the
  new parent layout — edit the expected cells and the test's docstring cell references).
- Label assertion → `"Replace original on success (deletes source)"`.
- Tooltip assertions — unchanged (verbatim preserved).
- **Add:** `_grid_position(win, win.transcribe_subs)` in the upscale grid == (3, 3) — and
  `win.subs_replace_source.parentWidget() is not win.add_subs_button.parentWidget()` must be
  **False** (same Mode B group) while `win.replace_source.parentWidget()` differs.
- Items 26-29 (persistence, key isolation, enqueue threading, column-3, cross-wire fence):
  **no edits**.

### Unmodified-pass invariants (zero edits; any required edit is a spec violation — escalate)

`tests/test_run.py`, `tests/test_v0417_subs_replace_source.py` (workstream-1 file A, all 25),
`tests/test_v0417_failpath_fix.py`, `tests/test_v0414_subs_job.py`,
`tests/test_v0414_mode_b_gui_executor.py`, `tests/test_v04151_subs_guard.py`,
`tests/test_v04153_subs_bitmap_remux_slow.py`, `tests/test_cli_v049.py`,
`tests/test_mcp_bridge.py`, `tests/test_client_local.py`, `tests/test_version_stamps.py`
(D6: no restamp ⇒ this gate stays green untouched). The `test_gui.py:517-535` verb-order test
and both F6 transcribe tests pass unmodified (§0.2 compatibility verified).

---

## 9. Failure analysis

| Failure | Behavior / mitigation |
|---|---|
| Windows (Segoe UI) font metrics exceed Linux reference | Width budget analysis holds under ~15% metric inflation (every span has ≥53px slack); the px gates live only in file A (not in CI's enumerated files); Karen re-measures the real Windows build before release (§10) |
| Non-reference platform renders `QGroupBox::title` oddly | Cosmetic only; §6.5 pre-approved fallback with Bob sign-off; no behavior depends on the title rendering |
| Explainer text drifts from button tooltip over time | The explainer is deliberately scope-only (no naming/rule claims) — the only facts it states (copies streams, no upscale) are the mode's definition; tooltips remain the single derivation for policy |
| Column-3 literal drift between `_subs_file_rows` and `_subs_detail_text` | Both pinned: test 28 (rows) + T8 (detail) assert against the same literals; cross-referencing comments at both sites |
| Upgrade path: old user QSettings | Keys/defaults unchanged; a stored `transcribe_subs=false` from any past blind edit is honored identically (key never renamed) — T6 asserts restore semantics |
| Future control added to a group inflates width again | T5 gate + T1 fence + structural column separation; K-1's lesson is encoded in tests now, not memory |
| Qt version differences in stacking for the de-overlap | The D5 layout has **zero** overlaps (T4 asserts), so stacking order no longer matters for these widgets |

No new runtime failure modes are introduced: no new options, no new schema fields, no executor
or engine changes; the diff is GUI-chrome-only plus one detail-string renderer.

---

## 10. Validation baseline for Karen (expand freely)

**Acceptance criteria:**

1. **K-1 resolved:** window minimum width ≤ 1360, expected ≈1290 (v0.4.16.2 parity) on the
   reference box, by the same procedure as the original measurement (offscreen window,
   `minimumSizeHint`, per-widget differential). Confirm the +242px regression is fully retired.
2. **Finding 1 resolved:** "Transcribe missing subtitles" is rendered, visible, checked-ON on
   fresh settings, controllable, persisted under `transcribe_subs` both directions; its toggle
   demonstrably reaches plan (spy or disabled-note override) and the start options (T9).
3. **Finding-1 twin resolved (flagged to Eric):** "Resume retained work" now paints (pixel-diff
   > 0) and "LAN monitoring…" still paints fully; the general no-overlap check passes on both
   grids.
4. **Surfaces separated (Finding 2):** the Mode B group contains exactly the Add Subtitles
   button + its replace checkbox + the scope explainer; no Mode B control remains in the
   Upscale group and vice-versa (T3); both replace tooltips' scope claims are literally true
   against the layout.
5. **K-2 resolved:** subs detail shows §6.3's "Source file:" lines in both postures and never
   "Resolution handling"; column-3 row strings unchanged (test 28 green).
6. **Mode A intact:** `git diff ac90825..HEAD -- vidlift/run.py` empty (also vs `0269cb0`);
   `tests/test_run.py` green unmodified; options dict fences (T9) green; one manual
   Mode-A-replace smoke remains as per workstream-1 §10-6 (no engine here — byte identity +
   the green replace block carry it).
7. **Mode B intact:** workstream-1 items 26-29 green unmodified; a live pick flow threads ON/OFF
   exactly as validated at the checkpoint (state-at-enqueue == checkbox).
8. **Window chrome:** min-height ≤ 725 measured; both groups render titled frames, not clipped
   (grab non-blank); the window fits 1366×768 in normal state (headline gate: width).
9. **Settings integrity:** no new/renamed QSettings keys; the four pre-existing keys behave
   per workstream-1 criterion 7; cross-process restore proof stands.
10. **Hygiene:** `test_version_stamps.py` 7/7 green unmodified (version stays 0.4.17.0 — D6);
    CHANGELOG `[0.4.17.0]` carries §6.6; docs sweep recorded; suite total ≈ 1000 passed /
    2 pre-existing skips.

**Edge cases to probe beyond §8:** Windows native-style QGroupBox title rendering on the real
installer build; a stored legacy `denoise/mode=bogus-legacy` entry + new layout (the
pre-existing config quirk Karen noted — must still read as `off` and not disturb layout);
very long translations/labels (none today — assert via T5 headroom comment); a subs row
selected while its detail updates (the `_set_details_text` compare-guard at :2068 still
applies); resize behavior just above the new minima.

**Security expectations:** unchanged attack surface (no new inputs); the restructure reads no
new files and spawns no new processes; QSettings trust zone unchanged.

**Performance expectations:** zero steady-state cost (layout only); construction cost +2
containers (~negligible); no new timers/threads.

**UI expectations (exact strings):** §6.3 table is the assertion source.

---

## 11. Final review checklist

✓ Requirements satisfied (Findings 1+2, K-1, K-2; Hermes direction items 1-3)
✓ Research completed (live-measured tree evidence; Qt span/stacking behavior probed, not assumed)
✓ Alternatives evaluated (D1: tabs / shared-grid-headers / dialog / chosen structural split; D6 version)
✓ Tradeoffs documented (height +105px for width −242px and structural separation; label shortening)
✓ Security reviewed (§10; no new surface)
✓ Performance reviewed (§10)
✓ Scalability reviewed (Mode B group has a free growth cell for future model/language controls)
✓ Observability unchanged by design (no runtime delta)
✓ Testability reviewed (T1/T2/T4/T5 make the whole defect class machine-assertable)
✓ Extensibility reviewed (group B accepts future Mode B controls without touching upscale width)
✓ Technical debt minimized (the v0.4.9 overlap dies instead of calcifying; K-1/K-2 closed at the root)
✓ Risks documented (§9)
✓ Deviations flagged for Eric (§2 F-1..F-4; version call §D6; QSS fallback rule §6.5)
✓ Forge guidance complete (§5-§7, exact code §6)
✓ Karen baseline complete (§10)

— Bob
