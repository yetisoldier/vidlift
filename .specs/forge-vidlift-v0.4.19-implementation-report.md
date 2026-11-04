# Forge Implementation Report — VidLift v0.4.19.0 follow-up batch

**Author:** Forge (implementation)
**Date:** 2026-11-04
**Spec:** `.specs/bob-vidlift-v0.4.19-implementation-spec.md` (read in full before any edit; v0.4.18.0 base, commit `ffc13fd`)
**Result:** all four workstreams implemented, one release (0.4.19.0, single bump), full suite green.
**Commits:** five per-milestone commits per spec §9 — `ef0040a` (M1/WS1), `a6a4920` (M2/WS3), `44edfb6` (M3/WS2), `46a8b9d` (M4/WS4), `8ae161e` (M5/version+CHANGELOG).

Line anchors below are CURRENT-tree positions (post-batch); the spec's anchors were v0.4.18.0 positions.

---

## WS1 (HIGH) — cancel observed during pre-transcription steps

**`vidlift/transcribe.py`**
- `:1278-1286` — (4-A1) `resolve_model_dir` signature: additive trailing `should_cancel: Callable[[], bool] | None = None` kwarg; `state_root` annotated exactly per the spec's binding signature; docstring gains the v0.4.19 paragraph (`:1300-1311`).
- `:1325-1334` — (4-A2) the entry observe, immediately after the docstring and BEFORE the `_engine_disabled_by_env()` refusal (`:1336`), message **S-1** `"cancelled during model resolution"` (a cancel is control flow and wins over the env refusal).
- `:1381-1398` — (4-A3) the armed download beats (O2, **S-2** `"cancelled during model download"`), installed ONLY when a progress channel exists (`progress is not None`) — the v0.4.16 conditional-arity rule (WS1-D3/R-5); `# noqa: F811` on the shadowing wrapper; the lazy `EngineCancelled` import pattern.
- `:2378-2391` — (4-A4) `transcribe_source_to_srt` docstring cancel-observe paragraph (the WS1 signature took no kwargs; `on_device` landed in M2 without a double edit).
- `:2451-2476` — (4-A4a) `_cancel_observe(message)` closure, inserted immediately before the `transcribe-resolve` step.
- `:2477-2490` — (4-A4b) O1 right after `on_step("transcribe-resolve")` (`:2478`, S-1) + the `resolve_model_dir` call threads `should_cancel=should_cancel` (`:2487`).
- `:2496` — (4-A4c) O3 `"cancelled during model load"` (S-3) immediately after engine construction, before the `download_log` block.
- `:2502-2508` — (4-A4d) O4 `"cancelled before language detection"` right after `on_step("transcribe-language")` (`:2503`), before `detect_language`; O5 `"cancelled during language detection"` immediately after it returns (`:2507`), before any `transcribe-run` work.
- `_on_segment` (now `:2510-2519`, message `cancelled during transcription` at `:2518`) — **verbatim, untouched** (WS1-D2).

**`vidlift/run.py`**
- `:2735-2747` — (4-A6, enumerated run.py touch 1 of 3) Mode A's own pre-resolve `resolve_model_dir(...)` gains `should_cancel=cancel` (`:2745`) + the v0.4.19 comment. `cancel` is the `run_file` parameter already forwarded at `:2779` (`should_cancel=cancel` on the core call — pre-existing, untouched).

**No change:** `subs_job.py` (4-A5 — `should_cancel` was already forwarded), `mcp_bridge/jobs.py` mappings `:271-281/:495-496`-equivalents (4-A7 — regression-pinned instead), the post-mux tail (`subs_job.py` `cancelled after remux, before replace`).

**Tests:** File A `W1a` T1–T7 (`tests/test_v0419_followup_batch.py:121-468`) — entry precedence + factory-never-called (T1), post-load raise with engine counters (T2), pre-detect raise (T3), post-detect raise with NO rebuild (T4, default fallback-wrapper path driven), the untouched `_on_segment` gate (T5), the download-beat arm + both arity pins (T6a/T6b/T6c), and the golden-path drift guard (T7). File A `W1b` T8–T10 (`:469-658`) — Mode B end-to-end wedge→cancel (S-3, never `failed`), Mode A end-to-end wedge→cancel (escapes the fail-open conversion), and the `_execute_subs_job`/`_execute_run_job` EngineCancelled→`JobOutcome(cancelled=True)` mapping pins. `tests/test_v0416_cancel_propagation.py` untouched, green.

---

## WS2 — plan-time "already has subtitles / output exists" → SKIP

**`vidlift/subs_job.py`**
- `:50` — (5-A1) `from .probe import ProbeError, classify, probe_file`.
- `:62-76` — (5-A2) `__all__` + the four verdict constants.
- `:148-205` — (5-A3) the four `SUBS_VERDICT_*` constants (`:154-160`) + `subs_plan_verdict(path, *, ffprobe="ffprobe", probe_runner=None, force=False)` (`:163`): output-exists FIRST and probe-free; `force` bypasses only the subtitle check; `ProbeError`/`OSError` fail OPEN to `probe-error`; the `source_has_usable_subtitles` predicate unchanged.
- Run-side guards (`:263-273` region — content unchanged; 5-A4 absence-of-change is the design) and `_on_segment`/post-mux tail: **untouched**.

**`vidlift/gui.py`**
- `:89-99` — (5-A5) imports extended with the four constants + `subs_plan_verdict`; module-header note rides the existing F-5 comment block.
- `:143-156` — (5-A6) collector exclusion: `if name.lower().endswith(".subs.mp4"): continue` inside the walk loop (case-insensitive, mp4 family only; `*.subs.mkv` stays collectible); docstring gains the v0.4.19 sentence. Shared by both modes' Add Folder and the drop handler (`gui.py:3902`).
- `:368-380` — `_subs_enqueue_summary(queued, skipped, blocked)` — new **module-level** pure helper, beside `_stage_label_text` (S-7 compose; extras clause omitted when zero).
- `:1841-1917` — (5-A7) `_subs_file_rows(self, paths, verdicts)` — `verdicts` is a REQUIRED argument; per-verdict rows; skip/blocked cells byte-identical to v0.4.18 (S-5/S-5a/S-6); probe-error discloses `"plan probe inconclusive — re-checked at run time"` in col 7 while remaining ready (fail-open).
- `:2021-2134` — (5-A8/5-A12) `_enqueue_subs_paths`: pre-flight block first (find_tools degrade → `"ffprobe"`, per-path `subs_plan_verdict`), ready/skipped/blocked partition, conflict guard over `ready` ONLY (deviation 3), enqueue `ready` only (`if ready:` guard so an all-skipped pick never call enqueue), rows ALWAYS render BEFORE the `not fresh_ids` early return (stable partition ready→skip→blocked), no-slate/timer for the no-work case with the composed S-7 summary + the exact no-work note, live-pending merge/slate extend/bootstrap verbatim, final summary through `_subs_enqueue_summary`.

**`vidlift/cli.py`**
- `:406-415` — (5-A9) lazy import extended to `SUBS_VERDICT_BLOCKED_OUTPUT, SUBS_VERDICT_SKIP_SUBTITLES, SubsJobError, run_subs_job, subs_output_path, subs_plan_verdict`.
- `:421-436` — (5-A10) pre-flight right after `print(f"== {path}")`: skip/blocked print the exact S-8 lines and `continue` — neither counts toward `failures` (WS2-D9; all-done batch exits 0); `force=True` threaded in (`:428`); probe faults enqueue to the run-side truth.

**`vidlift/mcp_bridge/tools.py`**
- (5-A11) `tool_transcribe` — docstring verdict paragraph `:774-777`; the loop-head lazy `subs_job` import `:788-792`; the pre-check after the isfile arm `:802-821`: additive `skipped/reason` rows with `job_id: null, ok: true` (S-9 reasons verbatim); force bypasses only the subtitle check. api_version untouched.

**Tests:** File A `W2` T11–T18 (`tests/test_v0419_followup_batch.py:968-1402`): the verdict matrix incl. zero-probe pins (T11, `:1006`), the collector case/case+family pins (T12), the GUI pick (T13), all-skipped static rows + S-7 + detail verdict (T14), ready-only conflict guard (T15), CLI exit semantics + `--force` spy (T16), MCP additive rows (T17), and the run-side backstop sameness pin (T18, `:1368`). `tests/test_v0414_subs_job.py` guards: green, **unedited**.

---

## WS3 — CPU/GPU device indicator (both modes)

**`vidlift/transcribe.py`**
- `:2357-2367` — (6-A1) trailing `on_device: Callable[[str, str], None] | None = None` kwarg (applied in the same pass as WS1's docstring edit, per §9 M2) + docstring paragraph (`:2404-2411`).
- `:2440-2445` — (6-A2) fire site 1 — `(resolved, device_pin)` the moment the core resolves (default-factory path only).
- `:2450-2455` — (6-A3) fire site 2 — `("cpu", device_pin)` inside `_default_on_fallback` after `fallback_notes.append(note)`. A foreign factory NEVER fires (D4 honest-unknown).

**`vidlift/subs_job.py`**
- `:236-239` — (6-A6) trailing `on_device=None` kwarg on `run_subs_job`; `:333` — forwarded verbatim as the core call's last kwarg.

**`vidlift/mcp_bridge/jobs.py`**
- `:456-468` — (6-A7) the `on_device(device, requested)` publisher closure (after `on_step`, publishes `progress["transcription_device"]`/`["transcription_device_requested"]`, touches state + record); `:508` — the `run_subs_job` call gains `on_device=on_device`. The mirror (`:777-802` region) persists the keys with zero changes.

**`vidlift/run.py`** (enumerated touches 2 and 3 of 3)
- `:2679-2686` — (6-A4) `progress["transcription_device"]` + `["transcription_device_requested"]` published immediately after `resolve_transcription_device`, pre-download/pre-construction.
- `:2652-2660` — (6-A5) the mutated `transcribe_device` (R1 wording `cpu (cuda unavailable)` at `:2651`, untouched) republished inside `_on_cuda_fallback`, before the notes append; `nonlocal` + R1 rule untouched.

**`vidlift/mcp_bridge/tools.py`**
- `:429-511` — (6-A8) `_transcription_echo` live-first rewrite: same-process `state.progress` keys → running-only `progress_snapshot` mirror (D-12) → the intact v0.4.15.3/16 result/manifest arms; echo key SET unchanged (`{"device", "device_requested"}` — values-only, so `api_version` stays 1 and T1–T6 resolve byte-identically when the keys are absent).

**`vidlift/gui.py`**
- `:383-424` — (6-A9) `_device_display(snapshot)` — the ONE wording rule (S-10 matrix incl. the `…` U+2026 resolving form and foreign-string-verbatim).
- `:426-444` — (6-A9) `_with_device_suffix(label, snapshot)` — suffix only for `transcribe`/`subtitles`/`subs-mux` phases AND only with a resolved fact (S-11).
- `:496-506, :570-595` — (6-A10) `run_stage_label`: the two download early-returns wrap through `_with_device_suffix`; the label tail restructured to assign at the three exits ending in `return _with_device_suffix(label, snapshot)`. Queued/terminal early-returns precede the tail, so they never gain a suffix; without the `transcription` fact the label is byte-identical to v0.4.18 (pinned by T22 + the untouched stage-label suites).
- `:2436-2441` — (6-A11) the LIVE detail overlay gains `Device: …` between the `Job:` line and the `Error:` arm.

**No change:** `vidlift/monitor.py` (6-A12 — pass-through inherits).

**Tests:** File A `W3` T19–T23 (`tests/test_v0419_followup_batch.py:660-966`): the core seam publication order + foreign-factory D4 pin (T19, `:661`), the echo live-first arms incl. mirror/dead-writer (T20), the `_device_display` pure matrix (T21, `:790`), the stage-label suffix gates incl. the download arm and absence-of-fact identity (T22), and the overlay lines for running/queued/absent (T23, `:885`). `tests/test_v0416_transcription_echo.py` untouched, green (all six T-rows + T-5b).

---

## WS4 — test-coverage gaps (F-1, F-7, D-2)

- **F-1** — `tests/test_v0418_subs_suppress_cleanup.py:26-28` (docstring group-5 line gains the test-24 entry) and `:350-385` — `test_24_stale_default_scratch_survives_custom_workdir` appended directly after test_23, exact spec code (stale default-named scratch survives a custom-workdir success; no cleanup note; direct helper call returns `[]`; dir survives). Mutation-verified: removing the cleanup path guard FAILS this test (checked live, then restored byte-identical).
- **F-7** — `tests/test_client_local.py:124-155` — `test_client_local_hundred_file_subs_batch_enqueues` appended after the cap test, exact spec code. Mutation-verified: `MAX_GUI_BATCH = 64` FAILS the pin (checked live, then restored byte-identical).
- **D-2** — `tests/test_v0417_failpath_fix.py`:
  - `:42-46` — module docstring gains the D-2 decision line after the "pins the fixed journey" paragraph.
  - `:247-250` — `from test_v0414_mode_b_gui import (  # noqa: E402  (D-2: house harness) _ScriptedClient, _window )` after the `MainWindow` import.
  - Deleted: the module-local `_alive` (v0.4.18 `:252`), local `class _ScriptedClient` (`:255-359`), and local `_window` generator (`:362-388`) — **−115 LoC of drift-prone duplicate**; `qapp`, `_PROBE_FAILURE`, `_PROBE_ERROR`, `_report`, `_plan_for`, `_row_state`, and the three bridge/transport tests kept; **zero assertion changes** in any v0417 test. Compatibility verified mechanically per §7.4: shared `enqueue_run` (no exhaustion assert — never triggered), shared `enqueue_subs` accepts the kwargs, shared `cancel_batch` appends via `script_tail` (consumed inside the queued-cancel test's 4-tick window; the GUI ignores the return map), shared `_window` also no-ops `time.sleep` (harmless).

---

## Deviations & judgment calls (vs Bob's spec — each intentional, none architectural)

1. **File E under-enumeration — two extra mechanical amendments.** The spec's File E listed exactly two `test_v0414_mode_b_gui.py` amendments, but the new REQUIRED `verdicts` parameter on `_subs_file_rows` (5-A7) also broke two further one-arg call sites in that same file: `test_subs_rows_collision_preflight_fires_for_non_4char_extensions` (now `:585-605`) and `test_subs_file_rows_column3_tracks_submitted_state` (now `:1098-1111`). Both were amended minimally and mechanically: explicit verdict maps (`SUBS_VERDICT_BLOCKED_OUTPUT` / `SUBS_VERDICT_READY`), the row-content assertions unchanged, docstrings updated with a one-line WS2 note. The spec's own contract ("``verdicts`` is a REQUIRED argument: a row must never be rendered from a verdict the pre-flight didn't make") admits no default, so this was the only suite-green path; the amendment stays inside File E (the designated amendment file). **Not** a stop-and-report trigger under §12 (File E is not on the unmodified-pass list), but Karen should treat the two extra call sites as spec-enumeration breadth, not content, drift.
2. **`_subs_enqueue_summary` call sites are bare-name, not `self.`** — the spec's 5-A8 flow code shows `self._subs_enqueue_summary(...)`, but the helper itself is specified module-level ("beside `_stage_label_text`, pure — Karen-testable without Qt"). Module-level requires bare calls; the `self.` form would AttributeError. Implemented module-level with bare calls at both sites (`gui.py:2130`, `:2172`). Zero functional difference; flagged so Karen isn't surprised.
3. **W1a T6c resolved to the spec's pre-authorized fallback.** The full success-path pin (a fetch materializing a model.bin that passes the real sha256 pin) would require the real 1.5 GB digest; the spec explicitly scopes T6c to "the arity call-shape assertion: fetch received exactly 2 args" when size/digest pinning proves brittle. Implemented exactly that: every legacy fetch call recorded arity 2, `TranscriptionError` (the honest sha-pin refusal, never `TypeError`), and the documented junk-`model.bin` cleanup asserted.
4. **W1b T9's "same blocking factory" mechanism.** `_execute_run_job` does not forward `services.transcribe_engine_factory` to `run_file` (that seam is read by `_execute_subs_job` only — `jobs.py`), and wiring it would violate the forbidden-drift list (unenumerated jobs.py/run.py change). T9 therefore monkeypatches the core's default `make_whisper_engine_with_fallback` in-process (the executor worker shares the test process) to a blocking factory — the same blocking-at-model-load window exercised through the REAL Mode A run path, REAL claim/cancel registry API, and REAL `should_cancel=cancel` forwarding. The journey and assertions (cancelled, S-3, never failed-open) are as specced; the seam substitution is test-local only. T8 uses the spec's actual seam (`services.transcribe_engine_factory`, read via the existing `getattr` in `_execute_subs_job`).
5. **W1a T4's fallback-wrapper fake.** The core's default wrapper is monkeypatched in-process (a real wrapper needs a CUDA/model box); pins are: wrapper constructed exactly once, count-detect calls==1, steps end at `transcribe-language`, never `transcribe-run`, S-4-after message. The real `_ResilientEngine` D2 re-raise discipline stays pinned in `test_v04152_cuda_fallback.py` (untouched, green).
6. **Two comment-narration choices (byte-level, flagged):** (a) the v0.4.17/v0.4.18 checkbox-narration comments above the `enqueue_subs` call were KEPT (the spec's exact 5-A8 block elides them, but the method's own docstring promises "comments moved with the code they narrate"; the LoC delta accommodates either); (b) the `subs_plan_verdict` header comment "The run-side guards above are UNCHANGED" (spec-verbatim) — the guards actually sit BELOW the helper in the file (the helper sits at the spec's own anchor, after `source_has_usable_subtitles`). Kept verbatim; prose nit only.
7. **Milestone commits.** Spec §9 prescribes commit-per-milestone; delivered as five commits (M1-M5) with per-milestone gate results in the messages.

**Unmodified-pass invariants — all held with zero edits:** `test_v0416_transcription_echo.py`, `test_v0416_cancel_propagation.py`, `test_v0414_transcribe.py`, `test_v0414_subs_job.py` (both run-guard tests), `test_progress_honesty.py`, `test_gui.py` (incl. F-13b), `test_v0413*.py`, `test_v04151_subs_guard.py`, `test_v0418_recursive_folder_add.py`, `test_v0418_ui_polish.py`, `test_stale_activity.py`, `test_model_management*.py`, `test_mcp_bridge.py`, `test_monitor*.py`, `test_gui_log_tail_pin.py`, `test_registry_*.py`, `test_v0417_subs_replace_source.py`, `test_v0417_ui_restructure.py`, `test_v0418_subs_suppress_cleanup.py` (tests 14-23 pre-existing), `test_client_local.py` (pre-existing), `test_v04162_vad_packaging.py` — verified by targeted runs during the milestones and by the full suite.

**Forbidden-drift audit (diff-checked against `ffc13fd`):**
- `run.py`: exactly THREE hunks (4-A6 `:2735-2747`; 6-A4 `:2679-2686`; 6-A5 `:2652-2660`) — the enumerated touches.
- No `api_version` change; no QSettings keys added; no registry schema change; `MAX_GUI_BATCH`/`VIDEO_EXTENSIONS` unchanged.
- `test_v0416_transcription_echo.py` and `test_v0416_cancel_propagation.py`: 0 diff lines.
- `_on_segment`, the run-side guards (`subs_job.py`), and the post-mux tail: byte-unchanged (additive-only subs_job diff verified hunk-by-hunk).
- All-skip GUI scripted flows stay green via the fail-open probe-error arm, as the spec's File E call-out requires (deliberate; stated in the M3 commit message).

---

## Test result (exact)

```
/home/yetisoldier/.venvs/vidlift/bin/python -m pytest -o 'addopts=' -q
1068 passed, 2 skipped in 107.98 s
```

- The 2 skips are pre-existing Windows-only guards (`tests/test_gpu_meter_no_window.py:132` and `:164`), untouched by this batch.
- Per-milestone gates: M1 → File A W1a/W1b + `test_v0416_cancel_propagation` + Mode B guards + cuda-fallback + model-management: 117 passed; M2 → echo/progress-honesty/GUI/monitor/mirror: 91 passed; M3 → full suite: 1066 passed, 2 skipped; M4 → full suite: 1068 passed, 2 skipped (+51 on the four touched test files); M5 → full suite at 0.4.19.0: 1068 passed, 2 skipped. Mutation spot-checks for F-1/F-7 both bite as §13 prescribes (guards restored byte-identical afterwards; tree clean).
- Version consistency verified: `filevers/prodvers/FileVersion/ProductVersion` all `(0, 4, 19, 0)` / `'0.4.19.0'`, `vidlift/__init__.py`, `pyproject.toml` — flipped exactly once (no double-bump); `import vidlift` resolves `0.4.19.0`.

---

## Not completed / boundary statements (explicit, per spec)

- **Nothing within the spec's change tables was left unimplemented.** All 15 WS1/WS2/WS3 rows, all 4 WS4 rows, both WS3 GUI surfaces, and the version/CHANGELOG work landed; no invariants from the drift list were crossed.
- **R-1 (accepted boundary, unchanged by design):** a cancel flipped while blocked INSIDE a hung native detect/model-load call is still unobservable (no Python-level poll interrupts a native call). Every Python-level window now observes; the interim remediation (quit the app) and the stale-activity row remain as documented. Fix path on recurrence: executor process isolation, not more polls.
- **R-2:** the GPU-phase-lock WAIT remains unobserved (pre-0.4.19 identical); named follow-up, not in this batch's scope.
- **R-3:** the GUI/CLI pick-path pre-flight is synchronous ffprobe-per-file (output-exists verdicts probe-free); async progress-dialog pre-flight remains the named future criterion.
- **Deferred to Karen's box:** the §13 functional items explicitly requiring real hardware (GPU/CUDA-visible box, cross-process mirror lag on a live GPU run, frozen-build smoke) are stated in the spec's validation baseline and cannot be exercised in this environment.