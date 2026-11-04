# VidLift v0.4.18 Implementation Spec — follow-up batch (recursive folder-add, sidecar suppression, scratch cleanup, UI polish)

**Author:** Bob (Principal Software Architect)
**Date:** 2026-11-04
**Source brief:** `.specs/hermes-brief-v0.4.18-followup-batch.md` (Hermes), with the four detail notes:
`.specs/hermes-followup-v0.4.18-modeb-add-folder-recursive.md` (WS1),
`.specs/hermes-followup-v0.4.18-srt-sidecar-suppression.md` (WS2),
`.specs/hermes-followup-v0.4.18-cleanup-scratch-on-success.md` (WS3),
`.specs/hermes-followup-v0.4.18-ui-title-position.md` (WS4).
**Audience:** Forge (implementation), Karen (validation baseline in §13)
**Scope:** ONE release, v0.4.18.0, covering all four workstreams. No double-bump.

---

## 0. Evidence verification

Every anchor below was re-verified against the working tree at `v0.4.17.0`
(commit `b506876`, the brief commit) before this spec was written. All line
numbers in §4–§8 are these pre-change anchors; they drift as earlier rows land,
so Forge applies each workstream's rows in table order, bottom-up within a file
where two rows share a region.

**WS1 anchors (verified):**

- `vidlift/gui.py:98-101` — `VIDEO_EXTENSIONS` set.
- `vidlift/gui.py:106` — `MAX_FILES = 500` (GUI pick-time guard).
- `vidlift/gui.py:20-22` — gui.py imports `sys`, `time`, `pathlib.Path`; **`os` is not imported** (the collector needs it — row 4-A1).
- `vidlift/gui.py:1554-1570` — Mode A `add_folder`, non-recursive `sorted(Path(folder).iterdir())`, the current MAX_FILES warning at :1563-1569.
- `vidlift/gui.py:1663-1796` — `choose_subs_files`: the file picker (:1683-1691), the v0.4.15.1 live-conflict guard (:1692-1713), the v0.4.17 enqueue call carrying `replace_source=self.subs_replace_source.isChecked()` (:1722-1725), the v0.4.15.4 enqueue catch list (:1726-1743), table-row append (:1759-1765), slate bootstrap + summary set (:1774-1796).
- `vidlift/gui.py:3494-3518` — `dropEvent`; the `candidate.is_dir()` arm (:3499-3504) uses one-level `iterdir()`; the F-13a cap warning (:3511-3517).
- `vidlift/gui.py:1339-1341` — Mode A "Add Folder" button (a *local*, not an attribute).
- `vidlift/mcp_bridge/client_local.py:49` — `MAX_GUI_BATCH = 64`, enforced per-call in `enqueue_run` (:144-148) and `enqueue_subs` (:186-190).
- No existing test exercises recursion (grep: no `rglob`/`os.walk` in `test_gui.py`, `test_v0414_mode_b_gui.py`, `test_v04151_subs_guard.py`). `tests/test_gui.py:1266-1305` (F-13a) drops synthetic *file* URLs only — the `is_dir()` arm is never exercised there, so those tests survive WS1 unmodified.

**WS2 anchors (verified):**

- `vidlift/subs_job.py:211-227` — the `transcribe_source_to_srt(...)` call with `workdir=workdir or (output_path + ".subs-work")` (:214).
- `vidlift/subs_job.py:232-241` — `mplex(...)`; raises `SubsJobError` on failure (:350-352, :360-362).
- `vidlift/subs_job.py:243-268` — the tail: `final_output/final_srt/notes` assignment (:243-245) then the v0.4.17 `replace_source` block (:246-268).
- `vidlift/subs_job.py:501` — `replace_subs_output`'s sidecar step is guarded by `os.path.isfile(srt_path)` — a missing sidecar is already a clean no-op (the replace-interaction answer, §1 D7).
- `vidlift/transcribe.py:2281-2284` — `transcribe_sidecar_srt_path(output_path)` → `{output-stem}.srt`; the core writes the KEEP sidecar at :2467-2468; scratch wav removed at :2486-2493 (workdir dir itself retained — the K-5 leak surface).
- `vidlift/run.py:2542-2929` — the Mode A subtitles phase, all four branches:
  - transcribed (:2569-2796): sidecar written by the core at `transcribe_sidecar_srt_path(plan.output_path)` == `sidecar_target` (:2567); mux at :2770-2776; `"carried"` + `sidecar_path` set post-mux at :2777-2778.
  - embedded-text extract (:2799-2824): the track already rode the encode as mov_text; the `-c:s srt` extract command (:2805-2813) is pure KEEP convenience.
  - adopted sidecar (:2825-2843): `shutil.copyfile(sub_plan.sidecar_path, sidecar_target)` (:2829), mux (:2831-2837), `sidecar_path` set post-mux (:2838-2839).
  - bitmap OCR (:2844-2905): `run_subtitle_ocr` writes its SRT at `sidecar_srt_path(plan.output_path)` == `sidecar_target` (verified: `subtitles.py:1026` `sidecar = sidecar_srt_path(output_path)`, returned at :1042); `sidecar_path` set PRE-mux at :2883; mux at :2891-2898; `embedded` at :2899-2902.
  - shared fail-open (:2906-2922): `except SubtitleOcrError`; `if os.path.isfile(sidecar_target): sidecar_kept = True` — the fallback-retention discipline WS2 must never weaken.
- `vidlift/run.py:1891-1921` — `run_file` kw-only block, `denoise: str | None = None,` terminal (:1920).
- `vidlift/run.py:55-64` — the `from .subtitles import (...)` block.
- `vidlift/subtitles.py:460-462` — `sidecar_srt_path`; `__all__` region :60-70.
- CLI print path: `vidlift/cli.py:340-359` — the `failed-open` / `ocr` / `carried` arms; the `carried` arm's `"(mov_text embedded + .srt sidecar)"` literal (:357) is the suppression-honesty touch point.
- Bridge: `jobs.py:246-247` (`subtitle_ocr`, `replace_source` absent-defaults reads), `jobs.py:264` (`denoise`), `jobs.py:459-484` (`_execute_subs_job` call site, absent-defaults comment at :470-474), `tools.py:736-771` (`tool_transcribe`), `schemas.py:42-73` (`_OPTIONS_RUN`), `schemas.py:120-139` (`transcribe` schema), `schemas.py:242-245` (`_validate_object` **rejects unknown fields** — schema additions are therefore mandatory for the new option), `schemas.py:185-195` (`TOOL_DESCRIPTIONS["transcribe"]`), `client_local.py:162-202` (`enqueue_subs`).
- GUI: `gui.py:1241-1244` (`_transcribe_subs_changed`), `gui.py:1228-1234` (`_subs_replace_source_changed`), `gui.py:2264-2284` (`start_upscale` options dict), `gui.py:652-664` (`transcribe_subs` checkbox block), `gui.py:1384-1387` (upscale grid row 3; (3,6,1,3) is free — transcribe_subs spans (3,3,1,3), column 6 is the stretch column via :1387).

**WS3 anchors (verified):** `subs_job.py:214` (the leak's origin), `transcribe.py:2486-2493` (wav removed, dir retained), Mode A's counterpart discipline at `run.py:1111-1151` (`cleanup_run_artifacts`) + `run.py:1082-1099` (`_SUCCESS_SCRATCH_EXTENSIONS` incl. `".ocr-work"`) + success-only call site `run.py:2955`.

**WS4 anchors (verified):** `gui.py:1483-1484` — the two `QGroupBox#Controls` QSS lines, verbatim as quoted by Hermes. `tests/test_v0417_ui_restructure.py` — T1 control lists (:86-100 `ALL_MODE_A_CONTROLS`, :152 `("add_subs_button", "subs_replace_source")`), T2 paint tuple (:190-196), T3 parentage (:208-224), T4 cell map (:243-248 incl. `_pos(subs_grid, win.subs_replace_source) == (0, 1, 1, 1)`), T5 size gates (:266+), T9 wiring fence (:419-429, source-greps `choose_subs_files`). Mode B group today: exactly 3 grid items (`gui.py:1401-1409`).

**Version stamps (verified):** `vidlift/__init__.py:9` `__version__ = "0.4.17.0"`; `pyproject.toml:3` `version = "0.4.17.0"`; `packaging/version_info.txt` `filevers=(0, 4, 17, 0)` (line 3), `prodvers=(0, 4, 17, 0)` (line 4), `StringStruct('FileVersion', '0.4.17.0')`, `StringStruct('ProductVersion', '0.4.17.0')` — the four `version_info` values. Gate: `tests/test_version_stamps.py` asserts parity (`SERVER_INFO` derives; untouched).

---

## 1. Decisions — every open decision in the four notes, answered

### WS1 (recursive folder-add)

**D1 (note open-decision 1 — recursion scope): BOTH buttons share one collector.** New module-level pure function `collect_video_files(folder) -> list[str]` in `vidlift/gui.py`, used by Mode A `add_folder`, the NEW Mode B `choose_subs_folder`, and — one-ingestion-policy — the `dropEvent` directory arm. Eric confirmed both modes; the drop case is the same operation wearing a different glove (see §2, deviation 1).

**D2 (note open-decision 2 — toggle/default/preference): recursion is unconditional. No checkbox, no QSettings key.** Rationale: (a) Eric's stated case — "a whole show (all seasons) at once" — IS the tree case; (b) the shallow pick remains available through the file pickers, so no capability is lost; (c) a tree containing only top-level videos returns exactly today's list (recursion degenerates cleanly), so the old behavior is a special case, not a mode; (d) zero new UI surface protects the K-1 width discipline and avoids a fourth Mode B row control. This decision is deliberately the opposite branch from Hermes's tentative "toggle, default ON" recommendation — an always-on rule plus a usable alternative surface beats a persisted fork in behavior nobody asked to turn off.

**D3 (note open-decision 3 — duplicates/symlinks): loop-proof by construction, deterministic dedupe.** `os.walk(folder, followlinks=False)` never descends into symlinked directories, so a symlink loop **cannot form** — no visited-inode bookkeeping to get wrong. Video files reached through file-level symlinks are deduped by `os.path.normcase(os.path.realpath(path))`; the **lexicographically first** name survives (dedupe runs over the sorted list, so the survivor never depends on walk order). Result is `sorted(...)` — deterministic queue order, generalizing today's `sorted(iterdir())`. Unreadable subtrees are skipped silently (`os.walk`'s documented default); a permission fault can never abort a walk that has already yielded valid siblings.

**D4 (count guard + batch policy): the `MAX_FILES = 500` guard evaluates the recursive TOTAL and the warning names the count and the tree** ("That folder tree holds N videos (subfolders included); add at most 500 at once."). Consequence discovered during evidence verification: a Mode B folder-add of 65–500 files would pass this guard and then die on `client_local.MAX_GUI_BATCH = 64`'s per-call raise — the feature's flagship case (5 seasons × 20 episodes) would fail at enqueue with a "run them in rounds" dialog. **Decision: `MAX_GUI_BATCH` 64 → 500, aligned with `MAX_FILES` — one batch-size policy (the v0.4.15.4 F-13a principle generalized).** Cost analysis: the cap is GUI-local (the MCP `tool_run`/`tool_transcribe` enqueue loops were never capped); registry worst case is ~500 pending JSON records (~0.5 MB) drained by the existing terminal-retention rules; the queue table already renders 500 rows for Mode A planning. Alternatives rejected: chunking the enqueue (games the cap, hides the policy, and `_begin_job_slate` still treats it as one slate); a Mode-B-only 64-at-pick refusal (contradicts Eric's stated case). `tests/test_client_local.py:112` uses the constant symbolically (`MAX_GUI_BATCH + 1`) and stays green unmodified.

### WS2 (sidecar suppression)

**D5 (note open-decision 1 — default): suppression is strictly OPT-IN, default OFF (the sidecar is kept by default).** This deviates from the note's tentative "suppress-by-default for Mode B" lean, and follows the brief's normative wording — "a NEW **opt-in** to suppress the redundant `.srt` sidecar" (Eric-approved language beats the note's hedged recommendation). Decisive reasons: the KEEP disposition is a deliberate, documented contract (v0.4.15; `docs/designs/v0.4.1-subtitles.md` §B.3; `run.py:1127-1128` cleanup carve-out); flipping it silently at upgrade would change the deliverable set for existing users and invalidate existing tests/assertions/doc; the absent-defaults invariant (`api_version` 1, the `replace_source` precedent) demands that an absent option mean *pre-0.4.18 behavior*. Eric opts in once per surface; QSettings persists it. Revisit criterion: if Eric asks for suppress-by-default after living with it, the change is two default literals + doc text (the mechanics are default-agnostic).

**D6 (note open-decision 2 — naming/polarity): `suppress_srt`, default `False`, everywhere.** Negative-action + default-False is the house's opt-in polarity (`replace_source`, `resume`, `force`); "suppress" names exactly what the default does *not* do. Bindings: QSettings keys `"suppress_srt"` (Mode A) / `"subs_suppress_srt"` (Mode B — the `subs_` prefix precedent from `"subs_replace_source"`); registry option key `"suppress_srt"` on both `run` and `subs` options dicts; `run_file(..., suppress_srt: bool = False)`; `run_subs_job(..., suppress_srt: bool = False)`; CLI `--suppress-srt` on both `run` and `subs`; MCP `run.options.suppress_srt` + `transcribe.suppress_srt`. The alternative (`write_srt_sidecar` default True) inverts every guard and forces the awkward "absent means True" exception to the absent-defaults-reading rule; rejected.

**D7 (note open-decision 3 + replace interaction): mechanism = "remove only after the embed succeeds," uniformly, both modes, all four Mode A branches.** A shared helper `subtitles.remove_srt_sidecar(srt_path, *, log_line=None) -> bool` (best-effort; returns the honest post-call truth) is the only new primitive. Placement invariants:
- Mode B: suppression runs **after `mplex` returns, BEFORE the replace block**. On a mux failure `mplex` raises `SubsJobError` (:350-352) before suppression is ever reached → the `{stem}.subs.srt` sidecar is exactly the fallback deliverable it is today. With `replace_source` also ON, suppression first means the replace helper's sidecar step finds no file and no-ops through its existing `os.path.isfile(srt_path)` guard (`subs_job.py:501`) — confirmed clean; `result.srt_path` was already set to `""` honestly before the helper runs, and the helper's `maybe_srt is not None` guard (`subs_job.py:261`) cannot overwrite it. **Confirmed per the note's question: the ordering + guard handle it with zero changes to the replace helper.**
- Mode A transcribed/adopted/OCR branches: removal immediately after the branch's successful `mux_srt_into_mp4` (transcribed :2770-2776, adopted :2831-2837, OCR :2891-2898). Any mux failure raises `SubtitleOcrError` and lands in the UNCHANGED shared fail-open (:2906-2922): the sidecar exists on disk, `sidecar_kept = True`, "embed failed; sidecar kept next to the output" — the required failure-artifact behavior, flag-independent.
- Mode A embedded-text branch: **no separate mux exists** (the track rode the encode as mov_text and the encode was verified at :2518). Suppression therefore *skips the extract ffmpeg call entirely* (:2804-2813): nothing is written, nothing needs removing, and there is no failure surface the fallback could serve. Status stays `"carried"`; the `embedded` dict is set exactly as today.
- Manifest/status honesty: on an actual removal/skip, `subtitles_block["sidecar_suppressed"] = True` and `"sidecar_path"` is NOT set (popped where it was assigned pre-mux). On a removal `OSError` the helper returns False, the sidecar survives, and `"sidecar_path"` is set as today — **the block never claims a suppressed file exists, nor hides one that does.** Additive manifest key; schema v2 additive convention holds (the `_build_manifest` comment at :3054-3057).

**D8 (delivery honesty surfaces):** `SubsJobResult.srt_path` becomes `""` when suppression shipped (the v0.4.17 rule "result.srt_path must never lie" — a suppressed sidecar has no location; the field stays a `str`, `""` is the v0.4.18 "deliberately none" sentinel, documented on the dataclass). Mode B notes gain `"srt sidecar suppressed after a successful mux"`. Mode A logs gain `subtitles: .srt sidecar suppressed after a successful embed (suppress_srt)` (exact strings in §10). The CLI run report amends the `carried` arm to not claim a sidecar that was removed.

### WS3 (scratch cleanup)

**D9 (mechanism/placement):** new `cleanup_subs_scratch(output_path, workdir_used, *, log_line=None) -> list[str]` in `vidlift/subs_job.py` — the deliberate Mode B counterpart of `run.cleanup_run_artifacts` (same three guardrails, documented as such in its docstring). Call site: in `run_subs_job` after the replace block, immediately before `return SubsJobResult(...)` — reachable ONLY past a successful `mplex` (every earlier raise — probe/guard/transcription/mux failure, `EngineCancelled` from the core or the pre-replace observe — skips it; a *failed replace* keeps the verified success and cleanup still runs, matching Mode A's cleanup-then-replace ordering where a replace failure at :2970-2974 also leaves cleanup done). Success-only is structural, not conditional.

**D10 (note constraint 3 — the path-guard's exact rule):** `workdir_used` is captured **once, pre-rename**, as `workdir or (output_path + ".subs-work")` (replacing the inline expression at :214), so the replace-source rename of the output can never confuse the cleanup target (note constraint 4 — confirmed by construction). The helper deletes ONLY when `normcase(abspath(workdir_used)) == normcase(abspath(default_scratch))`. A caller-supplied `workdir` is the caller's own directory — **never touched** (test harnesses and scripted callers pass tmp workdirs; auto-removing those would be both a guardrail breach and a test-hostile surprise). This is the moral equivalent of Mode A's run-id-namespaced candidate list: "only paths this run created, derived from paths this run owns."

**D11 (channels):** narration rides the job's notes list (Mode B's honest user-visible channel — CLI prints it, the bridge ships it): success `"cleanup subs scratch: {scratch}"`, failure `"WARNING subs scratch cleanup could not remove {scratch}: {exc}"`. Best-effort: `OSError` → warning note, return `[]`, success untouched. `.muxing.mp4` and `.replace-tmp` remain swept by their existing owners (`mplex` :322-362; `replace_subs_output` :471-479); `.subs-work` was the only leak (Karen K-5 confirmed).

### WS4 (title position + F-1)

**D12 (the QSS mechanics): `margin-top: 14px → 20px`, `::title` rule untouched, no negative `top`.** Geometry: with `subcontrol-origin: margin` + `subcontrol-position: top left`, the title anchors to the TOP of the margin box (the widget's outer edge) while the border sits at the margin's inner edge — so every pixel of added `margin-top` moves the *border down* relative to a fixed-anchored title, i.e. the title floats *up off the line* by exactly that delta. Under the Windows Segoe UI metric the title is ~15 px tall against 14 px of margin → ~1 px clearance (Eric's screenshot). 20 px yields ~5-6 px clearance — "slightly higher," no more. **Rejected alternative:** the note's first-listed `top: -8px` on `::title` — a negative subcontrol offset paints *above the widget's own rect*; QStyle may clip such painting platform-dependently, and it also risks colliding with the row above (the helper label). Margin growth is provable, portable, and clip-free; it costs 2 × 6 px of window height (two groups), recovered trivially — T5's height gate re-proves it. **Tuning protocol for the real Windows build (the note's "tune on Segoe UI" instruction, made concrete):** acceptable band is title-bottom-to-border clearance of 4-7 px at 100 % scale; if outside the band, adjust ONLY this margin in ±2 px steps and re-verify both groups. Linux offscreen cannot reproduce the metric; the committed value is 20 px and the check is visual-on-Windows plus the §7 assertions.

**D13 (Karen F-1 — exhaustiveness): the assertion lands as `subs_grid.count() == 5` with exact membership**, not the note's `== 3` — WS1 and WS2 add two Mode B controls in this same release, so pinning `== 3` would be red on arrival. The pin asserts the complete final set with exact cells (§7): `(0,0) add_subs_button`, `(0,1) subs_add_folder_button`, `(0,2) subs_replace_source`, `(0,3) subs_suppress_srt`, `(0,4) explainer`. Deviation from the note's literal count is deliberate and recorded (§2, deviation 5); the note's *intent* — "exactly its controls" — is honored at the release's actual shape.

---

## 2. Explicit deviations from the brief (six, each intentional)

1. **The drop handler also goes recursive** (beyond the literal "folder-add buttons" scope). The `dropEvent` directory arm currently shares add_folder's *count* policy but not its traversal; after WS1 a dropped show-root folder would silently queue NOTHING when the episodes live one level down (dirs are skipped by `iterdir()`'s file filter — pre-existing behavior, newly absurd next to recursive Add Folder). One traversal helper, one policy (the F-13a parity principle). Risk: none beyond the intended semantics change; F-13a's cap still applies on the recursive total; existing drop tests use only nonexistent file URLs and stay green.
2. **`MAX_GUI_BATCH` 64 → 500.** The brief names only the `MAX_FILES` guard; without this, WS1's flagship case fails at enqueue between 65 and 500 files (D4). Behavior surface change: Mode A Start batches of 65-500 ready files now enqueue instead of raising `BatchTooLarge`. Judged a defect-class fix rather than scope creep; flagged for Eric.
3. **Suppression default OFF** (against note open-decision 1's tentative suppress-by-default lean; per the brief's normative "opt-in" wording — D5).
4. **Two existing test files are amended rather than kept untouched** (the usual keep-green rule yields because this release deliberately re-tiles the Mode B surface): `tests/test_v0417_ui_restructure.py` (T1 control lists, T2 paint tuple, T3 parentage additions, T4 cell-map re-pin, T9 fence re-target) and the shared harness in `tests/test_v0414_mode_b_gui.py` (`_ScriptedClient.enqueue_subs` gains defaulted `suppress_srt=False` + records `last_subs_suppress_srt`). Every amendment is enumerated in §12; all other suites — notably `tests/test_run.py`, `tests/test_v0414_subs_job.py`, `tests/test_v0414_mode_b_gui_executor.py`, `tests/test_v04151_subs_guard.py`, `tests/test_v04153_subs_bitmap_remux_slow.py`, `tests/test_cli_v049.py`, `tests/test_mcp_bridge.py`, `tests/test_client_local.py`, `tests/test_gui.py`, `tests/test_version_stamps.py` — must stay green **unmodified**; an edit need there is a spec violation → escalate.
5. **F-1 count is 5, not the note's 3** (D13).
6. **WS4 mechanism is margin growth, not negative `::title top`** (D12) — the note offered both; the negative-offset branch carries a Windows clip risk the margin branch does not.

No other deviations. `api_version` stays 1; no new tools, endpoints, or registry fields; every new option is additive absent-defaults; existing QSettings keys are neither renamed nor migrated.

---

## 3. Architecture delta (one screen)

```
WS1:  QFileDialog (dir) ──▶ collect_video_files()   ← shared: add_folder / choose_subs_folder / dropEvent
                              │  os.walk(followlinks=False)  · realpath dedupe · sorted · VIDEO_EXTENSIONS
                              ▼
                    [len > MAX_FILES=500 → named warning, nothing added]        (gui.py)
                              │
        Mode A: _add_paths(paths)  ·  Mode B: _enqueue_subs_paths(paths)  ← choose_subs_files tail, refactored
                                                     │ enqueue_subs(paths, replace_source=…, suppress_srt=…)
                                                     ▼
WS2/WS3:   run_subs_job(…, replace_source, suppress_srt, workdir)          (subs_job.py)
             workdir_used = workdir or output_path + ".subs-work"   (captured pre-rename)
             transcribe → mplex ── fail ⇒ raise (sidecar + scratch kept: fallback, resume, diagnosis)
                              │
                              ▼ success
             suppress_srt? → remove_srt_sidecar(transcribed.srt_path)  (subtitles.py helper; honest bool)
             replace_source? → replace_subs_output (sidecar step no-ops; v0.4.17 unchanged)
             cleanup_subs_scratch(output_path, workdir_used)          (path-guarded rmtree, best-effort)
                              ▼
             SubsJobResult(output_path=final, srt_path="" when suppressed, notes carry all narration)

WS2 (Mode A):  run_file(…, denoise=None, suppress_srt=False)             (run.py — 4 enumerated touch sets)
             transcription / embedded-extract / adopted / OCR branches:
               · embedded + suppress → skip the -c:s srt extract entirely (track already mov_text in the encode)
               · others + suppress → mux_srt_into_mp4 success, then remove_srt_sidecar(<branch sidecar>)
               · any mux failure → UNCHANGED fail-open keeps the sidecar ("embed failed; sidecar kept")
               · block honesty: "sidecar_suppressed": True XOR "sidecar_path" — never both, never neither-of-record

WS4:  QGroupBox#Controls { margin-top: 20px }  (was 14px) — QSS-only; F-1: subs_grid.count() == 5 membership pin
```

Invariant ledger: `run.py` gains **exactly the §5 enumerated touches and nothing else** (WS1/WS3/WS4 touch zero run.py lines); `api_version` stays 1; Mode A's replace path (`run.py:1683-1796`, call site :2964-2974) is byte-identical; `transcribe.py`, `plan.py`, `registry.py`, `monitor.py`, `_proc.py` are untouched.

---

## 4. WS1 — recursive "Add Folder" for both modes

### 4.1 Change table

| # | Module : anchor | Binding / exact signature | Change | LoC |
|---|---|---|---|---|
| 4-A1 | `vidlift/gui.py:20` | `import os` | Add the import (collector needs `os.walk`/normcase/realpath) between `import sys`-region lines; alphabetical group order. | +1 |
| 4-A2 | `vidlift/gui.py` after `MAX_FILES` (:106-111) | `def collect_video_files(folder: str \| Path) -> list[str]:` | **New module-level pure function.** Full code in §4.2. House style: versioned docstring, `from __future__ import annotations` already module-wide. | +34 |
| 4-A3 | `vidlift/gui.py:1339-1341` | local `folder` button | Tooltip amended (exact text §10 S-3) so Mode A's button advertises recursion truthfully. | +2 |
| 4-A4 | `vidlift/gui.py:1554-1570` | `def add_folder(self) -> None:` | **Rewrite the traversal**: collector replaces the `iterdir()` list comprehension; MAX_FILES guard retained on the recursive total with the new tree-warning text (§10 S-4); `_add_paths` unchanged. | +9 −6 |
| 4-A5 | `vidlift/gui.py` after `add_subs_button` block (:764) | `self.subs_add_folder_button = QPushButton("Add Folder")` | **New Mode B button** (attribute-bound for the T1 fence and F-1 membership), tooltip §10 S-5, `clicked.connect(self.choose_subs_folder)`. Comment names WS1 + the structure-preservation fact. | +12 |
| 4-A6 | `vidlift/gui.py:1663-1796` | `def _enqueue_subs_paths(self, paths: list[str]) -> None:` | **Extract the whole post-picker tail** of `choose_subs_files` (:1692-1796: conflict guard, enqueue + catch, row append, slate bootstrap, summary) verbatim into the shared method; the v0.4.15.1/v0.4.15.4/v0.4.17 comments move WITH the code they narrate. | +8 (net ~0: code moves) |
| 4-A7 | `vidlift/gui.py:1663` | `def choose_subs_files(self) -> None:` | Shrinks to docstring + picker + `if not paths: return` + `self._enqueue_subs_paths(paths)`. | −110 (moved, not deleted) |
| 4-A8 | `vidlift/gui.py` after `choose_subs_files` | `def choose_subs_folder(self) -> None:` | **New** (§4.3): directory picker → collector → MAX tree-guard → empty-noop → `_enqueue_subs_paths(paths)`. | +27 |
| 4-A9 | `vidlift/gui.py:1401-1410` | subs grid | `addWidget(self.add_subs_button, 0, 0)` unchanged; `addWidget(self.subs_add_folder_button, 0, 1)` NEW; `addWidget(self.subs_replace_source, 0, 2)` **moved from (0,1)**; `addWidget(self.subs_suppress_srt, 0, 3)` NEW (created in §5 row 5-F6); `explainer` **moved (0,2)→(0,4)**; `setColumnStretch(4, 1)` (was 2). Comment block updated (picker-pair order mirrors the upscale group; options pair adjacent; explainer keeps the stretch). | +7 −4 |
| 4-A10 | `vidlift/gui.py:3499-3517` | `dropEvent` | Directory arm: `expanded.extend(collect_video_files(candidate))` replaces the one-level generator; cap message gains "(subfolders included)" (§10 S-6); comment cites the F-13a parity extension. | +4 −5 |
| 4-A11 | `vidlift/mcp_bridge/client_local.py:49` | `MAX_GUI_BATCH = 500` | 64 → 500 with the D4 comment (one batch-size policy; aligns with `gui.MAX_FILES`; MCP tools were never capped; registry cost bounded). | +4 |

`pathlib.Path` and `QFileDialog` are already imported. `subs_output_path` (`gui.py:87` import) is untouched — outputs stay beside each source; nested structure is preserved by derivation, not by code change. **Mode A output-dir caveat for the record:** when the user sets the upscale "Output Folder", Mode A outputs land there flat (pre-existing semantic, unchanged; the "preserve nested structure" rule governs the default beside-source behavior on both modes).

### 4.2 `collect_video_files` — exact code

```python
def collect_video_files(folder: str | Path) -> list[str]:
    """v0.4.18 (recursive folder-add, Eric's whole-show case): walk a
    folder TREE and return every recognized video (VIDEO_EXTENSIONS),
    deduplicated and sorted by path. Shared by BOTH Add Folder buttons
    (Mode A upscale, Mode B subtitles) and the drop handler — one
    ingestion traversal, one count policy. Recursion is unconditional
    (no toggle): the file pickers remain the shallow-pick surface.

    Symlink discipline: ``os.walk(followlinks=False)`` never descends
    into a symlinked directory, so a symlink loop CANNOT form (no
    visited-inode bookkeeping needed); file-level symlink duplicates
    collapse via normcase(realpath) dedupe, with the lexicographically
    first name surviving (dedupe runs over the sorted list, never on
    walk order). Unreadable subtrees are skipped silently (os.walk's
    documented default). The result is fully sorted — the pre-0.4.18
    ``sorted(iterdir())`` determinism generalized to the tree.
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(folder, followlinks=False):
        dirnames.sort()  # deterministic descent, independent of os.walk order
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.suffix.lower() in VIDEO_EXTENSIONS:
                found.append(str(candidate))
    seen: set[str] = set()
    collected: list[str] = []
    for path in sorted(found):
        key = os.path.normcase(os.path.realpath(path))
        if key in seen:
            continue
        seen.add(key)
        collected.append(path)
    return collected
```

### 4.3 `choose_subs_folder` — exact code

```python
    def choose_subs_folder(self) -> None:
        """v0.4.18 (WS1): the Mode B folder entry — a directory picker
        feeding the SHARED recursive collector (collect_video_files):
        the whole tree queues at once, symlink-loop-proof, deduplicated,
        deterministically ordered. Outputs land beside each source
        (subs_output_path derives from the source's own directory), so
        the nested structure is preserved by construction — the add
        never relocates or flattens. The MAX_FILES guard evaluates the
        recursive TOTAL and names the count; an empty folder is a
        silent no-op (the picker's already-cancelled discipline). The
        handoff (_enqueue_subs_paths) is choose_subs_files' exact
        post-pick tail: same conflict guard, same enqueue surface, same
        slate."""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose a folder of videos to add subtitles to",
            str(Path.home()),
        )
        if not folder:
            return
        paths = collect_video_files(folder)
        if len(paths) > MAX_FILES:
            QMessageBox.warning(
                self,
                "VidLift",
                f"That folder tree holds {len(paths)} videos (subfolders "
                f"included); add at most {MAX_FILES} at once.",
            )
            return
        if not paths:
            return
        self._enqueue_subs_paths(paths)
```

`add_folder` becomes the same four-stage shape (picker → collector → guard with the same tree warning → `_add_paths(paths)`).

---

## 5. WS2 — optional `.srt` sidecar suppression (both modes, every subtitle branch)

### 5.1 Change table

| # | Module : anchor | Binding / exact signature | Change | LoC |
|---|---|---|---|---|
| 5-A1 | `vidlift/subtitles.py` `__all__` (:60-70) | `"remove_srt_sidecar"` | Add after `"mux_srt_into_mp4",`. | +1 |
| 5-A2 | `vidlift/subtitles.py` after `sidecar_srt_path` (:462) | `def remove_srt_sidecar(srt_path: str, *, log_line=None) -> bool:` | **New** (code §5.2): best-effort removal returning the honest post-state; `FileNotFoundError` counts as success; other `OSError` → WARNING via `log_line`, returns False. | +26 |
| 5-A3 | `vidlift/subs_job.py` docstring (:12-15), `__all__` (:49-50) | — | v0.4.18 paragraph (opt-in `suppress_srt`; "deliberately empty `srt_path` sentinel); `__all__` is not changed for the helper import (import ≠ export). | +6 |
| 5-A4 | `vidlift/subs_job.py:38` | `from .subtitles import SubtitleOcrError, mux_language, remove_srt_sidecar` | Extend the existing import line (multi-line form). | +2 |
| 5-A5 | `vidlift/subs_job.py:124-159` | `run_subs_job(..., force=False, replace_source=False, suppress_srt: bool = False, workdir=None, ...)` | Insert `suppress_srt` **immediately after `replace_source`** (kw-only block — source-compatible for every caller; all call sites pass keywords). Docstring gains the opt-in paragraph + the empty-string `srt_path` contract. | +8 |
| 5-A6 | `vidlift/subs_job.py:191-196` | `workdir_used = workdir or (output_path + ".subs-work")` | Capture once, pre-rename (WS3 D10); the `transcribe_source_to_srt` call (:214) passes `workdir=workdir_used`. | +4 −1 |
| 5-A7 | `vidlift/subs_job.py:245` (after `notes = ...`, before `if replace_source:`) | — | **Suppression block** (code §5.3): guarded removal → `final_srt = ""` + success note; failure → helper's WARNING note, sidecar kept, `final_srt` stays truthful. | +12 |
| 5-B1 | `vidlift/run.py:55-64` | `from .subtitles import (..., remove_srt_sidecar, ...)` | **run.py touch 1 of 6** — extend the existing parenthesized import (insert after `mux_srt_into_mp4,` keeping rough alphabetical order). | +1 |
| 5-B2 | `vidlift/run.py:1919-1921` | `run_file(..., denoise: str \| None = None, suppress_srt: bool = False,) -> RunResult:` | **run.py touch 2 of 6** — terminal kw-only param + a versioned docstring paragraph (opt-in; after successful embed only; fail-open keeps the sidecar; embedded-text branch skips the extract). | +1 +8(doc) |
| 5-B3 | `vidlift/run.py:2777-2778` | transcribed branch | **run.py touch 3 of 6** — conditional disposition (code §5.4-B3): `status = "carried"` unchanged; suppressed-and-removed → `"sidecar_suppressed": True` + log line; else `"sidecar_path"` exactly as today. | +8 −1 |
| 5-B4 | `vidlift/run.py:2804-2824` | embedded-text branch | **run.py touch 4 of 6** — `if suppress_srt:` skips the WHOLE extract (no `record_step("subtitle-sidecar-extract")`, no ffmpeg command); sets `"carried"` + `"sidecar_suppressed": True` + the unchanged `embedded` dict + log line; `else:` keeps :2804-2824 verbatim. | +12 (existing block indents +1 level) |
| 5-B5 | `vidlift/run.py:2838-2839` | adopted-sidecar branch | **run.py touch 5 of 6** — post-mux conditional disposition mirroring B3 (removes the run's COPY at `sidecar_target`; the user's original `sub_plan.sidecar_path` is never a candidate). | +7 −1 |
| 5-B6 | `vidlift/run.py:2899-2902` tail | OCR branch | **run.py touch 6 of 6** — after the successful mux: suppressed → `remove_srt_sidecar(ocr_result.srt_path)`, on True `subtitles_block.pop("sidecar_path", None)` (it was set pre-mux at :2883) + `"sidecar_suppressed": True` + log; the `.ocr/` diagnosis kit is explicitly retained (comment cites the v0.4.1 retention contract). | +10 |
| 5-C1 | `vidlift/mcp_bridge/client_local.py:162-202` | `def enqueue_subs(self, paths, *, replace_source: bool = False, suppress_srt: bool = False) -> list[str]:` | New defaulted kw; options dict gains `"suppress_srt": bool(suppress_srt)`; docstring v0.4.18 sentence (absent-defaults posture preserved). | +5 |
| 5-C2 | `vidlift/mcp_bridge/jobs.py:247` | — | `_execute_run_job` gains `suppress_srt=bool(options.get("suppress_srt", False)),` after `replace_source=...` with the absent-defaults comment pattern. | +3 |
| 5-C3 | `vidlift/mcp_bridge/jobs.py:474` | — | `_execute_subs_job` gains `suppress_srt=bool(options.get("suppress_srt", False)),` after the `replace_source` kwarg + comment. | +4 |
| 5-C4 | `vidlift/mcp_bridge/schemas.py:51` | `_OPTIONS_RUN` | Add `"suppress_srt": {"type": "boolean"}` after `"replace_source"` with a v0.4.18 comment (opt-in; absent → False; embed-success-only; fail-open keeps the sidecar). | +5 |
| 5-C5 | `vidlift/mcp_bridge/schemas.py:136` | `TOOL_SCHEMAS["transcribe"]["properties"]` | Add `"suppress_srt": {"type": "boolean"}` after `"replace_source"` + comment. | +5 |
| 5-C6 | `vidlift/mcp_bridge/schemas.py:185-195` | `TOOL_DESCRIPTIONS["transcribe"]` | Append one sentence before "Enqueues subs jobs;…" (exact text §10 S-10). | +3 |
| 5-C7 | `vidlift/mcp_bridge/tools.py:736-771` | `tool_transcribe` | `suppress_srt = bool(args.get("suppress_srt", False))` after :748 + options entry `"suppress_srt": suppress_srt` (:765 region) + docstring arg-list update (:739). The absent-defaults comment pattern continues. | +5 |
| 5-D1 | `vidlift/cli.py:594-606` region | `run` parser | `run_parser.add_argument("--suppress-srt", action="store_true", help=(…))` (help §10 S-11), placed after `--resume`. | +6 |
| 5-D2 | `vidlift/cli.py:694-703` region | `subs` parser | `subs_parser.add_argument("--suppress-srt", action="store_true", help=(…))` (help §10 S-12) after `--replace-source`. | +6 |
| 5-D3 | `vidlift/cli.py:229-252` | `_cmd_run(..., suppress_srt: bool = False,)` | New trailing kw; pass `suppress_srt=suppress_srt` into `run_file` after `transcription_enabled=...` (:269). | +3 |
| 5-D4 | `vidlift/cli.py:370-406` | `_cmd_subs(..., suppress_srt: bool = False,)` | New trailing kw (after `replace_source`); pass `suppress_srt=suppress_srt` into `run_subs_job` (:405). | +3 |
| 5-D5 | `vidlift/cli.py:346-359` | run report print | `failed-open` arm unchanged; `ocr` arm gains the suppressed sidecar line; `carried` arm branches on `sidecar_suppressed` (exact prints §10 S-13/S-14). | +6 |
| 5-D6 | `vidlift/cli.py:814-830` | dispatch | `suppress_srt=args.suppress_srt` added to BOTH `_cmd_run` and `_cmd_subs` calls. | +2 |
| 5-E1 | `vidlift/gui.py` after `transcribe_subs` block (:664) | `self.suppress_srt = QCheckBox("Suppress .srt sidecar on success")` | **Mode A checkbox**: `setChecked(self._settings().value("suppress_srt", False, type=bool))`, tooltip §10 S-7, `toggled.connect(self._suppress_srt_changed)`, versioned comment. | +13 |
| 5-E2 | `vidlift/gui.py` after `subs_replace_source` block (:794) | `self.subs_suppress_srt = QCheckBox("Suppress .srt sidecar on success")` | **Mode B checkbox**: key `"subs_suppress_srt"` default False; tooltip §10 S-8; connect. | +13 |
| 5-E3 | `vidlift/gui.py` after `:_subs_replace_source_changed` (:1234) | `def _subs_suppress_srt_changed(...)` / `def _suppress_srt_changed(...)` | Two verbatim-pattern persist handlers (§5.5). | +10 |
| 5-E4 | `vidlift/gui.py:1386` | upscale grid | `grid.addWidget(self.suppress_srt, 3, 6, 1, 3)` with comment (row 3 is the subtitle-carry-policy row; (3,6,1,3) verified free; col 6 stretch is shared so no new minimum-width contribution — T5 re-proves). | +4 |
| 5-E5 | `vidlift/gui.py:2264-2284` | `start_upscale` options | `+ "suppress_srt": self.suppress_srt.isChecked(),` with the absent-defaults comment. | +3 |
| 5-E6 | `vidlift/gui.py:1722-1725` (inside `_enqueue_subs_paths` after 4-A6) | enqueue call | `+ suppress_srt=self.subs_suppress_srt.isChecked(),` with comment (the checkbox state IS the submitted option — the v0.4.17 rule extended). | +3 |

**Explicit `run.py` non-touches:** the fail-open handler (:2906-2922), `cleanup_run_artifacts`, `verify_output`, `replace_source_with_output` and its call site, the prepare/upscale/encode phases, and every non-subtitle line are byte-identical. The six touch sets above are the COMPLETE `run.py` delta; Forge diffs against this list and any seventh hunk is a spec violation → escalate before continuing.

**`SubsJobResult` dataclass note (amend its docstring, +3 LoC, `subs_job.py:61-65`):** "`srt_path` is the honest post-run sidecar location; v0.4.18's `suppress_srt` may legitimately make it `""` — the deliberately-suppressed sentinel (never a stale claim of a file that was removed)."

### 5.2 `remove_srt_sidecar` — exact code

```python
def remove_srt_sidecar(srt_path: str, *, log_line=None) -> bool:
    """v0.4.18 (opt-in sidecar suppression): best-effort removal of an
    .srt sidecar AFTER the soft-mux/embed succeeded.

    Returns True when no sidecar remains (removed, or already absent —
    FileNotFoundError is success), False when another OSError kept it;
    a failure logs a WARNING through ``log_line`` (the
    cleanup_run_artifacts channel style) and is NEVER fatal — the
    sidecar simply stays beside the output and the caller reports the
    honest post-call truth. Only ever called on paths the current job
    itself wrote this run (transcription/OCR outputs, the
    adopted-sidecar COPY); a user's original sidecar file is never a
    candidate.
    """
    try:
        os.remove(srt_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if log_line is not None:
            log_line(
                f"WARNING suppress-srt: could not remove {srt_path}: {exc}"
            )
            return False
        return False
    return True
```

### 5.3 Mode B suppression block (spec row 5-A7, exact)

```python
    # v0.4.18 (opt-in suppress_srt): the mux above SUCCEEDED, so the
    # sidecar is redundant — remove it BEFORE any replace, where the
    # helper's isfile guard (replace_subs_output step 4) then makes the
    # sidecar step a guaranteed no-op; on failure mplex already raised
    # ABOVE this line, so the sidecar survived as the fallback
    # deliverable. Removal is best-effort: an OSError keeps the file
    # and final_srt stays truthful (the helper logs the WARNING).
    if suppress_srt:
        if remove_srt_sidecar(transcribed.srt_path, log_line=notes.append):
            final_srt = ""
            notes.append("srt sidecar suppressed after a successful mux")
```

### 5.4 Mode A branch edits (run.py, exact shapes)

**B3 — transcribed (replaces :2777-2778):**

```python
                    subtitles_block["status"] = "carried"
                    # v0.4.18 (opt-in suppress_srt): the embed above
                    # succeeded, so the sidecar is redundant. Truthful
                    # bookkeeping either way: a removal failure keeps
                    # the file AND the sidecar_path key.
                    if suppress_srt and remove_srt_sidecar(
                        transcribed.srt_path, log_line=log
                    ):
                        subtitles_block["sidecar_suppressed"] = True
                        log(
                            "subtitles: .srt sidecar suppressed after a "
                            "successful embed (suppress_srt)"
                        )
                    else:
                        subtitles_block["sidecar_path"] = transcribed.srt_path
```

**B4 — embedded-text (replaces :2799-2824; the `else:` body is old :2804-2824 verbatim, +1 indent level):**

```python
                elif sub_plan.kind == "text" and sub_plan.source == "embedded":
                    # v0.4.18 (opt-in suppress_srt): the text track rode
                    # the ENCODE into the MP4 as mov_text — the extract
                    # step (the KEEP convenience) has no mux to gate on
                    # and no failure surface to fall back from, so
                    # suppression simply never runs it.
                    if suppress_srt:
                        subtitles_block["status"] = "carried"
                        subtitles_block["sidecar_suppressed"] = True
                        subtitles_block["embedded"] = {
                            "codec": "mov_text",
                            "language_tag": mux_language(sub_plan.lang),
                        }
                        log(
                            "subtitles: sidecar extract skipped — the "
                            "track is embedded in the encode (suppress_srt)"
                        )
                    else:
                        record_step("subtitle-sidecar-extract")
                        ...  # old :2805-2824 verbatim
```

**B5 — adopted sidecar (replaces :2838-2839):**

```python
                    subtitles_block["status"] = "carried"
                    # v0.4.18 (opt-in suppress_srt): removes the run's
                    # COPY at sidecar_target after the successful mux;
                    # the user's ORIGINAL sidecar (sub_plan.sidecar_path)
                    # is never a candidate.
                    if suppress_srt and remove_srt_sidecar(
                        sidecar_target, log_line=log
                    ):
                        subtitles_block["sidecar_suppressed"] = True
                        log(
                            "subtitles: .srt sidecar suppressed after a "
                            "successful embed (suppress_srt)"
                        )
                    else:
                        subtitles_block["sidecar_path"] = sidecar_target
```

**B6 — OCR (insert after the `embedded` dict :2899-2902, before the notes loop :2903):**

```python
                    # v0.4.18 (opt-in suppress_srt): the OCR sidecar sits
                    # at sidecar_target by construction
                    # (run_subtitle_ocr derives sidecar_srt_path(output));
                    # sidecar_path was set PRE-mux at :2883, so a
                    # suppressed success must POP it (truthful block).
                    # The .ocr/ diagnosis kit beside the output is
                    # retained regardless (v0.4.1 retention contract).
                    if suppress_srt and remove_srt_sidecar(
                        ocr_result.srt_path, log_line=log
                    ):
                        subtitles_block.pop("sidecar_path", None)
                        subtitles_block["sidecar_suppressed"] = True
                        log(
                            "subtitles: .srt sidecar suppressed after a "
                            "successful embed (suppress_srt)"
                        )
```

### 5.5 QSettings handlers (exact)

```python
    def _subs_suppress_srt_changed(self, checked: bool) -> None:
        """v0.4.18: persist the Mode B suppress-sidecar preference
        (QSettings house rule, verbatim _subs_replace_source_changed
        pattern). "subs_suppress_srt" is Mode B's own key — Mode A's
        ("suppress_srt") is never touched by it."""
        self._settings().setValue("subs_suppress_srt", bool(checked))

    def _suppress_srt_changed(self, checked: bool) -> None:
        """v0.4.18: persist the Mode A suppress-sidecar preference
        ("suppress_srt"); Mode B's "subs_suppress_srt" is never
        touched."""
        self._settings().setValue("suppress_srt", bool(checked))
```

---

## 6. WS3 — scratch cleanup on success (Mode B `.subs-work`)

### 6.1 Change table

| # | Module : anchor | Binding / exact signature | Change | LoC |
|---|---|---|---|---|
| 6-A1 | `vidlift/subs_job.py:45-52` | `__all__` | Add `"cleanup_subs_scratch"` (after `"replace_subs_output"`). | +1 |
| 6-A2 | `vidlift/subs_job.py` end of module (after :520) | `def cleanup_subs_scratch(output_path: str, workdir_used: str, *, log_line=None) -> list[str]:` | **New** (exact code §6.2): path-guarded, best-effort `shutil.rmtree` of the default-derived scratch dir. | +38 |
| 6-A3 | `vidlift/subs_job.py:268` (between the replace-except block and `return SubsJobResult(`) | — | Call: `cleanup_subs_scratch(output_path, workdir_used, log_line=notes.append)` + the structural comment (§6.3). Sits AFTER the replace block: `output_path` is the PRE-rename name the scratch was derived from; a failed replace still lands here (success preserved) exactly like Mode A's cleanup→replace ordering. | +7 |

Row 5-A3's docstring paragraph names the cleanup too (one paragraph, two v0.4.18 behaviors). No `run.py` involvement — Mode A's cleanup discipline is already complete (the brief's asymmetry observation).

### 6.2 `cleanup_subs_scratch` — exact code

```python
def cleanup_subs_scratch(
    output_path: str, workdir_used: str, *, log_line=None
) -> list[str]:
    """v0.4.18 (Karen K-5): remove the ``{output}.subs-work`` scratch
    directory after a SUCCESSFUL subs job — the Mode B counterpart of
    run.cleanup_run_artifacts (v0.4.7), with the same three guardrails
    held as structure, not convention:

    - success-only: the sole call site sits on the verified-success
      path (post-mux, post-replace); every raise path (probe, guard,
      transcription, mux, cancel) skips it, so the scratch survives for
      diagnosis, mirroring Mode A's failure/cancel retention;
    - best-effort: an OSError logs a WARNING and the success stands —
      cleanup never fails a job and never re-raises;
    - path-guarded: ONLY the exact default-derived
      ``output_path + ".subs-work"`` directory is ever deleted
      (normcase/abspath identity). A caller-supplied ``workdir`` is the
      caller's own directory and is NEVER touched here.

    ``output_path`` is the PRE-replace ``{stem}.subs.mp4`` path — the
    scratch name never follows a replace-source rename. Returns the
    removed paths ([] when nothing was removed).
    """
    default_scratch = output_path + ".subs-work"
    if os.path.normcase(os.path.abspath(workdir_used)) != os.path.normcase(
        os.path.abspath(default_scratch)
    ):
        return []  # caller-managed workdir — the path guard
    scratch = Path(default_scratch)
    if not scratch.is_dir():
        return []
    try:
        shutil.rmtree(scratch)
    except OSError as exc:
        if log_line is not None:
            log_line(
                f"WARNING subs scratch cleanup could not remove "
                f"{scratch}: {exc}"
            )
        return []
    if log_line is not None:
        log_line(f"cleanup subs scratch: {scratch}")
    return [str(scratch)]
```

### 6.3 Call-site block (spec row 6-A3, exact)

```python
    # v0.4.18 (Karen K-5, success-only): the .subs-work scratch dir is
    # Mode A's cleanup_run_artifacts counterpart. This line is reached
    # only past a successful mux (and any requested replace); every
    # failure/cancel above retains the scratch for diagnosis.
    # workdir_used was captured PRE-rename; the helper's path guard
    # deletes only the default-derived directory.
    cleanup_subs_scratch(output_path, workdir_used, log_line=notes.append)
```

---

## 7. WS4 — QGroupBox title position + F-1 exhaustiveness

### 7.1 Change table

| # | Module : anchor | Change | LoC |
|---|---|---|---|
| 7-A1 | `vidlift/gui.py:1483` | One token: `margin-top: 14px` → `margin-top: 20px` in the `QGroupBox#Controls` rule. The `::title` rule (:1484) is **byte-identical**. | ±0 (1 token) |
| 7-A2 | `vidlift/gui.py:1472` (above `self.setStyleSheet(`) | Comment capturing D12 (mechanics, the rejected negative-`top` branch, the ±2 px tuning band, the 4-7 px clearance criterion, "verify both groups on the real Windows Segoe UI build; Linux offscreen cannot reproduce the metric"). | +6 |
| 7-A3 | `tests/test_v0418_ui_polish.py` (new) | See §12 file D. | +130 |

No structural change, no geometry change beyond +6 px of margin per group (+12 px total height against T5's ≤725 ceiling — re-measured by the gate; v0.4.17.0's window min height is verified healthy against it by `test_v0417_ui_restructure.py` today, and 12 px is inside its slack; if Windows metrics disagree, the D12 tuning band is the adjustment surface).

---

## 8. Version + release hygiene

| # | Module : anchor | Change | LoC |
|---|---|---|---|
| 8-1 | `vidlift/__init__.py:9` | `__version__ = "0.4.18.0"` | 1 |
| 8-2 | `pyproject.toml:3` | `version = "0.4.18.0"` | 1 |
| 8-3 | `packaging/version_info.txt` | `filevers=(0, 4, 18, 0)` (line 3), `prodvers=(0, 4, 18, 0)` (line 4), `StringStruct('FileVersion', '0.4.18.0')`, `StringStruct('ProductVersion', '0.4.18.0')` — ALL FOUR values, the test_version_stamps gate's parity rule. | 4 |
| 8-4 | `CHANGELOG.md` above line 5 | New `## [0.4.18.0] - <release date>` section: **Added** — recursive Add Folder for both modes incl. drop parity (symlink-loop-proof shared collector; MAX_FILES tree guard; MAX_GUI_BATCH alignment 64→500 named as a Changed bullet); opt-in `suppress_srt` across its six surfaces (GUI×2/CLI×2/MCP×2) with the fail-open fallback promise; Mode B `.subs-work` success cleanup (K-5). **Changed** — Mode B group gains two controls (F-1 pin now asserts the five); QGroupBox titles floated (margin 14→20). **Notes** — no burn-in option exists or is planned (the v0.4.15 strip stands); run.py's only changes are the six enumerated WS2 touch sets. | +30 |
| 8-5 | Docs sweep: `README.md:54` (Add Folder description gains "including subfolders"), `README.md:171` (sidecar deliverable gains the opt-in suppression mention), `docs/usage.md:49` (same), `docs/cli.md:93` region (`vidlift subs` gains `--suppress-srt` and the folder/sidecar paragraphs), `docs/architecture.md` (Mode B scratch lifecycle: `.subs-work` now removed on success) | Surgical sentences only; `docs/designs/*` are frozen history — untouched. | ~12 |

**Estimated totals:** source + CHANGELOG/docs ≈ **300 LoC**; tests ≈ **760 LoC** (§12).

---

## 9. Implementation milestones (Forge)

| Milestone | Content (rows) | Exit gate |
|---|---|---|
| **M1 — WS1 core+surfaces** | 4-A1…4-A11 | `pytest tests/test_v0418_recursive_folder_add.py -x` green; `test_gui.py`, `test_v0414_mode_b_gui.py`, `test_v04151_subs_guard.py`, `test_client_local.py` green **unmodified** except the 4-A6/4-A7 harness ripples NONE (no harness change in M1) |
| **M2 — WS2/WS3 engines** | 5-A1…5-A7, 5-B1…5-B6, 6-A1…6-A3 + the `SubsJobResult` docstring note | `pytest tests/test_v0418_subs_suppress_cleanup.py tests/test_v0418_mode_a_suppress_srt.py -x` green; `test_v0414_subs_job.py`, `test_v0417_subs_replace_source.py`, `test_run.py`, `test_subtitles_v041.py` green **unmodified**; `git diff vidlift/run.py` contains ONLY the six enumerated touch sets |
| **M3 — WS2 plumbing** | 5-C1…5-C7, 5-D1…5-D6 | + `test_mcp_bridge.py`, `test_cli_v049.py`, `test_v04154_cli.py` green unmodified |
| **M4 — GUI options + WS4 + release** | 5-E1…5-E6, 7-A1…7-A2, 8-1…8-5, plus the enumerated test amendments (§12 files E/F) and new files D | full suite green incl. `test_version_stamps.py`; the amended v0.4.17/v0.4.14 files green |

Coding conventions: house style throughout — versioned comments (`v0.4.18`), keyword-only additions only, absent-defaults on every option surface, `os.path.normcase(os.path.abspath(...))` for path identity, lazy imports wherever a `run.py`↔sibling circularity exists (none needed here — `remove_srt_sidecar` comes from `subtitles.py`, already imported at module level by both consumers), UTF-8, no new third-party dependencies, no `api_version` change.

---

## 10. Exact strings catalog (stable text Karen can assert)

- **S-1** (MCP `run`/`transcribe` schema comments + tool docstring arg list): option name `suppress_srt` — "v0.4.18 (opt-in sidecar suppression): absent -> False (the absent-defaults rule); when True the redundant .srt sidecar is removed ONLY after the subtitle track is embedded/muxed successfully — a fail-open or mux failure keeps it as the fallback deliverable."
- **S-2** Mode B suppression success note: `srt sidecar suppressed after a successful mux`
- **S-3** Mode A folder tooltip: `Add recognized videos from a folder, including every subfolder (recursion is always on — a whole show at once).`
- **S-4** folder tree guard (both modes): `That folder tree holds {n} videos (subfolders included); add at most {MAX_FILES} at once.`
- **S-5** Mode B folder button tooltip: `Add recognized videos from a folder, including every subfolder — a whole show (all seasons) queues at once.\n\nOutputs land beside each source file, so the folder structure is preserved; the same per-run options (replace original, suppress .srt) apply as for picked files.`
- **S-6** drop guard: `That drop holds {n} videos (subfolders included); add at most {MAX_FILES} at once.`
- **S-7** Mode A suppress tooltip: `After subtitles are safely embedded in the finished MP4, do not keep the .srt sidecar beside the output — the toggleable track inside the MP4 is the deliverable.\n\nWhenever subtitles fail open (no embed), the sidecar that was written is kept as the fallback, no matter how this option is set.`
- **S-8** Mode B suppress tooltip: `After the subtitle track is safely muxed into the MP4, delete the redundant .srt sidecar — the toggleable track inside the MP4 is the deliverable.\n\nIf the mux fails, the .srt is kept as the fallback deliverable no matter how this option is set. Applies to Add Subtitles only — the upscale panel has its own sidecar option.`
- **S-9** helper warning (both consumers): `WARNING suppress-srt: could not remove {path}: {exc}`
- **S-10** `TOOL_DESCRIPTIONS["transcribe"]` appended sentence: `With the optional suppress_srt flag the redundant .srt sidecar is removed after a successful mux (a mux failure keeps it as the fallback); absent means keep — pre-0.4.18 callers are behavior-identical.`
- **S-11** `run --suppress-srt` help: `after subtitles are embedded in the verified output, delete the redundant .srt sidecar; if the embed fails open the sidecar is kept as the fallback (default: keep the sidecar)`
- **S-12** `subs --suppress-srt` help: `after the subtitle track is muxed into the MP4, delete the redundant .srt sidecar (on a mux failure the sidecar is kept as the fallback; default: keep the sidecar)`
- **S-13** CLI `carried` arm under suppression: `subtitles: carried (mov_text embedded; .srt sidecar suppressed)` (else arm unchanged: `subtitles: carried (mov_text embedded + .srt sidecar)`)
- **S-14** CLI `ocr` arm under suppression: `  sidecar: suppressed after a successful embed` (in place of the `sidecar: {path}` line when `sidecar_suppressed` is set)
- **S-15** Mode A log lines: `subtitles: .srt sidecar suppressed after a successful embed (suppress_srt)` and `subtitles: sidecar extract skipped — the track is embedded in the encode (suppress_srt)`
- **S-16** cleanup success note: `cleanup subs scratch: {scratch}`; cleanup warning: `WARNING subs scratch cleanup could not remove {scratch}: {exc}`
- **S-17** Mode B folder dialog title: `Choose a folder of videos to add subtitles to` (Mode A's `Choose a folder` unchanged)
- **S-18** checkbox labels (both): `Suppress .srt sidecar on success`; Mode B folder button: `Add Folder`

---

## 11. Failure analysis

| Failure | Behavior |
|---|---|
| Symlink loop inside a picked tree | Impossible by construction (`followlinks=False`); the symlinked dir is never descended; symlinked video *files* dedupe to one entry |
| >500 videos in a recursive add/drop | Named warning (count + 500 + "subfolders included"); nothing enqueued/added; no silent truncation |
| Unreadable subdir mid-tree | `os.walk` skips it silently; siblings still queue; job-level probe reports honestly at run time |
| Suppress ON + Mode B mux failure | `mplex` raises before suppression runs; `.subs.srt` kept; job fails honestly with the existing error text |
| Suppress ON + Mode A any-branch fail-open | Shared handler untouched; sidecar kept; `"embed failed; sidecar kept next to the output"` fires iff a file exists |
| Suppress removal raises OSError (AV scan, lock) | Helper logs S-9, returns False; sidecar stays; `sidecar_path`/result.srt_path report it; job/run still succeeds |
| Suppress ON + replace ON (Mode B) | Removal happens first; replace's sidecar step no-ops via its `isfile` guard; output renames normally; result paths honest (`srt_path == ""`) |
| `cleanup_subs_scratch` OSError (locked file inside) | S-16 warning note; success stands; directory leaks (the pre-0.4.18 state — strictly non-worse) |
| Custom `workdir` supplied (tests/scripted callers) | Path guard refuses: nothing deleted, `[]` returned |
| Cancel during transcription / pre-replace | Scratch retained (raise paths skip cleanup); sidecar kept iff written |
| Pre-0.4.18 registry records / MCP clients | Every new option reads absent → False → byte-equivalent v0.4.17 behavior; schema still rejects unknown fields |
| GUI state dir full at enqueue of a 500-file tree | `OSError` rides the existing F-13b catch → dialog; partial enqueues impossible (registry atomicity) |
| Windows: title metrics differ from the tuned value | D12 band check; only `margin-top` ±2 px adjustments permitted without spec revision |

No new single points of failure; every new mutation is either additive-removal of a this-run-created artifact or guarded against non-scratch paths.

---

## 12. Combined test plan

Conventions: harnesses reuse `tests/test_v0414_subs_job.py` (fake engine factory, fake `runner` manufacturing the remux/output), the `test_v0414_mode_b_gui.py` offscreen/isolated-QSettings harness, and the `test_run.py` fake-runner fakes. No real ffmpeg/whisper/network/GPU anywhere. Symlink tests `pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")` where creation is required.

### File A (new, ~250 LoC): `tests/test_v0418_recursive_folder_add.py`

**Group 1 — `collect_video_files` (pure, tmp_path):**
1. Nested tree (`show/Season 1/e1.avi`, `show/Season 2/e2.mkv`, top-level `pilot.mp4`) → all three, fully **sorted**, full paths.
2. Uppercase extension `.MP4` + mixed `.Ts`; non-videos (`.srt`, `.nfo`, `.txt`) skipped.
3. Empty dir / videos-absent tree → `[]`.
4. Symlinked directory `seasons -> real` PLUS the real dir: contents appear exactly once (realpath dedupe), walk terminates (the loop-safety proof: `a/link -> ..` self-loop terminates).
5. Two symlink *files* → one inode: exactly one entry, and the survivor is the lexicographically first name (sort-then-dedupe order proof).
6. Direct-child-only folder == the pre-0.4.18 `iterdir()` result byte-for-byte (regression identity).

**Group 2 — Mode A `add_folder` (offscreen window, dialog monkeypatched):**
7. Nested real tree → `_add_paths` receives the full sorted recursive list.
8. `collect_video_files` monkeypatched to 501 names → S-4 warning asserted (`"501"`, `"500"`, `"subfolders included"`); `_add_paths` never called. 500 → passes (boundary).

**Group 3 — Mode B `choose_subs_folder`:**
9. Nested tree → `_ScriptedClient` receives all paths in sorted order as ONE `enqueue_subs` call; subs rows render one per nested file (structure preserved in the Source column).
10. 501-name guard → warning; client never called.
11. Empty folder → silent no-op (no dialog, no enqueue).
12. Live-conflict: one nested file collides with a scripted running subs job → the v0.4.15.1 block dialog, nothing enqueued.
13. DropEvent directory arm over a REAL nested folder → every nested video lands in `_add_paths`.

### File B (new, ~360 LoC): `tests/test_v0418_subs_suppress_cleanup.py`

**Group 4 — `run_subs_job` suppression (fake engine + fake runner):**
14. Default OFF → behavior byte-identical to v0.4.17: `.subs.srt` beside output, `result.srt_path` points at it, no S-2 note (the option-OFF invariant).
15. ON → mux ok: `.subs.srt` GONE, mp4 present, `result.srt_path == ""`, S-2 in notes, the fake runner's mux argv still contained the srt path (mux happened before removal).
16. ON + mux failure (fake runner exits nonzero) → `SubsJobError`; `.subs.srt` KEPT; no suppression happened.
17. ON + removal OSError (monkeypatch `os.remove` to raise on the srt path only) → success stands, sidecar present, `result.srt_path` honest truth, S-9 in notes.
18. ON + `replace_source=True` → mp4 at `{stem}.mp4`, NO `.srt` anywhere, source deleted, `result.srt_path == ""`, replace narrated; replace helper's sidecar step demonstrably a no-op.

**Group 5 — `cleanup_subs_scratch` + integration:**
19. Happy run (default workdir) → `{stem}.subs.mp4.subs-work/` created by the fake transcribe seam, GONE after the job; S-16 success note in notes.
20. Custom `workdir=tmp_path/"mine"` → dir survives intact (path guard), no note.
21. Mux failure → scratch retained; cancel mid-transcribe (`should_cancel` True) → `EngineCancelled`, scratch retained.
22. Replace ON: scratch named off the PRE-rename output removed after the rename lands.
23. rmtree OSError (monkeypatch `shutil.rmtree`) → success stands, S-16 warning note, dir remains.

**Group 6 — plumbing:**
24. Schema: `validate_tool_args("transcribe", {"paths": ["x"], "suppress_srt": True})` accepts; `"yes"` → `InvalidInput(field="suppress_srt")`; absent validates; `run` options accept/reject identically.
25. `tool_transcribe` options carry the flag both ways; `_execute_subs_job` forwards `bool(options.get(...))` (patch-spy, incl. a legacy record with no key → False); `_execute_run_job` forwards to `run_file`.
26. `client_local.enqueue_subs(..., suppress_srt=True)` writes the option; no-kwarg call writes False.
27. argparse: both parsers default False, flags flip True (the `test_cli_v049.py:276-293` pattern); `_cmd_subs`/`_cmd_subs` thread-through assert via patched `run_subs_job`; `_cmd_run` identical.

### File C (new, ~230 LoC): `tests/test_v0418_mode_a_suppress_srt.py`

(Fake Mode A runs reusing `test_run.py`'s harness/fakes.)
28. Transcribed branch ON: mux ran, sidecar removed, manifest `subtitles.sidecar_suppressed is True`, NO `sidecar_path`; OFF: `sidecar_path` present (unchanged default).
29. Embedded-text branch ON: the fake runner records NO `-c:s srt` extract command at all; status `"carried"`; `embedded` dict present; suppressed flag set.
30. Adopted sidecar ON: the run's copy `{output-stem}.srt` removed; the USER's original sidecar beside the SOURCE is byte-identical (never a candidate).
31. OCR branch ON (fake OCR seam): sidecar removed post-mux; `.ocr/` kit retained; OFF unchanged.
32. Mux failure + ON (fake runner fails `mux_srt_into_mp4`) → fail-open: sidecar exists, `sidecar_kept` True, the kept-warning present (fallback promise).
33. Removal OSError + ON → success, sidecar present, `sidecar_path` set, S-9 in the log.
34. CLI print honesty: `carried`+suppressed prints S-13; `ocr`+suppressed prints S-14; default runs print the unchanged v0.4.17 lines.

### File D (new, ~130 LoC): `tests/test_v0418_ui_polish.py`

35. QSS string assertion: the applied stylesheet contains `QGroupBox#Controls {` with `margin-top: 20px` and the `::title` rule retains `top left` + `left: 12px` (no negative `top:` anywhere).
36. **F-1 exhaustiveness:** `subs_grid.count() == 5`; membership == exactly {`add_subs_button`, `subs_add_folder_button`, `subs_replace_source`, `subs_suppress_srt`, the explainer `QLabel`}; cells == (0,0,1,1)/(0,1,1,1)/(0,2,1,1)/(0,3,1,1)/(0,4,1,1).
37. Both new checkboxes: default OFF, persisted round-trip on fresh window, **key isolation** (`subs_suppress_srt` ↔ `suppress_srt` ↔ the two replace keys cross-untouched), tooltips contain "fallback" (S-7/S-8 contract words); the two new controls paint real pixels (the T2 hide-and-grab probe imported from `test_v0417_ui_restructure`).
38. `start_upscale` options source-fence gains `"suppress_srt": self.suppress_srt.isChecked()`; `_enqueue_subs_paths` source carries `suppress_srt=self.subs_suppress_srt.isChecked()`.

### File E (amended): `tests/test_v0417_ui_restructure.py`

- T1: `ALL_MODE_A_CONTROLS` += `"suppress_srt"`; the Mode B tuple += `"subs_add_folder_button"`, `"subs_suppress_srt"`.
- T2: paint tuple += the two new checkboxes.
- T3: parentage pins for both new controls (Mode A checkbox in the upscale group; the Mode B pair in the subs group).
- T4: re-pin the subs cell map per File D-36 (the `(0,1,1,1)` replace assertion becomes `(0,2,1,1)`); upscale pin += `suppress_srt → (3,6,1,3)`. Zero-overlap re-runs by construction.
- T9: re-target the Mode B fence from `choose_subs_files` to `_enqueue_subs_paths` (the 4-A6 extraction; assertion text extended with `suppress_srt`).
- T5: gates re-run; if the +12 px height pushes minimum size past a gate, that is D12 tuning-band feedback — record the new measurement, adjust margin within ±2 px, keep the pin.

### File F (amended): `tests/test_v0414_mode_b_gui.py` (harness only)

`_ScriptedClient.enqueue_subs(self, paths, *, replace_source=False, suppress_srt=False)` — defaulted kw + `self.last_subs_suppress_srt = bool(suppress_srt)`. Every existing scripted caller passes neither → all importers (`test_v04151_subs_guard.py`, `test_v0417_ui_restructure.py`) green unmodified.

### Unmodified-pass invariants (must hold with zero edits)

`tests/test_run.py`, `test_v0414_subs_job.py`, `test_v0414_mode_b_gui_executor.py`, `test_v04151_subs_guard.py`, `test_v04153_subs_bitmap_remux_slow.py`, `test_v04152_cuda_fallback.py`, `test_v04154_cli.py`, `test_v04154_transcribe_core.py`, `test_cli_v049.py`, `test_mcp_bridge.py`, `test_client_local.py`, `test_gui.py`, `test_v0417_subs_replace_source.py`, `test_v0416_*`, `test_version_stamps.py` (post 8-1…8-3 stamps). Any required edit = spec violation → escalate.

---

## 13. Validation baseline for Karen (expand freely)

**Acceptance criteria:**

1. **WS1 recursion, both modes + drop:** a nested show tree (`Show/Season 1/*.avi`, `Season 2/*.mkv`) added through (a) Mode A Add Folder, (b) Mode B Add Folder, (c) drag-drop queues EVERY episode, fully sorted, each output landing beside its own source (nested structure preserved end-to-end: `Season 2/e2.subs.mp4` lives in `Season 2/`). A shallow folder yields exactly the pre-0.4.18 result. Symlinked dirs cannot loop (construct `link -> ..` live on Windows if privileges allow; otherwise the unit pin stands). 501+ recursive videos → the S-4 warning names the count and refuses wholesale; nothing partially added. A 100-file Mode B folder add enqueues successfully (the MAX_GUI_BATCH alignment — previously refused).
2. **WS2 defaults:** with no flags set anywhere, a fresh-profile GUI/CLI/MCP run is byte-equivalent to v0.4.17 — sidecars written and kept in both modes, all four Mode A branches (prove transcribed + adopted + embedded via fakes/samples; OCR branch may be asserted through the harness; a real bitmap-sample OCR run is a nice-to-have).
3. **WS2 suppression ON:** Mode B — `.subs.mp4` exists with the mov_text track (ffprobe shows the subtitle stream) and NO `.srt` beside it; notes carry S-2; result/history `srt_path` is empty. Mode A — transcribed and adopted branches verified live with a sample: output carries the embedded track, no sidecar, manifest carries `sidecar_suppressed: true` and no `sidecar_path`. Embedded-text sample: no extract command ran at all.
4. **WS2 fallback promise (the load-bearing one):** force a mux failure under suppression (harness: fake runner; live: an exotic source codec suffices in Mode B) — the `.srt` is on disk afterward, flag or no flag; Mode A fail-open wording unchanged. Combined suppress+replace: single `{stem}.mp4`, no sidecar, no orphan renames, source gone, job succeeded.
5. **WS3:** after any successful Mode B job the `.subs-work` directory is gone (default, suppression, and replace postures all three); after a failed job and after GUI cancel it is PRESENT with its scratch (diagnosis retained); cleanup failure (hold a handle inside it on Windows) → job still succeeded + S-16 warning visible in the details/notes surface.
6. **WS4:** on the real Windows build (Segoe UI), both group titles float visibly above the border line — acceptance band 4-7 px of clearance at 100 % and 125 % display scale; title still clear of the first control row in BOTH the 4-row Upscale group and the single-row Mode B group; window min-width within T5's gate; no clipped title pixels (the D12 negative-top rejection is load-bearing here).
7. **Surfaces parity:** CLI `vidlift subs --suppress-srt` and `vidlift run --suppress-srt` behave identically to the GUI checkboxes; MCP `transcribe{suppress_srt: true}` and `run{options:{suppress_srt: true}}` likewise; `api_version` still 1; `tools/list` shows the two additive booleans; a stale pre-0.4.18 client omitting the field runs v0.4.17-identical.
8. **Persistence/isolation:** both new checkboxes survive restart; four keys (`replace_source`, `subs_replace_source`, `suppress_srt`, `subs_suppress_srt`) cross-isolated.
9. **Hygiene:** `pytest` full suite green; `git diff vidlift/run.py` shows ONLY the six §5-B touch sets; version stamps all read 0.4.18.0; CHANGELOG `[0.4.18.0]` present; docs sentences landed.

**Edge cases to probe beyond §12:** a tree whose nested dir name contains unicode/spaces; a tree read permission-denied mid-walk; the same show added twice (dedupe via `_add_paths`, conflict guard on second Mode B add while the first runs); replacement of a source that is itself `.mp4` under suppression (the strongest atomicity arm, v0.4.17 D2, re-verified with the new ordering); `\\server\share` UNC roots (realpath on Windows; `os.walk` over UNC); QSettings upgrade path (a 0.4.17 ini opened by 0.4.18 — new keys absent → defaults).

**Security expectations:** cleanup and suppression delete only paths derived from the current job's own output path or written by the current job this run (the adopted-sidecar case deletes the job's COPY, never the user's original); no globbing; the collector never follows symlinked directories; no privilege changes; schema boundary still rejects unknown fields.

**Performance expectations:** collector is one `os.walk` (single-digit ms per hundred files on local disk, one `realpath` syscall per video — negligible at the 500 cap); suppression is one `os.remove`; cleanup one `rmtree` of a small dir; no steady-state cost with all options OFF; recursion adds zero work for shallow folders.

---

## 14. Final review checklist

✓ Requirements satisfied — brief's four workstreams + version + invariants; every note's open decisions answered (§1 D1-D13)
✓ Evidence verified against the tree (§0; every anchor re-read on the v0.4.17.0 commit)
✓ Alternatives evaluated (D2 always-on vs toggle; D4 cap alignment options; D5 default polarity; D7 write-then-remove vs scratch-redirect — scratch-redirect rejected: it perturbs `transcribe_source_to_srt` geometry, breaks the `"subs.srt" in argv` phase-detection seam at `jobs.py:380`, and complicates the failure fallback; §6 vs leaving the leak)
✓ Tradeoffs documented (deliberate drop-path extension; MAX_GUI_BATCH alignment; embedded-branch skip-extract asymmetry)
✓ Security reviewed (§13; deletion confined to job-derived paths; collector loop-proof)
✓ Performance reviewed (§13)
✓ Observability designed (S-strings; `sidecar_suppressed` manifest fact; notes surface every mutation)
✓ Testability reviewed (pure collector, helper seams, named stable strings, harness reuse)
✓ Extensibility reviewed (suppress-by-default flip is a defaults-only change; a future Mode B output-dir keeps every guard valid)
✓ Technical debt minimized (run.py delta enumerated and minimal; shared collector/handlers; discipline duplicated only where v0.4.17 already chose duplication)
✓ Risks documented (§11)
✓ Deviations stated explicitly (§2 — six, each with rationale)
✓ Forge guidance complete (§4-§9) — one release, no double-bump
✓ Karen baseline complete (§13)

— Bob
