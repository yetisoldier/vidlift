# VidLift v0.4.19 Implementation Spec — follow-up batch (cancel pre-transcribe · plan-time skip · device indicator · test coverage)

**Author:** Bob (Principal Software Architect)
**Date:** 2026-11-04
**Target release:** v0.4.19.0 (single release; no double-bump)
**Base:** v0.4.18.0 (RELEASED, both forges)
**Input:** `hermes-brief-v0.4.19-followup-batch.md` + four follow-up notes (WS1 HIGH cancel, WS2 plan-time skip, WS3 device indicator, WS4 test coverage)
**Audience:** Forge (implementation), Karen (validation, §13)

Workstreams: **WS1** cancel observed during pre-transcription steps (HIGH, the wedged-job defect) · **WS2** plan-time "already has subtitles / output exists" → SKIP, not FAIL + collector fast-path · **WS3** CPU/GPU device indicator (honest resolving/fallback states, both modes) · **WS4** F-1 stale-scratch path-guard test, F-7 100-file enqueue pin, D-2 shared-harness decision.

---

## 0. Evidence verification

Every claim in the four notes was verified against the v0.4.18.0 tree before design:

- **WS1 root cause confirmed.** `should_cancel` is read at exactly two places in the Mode B path: `transcribe.py:2429` (`_on_segment`, inside the segment loop) and `subs_job.py:301` (post-mux tail). `transcribe-resolve` (`transcribe.py:2398-2416`) and `transcribe-language` (`transcribe.py:2418-2421`) have NO observe. The `_ResilientEngine.detect_language`/`transcribe_segments` arms (`transcribe.py:660-676`, `:707-722`) already re-raise `EngineCancelled` before any CPU rebuild (the D2 rule), so new observes raised in the core cannot be swallowed by the fallback wrapper. Executor mappings verified: `mcp_bridge/jobs.py:495-496` (subs) and `:271-281` (run) close `cancelled=True`; Mode A's run tail maps it at `run.py:3064-3067`.
- **WS1 download window is reachable.** `resolve_model_dir` (`transcribe.py:1278`) drives `_download_file` (`:1456-1515`) whose progress callbacks propagate exceptions: the default fetcher's `except BaseException` arm (`:1444-1451`) deletes only `.part.tmp` staging and re-raises; `_download_file` catches only `TranscriptionError`/`URLError`/`HTTPError`/`OSError` (`:1479-1484`) — an `EngineCancelled` raised inside a progress beat propagates out through the download-lock `with` (`:1361`), which releases. The v0.4.16 conditional-arity rule (`:1471-1478`) constrains the design (see §1/WS1-D3).
- **WS2 run-time guards confirmed.** `subs_job.py:215-219` (subtitle guard) and `:221-225` (output-exists guard) both raise `SubsJobError` at RUN time; the GUI's `_enqueue_subs_paths` (`gui.py:1878-1993`) enqueues every picked path BEFORE rendering rows (`:1956-1962`), so both guards surface as red `failed` rows. The existing blocked-row display (`gui.py:1746-1750`) is pre-submission truth but never prevented the enqueue-fail cycle.
- **WS2 collector confirmed.** `collect_video_files` (`gui.py:110-142`) filters by extension only; the single walk loop at `:130-133` is the exact insertion point for the `.subs.mp4` exclusion. Shared by both modes' folder-add and the drop handler (`gui.py:1712`, `:1865`, `:3710`).
- **WS3 gap confirmed.** `tools.py:_transcription_echo` (`:429-478`) serves `device`/`device_requested` but has NO live source for subs jobs mid-run (terminal `record.result` only, `:463-464`) and none for run jobs pre-phase-end (`state.subtitles` written after the phase at `run.py:3005/3010`; the docstring's "null while transcription is still in flight is the CORRECT answer" was true in-flight because nothing published). `device` is known progressively: core resolution at `transcribe.py:2381` (Mode B) / `run.py:2662-2665` (Mode A), and fallback settles CPU at `transcribe.py:2480-2484` / `run.py:2641-2653`. The mirror (`jobs.py:777-802`) persists exactly `progress`/`current_phase`/`last_activity_at` — a `progress`-dict key therefore reaches same-process reads AND the cross-process mirror with zero transport change. GUI render surfaces identified: `run_stage_label` (`gui.py:346-483`) and the LIVE detail overlay (`gui.py:2245-2270`).
- **WS4 confirmed.** `cleanup_subs_scratch` path guard at `subs_job.py:606-610`; tests 19-23 in `tests/test_v0418_subs_suppress_cleanup.py` cover custom-workdir/retention/replace/OSError but NOT stale-default-scratch-with-custom-workdir. The symbolic cap test is `test_client_local.py:103-122` (`MAX_GUI_BATCH + 1`); the 100-file acceptance is unpinned. The D-2 double lives at `test_v0417_failpath_fix.py:255-359`; the house harness it duplicates is `test_v0414_mode_b_gui.py:92-259` (`_ScriptedClient`, `_window`), already imported by name by `test_v0418_recursive_folder_add.py:49` and `test_v0418_ui_polish.py:44`.
- **Ripple inventory (enumerated, no surprises):** `test_v0414_mode_b_gui.py:498-540` (`test_subs_collision_blocked_row_never_overwrites`) asserts the blocked file IS enqueued (`win.job_ids == ["VJ1", "VJ2"]`) — WS2 changes exactly this; the test is amended (§12 File E). `test_v0414_mode_b_gui.py:542+` calls `win._subs_file_rows(paths)` with ONE argument — the signature gains a required `verdicts` parameter; the test is amended (§12 File E). `test_v0416_transcription_echo.py` T1-T6 stays green untouched (additive live-read ordering, §6.1). `run_stage_label` snapshots WITHOUT the new fact render byte-identical to v0.4.18 (the suffix is appended only when a `transcription.device` truth exists in the payload).

---

## 1. Decisions — every open decision in the four notes, answered

### WS1 (cancel during pre-transcription)

- **WS1-D1 — Placement.** FIVE observes, all inside/alongside the core (`transcribe_source_to_srt`, Mode-agnostic) plus the model-download seam: **(O1)** immediately after `on_step("transcribe-resolve")`, before `resolve_model_dir` — `"cancelled during model resolution"`; **(O2)** inside the download beats via a new `should_cancel` kwarg on `resolve_model_dir` — `"cancelled during model download"`; **(O3)** immediately after the engine factory returns (model loaded), before `transcribe-language` — `"cancelled during model load"`; **(O4)** after `on_step("transcribe-language")`, before `detect_language` — `"cancelled before language detection"`; **(O5)** immediately after `detect_language` returns, before `transcribe-run` — `"cancelled during language detection"`. All raise `EngineCancelled` with the existing lazy-import pattern. The audio-extraction window is already cancel-observed in both modes (both production runners route through `_subprocess_runner(..., cancel=cancel)` — `jobs.py:400/414`, and Mode A's `run_cmd`); it is not re-instrumented.
- **WS1-D2 — Helper shape.** One local closure `_cancel_observe(message)` inside `transcribe_source_to_srt` (one lazy import site, five one-line call sites). `_on_segment`'s inline check (`:2428-2434`) stays verbatim — migrating it to the helper is cosmetic churn on the hot path; DO NOT migrate it (keeps the diff reviewable).
- **WS1-D3 — Download observe honors the v0.4.16 conditional-arity rule (M-1).** The cancel-wrapped progress channel is installed ONLY when a progress channel already exists (default fetcher, or a caller-supplied `on_download_progress`). An injected legacy 2-arg `fetch` with no progress callback keeps its byte-identical 2-arg shape; such callers get the entry/return observes only (they had no mid-download observability before, so there is no beat to hang a check on — honest non-regression).
- **WS1-D4 — Both modes.** The core observes are shared; Mode B flows through `run_subs_job`'s existing `should_cancel` forwarding (`subs_job.py:266`). Mode A: `run.py` passes its own `engine_factory` (a closure over `make_whisper_engine_with_fallback`), so O3 covers Mode A's engine construction inside the core; the one Mode-A-only window NOT covered by the core is `run.py`'s OWN pre-resolve (`run.py:2716-2723`) — it gains `should_cancel=cancel` (the enumerated run.py touch, §4 row 4-A6). Executor mappings (`jobs.py:271`, `:495`) and Mode A's tail (`run.py:3064`) need NO changes; regression pins added (§12).
- **WS1-D5 — Residual boundary, stated honestly.** A cancel flipped while the process is blocked INSIDE a hung native call (the wedged CUDA-context detect/model-load Eric hit) still cannot be observed synchronously — no Python-level poll can interrupt CTranslate2 mid-call. The five observes close every Python-level window (download included), which covers the dominant cancel intents: user clicks cancel while the job is *between* native calls or during model download/load *setup*. Process isolation/worker-kill is the architectural answer for the in-hung-call case and is explicitly a future revisit criterion (§11 R-1); the interim remediation from the note (quit the app) is unchanged and stays documented.
- **WS1-D6 — Explicit non-goal.** The GPU-phase-lock WAIT (`_gpu_phase_lock().acquire(on_wait=...)`, `subs_job.py:243-250`, `run.py:2611-2620`) remains unobserved while waiting — a job queued behind a long GPU holder still can't cancel out of the wait loop. Not in the brief's scope; named follow-up (§11 R-2).

### WS2 (plan-time skip)

- **WS2-D1 — Where the skip verdict lives (Hermes open decision 1).** REUSE `source_has_usable_subtitles` at plan time via ONE new helper in `subs_job.py` — `subs_plan_verdict(path, *, ffprobe, probe_runner, force)` — shared by all THREE pre-flight surfaces (GUI pick, CLI subs, MCP transcribe). Not a lighter probe: the probe+classify the run-side guard needs IS the plan probe (one ffprobe call per file), and the run-side guards stay verbatim as the ground-truth backstop for direct enqueue paths (`subs_job.py:215-225` unchanged).
- **WS2-D2 — Verdict vocabulary (structural, not display strings).** Four module constants: `SUBS_VERDICT_READY = "ready"`, `SUBS_VERDICT_SKIP_SUBTITLES = "skip-subtitles"`, `SUBS_VERDICT_BLOCKED_OUTPUT = "blocked-output-exists"`, `SUBS_VERDICT_PROBE_ERROR = "probe-error"` (fail-open: enqueue — a plan-time probe fault is not absence, the VAD-preflight "unknown proceeds" discipline; the run-side probe names the truth). Surfaces map verdicts to display text locally.
- **WS2-D3 — Check order: output-exists FIRST, subtitle probe SECOND (deliberate divergence from run-guard order).** Run-side order (`subs_job.py:215` then `:221`) checks subtitles first; plan-side checks output-exists first because (a) it is probe-free — the dominant re-add case (VidLift's own `.subs.mp4` still on disk) costs zero subprocess time; (b) when both hold, "blocked — output exists" is the more actionable message (the file's old output must be moved/deleted, not its subtitles). No reachable contradiction exists: blocked files are never enqueued, so the run guard's order is never observed against them. `--force` bypasses ONLY the subtitle check at plan time — mirroring the run guard (`subs_job.py:222-225` has no force exception); force + existing output = blocked.
- **WS2-D4 — Distinct statuses (Hermes open decision 2).** DISTINCT. "skip — already has subtitles" (nothing to do, end-state holds) and "blocked — subtitles output already exists (will not be overwritten)" (would-overwrite; existing wording kept verbatim) read as different things because they ARE different things. Both are now plan-time, non-enqueued verdicts.
- **WS2-D5 — Batch summary (Hermes open decision 3).** Skipped/blocked files never enter the registry, so `run_counts`/`summarize_batch` can never inflate `failed` — the failed-count fix is structural. The enqueue-time summary line additionally NAMES the pre-flight outcome counts (exact strings §10 S-6/S-7/S-8).
- **WS2-D6 — Row mechanics.** Rows render for ALL picked files in a STABLE PARTITION: ready files first (pick order), then skip, then blocked (each pick-ordered). This preserves `_begin_job_slate`'s job↔row identity mapping (`gui.py:2378-2387`, job *i* → row `seed+i`) with zero slate-machinery change (§2 deviation 2). Skip/blocked rows are static: no job id, the poll never touches them; the detail pane renders their verdict cell via the existing kind-blind body (`_subs_detail_text`, `gui.py:2272-2300`).
- **WS2-D7 — Pre-flight BEFORE the conflict guard, guard over ready-only.** A live subs conflict on a file we would skip or block must not pop the conflict dialog for nothing (§2 deviation 3).
- **WS2-D8 — Collector exclusion is shared and unconditional.** `*.subs.mp4` (case-insensitive) is excluded in `collect_video_files` for BOTH modes and the drop handler — one ingestion policy (the D1 recursion precedent). Only the mp4 family: `movie.subs.mkv` is not a VidLift output (outputs are always `.mp4`) and remains collectible. The exclusion is NOT sufficient on its own (the replace-source-renamed `.mp4` is name-indistinguishable) — it is belt-and-suspenders behind the plan-time skip, exactly as the note frames it.
- **WS2-D9 — CLI exit-code semantics change (stated).** Skipped and blocked files no longer count toward `failures` in `_cmd_subs`: the desired end-state (a subtitled mp4 beside the source) already holds. `vidlift subs` exits 0 for an all-skip batch; exit 1 remains for REAL failures. Previously both conditions counted as failures — the change is the point of the workstream.
- **WS2-D10 — Performance posture.** The GUI pre-flight is a synchronous per-file ffprobe loop on the pick path — the same shape as Mode A's `plan_batch` probe loop (`gui.py:2027-2033`), an established UX at identical scale (both capped at 500 by the same constant family). Output-exists files skip the probe entirely. A progress-dialoged async plan stage is a documented future criterion, not v0.4.19 scope (§11 R-3).

### WS3 (device indicator)

- **WS3-D1 — "Resolving" before resolution (nuance 1).** The `device` echo stays `null` until the core/run publishes; the UI renders the requested value — `auto — resolving…` for running jobs, `auto (requested)` for queued — NEVER a bare guess like "CPU." The display layer keys on the SAME additive `transcription` echo block already on every payload (`tools.py:568`), so there is exactly one wording rule (§6 code, exact strings §10).
- **WS3-D2 — Honest fallback surfacing (nuance 2).** Display mapping: device `"cuda"` → `GPU (CUDA)`; device `"cpu"` with requested `"cpu"` or requested unknown (legacy records) → `CPU`; device `"cpu"` with requested `"auto"`/`"cuda"` → `CPU (GPU unavailable)`; the Mode A R1 echo `"cpu (cuda unavailable)"` → `CPU (GPU unavailable)`; any foreign device string renders verbatim, never massaged.
- **WS3-D3 — Both modes, one publication mechanism (nuance 3).** A new additive seam `on_device: Callable[[str, str], None] | None = None` on `transcribe_source_to_srt` (fires at core resolution, and again as `("cpu", requested)` from the fallback wrapper); `run_subs_job` forwards it; the Mode B executor publishes into `state.progress["transcription_device"]`/`["transcription_device_requested"]`. Mode A publishes the same two keys directly in `run.py` (at `run.py:2663` post-resolution and inside `_on_cuda_fallback`), because Mode A owns its device decision OUTSIDE the core — the core seam deliberately never fires under a foreign factory (the D4 honest-unknown rule, preserved). `_transcription_echo` becomes live-first, result/manifest as before.
- **WS3-D4 — Surfaces (nuance 4).** Minimum plus: (a) the LIVE detail overlay gains `Device: …` (including resolving/requested states) — auto-follows the running row each tick; (b) `run_stage_label` appends ` — GPU (CUDA)` / ` — CPU` / ` — CPU (GPU unavailable)` to the transcription-family phases (`transcribe`/`subtitles`/`subs-mux`, incl. the model-download early-return labels) once a RESOLVED device exists. Requested-only states never ride the stage label (no resolve-flap churn on ETA/busy keys); they live in the overlay only. Queued rows show requested-only (`auto (requested)`), Mode A queued jobs show nothing (the pin lives in the not-yet-built plan — honest absence, not a guess).
- **WS3-D5 — No transport/schema churn.** `api_version` stays 1; the echo block gains NO new keys (values only, live-sourced). The HTTP monitor inherits automatically (`monitor.py:12-13`, `:109-112` are pass-throughs of `_status_payload`); cross-process readers get the facts via the existing mirror (dead-writer rule honored — the mirror is trusted only while the record is running, same as `_transcription_model_echo`).

### WS4 (test coverage)

- **WS4-D1 (F-1).** ONE test, `test_24_stale_default_scratch_survives_custom_workdir` in `tests/test_v0418_subs_suppress_cleanup.py`: seed stale `{output}.subs-work`, run with `workdir=custom`, assert the stale dir survives, no cleanup note, and the direct helper call returns `[]`. Kills the mutation-3b hole (guard removal would delete the stale dir → assertion fails).
- **WS4-D2 (F-7).** ONE test, `test_client_local_hundred_file_subs_batch_enqueues` in `tests/test_client_local.py`: 100 real files against a real `LocalBridgeClient`+registry → 100 records, submission order, no `BatchTooLarge`, persisted `jobs.json` agrees.
- **WS4-D3 (D-2, the architecture call).** **Module-level shared harness — NOT conftest, NOT per-file duplication.** `test_v0417_failpath_fix.py` deletes its local `_ScriptedClient`/`_window` and imports the house harness: `from test_v0414_mode_b_gui import _ScriptedClient, _window`. Rationale: (1) the suite already settled this exact pattern TWICE in v0.4.18 (`test_v0418_recursive_folder_add.py:49`, `test_v0418_ui_polish.py:44`) — the v0417 file is the outlier, and D-2 exists precisely because a per-file copy drifts (its v0.4.18 `suppress_srt` ripple was the proof); (2) `conftest.py` is for fixtures by house convention — `_ScriptedClient` is a scripted class with per-test state, not a fixture, and conftest-imported classes give zero import-time/type visibility; (3) moving the double to `tests/_helpers` would churn the two existing importers for zero behavior gain — the module import is the shared home that already exists. Compatibility verified mechanically (§7.3): the shared `enqueue_subs`/`enqueue_run`/`cancel_batch`/`active_job_count` shapes are supersets of the v0417 local copy; the only behavioral nuance — shared `cancel_batch` appends a terminal snapshot via `script_tail` instead of truncating the tail — is consumed identically within the test's 4-tick window (cursor reaches the appended `cancelled` within 2 ticks), and the GUI ignores `cancel_batch`'s return map (`gui.py:2552`). No assertion changes in any v0417 test.

---

## 2. Explicit deviations from the brief/notes (each intentional)

1. **WS1 adds the model-download observe** (`resolve_model_dir` gains `should_cancel`; O2). The note says "before/around detect_language and model resolution"; a first-run 1.5 GB download IS the resolution window and was an unobserved multi-minute cancel gap. Implemented strictly within the v0.4.16 conditional-arity rule (WS1-D3). If Forge judges this a scope breach, the fallback is O1+O3+O4+O5 only — call it out in the Forge report rather than silently dropping it.
2. **WS2 renders rows as a stable partition (ready → skip → blocked), not strict pick order.** Strict pick order would require `_begin_job_slate` to accept a sparse row mapping; the partition keeps the v0.4.14 slate identity machinery (`gui.py:2348-2417`) byte-untouched. When nothing is skipped, partition order == pick order (all existing row-order tests hold).
3. **WS2 conflict guard moves AFTER pre-flight and evaluates the ready subset.** Pre-v0.4.19 it evaluated the full pick list; a live conflict on a now-skipped file no longer pops the dialog.
4. **WS2 plan-side check order is output-exists-first** (run-side order unchanged; WS2-D3 reasoning).
5. **WS2 CLI exit semantics change** (skip/blocked no longer failures; WS2-D9). Documented in the CHANGELOG as Changed.
6. **WS1 honesty boundary** (WS1-D5): the brief's "even during … language detection" is delivered as before/after observes; a TRUE mid-hang native call remains uninterruptible without process isolation (§11 R-1). Stated so Karen does not test a promise the architecture doesn't make.

Everything else follows the notes verbatim.

---

## 3. Architecture delta (one screen)

```
WS1  transcribe.py  resolve_model_dir(+should_cancel) ── armed download beats
     transcribe.py  transcribe_source_to_srt ── _cancel_observe ×4  (O1 O3 O4 O5)
     run.py         resolve_model_dir(..., should_cancel=cancel)     (Mode A pre-resolve)
     (jobs.py/run.py mappings unchanged — regression-pinned)

WS2  subs_job.py    SUBS_VERDICT_* + subs_plan_verdict() ── one helper
       ├─ gui.py    _enqueue_subs_paths: pre-flight → conflict(ready) → enqueue(ready)
       │            _subs_file_rows(paths, verdicts) — skip/blocked/probe-error rows
       ├─ cli.py    _cmd_subs pre-flight (skip/blocked ≠ failures)
       └─ tools.py  tool_transcribe pre-check (additive skipped rows)
     gui.py         collect_video_files: exclude *.subs.mp4 (both modes + drop)
     (run-side guards subs_job.py:215-225 unchanged — backstop)

WS3  transcribe.py  transcribe_source_to_srt(+on_device) — fires at resolution + fallback
     subs_job.py    run_subs_job(+on_device) — forwards verbatim
     jobs.py        _execute_subs_job on_device → state.progress[transcription_device{,_requested}]
     run.py         publishes the same two keys (resolve + _on_cuda_fallback)
     tools.py       _transcription_echo: live-first read (state.progress → mirror → result/manifest)
     gui.py         _device_display() one wording rule → detail overlay line + stage suffix

WS4  test_v0418_subs_suppress_cleanup.py test_24 (F-1)
     test_client_local.py 100-file pin (F-7)
     test_v0417_failpath_fix.py → shared harness import (D-2)
```

Invariants held: `api_version` stays 1 (no new payload keys — echo VALUES gain live sources; `progress` is the existing free-form dict) · new options: none · new QSettings keys: none · run.py touches enumerated (§4 rows 4-A6, §6 rows 6-A4/6-A5 — three sites, all additive) · `MAX_GUI_BATCH`/`VIDEO_EXTENSIONS` unchanged.

---

## 4. WS1 (HIGH) — cancel observed during pre-transcription steps

### 4.1 Change table

| # | Module : anchor (v0.4.18.0 lines) | Binding / exact signature | Change | LoC |
|---|---|---|---|---|
| 4-A1 | `vidlift/transcribe.py:1278-1285` | `def resolve_model_dir(pin: str \| None = None, *, state_root=None, fetch=None, on_download_progress=None, on_lock_wait=None, should_cancel: Callable[[], bool] \| None = None) -> str:` | **Additive trailing kwarg** `should_cancel`. Absent-defaults: every existing caller byte-identical. Docstring gains the v0.4.19 paragraph. | +5 |
| 4-A2 | `vidlift/transcribe.py:1312→1313` (env guard) | function body entry | **O-entry observe + docstring:** immediately after the docstring, BEFORE the `_engine_disabled_by_env()` check (cancel is control flow and wins over the refusal — the resilient-engine D2 precedence). Exact code §4.2. | +6 |
| 4-A3 | `vidlift/transcribe.py:1357-1360` | progress-consumer arms | **O-download observe (O2):** after the `_lock_heartbeat`/`None` arms, wrap `progress` — but ONLY when it is not None (conditional-arity, WS1-D3). Exact code §4.2. | +12 |
| 4-A4 | `vidlift/transcribe.py:2308-2327` | `def transcribe_source_to_srt(...) -> "TranscriptionResult":` | No signature change for WS1 (the WS3 `on_device` kwarg lands in §6 row 6-A1 — Forge applies both in one pass over the signature to avoid a double edit; the two kwargs are independent). Docstring gains the cancel-observe paragraph. | +4 |
| 4-A4a | `vidlift/transcribe.py:2397` (after `engine_kwargs` block, before `on_step("transcribe-resolve")`) | local closure | **`_cancel_observe(message)` definition** (exact code §4.3). | +7 |
| 4-A4b | `vidlift/transcribe.py:2398→2403` | resolve step | **O1 observe** right after `on_step and on_step("transcribe-resolve")`; the `resolve_model_dir` call (`:2403-2407`) gains `should_cancel=should_cancel`. | +4 |
| 4-A4c | `vidlift/transcribe.py:2412→2413` | post-factory | **O3 observe** right after engine construction, before the `download_log` block. | +2 |
| 4-A4d | `vidlift/transcribe.py:2418-2421` | language step | **O4 observe** right after `on_step and on_step("transcribe-language")`, before `detect_language`; **O5 observe** immediately after it returns, before `on_step("transcribe-run")` (`:2424`). | +4 |
| 4-A5 | `vidlift/subs_job.py` | — | **NO CHANGE.** `should_cancel` already forwards (`subs_job.py:266`); the observes live in the shared core. | 0 |
| 4-A6 | `vidlift/run.py:2716-2723` | Mode A's own `resolve_model_dir(...)` call | Gains `should_cancel=cancel` + one comment line (Mode A's pre-resolve download now observes cancels; `cancel` is the `run_file` parameter already forwarded at `:2756`). **Enumerated run.py touch 1 of 3.** | +2 |
| 4-A7 | `vidlift/mcp_bridge/jobs.py:271-281, :495-496` | EngineCancelled mappings | **NO CHANGE — regression-pinned instead** (§12 File A tests 6/7). | 0 |

`_on_segment` (`transcribe.py:2428-2436`) stays verbatim (WS1-D2). The post-mux tail (`subs_job.py:301-304`) stays verbatim.

### 4.2 `resolve_model_dir` — exact edits

Signature (4-A1):

```python
def resolve_model_dir(
    pin: str | None = None,
    *,
    state_root: str | os.PathLike | None = None,
    fetch=None,
    on_download_progress: Callable[[int, int | None], None] | None = None,
    on_lock_wait: Callable[[int | None, float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
```

Docstring addition (end of the existing docstring, 4-A1):

    v0.4.19 (WS1, HIGH): ``should_cancel`` arms the resolution window —
    an entry observe (before even the env refusal; a cancel is control
    flow, never an engine-error) and, when a progress channel exists,
    a per-beat observe inside the first-run download
    (``EngineCancelled`` propagates: the default fetcher's BaseException
    arm removes only the ``.tmp`` staging, the ``.part`` resume staging
    is untouched, and the download lock's ``with`` releases). The
    v0.4.16 conditional-arity discipline holds: with an injected legacy
    2-arg ``fetch`` and no caller progress callback there is no beat to
    arm, and the call keeps its byte-identical shape (entry/return
    observes only).

Body — entry observe (4-A2), immediately after the docstring, before the env check:

```python
    # v0.4.19 (WS1, HIGH): the entry observe — a cancel flipped before
    # (or while waiting to enter) the cache fast-path closes cancelled,
    # never sits at "running" until the next segment beat that may take
    # minutes to exist.
    if should_cancel is not None and should_cancel():
        from .run import EngineCancelled  # lazy: cancels surface

        raise EngineCancelled("cancelled during model resolution")
```

Body — download-beat arming (4-A3), replacing the tail after the two `progress` arms:

```python
    use_default_fetcher = fetch is None
    if use_default_fetcher:
        fetch = _default_fetcher()
    lock_path = target / ".download.lock"
    if on_download_progress is not None or use_default_fetcher:
        progress = _lock_heartbeat(on_download_progress, lock_path)
    else:
        progress = None
    # v0.4.19 (WS1, HIGH): the download's own beats observe the cancel —
    # a multi-minute first-run fetch is itself a pre-transcription
    # window (WS1-D3: only where a beat channel exists).
    if should_cancel is not None and progress is not None:
        base_progress = progress

        def progress(done: int, total: int | None) -> None:  # noqa: F811
            if should_cancel():
                from .run import EngineCancelled  # lazy: cancels surface

                raise EngineCancelled("cancelled during model download")
            base_progress(done, total)
```

### 4.3 `transcribe_source_to_srt` — exact edits

Helper (4-A4a), inserted immediately BEFORE line 2398's `on_step and on_step("transcribe-resolve")`:

```python
        # v0.4.19 (WS1, HIGH): the pre-transcription cancel observes —
        # the wedge class Eric hit (a job parked at transcribe-resolve /
        # transcribe-language read the flag NOWHERE until the first
        # segment callback). One closure, five sites; the lazy import
        # mirrors _on_segment's observer (which stays verbatim — the
        # hot loop is not churned). A cancel observed inside a hung
        # NATIVE detect/load call remains uninterruptible by
        # construction (documented boundary, spec §1 WS1-D5).
        def _cancel_observe(message: str) -> None:
            if should_cancel is not None and should_cancel():
                from .run import EngineCancelled  # lazy: cancels surface

                raise EngineCancelled(message)
```

O1 + threaded resolve (4-A4b) — the amended block:

```python
        on_step and on_step("transcribe-resolve")
        _cancel_observe("cancelled during model resolution")
        # v0.4.16 (model management, ADR-3): the previously decorative
        # ... (existing comment block unchanged) ...
        model_dir = resolve_model_dir(
            model_pin,
            state_root=state_root,
            on_download_progress=on_download_progress,
            # v0.4.19 (WS1): the resolution/download window observes the
            # flag itself (entry + armed beats).
            should_cancel=should_cancel,
        )
        engine = (
            engine_factory(model_dir, **engine_kwargs)
            if engine_kwargs is not None
            else engine_factory(model_dir)
        )
        _cancel_observe("cancelled during model load")
```

O4/O5 (4-A4d) — the amended `# -- language ----` block:

```python
        # -- language ----
        on_step and on_step("transcribe-language")
        _cancel_observe("cancelled before language detection")
        language, guess, warnings = detect_language(
            engine, wav_path, pin=language_pin
        )
        _cancel_observe("cancelled during language detection")
```

### 4.4 Mode A (`run.py`) — exact edit

`run.py:2716-2723` becomes:

```python
                        model_dir = resolve_model_dir(
                            knobs.get("model"),
                            on_download_progress=_on_model_download,
                            # the GPU lock's waiter: the download-lock
                            # wait gets the same visible labeling (its
                            # signature matches the callback contract).
                            on_lock_wait=_transcribe_wait_label,
                            # v0.4.19 (WS1, HIGH): Mode A's own
                            # pre-resolve observes the run's cancel flag
                            # too (download beats + entry/return).
                            should_cancel=cancel,
                        )
```

`cancel` is in scope (the `run_file` parameter, already forwarded at `run.py:2756`). Propagation: `EngineCancelled` is a `RunError`, NOT a `TranscriptionError` — it escapes the fail-open conversion at `run.py:2758-2786`, releases the GPU lock via the `finally` (`:2787-2789`), and closes through the existing tail mapping (`:3064-3067`) exactly like the mid-transcription cancel.

---

## 5. WS2 — plan-time "already subtitled / output exists" → SKIP

### 5.1 Change table

| # | Module : anchor | Binding / exact signature | Change | LoC |
|---|---|---|---|---|
| 5-A1 | `vidlift/subs_job.py:50` | `from .probe import classify, probe_file` | Extend: `from .probe import ProbeError, classify, probe_file` (alphabetical). | +1 −1 |
| 5-A2 | `vidlift/subs_job.py:62-70` | `__all__` | Append `"subs_plan_verdict"`, `"SUBS_VERDICT_READY"`, `"SUBS_VERDICT_SKIP_SUBTITLES"`, `"SUBS_VERDICT_BLOCKED_OUTPUT"`, `"SUBS_VERDICT_PROBE_ERROR"`. | +5 |
| 5-A3 | `vidlift/subs_job.py` after `source_has_usable_subtitles` (:144) | `def subs_plan_verdict(path: str, *, ffprobe: str = "ffprobe", probe_runner=None, force: bool = False) -> str:` + four module constants | **New shared helper** (exact code §5.2). | +48 |
| 5-A4 | `vidlift/subs_job.py:215-225` | run-side guards | **NO CHANGE** (the backstop; absence of change is the design). | 0 |
| 5-A5 | `vidlift/gui.py:88` | `from .subs_job import subs_output_path` | Extend to also import the four constants + `subs_plan_verdict` (parenthesized group, alphabetical; the module header comment gains the v0.4.19 note). | +8 −1 |
| 5-A6 | `vidlift/gui.py:130-133` | `collect_video_files` walk loop | Add the `.subs.mp4` exclusion (exact code §5.3). | +7 |
| 5-A7 | `vidlift/gui.py:1730-1775` | `def _subs_file_rows(self, paths: list[str], verdicts: dict[str, str]) -> list[list[str]]:` | **Signature gains required `verdicts`**; per-verdict row construction (exact code §5.4). Ready/blocked cells byte-identical to v0.4.18 for those verdicts. | +26 −17 |
| 5-A8 | `vidlift/gui.py:1878-1993` | `def _enqueue_subs_paths(self, paths: list[str]) -> None:` | **Pre-flight block first** (find_tools → verdicts → partition), conflict guard over `ready` only, enqueue `ready` only, rows rendered for the partition, slate bootstrap only when `fresh_ids`, summary names skip/block counts. Exact code §5.5. | +48 −22 |
| 5-A9 | `vidlift/cli.py:406-407` | lazy imports in `_cmd_subs` | `from .subs_job import (SUBS_VERDICT_BLOCKED_OUTPUT, SUBS_VERDICT_SKIP_SUBTITLES, SubsJobError, run_subs_job, subs_output_path, subs_plan_verdict)` | +7 −1 |
| 5-A10 | `vidlift/cli.py:416-418` | `_cmd_subs` loop head | Pre-flight verdict per file; skip/blocked print + `continue` (never failures). Exact code §5.6. | +14 |
| 5-A11 | `vidlift/mcp_bridge/tools.py:754-774` | `tool_transcribe` enqueue loop | Plan-time pre-check after the isfile arm; skipped rows are additive dicts, never enqueued. Exact code §5.7. Docstring gains the verdict paragraph. | +26 |
| 5-A12 | `vidlift/gui.py:1941-1944` + `:1988-1993` | no-slate/summary arms | Amended per §5.5 (rows render before the `not fresh_ids` early return; the summary composes the extras clause). | (in 5-A8) |

### 5.2 `subs_plan_verdict` — exact code (5-A3)

```python
# v0.4.19 (WS2): the plan-time skip verdict — already-done files are
# SKIPPED at the pre-flight surfaces (GUI pick, CLI subs, MCP
# transcribe), never enqueued to fail red at run time. The run-side
# guards above are UNCHANGED: they remain the ground-truth backstop
# for direct enqueues (older scripted callers, API bypasses).
SUBS_VERDICT_READY = "ready"
SUBS_VERDICT_SKIP_SUBTITLES = "skip-subtitles"
SUBS_VERDICT_BLOCKED_OUTPUT = "blocked-output-exists"
SUBS_VERDICT_PROBE_ERROR = "probe-error"


def subs_plan_verdict(
    path: str,
    *,
    ffprobe: str = "ffprobe",
    probe_runner=None,
    force: bool = False,
) -> str:
    """The plan-time verdict for one Mode B file (v0.4.19, WS2).

    Returns one of the ``SUBS_VERDICT_*`` constants:

    - ``blocked-output-exists`` — the ``{stem}.subs.mp4`` already
      exists; never overwritten. Checked FIRST and probe-free: the
      dominant re-add case costs no subprocess, and when both
      conditions hold the would-overwrite reason is the actionable one
      (a deliberate divergence from the run guard's order — spec §1
      WS2-D3). ``force`` does NOT bypass this (the run-side guard has
      no force exception either).
    - ``skip-subtitles`` — the source already carries a usable TEXT
      subtitle track (:func:`source_has_usable_subtitles`'s exact
      predicate: bitmap tracks and sidecars do NOT count).
      ``force=True`` reports ``ready`` — the genuine-retranscription
      override mirrors the run guard.
    - ``probe-error`` — the plan probe failed (ProbeError/OSError).
      Fail-OPEN: callers enqueue anyway and the run-side probe names
      the truth (a plan-time probe fault is not absence — the VAD
      preflight's "unknown proceeds" discipline).
    - ``ready`` — enqueue.
    """
    if os.path.exists(subs_output_path(path)):
        return SUBS_VERDICT_BLOCKED_OUTPUT
    if force:
        return SUBS_VERDICT_READY
    probe_kwargs = {}
    if probe_runner is not None:
        probe_kwargs["runner"] = probe_runner
    try:
        media = probe_file(path, binary=ffprobe, **probe_kwargs)
        report = classify(media)
    except (ProbeError, OSError):
        return SUBS_VERDICT_PROBE_ERROR
    if source_has_usable_subtitles(report):
        return SUBS_VERDICT_SKIP_SUBTITLES
    return SUBS_VERDICT_READY
```

### 5.3 Collector exclusion — exact code (5-A6)

Inside `collect_video_files`, the `for name in filenames:` loop body becomes:

```python
        for name in filenames:
            candidate = Path(dirpath) / name
            if candidate.suffix.lower() in VIDEO_EXTENSIONS:
                # v0.4.19 (WS2): VidLift's OWN keep-original outputs
                # (the {stem}.subs.mp4 family) are never re-ingested —
                # re-adding a finished folder must not re-queue its own
                # results. Name-based fast path only: the
                # replace-source-renamed .mp4 form is
                # name-indistinguishable and is caught by the WS2
                # plan-time subtitle probe instead (belt + suspenders).
                if name.lower().endswith(".subs.mp4"):
                    continue
                found.append(str(candidate))
```

The docstring's Shared-both-modes sentence gains: "v0.4.19 (WS2): never yields VidLift's own `*.subs.mp4` outputs (any case)."

### 5.4 `_subs_file_rows` — exact code (5-A7)

```python
    def _subs_file_rows(
        self, paths: list[str], verdicts: dict[str, str]
    ) -> list[list[str]]:
        """The subs table rows (same 8-column schema as the plan view;
        the Status cell carries the subs verdict instead of the upscale
        one) — the shared-table render contract for subs batches.

        v0.4.15.3 (F-5): the output path comes straight from
        :func:`subs_output_path` — one naming derivation for display,
        pre-flight, and the run guard. The old ``path[:-4]`` slice
        mangled non-4-char extensions and silently disabled the
        collision pre-flight for those files.

        v0.4.19 (WS2): the caller pre-flights every pick and hands the
        verdict map in (structural ``SUBS_VERDICT_*`` → display string
        mapping lives HERE, the render layer). Skip and blocked rows
        are STATIC — they carry no job and the 1 Hz poll never rewrites
        them. ``verdicts`` is a REQUIRED argument: a row must never be
        rendered from a verdict the pre-flight didn't make.
        """
        rows: list[list[str]] = []
        for path in paths:
            out_path = subs_output_path(path)
            existing = Path(out_path)
            verdict = verdicts.get(path, SUBS_VERDICT_READY)
            action = (
                "replace original → .mp4"
                if self.subs_replace_source.isChecked()
                else "keep original"
            )
            if verdict == SUBS_VERDICT_BLOCKED_OUTPUT:
                rows.append(
                    [
                        "blocked — subtitles output already exists (will not be overwritten)",
                        path,
                        "transcribe",
                        action,
                        out_path,
                        "",
                        "transcribed from the file's own audio",
                        "output exists: " + existing.name
                        + " (will not be overwritten)",
                    ]
                )
            elif verdict == SUBS_VERDICT_SKIP_SUBTITLES:
                rows.append(
                    [
                        "skip — already has subtitles",
                        path,
                        "skip (no transcription)",
                        "no action",
                        out_path,
                        "",
                        "already subtitled — nothing to do",
                        "",
                    ]
                )
            else:
                # ready (or probe-error: fail-open — enqueue; the
                # run-side check names the truth, and the warning cell
                # discloses the inconclusive plan probe honestly).
                rows.append(
                    [
                        "subs ready",
                        path,
                        "transcribe",
                        action,
                        out_path,
                        "",
                        "transcribed from the file's own audio",
                        (
                            "plan probe inconclusive — re-checked at run time"
                            if verdict == SUBS_VERDICT_PROBE_ERROR
                            else ""
                        ),
                    ]
                )
        return rows
```

### 5.5 `_enqueue_subs_paths` — the amended flow (5-A8, exact)

Replace the section from the docstring-tail through the slate/summary tail with:

```python
        # v0.4.19 (WS2): plan-time pre-flight BEFORE the conflict guard
        # — one probe per picked path (output-exists is probe-free;
        # find_tools failure degrades to the fail-open probe-error
        # verdict, enqueueing to the run-side truth). The verdict map
        # decides WHO gets enqueued; skip/block rows are rendered but
        # never become jobs (deviation 3: the conflict dialog now
        # evaluates the ready subset only).
        try:
            ffprobe = find_tools().ffprobe
        except MissingToolError:
            ffprobe = "ffprobe"  # probe fails honestly → probe-error → ready
        verdicts: dict[str, str] = {}
        for path in paths:
            verdicts[path] = subs_plan_verdict(path, ffprobe=ffprobe)
        ready = [
            path for path in paths
            if verdicts[path] in (SUBS_VERDICT_READY, SUBS_VERDICT_PROBE_ERROR)
        ]
        skipped = [
            path for path in paths
            if verdicts[path] == SUBS_VERDICT_SKIP_SUBTITLES
        ]
        blocked = [
            path for path in paths
            if verdicts[path] == SUBS_VERDICT_BLOCKED_OUTPUT
        ]
        # v0.4.15.1: the ONLY blocking condition — a live subs job over
        # the same (READY) source. ... (existing comment continues verbatim)
        live_subs_paths: frozenset[str] = frozenset()
        try:
            running, queued = self.client.queue_views()
        except AttributeError:
            running, queued = [], []
        live_subs_paths = self._active_subs_paths(running or [], queued or [])
        conflicts = [path for path in ready if path in live_subs_paths]
        if conflicts:
            # (dialog body unchanged — verbatim)
            ...
            return
        pending_ids: list[str] | None = None
        if self.job_ids:
            pending_ids = list(self.job_ids)
        fresh_ids: list[str] = []
        try:
            if ready:
                fresh_ids = self.client.enqueue_subs(
                    ready,
                    replace_source=self.subs_replace_source.isChecked(),
                    suppress_srt=self.subs_suppress_srt.isChecked(),
                )
        except (
            BatchTooLarge,
            FileNotFoundError,
            BridgeError,
            OSError,
        ) as exc:
            # (v0.4.15.4 F-13b dialog + pending-ids arm unchanged)
            ...
            return
        # v0.4.19 (WS2): rows ALWAYS render (the partition: ready first,
        # then skip, then blocked — _begin_job_slate's job↔row mapping
        # is row_offset+index over the READY list; skip/blocked rows
        # carry no job and sit BELOW the last job row, never aliased).
        seed = self.file_table.rowCount()
        ordered = ready + skipped + blocked
        self.file_table.setRowCount(seed + len(ordered))
        for offset, cells in enumerate(self._subs_file_rows(ordered, verdicts)):
            for column, text in enumerate(cells):
                self.file_table.setItem(
                    seed + offset, column, QTableWidgetItem(text)
                )
        if not fresh_ids:
            # nothing enqueued — the static skip/block rows still render
            # (and the summary below names why); no slate, no timer.
            if pending_ids is not None:
                self.job_ids = pending_ids
            self._set_summary(
                self._subs_enqueue_summary(0, len(skipped), len(blocked)),
                "Skipped and blocked files are not enqueued; the "
                "run-side guards remain the backstop.",
            )
            return
        # (the live-pending merge, slate extend/bootstrap blocks are
        # unchanged — verbatim)
        ...
        self._set_summary(
            self._subs_enqueue_summary(
                len(self._batch_slots) if pending_ids is not None
                else len(self.job_ids),
                len(skipped),
                len(blocked),
            ),
            "Transcribing audio and soft-muxing subtitles (no upscale).",
        )
```

New private helper (module-level, beside `_stage_label_text`, pure — Karen-testable without Qt):

```python
def _subs_enqueue_summary(queued: int, skipped: int, blocked: int) -> str:
    """v0.4.19 (WS2): the enqueue-time count line — enqueued jobs plus
    the pre-flight outcome counts, so a folder re-add answers 'what
    happened to the other 195 files' without red failures."""
    text = f"{queued} queued for subtitles"
    extras: list[str] = []
    if skipped:
        extras.append(f"{skipped} skipped (already subtitled)")
    if blocked:
        extras.append(f"{blocked} blocked (output exists)")
    if extras:
        text += " — " + ", ".join(extras)
    return text
```

### 5.6 `_cmd_subs` pre-flight — exact code (5-A10)

Inside `for path in files:`, immediately after `print(f"== {path}")`:

```python
        # v0.4.19 (WS2): the plan-time verdict — already-done files are
        # SKIPPED, not run-side failures (the end-state already holds;
        # WS2-D9: neither counts toward `failures`). --force remains the
        # subtitle override; a probe fault enqueues to the run-side truth.
        verdict = subs_plan_verdict(path, ffprobe=tools.ffprobe, force=force)
        if verdict == SUBS_VERDICT_SKIP_SUBTITLES:
            print("  skip — already has subtitles")
            continue
        if verdict == SUBS_VERDICT_BLOCKED_OUTPUT:
            print(
                "  blocked — output already exists (never overwritten): "
                f"{subs_output_path(path)}"
            )
            continue
```

### 5.7 `tool_transcribe` pre-check — exact code (5-A11)

Inside `for path in args["paths"]:`, after the isfile arm:

```python
        # v0.4.19 (WS2): the shared plan-time verdict at the MCP
        # enqueue surface — additive keys only (skipped/reason/job_id:
        # null); api_version stays 1. A probe fault (or missing ffprobe
        # here) enqueues to the run-side guard truth.
        verdict = subs_plan_verdict(
            path,
            probe_runner=getattr(services, "probe_runner", None),
            force=force,
        )
        if verdict == SUBS_VERDICT_SKIP_SUBTITLES:
            jobs.append(
                {"path": path, "job_id": None, "ok": True,
                 "skipped": True,
                 "reason": "already has a usable text subtitle track"}
            )
            continue
        if verdict == SUBS_VERDICT_BLOCKED_OUTPUT:
            jobs.append(
                {"path": path, "job_id": None, "ok": True,
                 "skipped": True,
                 "reason": "output already exists (never overwritten)"}
            )
            continue
```

`tool_transcribe`'s docstring gains: "v0.4.19 (WS2): already-subtitled sources and existing outputs are skipped at enqueue time (additive `skipped`/`reason` keys; `force` bypasses the subtitle check only)."

---

## 6. WS3 — CPU/GPU device indicator (both modes)

### 6.1 Change table

| # | Module : anchor | Binding / exact signature | Change | LoC |
|---|---|---|---|---|
| 6-A1 | `vidlift/transcribe.py:2326-2327` | `transcribe_source_to_srt(..., vad_report: dict \| None = None, on_device: Callable[[str, str], None] \| None = None) -> "TranscriptionResult":` | **Additive trailing kwarg** `on_device(device, requested)` + docstring paragraph (fires at the core's own resolution, and as `("cpu", requested)` from the fallback wrapper; NEVER under a foreign factory — the D4 honest-unknown rule). | +7 |
| 6-A2 | `vidlift/transcribe.py:2381-2382` | core resolution | Fire site 1 (§6.2). | +3 |
| 6-A3 | `vidlift/transcribe.py:2384-2388` | `_default_on_fallback` | Fire site 2 (§6.2). | +4 |
| 6-A4 | `vidlift/run.py:2662-2665` | Mode A resolution | Publish `state.progress["transcription_device"]/["transcription_device_requested"]` immediately after `resolve_transcription_device` (§6.3). **Enumerated run.py touch 2 of 3.** | +9 |
| 6-A5 | `vidlift/run.py:2641-2653` | `_on_cuda_fallback` | Publish the mutated `transcribe_device` inside the fallback closure (§6.3). **Enumerated run.py touch 3 of 3.** | +6 |
| 6-A6 | `vidlift/subs_job.py:174` | `run_subs_job(..., gpu_lock_factory=None, on_device=None) -> SubsJobResult:` | Additive trailing kwarg, forwarded verbatim to the core (§6.4). | +4 |
| 6-A7 | `vidlift/mcp_bridge/jobs.py:455` (after `on_step`) | closure + call (`:493`) | `on_device` publisher closure; `run_subs_job` call gains `on_device=on_device` (§6.5). | +14 |
| 6-A8 | `vidlift/mcp_bridge/tools.py:429-478` | `_transcription_echo(record, state, manifest) -> dict` | **Live-first read** (state.progress keys → mirror when running → existing result/manifest arms). NO key changes (§6.6). | +20 −6 |
| 6-A9 | `vidlift/gui.py` module level, before `run_stage_label` (:338) | `def _device_display(snapshot: dict \| None) -> str \| None:` + `def _with_device_suffix(label: str, snapshot: dict) -> str:` | **New pure helpers** (§6.7). | +46 |
| 6-A10 | `vidlift/gui.py:388-403, :465-483` | `run_stage_label` | Download early-returns + the label tail route through `_with_device_suffix` (§6.7). | +6 −4 |
| 6-A11 | `vidlift/gui.py:2248-2256` | `_render_detail_for_row` LIVE overlay | Insert the `Device: …` line between the `Job:` line and the `Error:` arm (§6.7). | +6 |
| 6-A12 | `vidlift/monitor.py` | — | **NO CHANGE** (pass-through inherits). | 0 |

### 6.2 Core fire sites — exact code

Fire site 1 (6-A2), after `resolved = resolve_transcription_device(device_pin)` / `device_field = resolved`:

```python
            # v0.4.19 (WS3): publish the resolution the moment the
            # decision exists — the pre-transcription phase's device is
            # no longer invisible (the contention-was-silent class).
            if on_device is not None:
                on_device(resolved, device_pin)
```

Fire site 2 (6-A3), inside `_default_on_fallback` after `fallback_notes.append(note)`:

```python
                # v0.4.19 (WS3): the fallback wrapper ALWAYS lands on CPU
                # (the result-device rule at the TranscriptionResult
                # tail) — the note's existence settles the honest device.
                if on_device is not None:
                    on_device("cpu", device_pin)
```

Signature/docstring (6-A1), trailing after `vad_report`:

```python
    vad_report: dict | None = None,
    # v0.4.19 (WS3): resolved-device publication — (device, requested)
    # fires when the CORE resolves (default factory path; a foreign
    # factory keeps the D4 honest-unknown rule — the core never saw the
    # decision) and again as ("cpu", requested) if the fallback wrapper
    # rescues. Absent = zero behavior change.
    on_device: Callable[[str, str], None] | None = None,
) -> "TranscriptionResult":
```

### 6.3 Mode A publication — exact code

6-A4 (`run.py:2662-2665` region):

```python
                        knobs = plan.transcription or {}
                        transcribe_device = resolve_transcription_device(
                            knobs.get("device") or "auto"
                        )
                        # v0.4.19 (WS3): publish the resolution to the
                        # live surface IMMEDIATELY (pre-download,
                        # pre-construction) — the status echo reads
                        # these progress keys live-first, so the UI
                        # answers "which device" from the moment the
                        # decision exists, not just post-run.
                        progress = dict(state.progress or {})
                        progress["transcription_device"] = transcribe_device
                        progress["transcription_device_requested"] = (
                            knobs.get("device") or "auto"
                        )
                        state.progress = progress
                        state.touch()
```

6-A5 (inside `_on_cuda_fallback`, after the `transcribe_device` mutation):

```python
                        # v0.4.19 (WS3): the fallback settles the honest
                        # device mid-flight on the SAME live keys.
                        progress = dict(state.progress or {})
                        progress["transcription_device"] = transcribe_device
                        state.progress = progress
                        state.touch()
```

(The closure's `nonlocal transcribe_device` and the R1 wording rule at `run.py:2643-2651` are untouched.)

### 6.4 `run_subs_job` — exact edits (6-A6)

Signature tail:

```python
    should_cancel=None,
    download_log=None,
    gpu_lock_factory=None,
    # v0.4.19 (WS3): forwarded verbatim to the core; absent = the
    # pre-0.4.19 silent-resolution behavior for direct/library callers.
    on_device=None,
) -> SubsJobResult:
```

Core call (`subs_job.py:252-268`) gains as its last kwargs:

```python
            should_cancel=should_cancel,
            download_log=download_log,
            on_device=on_device,
```

### 6.5 Executor publication — exact code (6-A7)

Insert after the `on_step` closure (`jobs.py:455`):

```python
    # v0.4.19 (WS3): the live device publication — the resolved device
    # and the requested pin land in the SAME progress dict the GUI's
    # 1 Hz tick, the MCP status tool, the HTTP /status envelope, and
    # the cross-process mirror already serve verbatim;
    # _transcription_echo reads these keys live-first for subs records
    # (no new payload keys; api_version stays 1).
    def on_device(device: str, requested: str) -> None:
        progress = dict(state.progress or {})
        progress["transcription_device"] = device
        progress["transcription_device_requested"] = requested
        state.progress = progress
        state.touch()
        record.touch()
```

The `run_subs_job` call tail (`jobs.py:493-494`) becomes:

```python
            should_cancel=cancel,
            on_device=on_device,
        )
```

### 6.6 `_transcription_echo` — exact rewrite (6-A8)

```python
def _transcription_echo(record, state, manifest) -> dict:
    """Additive status key (visibility ADR-3): the RESOLVED
    transcription device, honest-null everywhere a fact was not
    recorded — NEVER projected, NEVER inferred (the anti-lie rule;
    ``engine_note`` is not consulted).

    ... (the v0.4.15.3/v0.4.16 docstring paragraphs stay verbatim) ...

    v0.4.19 (WS3): the LIVE-first arm — from v0.4.19 the runner
    publishes the resolved device (+ requested pin) into
    ``state.progress`` the moment the decision exists
    (``transcription_device`` / ``transcription_device_requested``), so
    a RUNNING job answers the honest device from resolution onward
    instead of staying null until the phase/record closes. Same-process
    reads use the state_box RunState; cross-process reads use the
    persisted mirror, trusted only while the record is running (the
    D-12 dead-writer rule, mirrored from _transcription_model_echo).
    Older records/runners simply lack the keys — every v0.4.15.3..18
    behavior below is byte-identical for them.
    """
    echo: dict = {"device": None, "device_requested": None}
    # v0.4.19 (WS3): live fact read (same-process state, else the
    # running-only mirror).
    live_progress: dict = {}
    if state is not None:
        live_progress = getattr(state, "progress", None) or {}
    else:
        snapshot = getattr(record, "progress_snapshot", None) or {}
        if getattr(record, "state", None) == "running" and snapshot:
            live_progress = snapshot.get("progress") or {}
    live_device = live_progress.get("transcription_device") or None
    live_requested = (
        live_progress.get("transcription_device_requested") or None
    )
    if record.kind == "subs":
        requested = (record.options or {}).get("device")
        echo["device_requested"] = live_requested or (
            requested if requested else None
        )
        result = record.result or {}
        # live beats result; result's "" stays the D4 honest unknown
        echo["device"] = live_device or result.get("device") or None
        return echo
    # run-kind jobs (Mode A): live first, then the phase/manifest echo
    echo["device"] = live_device
    echo["device_requested"] = live_requested
    subs_block = getattr(state, "subtitles", None) if state is not None else None
    block = None
    if isinstance(subs_block, dict):
        block = subs_block
    elif manifest is not None and isinstance(
        (manifest.get("run") or {}).get("subtitles"), dict
    ):
        block = manifest["run"]["subtitles"]
    if block:
        tr = block.get("transcription") or {}
        echo["device"] = echo["device"] or tr.get("device") or None
    return echo
```

T-1..T-6 of `test_v0416_transcription_echo.py` stay green untouched: the live keys are absent in every scripted fixture there, so every arm resolves exactly as v0.4.16.

### 6.7 GUI displays — exact code

6-A9 (module level, immediately before `run_stage_label`):

```python
def _device_display(snapshot: dict | None) -> str | None:
    """v0.4.19 (WS3): the ONE device-wording rule. Reads the additive
    ``transcription`` echo (``device`` / ``device_requested``) that rides
    every status payload, and renders the honest state:

      device "cuda"                     -> "GPU (CUDA)"
      device "cpu" + requested "cpu"    -> "CPU"
      device "cpu" + requested unknown  -> "CPU"        (legacy records)
      device "cpu" + requested auto/cuda-> "CPU (GPU unavailable)"
      device "cpu (cuda unavailable)"   -> "CPU (GPU unavailable)"
      other truthy device               -> verbatim (never massaged)
      device null + requested, queued   -> "<requested> (requested)"
      device null + requested, running  -> "<requested> — resolving…"
      both null                         -> None (no display at all)

    Honest-null discipline: an absent fact renders NOTHING (older
    bridges, non-transcribing jobs, run jobs before the plan resolves)
    — never a fabricated "CPU" before the fallback actually happened.
    """
    if not snapshot:
        return None
    tr = snapshot.get("transcription") or {}
    device = tr.get("device")
    requested = tr.get("device_requested")
    if device:
        if device == "cuda":
            return "GPU (CUDA)"
        if device == "cpu (cuda unavailable)":
            return "CPU (GPU unavailable)"
        if device == "cpu":
            return (
                "CPU"
                if (requested == "cpu" or not requested)
                else "CPU (GPU unavailable)"
            )
        return str(device)
    if requested:
        if snapshot.get("state") == "queued":
            return f"{requested} (requested)"
        return f"{requested} — resolving…"
    return None


def _with_device_suffix(label: str, snapshot: dict) -> str:
    """v0.4.19 (WS3): the transcription-family stage labels (Mode B
    ``transcribe``/``subs-mux``, Mode A ``subtitles``) carry the
    RESOLVED device once known — the at-a-glance answer. Pre-resolution
    and requested-only states never ride the stage label (no ETA/busy
    key churn on the resolve edge for them; the detail overlay carries
    the resolving wording).
    """
    if (snapshot or {}).get("current_phase") not in (
        "transcribe",
        "subtitles",
        "subs-mux",
    ):
        return label
    tr = (snapshot or {}).get("transcription") or {}
    if not tr.get("device"):
        return label
    display = _device_display(snapshot)
    if display is None:
        return label
    return f"{label} — {display}"
```

6-A10 (`run_stage_label`): the two download early-returns (`:395-402`) wrap: `return _with_device_suffix(f"downloading transcription model — …", snapshot)`; the label tail (`:465-483`) restructures to assign `label` at the three exits and end with `return _with_device_suffix(label, snapshot)`:

```python
    # §3b: the phase_note suffix — ... (comment unchanged) ...
    note = progress_state.get("phase_note")
    if note:
        if isinstance(note, str) and note.startswith(base):
            label = note
        else:
            label = f"{base} ({note})"
    elif step and not (
        isinstance(step, str)
        and (
            step.startswith("subtitle")
            # a token that is already rendered inside base ... (unchanged)
            or step == phase
            or (isinstance(step, str) and step in base)
        )
    ):
        label = f"{base} ({step})"
    else:
        label = base
    # v0.4.19 (WS3): the resolved-device suffix for the transcription
    # family (suffix helper owns the phase/fact gates).
    return _with_device_suffix(label, snapshot)
```

6-A11 (`_render_detail_for_row`), inserted after the `lines = [...]` construction (`:2245-2248`), before the `Error:` arm:

```python
        # v0.4.19 (WS3): the device fact line — the resolved GPU/CPU
        # truth, the requested pin while resolving/queued, absent when
        # neither fact exists (older bridges, non-transcribing jobs).
        device_display = _device_display(snapshot)
        if device_display is not None:
            lines.append(f"Device: {device_display}")
```

---

## 7. WS4 — test-coverage gaps (F-1, F-7, D-2)

### 7.1 Change table

| # | Module : anchor | Binding | Change | LoC |
|---|---|---|---|---|
| 7-A1 | `tests/test_v0418_subs_suppress_cleanup.py` (append after test_23, :335+) | `def test_24_stale_default_scratch_survives_custom_workdir(tmp_path, engine_on):` | New test (F-1) — exact code §7.2. | +30 |
| 7-A2 | `tests/test_client_local.py` (after `:122`) | `def test_client_local_hundred_file_subs_batch_enqueues(tmp_path, monkeypatch):` | New test (F-7) — exact code §7.3. | +33 |
| 7-A3 | `tests/test_v0417_failpath_fix.py:252-259, :362-388` | `_alive` + local `_ScriptedClient` + local `_window` | **Deleted**; replaced by `from test_v0414_mode_b_gui import _ScriptedClient, _window  # v0.4.19 (D-2): house harness` in the GUI-level import block (after `:240`). Module header gains the D-2 line. Zero assertion changes. | +4 −115 |

### 7.2 F-1 — exact test code (7-A1)

```python
def test_24_stale_default_scratch_survives_custom_workdir(
    tmp_path, engine_on
):
    """Karen F-1 (v0.4.18 validation): the mutation-3b hole, now pinned.
    A STALE default-named {output}.subs-work (retained from a PRIOR
    failed run, per the failure-retention rule) must survive the
    CURRENT successful run's cleanup when that run used a
    caller-supplied workdir — the path guard compares the CURRENT
    workdir against the default-derived name, and they legitimately
    differ. Without the guard, a success would silently rmtree the
    prior run's retained diagnostic scratch."""
    from vidlift.subs_job import cleanup_subs_scratch

    stale = tmp_path / "movie.subs.mp4.subs-work"
    stale.mkdir()
    (stale / "prior-run-diagnostics.txt").write_text("retained")
    mine = tmp_path / "mine"
    mine.mkdir()
    runner = FakeRunner(_probe_payload())
    result = run_subs_job(
        _media(tmp_path),
        probe_runner=runner,
        runner=runner,
        workdir=str(mine),
        **_job(),
    )
    # the stale dir is NOT this run's scratch — a SUCCESS must not
    # delete it, and no cleanup note may be claimed for it.
    assert (stale / "prior-run-diagnostics.txt").read_text() == "retained"
    assert not any(
        "cleanup subs scratch" in note for note in result.notes
    )
    # the helper agrees at the direct-call level: the path guard
    # returns [] (nothing removed) for the custom-workdir pair.
    assert (
        cleanup_subs_scratch(str(tmp_path / "movie.subs.mp4"), str(mine))
        == []
    )
    assert stale.is_dir()
```

### 7.3 F-7 — exact test code (7-A2)

```python
def test_client_local_hundred_file_subs_batch_enqueues(tmp_path, monkeypatch):
    """Karen F-7 (v0.4.18 validation): the flagship 64→500 acceptance —
    'a 100-file Mode B folder add enqueues' — pinned against the REAL
    registry/executor client (the symbolic MAX_GUI_BATCH + 1 cap test
    alone never proved the in-range case): 100 records land, in
    submission order, one subs job per path, no BatchTooLarge, and the
    persisted registry file agrees."""
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    files = []
    for i in range(100):
        path = tmp_path / f"ep{i:03d}.avi"
        path.write_text("")
        files.append(str(path))
    client = LocalBridgeClient(state_dir=tmp_path, start_executor=False)
    try:
        job_ids = client.enqueue_subs(files)
        assert len(job_ids) == 100
        queued = client.registry.queued()
        assert len(queued) == 100
        assert [record.paths[0] for record in queued] == files
        for record in queued:
            assert record.kind == "subs"
            assert record.options["device"] == "auto"
            assert record.options["force"] is False
        data = json.loads((tmp_path / "jobs.json").read_text())
        saved = [row for row in data["jobs"] if row["kind"] == "subs"]
        assert len(saved) == 100
    finally:
        client.stop()
```

### 7.4 D-2 migration — exact diff shape (7-A3)

- Header docstring gains one line after the "This file pins the fixed journey" paragraph: "v0.4.19 (D-2, Bob's decision): the client double is the HOUSE harness (`test_v0414_mode_b_gui._ScriptedClient`/`_window`), not a per-file copy — the v0.4.18 `suppress_srt` ripple was the drift this rule exists to kill."
- Import block after `from vidlift.gui import MainWindow  # noqa: E402` gains:
  ```python
  from test_v0414_mode_b_gui import (  # noqa: E402  (D-2: house harness)
      _ScriptedClient,
      _window,
  )
  ```
- Delete `:252-259` (`_alive`) — no: `_alive` is used ONLY by the local `_window`; the shared harness owns its own `_alive`. Delete `_alive` (`:252`), the local `class _ScriptedClient` (`:255-359`), and the local `_window` generator (`:362-388`). Keep `qapp` fixture (`:246-249`), `_PROBE_FAILURE`, `_PROBE_ERROR`, `_report`, `_plan_for`, `_row_state`.
- **Verified compatible** (no test-body edits): shared `make_job(kind=...)` covers all four GUI tests' `kind="subs"`/`kind="run"` calls; shared `enqueue_subs` accepts `replace_source`/`suppress_srt` kwargs; shared `cancel_batch` appends a terminal snapshot via `script_tail` (consumed within the queued-cancel test's 4-tick window; the GUI ignores the return map, `gui.py:2552`); shared `_window` additionally no-ops `time.sleep` (harmless here); shared `enqueue_run` drops the exhaustion `AssertionError` (never triggered — every test scripts exactly as many ids as it picks).
- The three bridge/transport tests (`:116-230`) never touch the client class — untouched.

---

## 8. Version + release hygiene

| File | Edit |
|---|---|
| `packaging/version_info.txt` | `filevers=(0, 4, 19, 0)` · `prodvers=(0, 4, 19, 0)` · `StringStruct('FileVersion', '0.4.19.0')` · `StringStruct('ProductVersion', '0.4.19.0')` — all four values, one pass. |
| `vidlift/__init__.py:7` | `__version__ = "0.4.19.0"` |
| `pyproject.toml` (`version =` line) | `version = "0.4.19.0"` |
| `CHANGELOG.md` | New `## [0.4.19.0] - 2026-11-04` section above `[0.4.18.0]` (Forge writes entries; skeleton below). |

CHANGELOG skeleton (Forge fills exactly these entries; Quill tone-checks):

```
## [0.4.19.0] - 2026-11-04

### Fixed
- Cancel is now observed during the pre-transcription steps (model
  resolution incl. first-run download, engine load, language
  detection) in BOTH modes — a job parked at `transcribe-language` no
  longer ignores Cancel Current/Cancel Batch. (Boundary, honest: a
  process blocked INSIDE a hung native detect/model-load call still
  needs an app quit; every Python-level window is now observed.)

### Added
- Plan-time "already done" verdicts for Mode B: sources that already
  carry a usable text subtitle track render "skip — already has
  subtitles" and are never enqueued (a would-overwrite still shows
  "blocked — subtitles output already exists (will not be
  overwritten)"); GUI, CLI, and MCP surfaces share one verdict helper;
  --force remains the override (subtitle check only). The recursive
  collector additionally never re-ingests VidLift's own *.subs.mp4
  outputs (both modes + folder drop).
- CPU/GPU device indicator: the in-progress job shows the resolved
  transcription device — "GPU (CUDA)", "CPU", or an honest "CPU (GPU
  unavailable)" when auto fell back — plus "auto — resolving…" /
  "auto (requested)" before the decision exists (stage label suffix +
  the detail overlay's Device line; the /status payload's additive
  transcription echo now serves live facts, api_version still 1).
```

And under `### Changed`: "CLI `vidlift subs`: already-subtitled / output-exists files are reported as skip/blocked and no longer count as failures (exit 0 for an all-done batch)."

---

## 9. Implementation milestones (Forge)

Each milestone is independently testable and lands green; commit per milestone.

1. **M1 — WS1 (cancel observes; start here: HIGH).** Rows 4-A1…4-A6 + File A groups W1a/W1b tests. No GUI/CLI changes. Gate: targeted pytest of File A WS1 groups + the full WS1 regression pins (test_v0416_cancel_propagation untouched-green).
2. **M2 — WS3 (device indicator).** Rows 6-A1…6-A11 + File A groups W3a/W3b/W3c tests. Lands on top of M1 in `transcribe.py` (the 4-A4/6-A1 signature edit is applied ONCE with both kwargs — do M1's signature paragraph without `on_device`, then M2 appends it, to keep M1's diff clean). Gate: File A WS3 groups + `test_v0416_transcription_echo.py` green (untouched) + `test_progress_honesty.py`/`test_gui.py` stage-label suites green (suffix is additive-gated).
3. **M3 — WS2 (plan-time skip).** Rows 5-A1…5-A11 + File A group W2 tests + File E amendments in the same milestone (the two amended v0414 tests would fail red between "helper added" and "GUI rewired" — keep them atomic). Gate: full suite.
4. **M4 — WS4 (test coverage).** Rows 7-A1…7-A3. Gate: full suite offscreen-green on BOTH forged platforms where feasible.
5. **M5 — Version + changelog.** §8, then the release sweep Karen prescribes (no double-bump: pyproject/__init__/version_info flip exactly once, here).

Forbidden drift: no new QSettings keys; no registry schema change; no `api_version` bump; no `run.py` edits beyond the three enumerated touches (4-A6, 6-A4, 6-A5); no edits to `test_v0416_transcription_echo.py`, `test_v0416_cancel_propagation.py`, or the run-side guards/`_on_segment`/post-mux tail.

---

## 10. Exact strings catalog (stable text Karen can assert)

**WS1 (cancel messages, `EngineCancelled` args):**
- S-1 `"cancelled during model resolution"` (core O1 + `resolve_model_dir` entry)
- S-2 `"cancelled during model download"` (armed download beat)
- S-3 `"cancelled during model load"` (post-construction)
- S-4 `"cancelled before language detection"` / `"cancelled during language detection"` (detect edges)
- Unchanged, NOT to be retexted: `"cancelled during transcription"` (`transcribe.py:2432`), `"cancelled after remux, before replace"` (`subs_job.py:304`).

**WS2 (GUI cells/summaries):**
- S-5 skip status cell: `"skip — already has subtitles"`
- S-5a skip row: col2 `"skip (no transcription)"`, col3 `"no action"`, col6 `"already subtitled — nothing to do"`, col7 `""`
- S-6 `"subs ready"` / `"blocked — subtitles output already exists (will not be overwritten)"` (verbatim, unchanged); probe-error warning col7: `"plan probe inconclusive — re-checked at run time"`
- S-7 no-work summary: `f"{0} queued for subtitles — N skipped (already subtitled), M blocked (output exists)"` (extras clause omitted when zero) + note `"Skipped and blocked files are not enqueued; the run-side guards remain the backstop."` — the composed text comes from `_subs_enqueue_summary`; with normal picks: `"<N> queued for subtitles"` (+ `" — N skipped (already subtitled)"`, `", M blocked (output exists)"`)
- S-8 CLI lines: `"  skip — already has subtitles"` · `"  blocked — output already exists (never overwritten): {subs_output_path}"`
- S-9 MCP reasons: `"already has a usable text subtitle track"` · `"output already exists (never overwritten)"` (additive keys `skipped: true`, `job_id: null`, `ok: true`)
- Verdict constants (structural, never user-visible): `"ready"` / `"skip-subtitles"` / `"blocked-output-exists"` / `"probe-error"`

**WS3 (display):**
- S-10 overlay: `"Device: GPU (CUDA)"` · `"Device: CPU"` · `"Device: CPU (GPU unavailable)"` · `"Device: auto — resolving…"` · `"Device: auto (requested)"` (requested value substituted; `…` is U+2026, matching the busy-label family)
- S-11 stage suffixes: `" — GPU (CUDA)"` · `" — CPU"` · `" — CPU (GPU unavailable)"` (appended to transcription-family labels only, resolved device only)
- progress keys (structural): `"transcription_device"`, `"transcription_device_requested"`

---

## 11. Failure analysis

- **R-1 (accepted boundary, documented):** cancel flipped while blocked INSIDE a hung native detect/model-load call is unobservable by design (WS1-D5). Mitigation: unchanged app-quit remediation; the stale-activity yellow row (`gui.py:2660-2672`) still names the wedge honestly. Revisit criterion: if Eric hits the wedge again AFTER 0.4.19.0, the fix is executor process isolation (killable worker), not more polls.
- **R-2 (named follow-up):** GPU-phase-lock WAIT is unobserved; a subs job queued behind a 40-minute upscale hold can't cancel out of the wait. Not regressed (pre-0.4.19 identical); candidate for a future `should_cancel`-aware wait loop.
- **R-3 (performance):** plan-time probing is synchronous on the pick path — up to 500 ffprobe calls worst case (same shape as Mode A's Plan click; output-exists verdicts are probe-free). Revisit criterion: Eric reports the picker stalling → async progress-dialog pre-flight.
- **R-4 (probe fault honesty):** plan probes fail OPEN (`probe-error` → enqueue; run-side names the truth). Never a silent skip on a probe fault.
- **R-5 (arity regression):** the download-cancel arming is installed only where a progress channel exists; legacy 2-arg fetch fakes keep their shape (M-1). Pinned in §12 W1a-T6.
- **R-6 (fallback wrapper swallow):** impossible by construction — the resilient engine's arms re-raise `EngineCancelled` first (D2, `transcribe.py:660-676`, `:707-722`). Pinned by W1a-T4 raising between construction and detect (no rebuild side-effect asserted).
- **R-7 (mirror staleness):** cross-process readers see the device only after the ≤10 s mirror cadence (or the touch-path mirror); same-process readers see it at once. Consistent with every existing progress fact.
- **R-8 (exit-code consumers):** scripted pipelines keying on `vidlift subs` exit 1 for already-done files will now see 0 — deliberate, documented in CHANGELOG Changed.

---

## 12. Combined test plan

### File A (new, ~420 LoC): `tests/test_v0419_followup_batch.py`

`"""v0.4.19 — cancel pre-transcribe + plan-time skip + device indicator."""` Conventions: house fakes — `from test_v0414_subs_job import FakeEngine, FakeRunner, _probe_payload, _seed_model_state` (plus the `engine_on` fixture pattern of test_v0418_subs_suppress_cleanup.py and the `_lock`/`NoLock` shape); GUI tests import `from test_v0414_mode_b_gui import _ScriptedClient, _window`. No network/GPU/real-model. `qapp` fixture pattern verbatim from the house harness.

**W1a — core cancel observes (transcribe.py, engine_on + seeded model state):** helper `_core(tmp_path, **over)` builds the `transcribe_source_to_srt` call with `FakeRunner(_probe_payload())`, `state_root=str(tmp_path/"state")` + `_seed_model_state(...)`, `engine_factory=lambda p: FakeEngine(p)`, `on_step=steps.append`.
- T1 flag True at entry → raises `EngineCancelled`, str == S-1, steps == `["transcribe-resolve"]`, and the factory was NEVER called (counter seam).
- T2 factory flips the flag (closure box) → raises S-3 `"cancelled during model load"`, steps == `["transcribe-resolve"]`, factory counter == 1, `FakeEngine.detect_language` calls == 0.
- T3 `on_step` flips the flag on `"transcribe-language"` → raises S-4-before, engine `.detect_language` call count == 0.
- T4 engine `detect_language` flips the flag mid-detect → raises S-4-after (`"cancelled during language detection"`), steps end at `"transcribe-language"` (never `"transcribe-run"`), and NO CPU-rebuild side-effect occurred (the wrapper's D2 re-raise discipline — drives the fallback path via the wrapper fake, engine `_ResilientEngine` arm pins unchanged).
- T5 flag flips only at `transcribe-run` → the EXISTING `_on_segment` observe raises `"cancelled during transcription"` (proves the v0.4.14 observe is untouched and still the first segment-level gate).
- T6 download-beat arming: `resolve_model_dir` with injected arity-3 fetch + caller progress cb; should_cancel True after the first beat → raises S-2; assert the `.complete` marker absent and no `.part` left under the seeded tag dir; ALSO: injected legacy arity-2 fetch with NO progress cb and should_cancel=True → raises S-1 at ENTRY (never a TypeError — the conditional-arity pin, R-5). And should_cancel=None + arity-2 fetch → byte-identical legacy behavior (returns the path contract-faked… use a fetch that materializes the expected support files + model.bin honoring expected_size via the tag's MODEL_SUPPORT_FILES sizes; if pinning real sizes proves brittle, scope T6c to the arity call-shape assertion: fetch received exactly 2 args).
- T7 absent `should_cancel` (None) → golden path unchanged: same cue count/srt as the v0.4.14 FakeEngine golden (guard against behavioral drift of the golden path).

**W1b — both modes close `cancelled` (executor integration, the test_v0416_cancel_propagation harness shape):**
- T8 Mode B: real registry/McpServer with a `transcribe_engine_factory` that BLOCKS on an event; after the job claims, main cancels via the registry cancel API, then releases the event → job closes `cancelled` (never `failed`), error names a WS1 message; registry transition asserted at `jobs.py:752-753` level.
- T9 Mode A: run job with transcription enabled, same blocking factory → closes `cancelled`; manifest/log fields per the run mapping arm.
- T10 mapping pins (unit): `_execute_subs_job`/`_execute_run_job`-level — `EngineCancelled` raised by the stubbed core → `JobOutcome(cancelled=True)` (guards the `jobs.py:271/:495` mappings against reorder regressions).

**W2 — plan-time skip:**
- T11 `subs_plan_verdict` matrix: output-exists → `"blocked-output-exists"` WITHOUT probing (counting probe_runner asserts zero calls); force+output-exists → blocked; force alone → `"ready"` without probing; text-subs payload → `"skip-subtitles"`; bitmap-only (`pgssub`/`dvd_subtitle` stream) → `"ready"`; ProbeError → `"probe-error"`; OSError (missing binary) → `"probe-error"`.
- T12 collector: nested tree with `a.subs.mp4`, `B.SUBS.MP4`, `plain.mp4`, `x.subs.mkv` → only `plain.mp4` and `x.subs.mkv` collected (suffix-case pin + the not-our-output pin).
- T13 GUI pick (house harness): three scripted files — verdicts ready/skip/blocked (monkeypatch `gui_mod.subs_plan_verdict` to a dict-driven fake) → `enqueue_subs` receives ONLY the ready path; rows = [ready row "subs ready", skip row S-5/S-5a cells, blocked row S-6]; `win.job_ids == ["VJ1"]`; `_batch_row_for_job == {"VJ1": 0}` (slate mapping preserved); summary contains `"1 queued for subtitles"` + `"1 skipped (already subtitled)"` + `"1 blocked (output exists)"`.
- T14 all-skipped pick: rows render, `win.job_ids == []` (fresh pick), slate/timer NOT bootstrapped (`win._batch_row_for_job == {}`), summary = the S-7 no-work compose; detail pane on the skip row shows `"Verdict: skip — already has subtitles"`.
- T15 conflict guard over ready-only: live subs job over the SKIPPED file only (queue_views fake) → NO dialog, pick proceeds.
- T16 CLI: monkeypatched `vidlift.subs_job.subs_plan_verdict` verdicts → skip/blocked print S-8 lines, exit 0 on all-done; a real failure (run_subs_job raising SubsJobError, monkeypatched) still yields exit 1; `--force` passes `force=True` into the verdict helper (spy).
- T17 MCP `tool_transcribe`: probe_runner fake serving a text-subs payload → row `{"skipped": True, "reason": S-9a, "job_id": None, "ok": True}`; `force: true` → enqueued; unknown-file arm unchanged (`ok: False`).
- T18 run-side backstop unchanged: the two existing guard tests (test_v0414_subs_job) stay green UNEDITED — plus one NEW pin: direct `run_subs_job` on already-subtitled source raises `SubsJobError` with the SAME pre-0.4.19 message (the backstop never regressed into a silent skip).

**W3 — device indicator:**
- T19 core seam: monkeypatched `transcribe.resolve_transcription_device` → `"cuda"` + monkeypatched `transcribe.make_whisper_engine_with_fallback` → FakeEngine → `on_device` call log == `[("cuda", "auto")]`; the wrapper-invoking fallback fake → second call `("cpu", "auto")`; foreign factory → NO calls (D4 pin).
- T20 `_transcription_echo` unit matrix (fakes like test_v0416's `_record`/`_live`): subs live-progress keys present → live wins over options; subs terminal (no live) → result arm intact; run-kind live keys → served; run-kind live-absent + `state.subtitles` → v0.4.16 arm intact; cross-process arm: `progress_snapshot` trusted only when `record.state == "running"` (terminal + stale mirror → null); the six T1-T6 fixtures re-asserted in file D's stead… no: T1-T6 live in test_v0416 (untouched); T20 adds ONLY the new arms (live subs, live run, mirror-fallback, dead-writer).
- T21 `_device_display` pure matrix (no Qt): every S-10 mapping + both-null → None + foreign string verbatim.
- T22 `run_stage_label` suffix: snapshot `{"state": "running", "current_phase": "transcribe", "progress": {}, "transcription": {"device": "cuda", "device_requested": "auto"}}` → `"transcribing audio — GPU (CUDA)"`; segments variant → `"transcribing audio — 12 segments done — CPU (GPU unavailable)"`; phase `"encode"` with the same transcription block → NO suffix; missing transcription key → byte-identical v0.4.18 label; requested-only → NO suffix; download-arm label gets the suffix when device known.
- T23 GUI overlay: scripted running snapshot carrying the echo → details contain `"Device: GPU (CUDA)"`; scripted queued subs snapshot with `device_requested` only → `"Device: auto (requested)"`; no echo → no `Device:` line at all.

### File B (amended, +30): `tests/test_v0418_subs_suppress_cleanup.py`
7-A1 appended (module docstring group-5 line gains "24 stale-default scratch + custom workdir → survives, [] returned (F-1)"). Everything else untouched.

### File C (amended, +33): `tests/test_client_local.py`
7-A2 appended after the cap test.

### File D (amended, +4 −115): `tests/test_v0417_failpath_fix.py`
7-A3 (D-2 migration).

### File E (amended, ±12): `tests/test_v0414_mode_b_gui.py` (enumerated harness amendment)
- `test_subs_collision_blocked_row_never_overwrites` (:498-540): re-aim at the v0.4.19 truth — docstring updated ("…the blocked file is NEVER ENQUEUED under v0.4.19 WS2; the run-side gate remains the backstop for direct enqueues"); monkeypatch `gui_mod.subs_plan_verdict` deterministic fake (fresh→ready, blocked-path→blocked); assert `win.job_ids == ["VJ1"]`, blocked row cells verbatim (S-6), old output bytes untouched; the scripted `"VJ2"` is simply never claimed.
- `test_subs_rows_output_cell_uses_subs_output_path_for_every_extension` (:542+): pass `verdicts={p: SUBS_VERDICT_READY for p in paths}` explicitly (imports the constant).
- All OTHER tests in the file untouched: all-ready picks keep pick order (partition is order-preserving), `"subs ready"` cell text unchanged, and the scripted subs flows probe junk files → `probe-error` → ready, so no mock needed for them (the pre-flight's fail-open arm IS what keeps them green — this is deliberate and must be called out in the commit message).

### Unmodified-pass invariants (must hold with zero edits)
- `test_v0416_transcription_echo.py` (T1-T6), `test_v0416_cancel_propagation.py`, `test_v0414_transcribe.py`, `test_v0414_subs_job.py` (both run-side guard tests), `test_progress_honesty.py`, `test_gui.py` (incl. F-13b), `test_v0413*.py`, `test_v04151_subs_guard.py`, `test_v0418_recursive_folder_add.py`, `test_v0418_ui_polish.py`, `test_stale_activity.py`, `test_model_management*.py`, `test_mcp_bridge.py`, `test_monitor*.py`, `test_gui_log_tail_pin.py`, `test_registry_*.py`, `test_v0417_subs_replace_source.py`, `test_v0417_ui_restructure.py`, `test_v0418_subs_suppress_cleanup.py` (pre-existing tests 14-23), `test_client_local.py` (pre-existing), `test_v04162_vad_packaging.py`.
- If ANY of these needs an edit, that is a spec deviation — Forge stops and reports; Karen treats it as a finding.

---

## 13. Validation baseline for Karen (expand freely)

**Build/defaults gate:** version 0.4.19.0 consistent in all five places (`version_info.txt` ×4 values, `__init__.py`, `pyproject.toml`); full suite green offscreen; frozen smoke run.

**WS1 (functional):**
- Cancel Current during model-load window (real GPU box, cold CUDA context): job closes `cancelled` within ~1 s of the click, error text is one of S-1/S-3/S-4; batch resumes with the next queued job. Confirm BOTH modes (Mode A transcription run and Mode B subs job).
- Cancel during a first-run model download (delete/move the whisper cache on a scratch profile): closes `cancelled`; the `.part` staging is re-usable (a retry resumes, not restarts-at-zero behavior regression).
- Regression: mid-transcription cancel (segment loop) and post-mux cancel still close `cancelled` with S-1-family messages intact (test_v0416_cancel_propagation green).
- Honest boundary (expected, not a failure): a job truly hung INSIDE the native detect call still requires app quit — validate the stale-activity row still renders instead.

**WS2 (functional):**
- Re-add a completed Mode B folder: previously-red files now show "skip — already has subtitles" (renamed `.mp4` sources / embedded-subs sources) or "blocked — subtitles output already exists…" (`*.subs.mp4` present); 0 `failed` records in history; batch summary shows the queued/skipped/blocked compose (S-7); the poll never rewrites the static rows.
- `--force` re-transcribes despite embedded subs (CLI), and still refuses on existing output.
- `vidlift subs` on an all-done folder: exit 0, S-8 lines, no jobs; a folder with one broken file: exit 1 for THAT file only.
- MCP transcribe on a mixed list: additive skipped rows with S-9 reasons; api_version == 1 untouched; no `*.subs.mp4` ever re-ingested via folder-add or drop (any case).
- Probe-fault edge: temporarily hide ffprobe → picks still enqueue (probe-error rows carry the S-6 inconclusive warning cell); run-side names missing-tool honestly.

**WS3 (functional/UI):**
- Mode B on the 3090 Ti box: running subs job shows `Device: GPU (CUDA)` in the detail overlay and ` — GPU (CUDA)` on the transcribe stage label.
- GPU contended (hold VRAM via LM Studio): `auto` job lands `CPU (GPU unavailable)` (overlay + suffix) — Eric's original invisibility case.
- `device: cpu` pin → "CPU" (NOT "GPU unavailable"); queued subs rows show "auto (requested)"; pre-resolution shows "auto — resolving…" and NEVER a bare "CPU".
- Mode A transcription run: the same truth during the `subtitles` phase; post-run manifest/history echo unchanged from v0.4.15.3/16; the monitor `/status` shows live device mid-run (cross-process readers tolerate the ≤10 s mirror lag).
- Older terminal records (pre-0.4.19 registry rows) render exactly as v0.4.18 (no Device line when absent).

**WS4:** F-1/F-7 tests exist and pin the described behaviors (mutation check: deleting the cleanup guard fails test_24; restoring MAX_GUI_BATCH=64 fails the 100-file pin); D-2: v0417 file imports the house harness, zero local doubles, all five tests unchanged-green.

**Performance expectations:** picker pre-flight ≈ Mode A Plan-click latency; no per-tick payload growth beyond two progress keys; echo helper is pure dict reads.

**Security/robustness:** no new QSettings keys; no new registry fields; no API surface changes beyond additive payload VALUES; frozen-build imports (PyQt offscreen) unaffected by the new gui helpers (pure functions).

**Acceptance:** every Functional item above + §12 gate files green + unmodified-pass list untouched. Anything in the "expected, not a failure" bullets is a documented boundary, not a waiver for new defects.

---

## 14. Final review checklist

✓ Requirements satisfied — all four workstreams, one release, every open decision answered (§1)
✓ Research/evidence completed — every claim verified against the v0.4.18.0 tree (§0)
✓ Alternatives evaluated — skip-at-plan vs registry-skip-state (registry state rejected: transition-table + poll + monitor churn for the same UX); device via progress keys vs new RunState attribute (progress keys chosen: flow through the mirror + payload with zero transport change); D-2 three-way decided (§1 WS4-D3); per-workstream tradeoffs in §1-§7
✓ Tradeoffs + deviations documented (§1, §2)
✓ Security reviewed — no new trust boundaries; subprocess/arity disciplines preserved (R-5)
✓ Performance reviewed — R-3/R-7 postures stated with revisit criteria
✓ Scalability — 500-file cap family unchanged; skip pre-flight is the bounded-cost addition
✓ Observability — WS3 IS the observability workstream; device facts ride existing channels
✓ Testability — pure helpers (`subs_plan_verdict`, `_subs_enqueue_summary`, `_device_display`, `_with_device_suffix`), seam-first core kwargs, harness reuse
✓ Extensibility — additive-only seams (`should_cancel`, `on_device`, verdict constants); future process-isolation named as the wedge fix path
✓ Technical debt — D-2 dedupe lands; no new QSettings/registry/api debt
✓ Risks documented (§11)
✓ Acceptance criteria defined (§12, §13)
✓ Forge guidance complete — change tables with file:line anchors, exact code, milestone gates, forbidden-drift list
✓ Karen validation baseline complete (§13)
