# Karen Re-validation Report — VidLift v0.4.16.1 (Forge fail-path fix)

**Validator:** Karen (adversarial Principal QA, read-only)
**Date:** 2026-11-03 (America/Chicago)
**Scope:** Forge's v0.4.16.1 candidate (commit `3785129`, "wip(v0.4.16.1):
fail-path fix + specs checkpoint before validation") against the two defects in
`.specs/forge-vidlift-failpath-fix-task.md` — (1) the fail→failed transition not
reaching every GUI surface on Eric's Mode-B probe-failure repro, (2) the failure
reason not surfaced anywhere. Release gate for the v0.4.16.1 patch.
**Interpreter:** `/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3,
pytest 9.1.1) exclusively. All evidence in-repo; probes ran as in-memory
heredoc scripts (no repo files created or modified beyond this report; the
real `~/.vidlift/` was never read; nothing was written to `/tmp/`).

---

## Executive Summary

**Verdict: PASS.**
**Overall risk: Low. Confidence: High.**

Both defects are fixed with a correctly localized, read-path-only patch
(3 source files + 1 new test file; no pre-existing test touched). I validated
the fix **beyond Forge's own tests**: I reproduced Eric's exact scenario under
a registry-faithful scripted harness (snapshot stable within a tick — the true
"stale running overlay" window), confirmed the pre-fix code reproduces the
live report (stale `running — probe` overlay + both bars pulsing forever over
a failed batch) via an in-memory pre-fix emulation, and confirmed the fixed
code repairs every surface: details body `Verdict: failed`, overlay
`State: failed` + the verbatim probe error, both bars out of the busy pulse
with stale labels cleared. The `.mkv` transcribe-failure variant, the Mode-A
variant, mid-batch failures, the queued-cancel settle, the running-cancel
settle, and the happy path (no bogus error line, no terminal stutter) all
hold. The `error` key is genuinely additive: `/history` row key sets are
exactly the old keys + `error` in both serializations (`client_local.history`
and `tool_list`'s history block), the HTTP `/history` route is an untouched
pass-through, `api_version` stays 1, and old records persisted without the
key load as honest `null`.

The full suite is green and deterministic across three runs: **921 passed,
2 skipped** every time (the 2 skips are the pre-existing Windows-only
GPU-meter tests, expected on Linux). The 7 new tests account exactly for
914 → 921. The v0.4.16.0 cancel fix, model-management, discovery card,
hermeticity, monitor, Mode-B, and version-stamp gates all pass. The
version-tuple drift the checkpoint fixed (`filevers`/`prodvers` stuck at
0.4.16.0) is confirmed resolved — all four `version_info.txt` stamps plus
`pyproject.toml` and `__init__.py` read 0.4.16.1.

Three findings, none above **Low**: one pre-existing residual edge (K-1:
a user who *explicitly deselects* the row mid-run keeps a stale running
overlay in the details panel — every other surface lands failed; one click
repairs; not a regression), one test-fidelity note (K-2: the new GUI tests'
scripted client clamps mid-tick reads, so their overlay-specific assertion
would pass even on pre-fix code — but their body + bar assertions do catch
a revert, and my strict-semantics probes close the gap), and one release-
hygiene item (K-3: no CHANGELOG entry for 0.4.16.1 yet — expected at this
wip checkpoint; must land in the release commit).

---

## Test Plan & Execution

| # | Test | Purpose | Result |
|---|------|---------|--------|
| 1 | Full suite, run 1 (`pytest -q`) | Determinism + global regression | **921 passed, 2 skipped** (103.05s) |
| 2 | Full suite, run 2 | Determinism (task-required repeat) | **921 passed, 2 skipped** (103.53s) |
| 3 | Full suite, run 3 (`-rs`) | Skip identification | **921 passed, 2 skipped** (103.24s) — skips: `test_gpu_meter_no_window.py:132,164`, Windows-only, pre-existing |
| 4 | `tests/test_v0417_failpath_fix.py -v` | The 7 new fail-path tests | **7/7 PASS** (bridge reason surfacing, client additive key, monitor /history, Mode-B GUI primary, Mode-A variant, measured-truth settle, queued-cancel settle) |
| 5 | `test_v0416_cancel_propagation` + `test_model_management` + `test_model_management_gui` + `test_v0416_discovery_card` + `test_hermetic_state_dir` + `test_registry_batch_cancel` | v0.4.16.0 cancel fix, model-management, discovery card, hermeticity | **86/86 PASS** |
| 6 | `test_gui` + `test_monitor` + `test_client_local` + `test_mcp_bridge` + `test_v0414_mode_b_gui` + `test_v0414_mode_b_gui_executor` + `test_version_stamps` | GUI/monitor/client/Mode-B/version-stamp regression | **136/136 PASS** |
| 7 | Diff audit `20dfe61..3785129` | No pre-existing test modified; change surface | **CONFIRMED** — tests/ gains only the new file (633 lines); modified: `gui.py`, `client_local.py`, `tools.py`, `__init__.py`, `pyproject.toml`, `version_info.txt`, `.gitignore` |
| 8 | **Probe S1** (strict harness) — Eric's exact window: overlay reads `running — probe — reading source metadata` at tick 2, failure lands tick 3 | Defect 1 on the true stale window | **PASS** — repaired to `State: failed` + `Verdict: failed` + verbatim `Error:`; bars settled, labels cleared |
| 9 | **Probe S3** (pre-fix emulation, same flow) | Prove the defect was real and the harness exercises it | **DEFECT REPRODUCED** — stale running overlay + both bars pulsing over the failed batch (Eric's live report, exactly) |
| 10 | **Probe S2** (strict harness + user deselect) | Residual edge hunt | **K-1 CONFIRMED** (see Findings) |
| 11 | **Probe S4** (Forge's clamping harness + pre-fix emulation) | Would the new tests catch a revert? | **YES** — `Verdict: running` body assertion + bar assertions fail on pre-fix; overlay assertion alone would not (K-2) |
| 12 | **Probes G1–G6** (independent GUI harness) — .avi probe failure, .mkv transcribe failure, deselect, 2-job mid-batch failure, happy path, running-cancel | Broad fail-path matrix | **All PASS** (G3's deselect observation → K-1) |
| 13 | **Probes P1–P5** (bridge/monitor, no Qt) — api_version, /history pass-through, key-set additivity, old-record null, exception text | Defect 2 contract | **All PASS** |
| 14 | Version stamps | 0.4.16.1 parity incl. binary tuples | **PASS** (suite-pinned; tuple drift resolved) |

---

## Per-defect validation

### Defect 1 — fail→failed transition does not reach every UI surface → **FIXED**

**Root cause (verified in the parent commit's source, `git show 20dfe61:vidlift/gui.py`):**
three read-path gaps conspired. (a) `_poll_jobs`' batch-end arm returned
immediately after `_batch_finished` on the false premise (stale comment) that
`_batch_finished` writes the details panel — it writes only the summary,
batch bar, and table. (b) `_render_selected_detail_on_tick`'s live-batch gate
keyed on `self.batch is None` — a v0.4.13 guard that predates Mode B's
plan-less subs slate, so Mode B never received per-tick detail renders at all
(only claim-edge and user-selection renders). (c) `_batch_finished` never
ended the bars' busy pulse, and a probe-phase failure produces no measured
fraction, so nothing ever called the bars out of `set_busy(True)`. With no
later tick to repair (the elapsed timer stops when `job_ids` drains), a
Mode-B probe failure froze the details panel at `running — probe` and left
both bars animating over a failed batch — Eric's report, exactly.

**The fix (gui.py):** the batch-end arm now calls
`_render_selected_detail_on_tick()` before returning (gui.py:2425); the gate
re-keys on the seeded row map `_batch_row_for_job` alone (gui.py:2480), with
the pending-plan state still resolving to "no live slate" (`_render_pending_files`
clears the map, gui.py:1457 — verified); and the new `_settle_run_bars()`
(gui.py:3264) ends the pulse and clears the stale busy labels on every
`_batch_finished` (gui.py:3292), determinate values untouched (F-4 honored —
pinned by the measured-truth test).

**Evidence beyond the suite:** my strict-semantics probe (S1) shows the true
window — tick 2 overlay `Job: KJ1   State: running — probe — reading source
metadata`, tick 3 (failure + batch end) lands `State: failed`, `Verdict:
failed`, `Error: probe command failed (exit 1: Permission denied): ffprobe
…`, `stage_bar._busy is False`, `overall_bar._busy is False`, stage label
`""`, row cell `failed`, summary `Batch finished with failures`. The pre-fix
emulation (S3) on the identical flow leaves the running overlay and both bars
pulsing — the defect is real, the harness exercises it, and the fix is what
repairs it. The `.mkv` transcribe-failure variant (G2), a two-job batch with a
mid-batch probe failure (G4 — selected row lands failed + error while the
batch is still live, bars correctly keep animating until the batch ends),
the queued-cancel settle (Forge's test), the running-cancel settle (G6), and
the happy path (G5 — `State: succeeded`, no `Error:` line, no
`succeeded — succeeded` stutter) all hold.

### Defect 2 — the failure reason is not surfaced anywhere → **FIXED**

**Root cause:** the record-level `error` was already persisted by the executor
(`jobs.py:728 record.error = outcome.error`, stamped from the exception text
— e.g. `ProbeError: ffprobe failed on …` / `VideoToolError: {phase} command
failed (exit …)`) and already serialized in jobs.json (`registry.py:199`), and
the per-job status payload already carried it (`tools.py:558`). But both
`/history` serializations dropped the key, and the GUI overlay never rendered
it — so `state: "failed"` traveled with no WHY.

**The fix:** `"error": record.error` added to `LocalBridgeClient.history`
rows (client_local.py:280) and `tool_list`'s history block (tools.py:713);
the GUI overlay renders `Error: {error}` verbatim when present (gui.py:1970-
1972). The HTTP `/history` route is an untouched pass-through
(monitor.py:262 → `StatusView.history` → `client.history`), so the key lands
with zero route/transport change.

**Contract evidence (probes P1–P5):**
- `/history` row key set == the v0.4.16.0 key set + `error` exactly — nothing
  renamed, dropped, or reordered (P2, both serializations).
- `/status` `api_version == 1` (P1a); the `/history` envelope is unchanged
  and never carried `api_version` (P1b).
- The reason rides verbatim, naming the actual exception: `probe command
  failed (exit 1: Permission denied): ffprobe …` (P5); succeeded rows carry
  honest `null` (P3a).
- Old records persisted without the key load as `error=None`, no crash (P3 —
  hand-stripped jobs.json reloaded through the real Registry + client).
- The GUI overlay shows the reason on both Mode B and Mode A (G1/G2 + Forge's
  Mode-A test), including the exact `{phase} command failed (exit …)` shape.

---

## Findings (severity-graded)

### K-1 (Low, residual, pre-existing) — details panel keeps a stale running overlay if the user deselects the row mid-run

- **Issue:** if the user explicitly clears the table selection while a job
  runs (click on empty table area), then the job fails, the details panel
  keeps the last-rendered `State: running — probe — reading source metadata`
  overlay — `_render_selected_detail_on_tick` returns early with no selection,
  and no other path re-renders after the batch-end tick.
- **Impact:** one surface (the details panel) shows stale text until the user
  clicks any row (one click repairs). The row cell, batch bar, summary, LIVE
  busy state, and bars all land `failed` correctly; the failure reason is
  still visible in `/history` and on re-selection.
- **Evidence:** probe S2 — tick 2 overlay `running — probe …`; deselect;
  tick 3 failure → overlay still `running — probe …`, row cell `failed`,
  bars settled, summary `Batch finished with failures`.
- **Not a regression:** the pre-fix code behaves identically in this edge
  (deselect froze the panel for any state); the fix strictly improves the
  common path. Outside Eric's scenario (his selection was intact).
- **Recommendation:** queue a small follow-up (e.g., on batch end, if no row
  is selected but a stale overlay maps to a just-drained job, re-render or
  clear the overlay). Does not gate this patch.

### K-2 (Informational, test fidelity) — the new GUI tests' scripted client clamps mid-tick reads, weakening their overlay-specific assertion

- **Issue:** `_ScriptedClient.status_snapshot` clamps to the sequence end,
  so during tick 2 the claim edge's re-read already sees the terminal
  snapshot. The primary test's tick-2 panel therefore shows the failed
  overlay one tick early, and its overlay assertion (`"running" not in` the
  state line) would pass even on pre-fix code.
- **Evidence:** probe S4 — pre-fix emulation under the clamping harness:
  `Verdict: running` present (body assertion catches the revert: **yes**),
  bars busy (bar assertions catch: **yes**), overlay already `State: failed`
  (overlay assertion catches: **no**).
- **Net:** the regression test still catches a revert of the fix through its
  body + bar assertions, and my strict-semantics probes (S1/S3) independently
  pin the true window. No product defect.
- **Recommendation:** optionally tighten the harness (mid-tick reads serve
  the current tick's snapshot) when this file is next touched.

### K-3 (Informational, release hygiene) — no CHANGELOG entry for 0.4.16.1 yet

- **Issue:** `CHANGELOG.md`'s latest entry is 0.4.16.0; the checkpoint commit
  stamps 0.4.16.1 everywhere the suite enforces (pyproject, `__init__.py`,
  all four `version_info.txt` values) but not the changelog.
- **Impact:** none at this wip checkpoint (the commit is explicitly
  "before validation"; the project's trail has Quill land release docs after
  Karen's verdict — cf. `quill-vidlift-v0.4.16-task.md`).
- **Recommendation:** the release commit must carry the 0.4.16.1 entry
  (fail-path settle + failure-reason surfacing + version-tuple fix) before
  the patch ships.

### Positive notes

- The version-tuple drift (`filevers`/`prodvers` still 0.4.16.0 while the
  strings said 0.4.16.1) — the checkpoint's "only red" — is confirmed fixed
  and pinned by `test_version_stamps` (all four values derive-checked).
- The fix honors the established invariants it touches: F-4 measured truth
  never recedes (pinned), the cancel path's registry truth is untouched (the
  fix is GUI read-path only), `_set_details_text`'s compare-before-write
  keeps unchanged renders scroll-safe, and the overlay's tolerant-shape
  contract (§2.2 state matrix) is preserved (`state_part` degrades to the
  bare state word; absent error adds nothing).

---

## Release-gate statement

**v0.4.16.1 is CLEARED for the patch release.** Both defects from
`forge-vidlift-failpath-fix-task.md` are verified fixed on the real paths
under registry-faithful timing; the failure reason now surfaces verbatim
through every read surface (`/history` both serializations, HTTP pass-through,
GUI overlay) as a purely additive key with `api_version` unchanged; the full
suite is green and deterministic (921 passed / 2 skipped, three identical
runs); no pre-existing test was modified; and the v0.4.16.0 cancel fix,
model-management, discovery card, hermeticity, and version-stamp gates all
hold. No Critical or High findings. Ship contingent only on K-3 (the
CHANGELOG entry landing with the release commit, per the standard trail);
K-1 is a known pre-existing edge queued for a follow-up, and K-2 is a
test-fidelity note with no product impact.

**Verdict: PASS — Ready for Production (patch).**
