# Karen Validation Report — VidLift v0.4.19.0 follow-up batch

**Author:** Karen (validation)
**Date:** 2026-11-04
**Spec validated against:** `.specs/bob-vidlift-v0.4.19-implementation-spec.md` (read in full, 1,260 lines; §13 = baseline, §11 = binding drift list)
**Implementation report:** `.specs/forge-vidlift-v0.4.19-implementation-report.md`
**Diff under test:** `git diff ffc13fd..HEAD` (implementation commit `0963a2a`; five milestone commits `ef0040a`/`a6a4920`/`44edfb6`/`46a8b9d`/`8ae161e`)
**Method:** behavioral first — the full suite, the spec's §12 gate files, 13 purpose-built adversarial probes (K1–K12 + the probe-fault edge, scratch files outside the repo), 3 mutation experiments (each restored byte-identical, `git diff` = 0 verified), offscreen GUI rendering with programmatic string assertions, and hunk-by-hunk diff review.

---

## Executive Summary

**Overall Status: PASS (code) / WARN (release — environment gaps only, no defects)**

**Overall Risk: Low**

**Confidence: High** for everything exercisable in this environment; the unexercisable items are enumerated below with reasons.

- **The CODE is CLEARED.** Every §13 criterion that is software-verifiable passes, most proven behaviorally end-to-end through the real executor/registry (not just "code exists"). Zero defects found — no Critical, High, Medium, or Low product defects. The live defect (a job wedged at `detect_language` could not be cancelled) is **proven fixed in both modes**.
- **The RELEASE is CLEARED from the software-validation standpoint**, with three §13 items this environment cannot exercise (no GPU box, no frozen build, no pixel-level image review by this validator). These are validation-environment gaps, not defects; they are listed as the residual pre-ship checklist for Eric's box. Per the spec's own rule (§13): documented boundaries are not waivers for new defects — and no new defect was found.

**Full suite (project interpreter, byte-identical delivered tree):**

```
/home/yetisoldier/.venvs/vidlift/bin/python -m pytest -o 'addopts=' -q
1068 passed, 2 skipped in 107.13 s
```

Reproduced twice (106.32 s, 107.13 s); matches Forge's claim exactly. The 2 skips are the pre-existing Windows-only guards (`tests/test_gpu_meter_no_window.py:132`, `:164` — file untouched by this batch, verified by name in the diff).

---

## Per-criterion PASS/FAIL table (Bob's §13, plus the four workstream briefs)

| # | §13 criterion | Verdict | Evidence (how proved) |
|---|---|---|---|
| B-1 | Version 0.4.19.0 consistent in all five places | **PASS** | `git diff ffc13fd..HEAD -- packaging/version_info.txt vidlift/__init__.py pyproject.toml`: `filevers`/`prodvers`/`FileVersion`/`ProductVersion` all `(0,4,19,0)`/`'0.4.19.0'`; `__init__.py:9` `__version__ = "0.4.19.0"`; `pyproject.toml:3`. Runtime: `import vidlift` → `0.4.19.0`. Single bump: M5 commit `8ae161e` touches exactly the 4 version/CHANGELOG files (no double-bump). |
| B-2 | Full suite green offscreen | **PASS** | 1068 passed, 2 skipped, reproduced twice on the byte-identical tree (`git status` clean after all mutation restores). |
| B-3 | Frozen smoke run | **NOT VERIFIED** | No frozen build exists in this environment (no `dist/`/`build/`; PyInstaller specs present but unbuilt). Not a defect; listed as residual. |
| W1-a | Cancel during model-load window closes `cancelled` (~1 s of click), S-1/S-3/S-4 text, BOTH modes | **PASS** (in-process equivalents) | File A T8 (Mode B) / T9 (Mode A): real registry/McpServer/claim/cancel API; job blocks at the factory, cancel flips, release → `state == "cancelled"`, error `"cancelled during model load"` (S-3), history row `cancelled`, never `failed`. My K1/K2 additionally prove the `detect_language` wedge window in both modes (S-4-after) and K3/K4 the resolve window (S-1). The ~1 s-on-real-GPU timing needs a CUDA box (residual); the Python-level observe latency is a flag read — the cancel lands on the first observe after the blocking call returns (proven: all probes close cancelled immediately after release). |
| W1-b | Cancel during first-run model download closes `cancelled`; `.part` staging re-usable | **PASS** (core level) | T6a: `resolve_model_dir` with injected arity-3 fetch + progress cb; flag flips after beat 1 → raises `EngineCancelled("cancelled during model download")` (S-2); `.complete` absent; no `*.part`/`*.part.tmp` survives. Staging discipline code-verified (`transcribe.py:1381-1398` arms only when a beat channel exists; the default fetcher's `BaseException` arm removes only `.tmp` — spec §0 evidence re-checked against the current tree). A real 1.5 GB network download is not exercisable (suite is local-only by design). |
| W1-c | Regression: mid-transcription + post-mux cancel still close `cancelled` | **PASS** | T5: flag flipped only at `transcribe-run` → the untouched `_on_segment` observe raises `"cancelled during transcription"` (verbatim v0.4.14 message). Post-mux tail byte-identical (extracted from `ffc13fd` and `HEAD`, diffed: IDENTICAL). `test_v0416_cancel_propagation.py`: 0 diff lines, green in the 174-test invariant run. |
| W1-d | Honest boundary: hung native call still needs app quit; stale-activity row renders | **PASS** (documented boundary) | R-1 stated in spec §11, Forge report, and the CHANGELOG ("Boundary, honest"). The observes are before/after the native call by construction (`transcribe.py:2502-2508`). `test_stale_activity.py` untouched-green. My K6 proves the analogous wait-window boundary behaviorally (below). |
| W1-e | **User brief:** cancel observed during BOTH `transcribe-resolve` AND `transcribe-language`, in BOTH modes, raising `EngineCancelled`, closing `cancelled` not `failed` | **PASS** | K1 (Mode B) / K2 (Mode A): job wedged INSIDE `detect_language` (the exact live-defect shape), cancel via the real registry API, release → closes `cancelled` with `"cancelled during language detection"`, never `failed`, history row `cancelled`. K3 (Mode B) / K4 (Mode A): cancel during the resolve window → the real `resolve_model_dir` entry observe raises `"cancelled during model resolution"` → `cancelled`. T8/T9 cover the model-load window in both modes. T10 pins the `jobs.py` `EngineCancelled`→`JobOutcome(cancelled=True)` mappings (subs `jobs.py:510`, run `jobs.py:271`/`:574`). |
| W1-f | **User brief:** the new observes cannot be swallowed by `_ResilientEngine`'s CPU-rebuild arms | **PASS** | K5 drives the REAL `_FallbackEngine` (the spec's `_ResilientEngine`): an `EngineCancelled` from the inner engine's `detect_language`/`transcribe_segments` re-raises verbatim with **zero** ctor calls (no CPU rebuild) and no fallback note; positive control — a non-cancel `RuntimeError` DOES rebuild once + speaks the note (so the test bites). Code: `transcribe.py:670-676`, `:718-723`, `:742-743` (D2 re-raise before any rebuild). `test_v04152_cuda_fallback.py` untouched-green. Additionally: O4/O5 fire in the core OUTSIDE the wrapper's try/except, so the wrapper arms cannot even see them. |
| W1-g | **User brief:** R-2 non-goal — GPU phase-lock WAIT remains unobserved, untouched and documented | **PASS** | K6 (behavioral): held the real `_gpu_phase_lock()`, enqueued a subs job, waited for the visible `"waiting for GPU"` step, cancelled → the job does **NOT** close during the wait (asserted after a 1 s grace — the non-goal holds); on lock release the flipped flag lands at the first post-wait observe (O1, `"cancelled during model resolution"`) → `cancelled`. Code: no `should_cancel` anywhere in the acquire paths (`subs_job.py:315`, `run.py:2619`, `engine_ncnn.py:2332-2376` — signature is `on_wait` only). Documented: spec §11 R-2, Forge report boundary statement. |
| W1-h | §13: batch resumes with the next queued job after the cancel | **PASS** | K12: two-job Mode B batch; first wedges at the factory, cancelled; second job then runs to `succeeded`. |
| W2-a | Re-add a completed Mode B folder: skip/blocked rows, 0 failed records, S-7 summary, poll never rewrites static rows | **PASS** | T13: ready/skip/blocked pick → only the ready path enqueued (`win.job_ids == ["VJ1"]`, slate mapping `{"VJ1": 0}` preserved), rows in the stable partition with exact S-5/S-5a/S-6 cells, summary `"1 queued for subtitles — 1 skipped (already subtitled), 1 blocked (output exists)"`. T14: all-skip pick → static rows, `job_ids == []`, no slate/timer (`_batch_row_for_job == {}`), S-7 no-work compose, detail pane `"Verdict: skip — already has subtitles"`. Skipped/blocked files never enter the registry → structurally cannot become `failed` records. **Real-ffmpeg proof (no fakes):** K7 builds a real MKV with an embedded subrip track (real x264 encode, 115 KB, real ffprobe classification) → `subs_plan_verdict` returns `skip-subtitles`; K8 runs the real CLI on it → `"  skip — already has subtitles"`, exit 0. |
| W2-b | `--force` re-transcribes despite embedded subs; still refuses existing output | **PASS** | T11(b,c): force+output-exists → `blocked` (probe-free); force alone → `ready` (probe-free). T16: `--force` threads `force=True` into the verdict helper (spy). K7 (real ffprobe): force → `ready`; force + existing output → `blocked`. |
| W2-c | CLI all-done folder: exit 0, S-8 lines; one broken file: exit 1 for that file only | **PASS** | T16: all-done → rc 0; a real `SubsJobError` at run time → rc 1. K8 (real CLI, real files): rc 0, both S-8 lines verbatim. Loop structure code-verified: skip/blocked `continue` before the `failures` counter (`cli.py:421-436`). |
| W2-d | MCP transcribe mixed list: additive skipped rows S-9; api_version == 1; no `*.subs.mp4` re-ingested (any case, folder-add + drop) | **PASS** | T17: `{"skipped": True, "reason": "already has a usable text subtitle track", "job_id": None, "ok": True}`; force enqueues; unknown-file arm unchanged (`ok: False`, no skip keys). T12 + K9 (real files): `a.subs.mp4`/`B.SUBS.MP4` excluded, `plain.mp4` + `x.subs.mkv` collected. `api_version`: `monitor.py:52 API_VERSION = 1` untouched, `test_monitor.py:269` asserts `== 1` green; the only diff mentions are two comments. Collector shared by both modes' folder-add and the drop handler (`gui.py` single `collect_video_files`). |
| W2-e | Probe-fault edge: hide ffprobe → picks still enqueue (S-6 inconclusive warning cell); run-side names the tool honestly | **PASS** | Live probe (scratch `karen_probe_fault.py`): `find_tools` monkeypatched to raise `MissingToolError` → GUI pick still enqueues (`job_ids == ["VJ1"]`), row status `"subs ready"`, warning cell `"plan probe inconclusive — re-checked at run time"`; direct `run_subs_job` with a failing probe raises `ProbeError("ffprobe failed on …: ffprobe: command not found")` — the run-side truth, never a silent skip. T11(f) pins `ProbeError`/`OSError` → `probe-error` fail-open. |
| W2-f | Status surface distinguishes skipped from blocked/failed | **PASS** | Three distinct row statuses rendered from the real widget (visual Scene 1): `"subs ready"`, `"skip — already has subtitles"` (col2 `"skip (no transcription)"`, col3 `"no action"`), `"blocked — subtitles output already exists (will not be overwritten)"`; summary composes all three counts. |
| W3-a | Mode B GPU box: `Device: GPU (CUDA)` overlay + ` — GPU (CUDA)` stage suffix | **PASS** (offscreen + live publication; real-3090 Ti rendering is residual) | Offscreen Scene 2 (real MainWindow, scripted payload): overlay contains `"Device: GPU (CUDA)"`; `run_stage_label` → `"transcribing audio — 12 segments done — GPU (CUDA)"`. K10 proves the publication machinery live through the real executor. |
| W3-b | GPU contended → `CPU (GPU unavailable)` (overlay + suffix) — Eric's case | **PASS** (proven LIVE on this CUDA-less box) | K10: a real Mode B job, `auto` request → `resolve_transcription_device("auto") == "cpu"` on this box → the LIVE status payload renders `_device_display == "CPU (GPU unavailable)"` (asserted on the real payload, not a fixture). Scene 4: overlay `"Device: CPU (GPU unavailable)"` + stage `"transcribing audio — 3 segments done — CPU (GPU unavailable)"`. |
| W3-c | `device: cpu` pin → `"CPU"` (not "GPU unavailable"); queued rows `"auto (requested)"`; pre-resolution `"auto — resolving…"`, never a bare "CPU" | **PASS** | T21 pure matrix: cpu+cpu → `"CPU"`; cpu+unknown → `"CPU"`; cpu+auto/cuda → `"CPU (GPU unavailable)"`; queued+requested → `"cuda (requested)"`; running+requested-only → `"auto — resolving…"`; both null → None. T23 + Scene 3 (real widget): `"Device: auto — resolving…"`; requested-only never rides the stage label (`"transcribing audio"` unsuffixed). K10 phase (1): the LIVE pre-resolution payload carries `device: None` + `device_requested: "auto"` — the resolving data contract. |
| W3-d | Mode A same truth during `subtitles` phase; post-run manifest/history echo unchanged; monitor /status live device mid-run | **PASS** | K11: real Mode A run job, blocked at the core factory → LIVE status payload carries `transcription_device`/`_transcription_device_requested` in `progress` and the echo (keys exactly `{"device", "device_requested"}`); job completes `succeeded`. T20: echo arms — subs live wins over result/options; run-kind live served; live-absent keeps the v0.4.16 `state.subtitles` arm; mirror trusted only while `running` (terminal + stale mirror → null). `test_v0416_transcription_echo.py` T1–T6: 0 diff, green. Cross-process: K10 polls `jobs.json` — the mirror persists both keys within the 10 s cadence (R-7). `test_monitor.py` green (envelope pass-through). |
| W3-e | Older terminal records render exactly as v0.4.18 (no Device line when absent) | **PASS** | T22: no `transcription` key → byte-identical v0.4.18 label; T23: no echo → no `Device:` line at all; T20: older records lack the live keys → every pre-0.4.19 arm resolves as before. |
| W3-f | **User brief:** `_transcription_echo` live-first; api_version stays 1; no new payload keys | **PASS** | `tools.py:429-511` rewrite: same-process `state.progress` → running-only `progress_snapshot` mirror (D-12) → intact result/manifest arms; echo dict remains `{"device", "device_requested"}` (asserted on live payloads in K10/K11). api_version: comments only in the diff; `API_VERSION = 1`; `test_monitor.py:269` green. |
| W4-a | F-1 test exists and pins the stale-scratch path-guard; mutation bites | **PASS** | `tests/test_v0418_subs_suppress_cleanup.py:350-385` (test_24, exact spec code). **Mutation executed:** removed the path guard (`subs_job.py:676-680`) → test_24 **FAILS** (stale dir rmtree'd → `FileNotFoundError`); restored byte-identical (`git diff` = 0). |
| W4-b | F-7 100-file enqueue pin; mutation bites | **PASS** | `tests/test_client_local.py:124-155` (exact spec code: 100 records, submission order, per-record kind/options, persisted `jobs.json` agrees). **Mutation executed:** `MAX_GUI_BATCH = 64` → the pin **FAILS** (`BatchTooLarge` at `client_local.py:205`); restored byte-identical. |
| W4-c | D-2 executed, not deferred: house harness imported, zero local doubles, all five v0417 tests unchanged-green | **PASS** | Diff: local `_alive`/`_ScriptedClient`/`_window` deleted (−115 LoC), `from test_v0414_mode_b_gui import _ScriptedClient, _window` added (`test_v0417_failpath_fix.py:247-250`); zero assertion changes (diff shows only the deletion + import + docstring line). **Liveness mutation executed:** broke the HOUSE harness's `enqueue_subs` → the v0417 GUI tests **FAIL** with the injected error (the import is live, not a stale copy); restored byte-identical. All 4 v0417 GUI tests + the 3 bridge tests green (51 tests across the four touched files). |
| P-1 | Performance: picker pre-flight ≈ Mode A Plan-click shape; no per-tick payload growth beyond two keys; echo helper pure | **PASS** (shape-verified; not benchmarked) | `subs_plan_verdict` checks `os.path.exists(subs_output_path(path))` FIRST (probe-free, `subs_job.py:196-197`); the GUI loop is the same synchronous per-file shape as Mode A's `plan_batch` probe loop. The publisher adds exactly two keys (`jobs.py:456-468`). `_transcription_echo` is pure dict/getattr reads. A quantitative 500-file latency benchmark was not run (residual, R-3). |
| S-1 | Security/robustness: no new QSettings keys; no new registry fields; no API surface changes beyond additive values; frozen imports unaffected | **PASS** (first three) / **NOT VERIFIED** (frozen) | `git diff ffc13fd..HEAD -- vidlift/ | grep QSettings` → 0. `registry.py`/`monitor.py`: 0 diff lines. api_version: comments only. The new GUI helpers are module-level pure functions; the offscreen GUI suite imports and exercises them. Frozen smoke: no build exists (B-3). |
| D-1 | run.py has exactly Bob's three enumerated touches (4-A6, 6-A4, 6-A5) and nothing more | **PASS** | `git diff ffc13fd..HEAD -- vidlift/run.py | grep -c '^@@'` → **3**. Hunk 1 = 6-A5 (`_on_cuda_fallback` republish, `run.py:2652-2660`); hunk 2 = 6-A4 (post-resolution publish, `:2679-2686`); hunk 3 = 4-A6 (`should_cancel=cancel` on the pre-resolve, `:2745`). 4-A6 additionally proven behaviorally by K4. |
| D-2 | No forbidden drift (§11): no new QSettings keys, no registry schema change, no api_version bump, no run.py edits beyond the three, no edits to `test_v0416_transcription_echo.py`/`test_v0416_cancel_propagation.py`/run-side guards/`_on_segment`/post-mux tail | **PASS** | All verified: QSettings 0; registry/monitor 0 diff; api_version comments only; run.py 3 hunks; the two forbidden test files 0 diff; `_on_segment`, post-mux tail, and the full run-side guard block extracted from `ffc13fd` and `HEAD` and diffed — **byte-identical**. Test files touched: exactly the 5 enumerated (File A new + B/C/D/E). The §12 unmodified-pass list ran green (174-test invariant run + `test_gui.py` 48). |
| C-1 | §12 gate files green | **PASS** | File A `test_v0419_followup_batch.py` (1,404 LoC, T1–T23 all present and green — W1a/W1b, W3, W2 groups run individually); File B test_24 + pre-existing 14-23 green; File C 100-file pin + pre-existing green; File D all green post-D-2; File E amended tests green. |
| C-2 | CHANGELOG matches spec §8 skeleton | **PASS** | Diff shows the Fixed/Added/Changed entries verbatim against the skeleton, including the honest boundary statement and the CLI exit-semantics Changed entry. |

---

## Behavioral evidence inventory (what I ran, beyond the suite)

**Adversarial probes (scratch, outside the repo — all PASS):**

| ID | What it proves | Result |
|---|---|---|
| K1 | Mode B wedged INSIDE `detect_language` (the live defect) → cancel → `cancelled`, S-4-after, never `failed` | PASS |
| K2 | Mode A same window → `cancelled`, never failed-open (EngineCancelled escapes the TranscriptionError conversion, `run.py:3087` tail) | PASS |
| K3 | Mode B resolve window: the core threads a LIVE `should_cancel` into `resolve_model_dir`; the real entry observe raises S-1; job closes `cancelled` | PASS |
| K4 | Mode A's OWN pre-resolve (touch 4-A6) receives `should_cancel=cancel` reading the registry flag; cancel during it closes `cancelled` via the real entry observe | PASS |
| K5 | The REAL `_FallbackEngine` re-raises `EngineCancelled` on both arms with NO CPU rebuild/no note; positive control confirms a real failure DOES rebuild (the test bites) | PASS |
| K6 | R-2 boundary: cancel during the GPU-lock WAIT does not close the job (non-goal holds); post-wait the cancel lands at O1 | PASS |
| K7 | REAL ffmpeg/ffprobe (no fakes): embedded subrip track → `skip-subtitles`; plain → `ready`; force → `ready`; existing output → `blocked` (force does not bypass) | PASS |
| K8 | REAL CLI on real files: all-done batch → exit 0, both S-8 lines | PASS |
| K9 | REAL collector: `*.subs.mp4` excluded any case; `*.subs.mkv` still collected | PASS |
| K10 | Mode B LIVE device publication: pre-resolution payload `device: None` + `requested: "auto"` (resolving contract); post-resolution live payload carries the device; `_device_display` on the LIVE payload → `"CPU (GPU unavailable)"` (Eric's case, real); echo keys exact; mirror persists both keys to `jobs.json` within cadence; terminal record's result arm intact | PASS |
| K11 | Mode A LIVE device publication mid-run (6-A4) through the real run path | PASS |
| K12 | Batch resumes with the next queued job after a cancelled wedge | PASS |
| PF | Probe-fault edge at the GUI level: hidden ffprobe → pick still enqueues with the S-6 warning cell; run-side names the tool honestly | PASS |

**GUI visual validation (offscreen, real MainWindow):** four scenes rendered and captured (`shot1`–`shot4` PNGs in the scratch dir). Programmatic assertions on the live widgets all passed: the WS2 row partition + summary; `"Device: GPU (CUDA)"` overlay + suffixed stage label; `"Device: auto — resolving…"` with NO stage suffix; `"Device: CPU (GPU unavailable)"` overlay + suffix. **Limitation, stated honestly:** this validator cannot inspect images (no image-input support in this model), so pixel-level layout review (alignment/spacing/cropping) was not performed by me; the visual evidence is the exact-string assertions read from the rendered widgets, and the PNGs are saved for human review at `/home/yetisoldier/.hermes/cache/scratch/opencode/karen-v0419/`.

**Mutation experiments (each restored byte-identical; `git diff` = 0 and the four touched test files re-run green afterwards):**

1. F-1: cleanup path guard removed → `test_24` FAILS (`FileNotFoundError` — the stale scratch was deleted). Guard restored.
2. F-7: `MAX_GUI_BATCH = 64` → the 100-file pin FAILS (`BatchTooLarge`). Restored.
3. D-2 liveness: house harness `enqueue_subs` broken → v0417 GUI tests FAIL with the injected error (the shared import is live). Restored.

---

## Defects

**None found.** No Critical, High, Medium, or Low product defects. Every failure observed during validation was a defect in my own probes, and one of them re-confirmed correct product behavior:

- My K10 probe initially expected device publication under an injected `services.transcribe_engine_factory` — the product correctly stayed silent (the D4 honest-unknown rule: a foreign factory means the core never saw the device decision). The production path (default factory) publishes, as re-proven by the fixed K10.
- My probe expected `api_version` on the MCP status payload — it never rode that payload (it is the HTTP `/status` envelope's identity block, `monitor.py:205`, pinned by the untouched `test_monitor.py:269`).
- My cross-process probe initially used a second in-process `LocalBridgeClient` (which never re-reads `jobs.json`); the true cross-process contract is the mirror's `jobs.json` persistence, which K10 then verified directly.

**Forge's seven reported deviations — verdict on each (all reviewed against the spec):**

1. **File E two extra mechanical amendments — ACCEPTED.** The spec's own 5-A7 contract ("`verdicts` is a REQUIRED argument: a row must never be rendered from a verdict the pre-flight didn't make") admits no default, so the two additional one-arg call sites in `test_v0414_mode_b_gui.py` (`:585-605`, `:1098-1111`) had to pass explicit verdict maps. Both amendments are plumbing only; row-content assertions unchanged; File E is the designated amendment file and is not on the unmodified-pass list. Spec-enumeration breadth, not content drift.
2. **`_subs_enqueue_summary` bare-name calls — ACCEPTED.** The spec specifies the helper module-level ("beside `_stage_label_text`, pure — Karen-testable without Qt"); the `self.` form in the spec's 5-A8 flow block would `AttributeError`. Zero functional difference.
3. **T6c arity-shape scoping — ACCEPTED.** The spec §12 T6 pre-authorizes exactly this fallback ("if pinning real sizes proves brittle, scope T6c to the arity call-shape assertion"). Implemented as authorized: arity-2 call shapes recorded, honest sha-pin `TranscriptionError` (never `TypeError`), junk-`model.bin` cleanup asserted.
4. **T9's seam substitution — ACCEPTED.** Wiring `services.transcribe_engine_factory` into `_execute_run_job` would itself be unenumerated `jobs.py` drift (forbidden). T9 drives the REAL Mode A run path, registry claim/cancel API, and `should_cancel=cancel` forwarding with an in-process factory substitution. My K2/K4 independently prove the same journey, and K4 specifically proves the 4-A6 call site.
5. **T4's fake wrapper — ACCEPTED, gap closed by K5.** T4 pins the no-rebuild discipline via a wrapper fake; the REAL `_FallbackEngine` D2 discipline is now directly proven by my K5 (both arms, positive control), in addition to the untouched `test_v04152_cuda_fallback.py`.
6. **Comment-narration choices — ACCEPTED** (cosmetic; verified in the diff).
7. **Five milestone commits — matches spec §9.**

---

## Release Recommendation

**Ready for Production** from the software-validation standpoint.

- The CODE is **cleared**: every §13 criterion exercisable in software passes, the majority proven behaviorally end-to-end (real executor, real registry, real cancel API, real ffmpeg/ffprobe, real offscreen GUI), the forbidden-drift list is fully honored (byte-level checks, not just eyeball), and the three WS4 mutation checks bite and were restored byte-identical.
- The RELEASE carries **no defect**; the WARN on overall status reflects only validation-environment gaps, listed below. None is a reason to hold the release; all are items the spec itself scopes to real hardware.

**Residual risks / not verified (explicit):**

1. **Frozen-build smoke run (§13 build gate)** — no frozen build exists in this environment. The new GUI helpers are pure module-level functions and the offscreen suite imports the full GUI cleanly, but the actual PyInstaller smoke remains to be run where builds are made.
2. **Real-GPU-box items (§13 WS1/WS3)** — cold-CUDA-context cancel within ~1 s of the click, `Device: GPU (CUDA)` rendered on the 3090 Ti, GPU-contended fallback observed on real contention, and the real first-run 1.5 GB download cancel with `.part` resume over a real network. The software paths are proven with the closest in-process equivalents (blocking seams standing in for the native calls); the hardware renderings are not exercisable here. Note K10 already proves the "CPU (GPU unavailable)" case live on this CUDA-less box.
3. **Pixel-level visual review** — I cannot view images (model limitation). The offscreen renders are string-asserted from the live widgets and the PNGs are saved for human review; a human glance at the four screenshots on the target box closes this.
4. **Quantitative picker latency (R-3)** — the synchronous pre-flight's cost shape is code-verified (probe-free output-exists fast path; same shape as Mode A's Plan click) but not benchmarked at the 500-file scale. Revisit criterion per the spec: Eric reports the picker stalling.
5. **R-1 (architectural boundary, unchanged by design)** — a cancel flipped while blocked INSIDE a hung native detect/model-load call remains unobservable without process isolation; documented in the spec, the report, and the CHANGELOG. Revisit criterion: recurrence on Eric's box → executor process isolation.
6. **R-2 (named follow-up)** — the GPU-phase-lock WAIT stays unobserved; proven behaviorally by K6 to be exactly the documented boundary (and the cancel now lands at the first post-wait observe, which pre-0.4.19 it did not).

**Commit note:** this report could not be committed — the environment's permission rules deny `git commit` for this session. The report file is written to `.specs/karen-vidlift-v0.4.19-validation-report.md`; the commit must be made by a session with commit permission (suggested message: `docs(v0.4.19): Karen validation report — code cleared, release ready; 1068 passed / 2 skipped reproduced`).
