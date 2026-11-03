# Karen Re-validation Report — VidLift v0.4.16 (Forge remediation of Karen's WARN)

**Validator:** Karen (adversarial Principal QA, read-only)
**Date:** 2026-11-03 (America/Chicago)
**Scope:** Forge's remediation of D-1..D-7 from
`.specs/karen-vidlift-v0.4.16-validation-report.md`, plus regression
confirmation of the previously-verified surface.
**Interpreter:** `/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3,
pytest 9.1.1) exclusively. All evidence in-repo; new probes under
`.karen/v0416-probes/` (`*_v2.py`, `probe_lock_stale_unlink.py`), state
roots pinned via `$VIDLIFT_STATE_DIR` — the real `~/.vidlift` was never
read or written; nothing was written outside the repo.

---

## Executive Summary

**Verdict: PASS.**
**Overall risk: Low. Confidence: High.**

All seven defects from my v0.4.16 validation are remediated, each with
the promised test cell now landed and green. I re-probed the three
Mediums on the REAL paths (real `run_file` Mode A with an injected fake
default fetcher; a real manifest from a real successful run read the
way Hermes reads it; the true D-4 lock scenario on the real filesystem)
— all three hold. The previously-verified surface is regression-free,
and the full suite is green and deterministic across three runs
(914 passed / 2 skipped, identical every time; the 19 new tests account
exactly for 895 → 914).

One **new Low residual** surfaced (R-1): the download lock's
stale-reclaim arm can still spin unbounded when the cache dir is
read-only AND the lock has gone stale — an adjacent arm the D-4 fix
did not bound (it bounded the `os.open` arm only). Same class and
symptom as the original D-4 ("never a hang" violated for the
downloading thread), narrower trigger (perm flip **plus** staleness).
It does not gate the release; it should be queued as a small follow-up
fix in the same spirit as D-4.

One honesty note on my own prior evidence: my original probe-3's
five-second "still spinning" observation was in fact the **designed
live-contender wait** — with the contender's lock file still present,
`os.open(O_CREAT|O_EXCL)` short-circuits to `EEXIST` before any
write-permission check, so the perm flip never reached the `except
OSError` arm. The underlying D-4 code defect was real (reachable once
the lock file is absent) and Forge's fix is correct, but the original
report's mechanism attribution was imprecise. The v2 probe below
demonstrates the true defect scenario and its fix.

---

## Test Plan & Execution

| # | Test | Purpose | Result |
|---|------|---------|--------|
| 1 | Full suite, run 1 (`pytest -q`) | Determinism + global regression | **914 passed, 2 skipped** (104.98s) |
| 2 | Full suite, run 2 | Determinism | **914 passed, 2 skipped** (101.84s) |
| 3 | Full suite, run 3 (`-rs`) | Skip identification | **914 passed, 2 skipped** (102.37s) — skips are the pre-existing Windows-only GPU-meter tests (`test_gpu_meter_no_window.py:132,164`), expected on Linux |
| 4 | `tests/test_v0416_mode_a_honesty.py` | D-7(c): H-2 Mode A parity + H-3 Mode A analog + executor-wiring pin | **3/3 PASS** |
| 5 | `tests/test_v0416_transcription_echo.py` | D-7(b): T-1..T-6 device-echo matrix (+ T-5b negative arm) | **7/7 PASS** |
| 6 | `tests/test_v0416_discovery_card.py` | D-7(a): D-1..D-6 discovery-card matrix on real window wiring + live HTTP (+ D-6b pid-unique pin) | **7/7 PASS** |
| 7 | `tests/test_model_management.py` (full) | M/L/D/S/G/H/R matrices incl. new L-8/L-8b (D-4) | **45/45 PASS** |
| 8 | `tests/test_hermetic_state_dir.py` + `tests/test_v0416_cancel_propagation.py` | Hermeticity + §4.6 cancel regression | **17/17 PASS** |
| 9 | `tests/test_monitor.py` + `tests/test_model_management_gui.py` | `api_version` additivity, routes, GUI pill/install | **33/33 PASS** |
| 10 | Lock matrix selection (L-1..L-6, G-7, heartbeat, stale) | Previously-verified lock behaviors | **8/8 PASS** |
| 11 | **Probe 1v2** — real `run_file` Mode A, injected fake default fetcher, production-mirrored `state_box` (incl. the jobs.py:157 record seam), watcher sampling reader truth | D-1 + D-2 on the real path | **FIXED** (details below) |
| 12 | **Probe 6v2** — real successful Mode A run → real manifest → Hermes one-shot `_status_payload` (terminal record, no live state) | D-3 end to end | **FIXED** (details below) |
| 13 | **Probe 3v2** — real-filesystem D-4 scenario: round-0 contention, then contender releases + dir chmod'd read-only mid-wait | D-4 on the real filesystem | **FIXED** (degrades OPEN in 1.00s) |
| 14 | **Probe 3c** (new) — read-only dir + stale lock (mtime 40 min old) | D-4 adjacent arm: stale-reclaim unlink failure | **RESIDUAL R-1 CONFIRMED** (still spinning at 5 s) |
| 15 | Probe 2 re-run — cancel flips mid-download (bounded window) | §4.6 cancel regression | **PASS** (cancelled, manifest written, no lock residue, no hang) |
| 16 | Probe 4 re-run — discovery-card behavior matrix + live HTTP | Card regression + D-5 perms | **PASS** on all behavior rows; perms now 0o600 (see note on the probe's stale "D-1" row) |
| 17 | Version stamps + additivity spot-checks | 0.4.16.0 parity; no new kinds | **PASS** (pyproject / `__init__` / version_info all 0.4.16.0; enqueue kinds run/subs/benchmark only; `api_version == 1` pinned by suite) |

---

## Per-defect re-validation

### D-1 (Medium) — Mode A download beats never touched the JobRecord → **FIXED**

- **Code:** run.py:2655-2693 — the publisher resolves
  `box_record = state_box.get("record")` and touches it on every landed
  beat; the executor wires `state_box["record"] = record`
  (jobs.py:151-157). The CLI (no state_box) is explicitly unchanged
  (defensive read, documented).
- **Tests:** `test_run_executor_wires_the_record_into_the_state_box`
  (pins the production seam — a regression here silently revives D-1)
  and `test_h2_mode_a_download_beats_touch_record_and_state` (the
  promised H-2) — both green. The H-2 test drives the real `run_file`
  branch through a phase-time-imported fake resolve (run.py:2635
  imports at phase time, so the monkeypatch genuinely redirects the
  production call).
- **Independent probe (1v2):** real `run_file` Mode A, injected fake
  **default fetcher** (the real resolve/lock/sha path, not a patched
  resolve), production-mirrored state_box. During a 1.6 s download
  window with 5 beats: RECORD `last_activity_at` advanced **1.20 s**
  (pre-fix: 0.00 s), STATE advanced 1.20 s, and the reader's
  `activity_age_seconds` stayed at **max 0.39 s** (pre-fix: climbed
  1.51 s across the window, linearly past the 900 s threshold at
  15 min of a 17-min download).

### D-2 (Medium) — stale `model_download` fact survived the fail-open → **FIXED**

- **Code:** run.py:2742-2761 — the `except TranscriptionError` arm pops
  `model_download` unconditionally AND moves the step label to
  "subtitles failed open (transcription unavailable)" (the byte-fact
  pop alone would have left the stage label rendering "downloading…"
  — Forge caught that nuance; the label is cadence-honest now).
- **Test:** `test_h3_mode_a_fail_open_pops_the_stale_download_fact`
  (the H-3 Mode A analog) — green.
- **Independent probe (1v2, sha-mismatch arm):** post-run
  `state.progress` keys = `['step']` only; the reader payload carries
  no `model_download`; step label = 'subtitles failed open
  (transcription unavailable)'; the run still closes ok=True with
  `subtitles.status == "failed-open"` and the sha-mismatch reason
  (fail-open semantics preserved).

### D-3 (Medium) — `_transcription_echo` read a non-existent manifest key → **FIXED**

- **Code:** tools.py:471-474 — the echo now reads
  `(manifest.get("run") or {}).get("subtitles")` (Forge's `or {}`
  guard keeps a `run: null` manifest honest-null instead of raising).
- **Tests:** `test_t5_terminal_run_manifest_echo_is_live` (the D-3 pin)
  plus `test_t5b_manifest_without_run_subtitles_stays_null` — a
  negative arm proving the OLD wrong top-level key stays null (the
  echo reads the real layout, not a shape that happens to pass) —
  both green.
- **Independent probe (6v2):** a REAL successful Mode A run (fake warm
  resolve + stub engine) recorded `device: 'cpu'`; the real manifest
  stores it at `run.subtitles.transcription.device` with **no**
  top-level `subtitles` key; and Hermes's one-shot read — a terminal
  registry record with `manifest_path`, no live state, through the
  FULL `_status_payload` — echoes `transcription.device: 'cpu'`
  (pre-fix: always null).

### D-4 (Low) — lock spun forever on a mid-wait permission flip → **FIXED** (residual adjacent arm: R-1)

- **Code:** transcribe.py:798-802, 823-887 — a `PermissionError` on ANY
  round degrades OPEN; otherwise `MODEL_LOCK_OERROR_DEGRADE_ROUNDS`
  (3) consecutive non-FileExistsError OSErrors degrade OPEN; an
  intervening `FileExistsError` resets the streak (contention is not
  degradation).
- **Tests:** `test_l8_midwait_permission_flip_degrades_open_bounded`
  (scripted FE→OE→FE→PE: the any-round arm fires, bounded, contender's
  lock untouched) and `test_l8b_...` (FE + exactly 3 consecutive OEs)
  — both green.
- **Independent probe (3v2, real filesystem):** the TRUE defect
  scenario — round-0 `FileExistsError` (live contender), then
  mid-wait the contender releases AND the dir goes read-only — the
  next `os.open` must create and raises `PermissionError` with
  `rounds > 0`. Post-fix: **degrades OPEN in 1.00 s** (pre-fix:
  unbounded 1 Hz spin).
- **Honest correction of my prior evidence:** the original probe-3
  chmod'd the dir while the contender's lock file still existed;
  `O_CREAT|O_EXCL` short-circuits to `EEXIST` before any write-perm
  check, so what it observed at 5 s was the designed live-contender
  wait, not the defect arm. The defect itself was real (code
  reading: the `except OSError` arm retried unconditionally after
  round 0, reachable once the lock file is absent) and is now fixed
  and pinned — but the original report's mechanism attribution was
  imprecise, and I am correcting it here.

### D-5 (Low) — discovery card world/group-readable → **FIXED**

- **Code:** gui.py:907-917 — the card is created via
  `os.open(..., 0o600)` (umask can only tighten further), the state
  dir is created `0o700` when the writer is the creator, and
  `os.replace` carries the staging file's mode onto the final
  artifact — so even a pre-existing 0o664 card from an older build is
  self-healed on the next write.
- **Tests:** `test_d1_...` and `test_d3_...` assert
  `stat.S_IMODE(...) == 0o600` on real MainWindow writes — green.
- **Probe 4 re-run:** "card file perms: 0o600".

### D-6 (Low) — fixed staging name, two-instance interleave → **FIXED**

- **Code:** gui.py:899-905 — pid-unique staging
  `monitor.json.<pid>.tmp`; `os.replace` moves only this writer's own
  completed card.
- **Test:** `test_d6b_staging_name_pid_unique_no_torn_write` — two
  writers with distinct pids produce distinct staging names, the
  final card is the last writer's COMPLETE write, and no staging
  residue of either shape survives — green.

### D-7 (Low) — the missing test matrices → **LANDED, ALL GREEN**

- **(a)** visibility spec D-1..D-6 discovery-card matrix:
  `tests/test_v0416_discovery_card.py` — 7 tests on the REAL wiring
  (real `LocalBridgeClient` on a tmp state dir, isolated QSettings,
  loopback host, live HTTP auth parity), including the D-5 perms and
  D-6 staging pins.
- **(b)** visibility spec T-1..T-6 device-echo matrix:
  `tests/test_v0416_transcription_echo.py` — 7 tests (T-1..T-6 plus
  the T-5b negative arm), each driving the real `_status_payload`.
- **(c)** model-management H-2 Mode A heartbeat-parity test:
  `tests/test_v0416_mode_a_honesty.py` — 3 tests (the executor-wiring
  seam pin, H-2, and the H-3 Mode A analog).
- Plus the D-4 probe regressions L-8/L-8b in the lock matrix.
- **Count arithmetic:** 895 (prior) + 3 + 7 + 7 + 2 = **914** —
  exactly the delivered suite count; nothing else changed.

---

## New residual defect

### R-1 (Low) — the lock's stale-reclaim arm still spins unbounded on a read-only dir with a stale lock

**Impact.** A downloader thread can spin at 1 Hz forever (the
"never a hang" promise violated — the same symptom class as the
original D-4): a GUI install sticks at "waiting/downloading…", a job
worker never progresses. Trigger is narrower than D-4's: the cache
dir must be read-only **and** the lock must be (or go) stale.

**Evidence.** Probe 3c (`.karen/v0416-probes/probe_lock_stale_unlink.py`):
dir chmod'd `r-x`, contender lock present with mtime 40 min in the
past → `_model_download_lock` was still retrying at 1 Hz after 5 s.
Mechanism (transcribe.py:832-851): `os.open` raises `FileExistsError`
(the file exists — `EEXIST` short-circuits before write-perm checks,
so the D-4 `PermissionError` arm never fires) → stale check true →
`os.unlink` raises `PermissionError`, swallowed by
`except OSError: pass` ("races are retries") → `continue` → FE →
stale → … unbounded. Reachable composites: (a) read-only dir entered
with a stale lock (dead writer); (b) perm flip mid-wait + the live
contender finishes — its own release unlink is swallowed too
(transcribe.py:902-906), so the lock goes stale 30 min later and the
waiter enters the spin. L-8/L-8b do not pin this arm (they script
`os.open` only; their lock never goes stale).

**Recommendation.** In the stale arm, treat consecutive failed
unlinks (or a `PermissionError` there specifically) as degrade-OPEN —
the same one-line-class fix as D-4's arm. Queue as a follow-up; not a
release gate (Low, pathological trigger, no data-integrity impact —
the sha256 pin remains the backstop).

---

## Regression confirmation (previously-verified surface)

- **Suite:** 914 passed / 2 skipped × **3 identical runs** (104.98s,
  101.84s, 102.37s), venv interpreter; skips are the pre-existing
  Windows-only GPU-meter cells.
- **Conditional-arity fetch seam:** M-1/legacy-fake discipline green
  (45/45 in the model-management matrix).
- **Lock behaviors:** create-close, ≤1 Hz heartbeat, 30-min stale
  reclaim, second-acquirer fast path, zero double downloads
  (L-1..L-6, G-7) — 8/8 selected green.
- **§4.6 cancel fix:** `test_v0416_cancel_propagation.py` 13/13, plus
  my mid-download bounded-window probe re-run: cancel flips
  mid-download → run closes `cancelled`, cancelled manifest written,
  no lock residue, no hang.
- **Hermeticity:** 4/4; no real-profile reads/writes.
- **Additivity/stability:** `api_version == 1` (pinned by
  test_monitor.py:269 and the S-8 envelope guard); no new MCP tools,
  monitor routes, or enqueue kinds (run/subs/benchmark only); version
  stamps 0.4.16.0 in parity (pyproject / `__init__` /
  version_info.txt).
- **Discovery-card behavior:** probe 4 re-run — auth gating (401/200),
  S-4 (card never served), S-5 (token never in chrome), D-2
  (port-fallback truth), D-4 (override), D-5 (empty-open canary), D-3
  (toggle-off rewrite), F-1 (unwritable-dir degrade) all PASS. The
  probe's own "D-1" row reads FAIL only because of a probe-hygiene
  artifact: with a fresh ini the window auto-starts at construction
  on the default port (8765) and `_start_monitor()` correctly no-ops
  while already serving — the card honestly records the ACTUAL port,
  which is the card's purpose; the new suite test pins the correct
  expectation (`card["port"] == served`) and passes. (My probe also
  `rmtree`s a settings *file*, which silently no-ops — the first
  re-run inherited the prior session's persisted empty-token ini,
  explaining a transient auth-gating FAIL that vanished with a clean
  ini. Probe artifacts, not product behavior.)
- **CHANGELOG:** the remediation section documents D-1..D-6 honestly
  (including "no behavior claim extends beyond what the landed tests
  pin"), and the release's headline Fixed claims — Mode A download
  honesty, no stale download facts, terminal device echo — now hold
  on the realistic paths per the probes above.

---

## Release-Gate Statement

**The release gate is MET.**

All three Medium honesty defects (D-1, D-2, D-3) are fixed and
independently verified on the real paths; the four Lows (D-4, D-5,
D-6, D-7) are fixed with pinned tests; the promised test matrices are
landed and green; the previously-verified surface (fetch seam, lock
discipline, §4.6 cancel, hermeticity, additivity, version parity) is
regression-free; and the full suite is deterministic across three
runs. No Critical, High, or Medium severity defects remain.

**Defect roll-up:** Critical 0 · High 0 · Medium 0 · Low 1 (R-1, new,
residual adjacent to D-4) · Informational 0 new (I-1..I-6 from the
prior report stand as informational).

**Recommendation: Ready for Production.** Ship v0.4.16.0. Queue R-1
(the stale-reclaim unlink bound) as a small follow-up fix in the same
spirit as D-4; it is Low-severity, pathological-trigger, and
integrity-safe (sha256 backstop), and does not gate this release.
