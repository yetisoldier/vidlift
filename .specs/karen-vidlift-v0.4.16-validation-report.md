# Karen Validation Report — VidLift v0.4.16 (full uncommitted change set)

**Validator:** Karen (adversarial Principal QA, read-only)
**Date:** 2026-11-03 (America/Chicago)
**Scope:** the entire uncommitted v0.4.16 change set — 16 modified files
(~1,461 insertions) + 5 new files: discovery card + `transcription` block
(visibility), model management (fetch-seam progress, per-tag lock, GUI
pill/install, status surfacing), the §4.6 cancel observe-fix, and suite
hermeticity.
**Interpreter:** `/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3,
pytest 9.1.1) exclusively. All evidence in-repo; probes under
`.karen/v0416-probes/` (state roots pinned via `$VIDLIFT_STATE_DIR` — the
real `~/.vidlift` was never read or written by any validation step).

---

## Executive Summary

**Verdict: WARN — ready with minor fixes.**
**Overall risk: Medium. Confidence: High.**

The suite is green and deterministic (895 passed / 2 skipped, three
repeat runs), hermeticity holds, the cancel fix survives adversarial
probing end to end, and the discovery card behaves exactly per its
spec matrix (which I had to execute myself — see D-7). But three
**Medium** honesty defects — each sitting in exactly the cells of the
validation specs' promised-but-never-delivered test matrices — mean
three of the release's own headline guarantees do not hold on
realistic paths. None are Critical/High; each is a small, localized
fix (a record-touch in one closure, a dict-pop in one except arm, a
one-line manifest key path).

---

## Test Plan & Execution

### Full suite (determinism)

| Run | Result | Time |
|-----|--------|------|
| 1 | 895 passed, 2 skipped | 93.55s |
| 2 | 895 passed, 2 skipped | 91.98s |
| 3 | 895 passed, 2 skipped | 91.01s |

The 2 skips are the pre-existing Windows-only GPU-meter tests
(`test_gpu_meter_no_window.py:132,164`) — expected on Linux. Count
matches the task's expected 895/2 exactly. Hermeticity:
`tests/test_hermetic_state_dir.py` 4/4 PASS in every run (default
root never `~/.vidlift`; env pin wins; explicit arg wins; GPU lock
rides the tmp root).

### Adversarial probes (all under `.karen/v0416-probes/`, venv python)

| # | Probe | Result |
|---|-------|--------|
| 1 | `probe_mode_a_record_touch.py` — real `run_file` Mode A transcription, injected fake default fetcher (run_file passes no `fetch=` seam), real Registry record + state_box, watcher sampling the reader truth | **DEFECT D-1 confirmed** (record frozen, state advances, `activity_age_seconds` climbs during beats) + exposed **D-2** (stale `model_download` survives fail-open) |
| 2 | `probe_cancel_during_download.py` — cancel flips mid-download (the documented bounded window) | **SAFE**: download completes (bounded), run closes `cancelled`, cancelled manifest written, no lock residue, no hang |
| 3 | `probe_lock_perm_flip.py` — round-0 lock contention, then cache dir made read-only mid-wait | **DEFECT D-4 confirmed** (unbounded 1 Hz retry loop; still spinning after 5 s) |
| 4 | `probe_discovery_card.py` — real `MainWindow._start_monitor`/`_write_monitor_discovery`/`_toggle_monitor` + live HTTP against the served monitor | **ALL PASS**: D-1..D-5 spec matrix, auth gating (401/200), S-4 (card not served), S-5 (token not in chrome), F-1 (unwritable dir degrade), atomic write — **but perms 0o664 → D-5** |
| 5 | `probe_transcription_echo.py` — the `transcription` device-echo block's five derivation arms | T-1/T-2/T-3/T-4/T-6 PASS; **T-5 FAIL → D-3** (manifest echo arm dead) |
| 6 | `probe_t5_manifest_echo.py` — real manifest shape vs the echo's read path | Confirms real manifests store `subtitles` under `manifest["run"]`, not top-level → D-3 conclusive |

### Code review coverage

Every hunk of all 16 modified files + all 5 new files was read:
config.py, monitor.py, gui.py, transcribe.py, run.py, jobs.py,
tools.py, server.py, conftest.py, ui/model_install.py, and the five
new/changed test files. Specific seams verified by review + suite:
conditional-arity fetch discipline (`_download_file` passes the third
arg only when a consumer exists — M-1/M-2 pin it), the lock's
create-close/heartbeat/stale-reclaim/second-acquirer-fast-path
(L-1..L-6, G-7: zero double downloads), the pre-lock warm-cache fast
path (transcribe.py:971 — warm resolves never contend), the
`EngineCancelled` exact-type benchmark mapping, the executor's
raced-terminal-state logging, `api_version` additivity (S-6/S-7/S-8),
no new MCP tools/routes/kinds (R-1/R-2/R-4), version-stamp parity
(0.4.16.0 across pyproject/`__init__`/version_info), and frozen-mode
state-dir consistency (run_gui.py exports the env before the bridge
resolves; pill's `default_state_dir()` and jobs' `services.state_dir`
agree in dev and frozen).

---

## Defects

### D-1 (Medium) — Mode A download beats never touch the JobRecord; `activity_age_seconds` keeps climbing during a first-use model download inside an upscale job

**Impact.** Every reader of a Mode A (run-job) first-use model
download — the GUI's stale-activity row marker ("running (no activity
N min)"), the stage label's "— last activity N min ago" suffix, and
`/status`'s `activity_age_seconds` — sees the job go quiet after 15
minutes of an actively-progressing download. This is the exact
symptom class the changelog's headline Fixed claim describes
("every beat touches the live record/state so `activity_age_seconds`
stops climbing during a long download"). Mode B (standalone subs) is
fixed properly; Mode A is not.

**Evidence.** Probe 1 drove the real `run_file` Mode A path: during a
1.6 s download window with 5 beats, the RECORD's `last_activity_at`
advanced **0.00 s** while the STATE's advanced **1.20 s**, and the
reader's `activity_age_seconds` grew **1.51 s** across the window —
it scales linearly, crossing the 900 s `STALE_ACTIVITY_SECONDS`
threshold at 15 min of a 17-min download. Root cause is structural:
run.py's `_on_model_download` closure (run.py:2656-2677) references
only `state` — `run_file` has no record handle — while jobs.py's Mode
B publisher touches both. `_status_payload` computes
`activity_age_seconds` from `record.last_activity_at` (tools.py:422).
The validation spec promised an H-2 Mode A parity test; none was
delivered (see D-7) — the one test that would have caught this.

**Recommendation.** Thread a record-touch into Mode A's publisher
(mirror jobs.py's `record.touch()` via the state_box/executor seam),
or derive `activity_age_seconds` from the live state's
`last_activity_at` when a live state exists. Then land the missing
H-2 test.

### D-2 (Medium) — Mode A fail-open leaves a stale `model_download` fact; status surfaces report "downloading" for a download that terminally failed

**Impact.** When the in-run download fails (network error, sha256
mismatch, truncated fetch — realistic first-use failures), the
`model_download` byte fact is never cleared: the pop at run.py:2690
runs only on resolve's success path, and run.py's `record_step`
merges without popping (unlike jobs.py's Mode B `on_step`, which pops
unconditionally). For the rest of the run, `run_stage_label` renders
"downloading transcription model — X / Y (pct%)" during subsequent
phases, and `_transcription_model_echo` / the `/status` envelope
report `state: "downloading"` for a dead download — an anti-lie
violation. Self-heals at terminal close (the D-12 mirror rule).

**Evidence.** Probe 1's post-run inspection: `state.progress` still
carried `model_download` (last beat 5,000,000/5,000,000) after the
sha-mismatch fail-open; code reading confirms the except arm
(run.py:2723-2731, `TranscriptionError` → `SubtitleOcrError`) performs
no pop.

**Recommendation.** Pop the fact in the fail-open conversion (the
`except TranscriptionError` arm), mirroring Mode B's unconditional
pop; add the H-3 Mode A analog test.

### D-3 (Medium) — `_transcription_echo`'s manifest-echo arm is dead code: it reads `manifest["subtitles"]` but the manifest stores it at `manifest["run"]["subtitles"]`

**Impact.** A finished/failed run job read with no live state (the
cross-process / post-close case — exactly Hermes's one-shot read of a
completed job) always echoes `transcription.device: null` even when
the manifest records the resolved device. The visibility spec's T-5
and the P1 visibility promise are silently broken for terminal run
jobs.

**Evidence.** `_build_manifest` writes `subtitles` inside the `"run"`
dict (run.py:3023); `_transcription_echo` reads
`manifest.get("subtitles")` top-level (tools.py, the elif arm). Probe
5's T-5 with the real manifest shape returned `device: None` where
`"cpu"` was recorded; probe 6 confirmed real manifests carry no
top-level `subtitles`. The rest of the payload path already reads
`manifest.get("run")` (tools.py:618) — the echo is the outlier.

**Recommendation.** One-line fix: read
`manifest.get("run", {}).get("subtitles")`; land the missing T-5 test.

### D-4 (Low) — The per-tag download lock loops forever if the cache dir becomes unwritable after round-0 contention (contradicts "never a hang")

**Evidence.** Probe 3: with a live contender's lock present and the
dir then chmod'd read-only, `_model_download_lock` was still
retrying at 1 Hz after 5 s. The degrade-OPEN arm fires only when
`rounds == 0`; later-round OSErrors are treated as races and retried
unconditionally (transcribe.py, the `except OSError` arm). Pathological
trigger (perm flip mid-wait), but the downloading thread then spins
silently forever — a GUI install sticks at "Downloading…", a job
worker never progresses.

**Recommendation.** Bound consecutive non-FileExistsError OSErrors
(degrade OPEN after a small count), or treat PermissionError as
degrade-OPEN on any round.

### D-5 (Low) — The discovery card (secret-bearing: effective monitor token) is written world/group-readable

**Evidence.** Probe 4: `stat` mode `0o664` on this box (`0o644` under
a default umask). The task explicitly asked to verify file perms. On
multi-user machines any local user can read the LAN monitor token
(including a QSettings-overridden one), locally bypassing the
`/status`/`/history` token gate. Mitigations: same trust zone as the
QSettings ini (also 0644 on Linux), read-only surface, the baked
default token is public in-repo, single-user dev box.

**Recommendation.** Create the card `0o600` (and consider `0o700` on
the state dir). Cheap, closes the gap the "same trust zone" argument
leaves on shared machines.

### D-6 (Low) — Discovery card staging name is fixed (`monitor.json.tmp`): two concurrent GUI instances can interleave into one staging file

**Evidence.** Static: gui.py `_write_monitor_discovery` uses
`path.with_suffix(".json.tmp")` with no pid uniquing; the registry
explicitly supports multi-writer convergence, so two GUIs on one
state dir is a contemplated scenario. Writer A's `os.replace` can
move B's mid-write staging file into place → torn card JSON. Worst
case a watcher reads a torn card → parse error → treated as no card
(best-effort provisioning; the truth surface `/status` is unaffected).

**Recommendation.** Pid-unique staging name.

### D-7 (Low) — The release's own validation-spec test matrices were only partially delivered

**Evidence.** Delivered: the model-management M/L/D/S/G/H-1/H-1b/H-3/R
matrices are essentially complete and green. Missing: (a) the
visibility spec's **D-1..D-6 discovery-card matrix — zero tests**
(a secret-bearing provisioning artifact shipped untested; my probe 4
had to execute the matrix — all PASS, so the code works, but nothing
pins it); (b) the visibility spec's **T-1..T-6 device-echo matrix** —
only the S-8 idle-shape guard exists (T-5's absence is exactly why
D-3 shipped broken); (c) the model-management spec's **H-2 Mode A
heartbeat parity test** (its absence is exactly why D-1 shipped).
Three of the four Medium/Low code defects live in these untested
cells — the correlation is the finding.

**Recommendation.** Land the missing matrices before or immediately
after release; they are small and the probe harnesses already exist.

### Informational

- **I-1:** `model_install.py`'s `on_wait` emits a `{"waiting": True,
  holder, seconds}` fact the pill slot ignores (the pill renders
  "downloading…" during a lock wait; G-7 asserts the signal, not a
  distinct wait label). The docstring's "the pill's slot shows the
  wait" overstates.
- **I-2:** The board/publishers hardcode `file: "model.bin"` during
  the KB-scale support-file fetches too (documented rationale; brief
  cosmetic inaccuracy).
- **I-3:** `_write_monitor_discovery` re-reads QSettings for
  `bind_host` (second read could diverge from the token read;
  single-threaded GUI — cosmetic).
- **I-4:** The validation task brief referenced a
  `state_watch_plugin` in tests/conftest.py; no such plugin exists.
  The actual hermeticity mechanism is the `_hermetic_state_root`
  autouse fixture + `test_hermetic_state_dir.py` (verified: all
  `state_dir` consumers resolve lazily through the patched
  `benchmark.state_dir`; `Registry` requires an explicit root;
  frozen mode exports the env before resolution). Task-description
  discrepancy, not a code defect.
- **I-5:** A crash between staging write and `os.replace` can strand
  `monitor.json.tmp` (self-heals on the next write).
- **I-6:** The default fetcher's fixed `.part.tmp` name means the
  rare stale-reclaim-raced concurrent download pair can interleave —
  bounded by the sha256 pin (documented backstop).

---

## Verified-working (evidence summary)

- **Suite:** 895/2 × 3 deterministic runs, venv interpreter.
- **Hermeticity:** 4/4; every default-root consumer patched; no
  real-profile reads/writes from the suite.
- **Discovery card:** full D-1..D-5 behavior, atomic write, honest
  `token_source` (default/qsettings/empty-open), empty-token
  open-bind canary labeled and behavior-unchanged, card never served
  over HTTP, token never in window chrome, unwritable-dir degrade,
  port-fallback truth, effective-token-only (no other app secrets).
- **Fetch seam:** conditional arity keeps every legacy 2-arg fake
  valid (M-1); per-chunk beats with honest Content-Length/None
  totals (M-3/M-4); truncation/sha/too-small guards preserved
  (M-5/M-6); `.part` short-circuit (M-7); 4 Hz publisher throttle
  with final-beat landing (M-8/H-1).
- **Lock:** create-close (no Windows sharing violation), ≤1 Hz
  mtime heartbeat, 30-min stale reclaim, second-acquirer fast path
  with zero double downloads (G-7), round-0 degrade-OPEN, pre-lock
  warm-cache fast path.
- **Cancel fix:** all six run-level boundary tests, the
  subprocess terminate→kill escalation (real interpreter children),
  bridge-level cancel-current/cancel-batch/benchmark journeys, the
  GUI tick transition test, and my mid-download bounded-window
  probe (lands `cancelled`, manifest written, no lock leak, no
  hang). The terminated-child mask (rc −15 → cancelled, never
  `failed`) is pinned. Benchmark `EngineCancelled` maps by exact
  type before the `RunError` arm.
- **Additivity/stability:** `api_version` stays 1; `/health`
  unchanged; payload key-set guard (S-8); no new MCP tools, monitor
  routes, or enqueue kinds (R-1/R-2/R-4); version stamps 0.4.16.0 in
  parity.

---

## Release-Gate Statement

**The release gate is MET for the cancel observe-fix and hermeticity
workstreams** — both survived adversarial probing beyond their
delivered tests, with no defects found.

**The gate is NOT fully met for the model-management/visibility
honesty claims.** Three Medium defects (D-1, D-2, D-3) each
contradict a specific guarantee this release advertises —
`activity_age_seconds` stops climbing (Mode A), no stale download
facts, and device echo for terminal jobs — on realistic paths, and
all three sit in cells of the validation specs' own promised test
matrices that were never delivered (D-7). None are
Critical/High: no data loss, no crash, no security break, no queue
corruption; Mode B — the primary repro path — is fixed properly.

**Recommendation: Ready with Minor Fixes.** Ship after landing the
three small code fixes (each ≤ ~5 lines: a record-touch in Mode A's
publisher, a `progress.pop` in the fail-open arm, the manifest key
path) plus the missing D-1..D-6 / T-1..T-6 / H-2 test matrices. If
Eric chooses to ship as-is, the three Mediums must be named as
known issues in the CHANGELOG — the current "Fixed" claims for Mode
A download honesty and terminal device echo would otherwise
overstate.

**Defect roll-up:** Critical 0 · High 0 · Medium 3 (D-1, D-2, D-3) ·
Low 4 (D-4, D-5, D-6, D-7) · Informational 6.
