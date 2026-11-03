# Architecture Specification — VidLift Transcription-Model Management

**Author:** Bob (Principal Software Architect)
**Date:** 2026-11-03
**Status:** Recommended — ready for Forge
**Task:** `.specs/bob-vidlift-model-management-task.md`
**Target release:** v0.4.16 (ships alongside the visibility work; `api_version` stays 1)

---

## 1. Executive Summary

Eric ran a Mode B "add subtitles" job and watched it sit **17 minutes with no UI
signal** while the faster-whisper `medium` model (~1.5 GB) downloaded silently —
`current_phase=transcribe`, `progress.step=transcribe-language`, and
`activity_age_seconds` climbing with zero indication of a download. He could not tell
whether the model was present, downloading, or missing, and had no way to install it.

**The root cause is not a missing widget — it is a progress channel that is wired off
end-to-end.** Verified in source (v0.4.15.4):

- `resolve_model_dir` passes `on_progress=None` **literally** at both `_download_file`
  call sites (`transcribe.py:847-854`, `860-867`).
- `_download_file` only invokes `on_progress` **once, after completion**
  (`transcribe.py:967-968`).
- The fetch seam, `fetch(url, target_path)` (`transcribe.py:883-913`), has **no byte
  channel at all** — `shutil.copyfileobj` with a post-hoc `Content-Length` check.
- `transcribe_source_to_srt` declares `on_download_progress` (`transcribe.py:1612`)
  and **never calls it** (its only use today is gating a post-hoc log line at
  `1685-1688`).
- No production caller passes download progress anywhere: `_execute_subs_job`
  (`jobs.py:384-400`) omits it, Mode A (`run.py:2516`, `2522-2544`) has no such kwargs.

**Recommendation — four additive moves, zero new dependencies, zero contract breaks:**

1. **Open the seam (minimal signature change):** the `fetch` contract gains an optional
   third positional `on_progress(done_bytes, total_bytes_or_None)`, invoked per chunk
   by the default fetcher; threaded `fetch → _download_file → resolve_model_dir →
   transcribe_source_to_srt → run_subs_job` (the last hop's parameters **already
   exist** — `subs_job.py:113-115` — and are merely forwarded today). Conditional-arity
   call discipline keeps every existing 2-arg test fake passing untouched.
2. **Publish truth into the existing heartbeat:** in-run publishers
   (`_execute_subs_job`, Mode A `run.py`) record
   `progress["model_download"] = {"bytes_done", "bytes_total"}` and **touch the live
   state/record per chunk** — so `progress["step"]` names the download and
   `activity_age_seconds` stops climbing. This kills Eric's exact symptom through the
   SAME `progress` dict the GUI 1 Hz tick, the `/status` per-job payload, and the
   10-second persisted mirror already consume. No new telemetry path.
3. **Detect with the shipped contract, not a new one:** "installed" == the
   `resolve_model_dir` fast path's own rule — `.complete` marker **and** `model.bin`
   exist at `<state_root>/whisper/models/<tag>/` (`transcribe.py:830-831`; pinned by
   `tests/test_v04154_transcribe_core.py` — "the contract checks ONLY the .complete
   marker"). Integrity is enforced where the core already enforces it — sha256-pinned at
   download time, self-healing re-fetch on marker/bin desync (F-11). **No per-tick
   re-hashing** of a 1.5 GB file.
4. **GUI owns the install, structurally:** a title-stack status pill beside the monitor
   pill plus an explicit **Install** button; the download runs on a `QThreadPool`
   `QRunnable` (the GPU-meter pattern — the only background-work pattern in this GUI),
   NOT the serial `JobExecutor` (a 1.5 GB fetch must never starve the job queue) and NOT
   any MCP/HTTP surface (Hermes stays read-only: no new tool, no new endpoint, `do_GET`
   only).

State surfaces additively as **`transcription_model`** — per-job in `_status_payload`
(tools.py, beside the visibility spec's `transcription` block) and once in the
`/status` envelope (so Hermes sees the machine truth even with an idle queue).
`api_version` is **not** bumped (monitor.py:52-55 convention).

**One-line reason:** the fix threads bytes through the seam the module already owns and
publishes them into the heartbeat the status contract already serves — the failure was
never that VidLift lacked a download; it lacked a *signal*, and signals are this repo's
home turf.

---

## 2. Requirements

### 2.1 Functional (task-named)

| ID | Requirement | Where it lands |
|----|-------------|----------------|
| F1 | **Detect** — explicit UI indication: transcription model present (installed) or not | GUI title-stack pill (1 Hz/event-refreshed disk check) + `transcription_model` status keys |
| F2 | **Progress** — real download progress during any fetch, never mistaken for a hang | `fetch` on_progress channel → `progress["model_download"]` + step label + heartbeat touch; GUI run-panel render; stage-bar fraction |
| F3 | **Install** — explicit in-UI install action when the model is missing | GUI `Install` button → QThreadPool worker → `resolve_model_dir` through the existing fetch seam |
| F4 | **Remote surfacing** — model state in the per-job `_status_payload` for Hermes | Additive `transcription_model` key (per-job) + additive envelope key (idle truth); read-only |

### 2.2 Non-functional (constraints — non-negotiable)

- **N1 No new dependencies.** Reuse `fetch`, `model_cache_dir`, `MODEL_SHA256`,
  `model_download_url_for`, stdlib only.
- **N2 Cross-platform honesty.** Windows is the release target; tests on Linux +
  windows-latest. No POSIX-only locking (no `fcntl`); no fork-only semantics.
- **N3 Additive status keys only; `api_version` stays 1.** monitor.py's documented
  convention; the visibility `_transcription_echo` block (already landed, uncommitted)
  keeps its exact shape.
- **N4 Hermes read-only.** Hermes observes model state; it must be *structurally unable*
  to trigger an install. Install is a GUI action only.
- **N5 No test breakage.** The existing suite stays green **unmodified** (the
  conditional-arity seam discipline exists precisely for this; Forge adds new tests).
- **N6 Honest-null culture.** Unknown is `null`, never fabricated; a stalled/failed
  download reports its truth, not a spinner of lies.

---

## 3. Current-State Analysis (verified in source)

### 3.1 Detection today

`resolve_model_dir` (`transcribe.py:793-880`):

```python
marker = target / ".complete"
if marker.is_file() and (target / "model.bin").is_file():
    return str(target)          # fast path — existence-only, no hash (830-831)
```

- Marker-without-`model.bin` debris falls through to a size/sha-verified re-fetch
  (F-11, `832-835`).
- Post-download, `model.bin` is sha256-verified against `MODEL_SHA256` (`868-878`);
  mismatch → delete + raise naming the retry.
- The fast path deliberately does **not** re-hash on every load (tests pin this).
  **Conclusion: existence is the shipped definition of "installed"; the sha256 pins
  guard the *delivery*, not every read.** The UI indicator mirrors exactly this.

### 3.2 The cache-layout truth (cross-platform)

`model_cache_dir(state_root)` → `<state_root>/whisper/models/<tag>/`; with
`state_root=None` → `benchmark.state_dir()` = `$VIDLIFT_STATE_DIR` else `~/.vidlift`
(`benchmark.py:85-94`, no frozen branch). The frozen GUI converges this because
`run_gui.py` **sets `VIDLIFT_STATE_DIR` before any resolution** (`run_gui.py:23-44`) —
so in production all three consumers (Mode A resolve, Mode B `state_root=
services.state_dir`, the GUI indicator) agree. Dev sessions converge on `~/.vidlift`.
**Invariant the design relies on:** the GUI indicator and install use
`model_cache_dir(client_local.default_state_dir())` — the same root Mode B passes and
`run_gui.py` pins Mode A to.

### 3.3 Threads and surfaces

| Concurrency unit | Thread | Role |
|---|---|---|
| `JobExecutor` | `vidlift-mcp-worker` (single) | runs `run`/`subs`/`benchmark` jobs serially; in-run downloads happen HERE |
| progress mirror | `vidlift-progress-mirror` daemon | persists `progress_snapshot` every 10 s (`jobs.py:712-721`) |
| monitor HTTP | `vidlift-monitor` daemon | serves `/status` from the same registry (`monitor.py:312-315`) |
| GPU meter | `QThreadPool` + `QRunnable` + `pyqtSignal` | **the only existing GUI background pattern** (`gpu_meter.py:144-264`) |
| GUI status | 1 Hz `QTimer` → `_poll_jobs` → `enqueue_snapshots` → `_status_payload` | pure polling projection (`gui.py:502-505`, `1963`) |

Mode A's direct `resolve_model_dir` (`run.py:2516`) runs **under the GPU phase lock**
(`run.py:2459-2483`) — a silent download also serializes other processes' GPU work;
making it visible is doubly load-bearing.

### 3.4 The visibility convention this feature extends

`_transcription_echo(record, state, manifest)` (tools.py:429-472, landed additive):
one helper, honest-nulls, read from `(record, state, manifest)`, called once at
`_status_payload` (tools.py:516). This feature adds a **sibling** helper/key pair.
`api_version` untouched. `test_status_payload_shape_idle` (test_monitor.py:265) and
`test_status_payload_shape_live_passthrough` (282) assert values and served==fresh —
both tolerant of additive keys.

---

## 4. Options Evaluated

### Option A — Thread progress through the existing seam; publish into `progress["model_download"]`; GUI pill + QThreadPool install (RECOMMENDED)

- **Shape:** minimal fetch-seam signature extension (conditional-arity), publishers in
  jobs.py/run.py writing the live progress dict, disk-check detection, GUI title-stack
  pill/Install button, QThreadPool worker, per-tag lockfile in the cache dir.
- **Advantages:** one telemetry path (`progress` dict) serves GUI tick, `/status`
  per-job, and the persisted mirror *simultaneously* — the repo's "one truth, two
  readers" extended, not forked. Heartbeat fix is free (`record.touch()`/`state.touch()`
  per chunk → `activity_age_seconds` behaves). Zero new endpoints/tools/deps.
- **Cost:** ~90 LoC transcribe.py, ~25 jobs.py, ~15 run.py, ~55 tools.py, ~15
  monitor.py, ~120 gui.py + new tests.
- **Risk:** conditional-arity seam is a deliberate contract nuance (documented, and
  enforced only when progress is requested); the lockfile is new concurrency surface
  (ADR-5, scoped to the download critical section).

### Option B — Synthesize progress from the staging file's growth (poll `model.bin.part.tmp` on disk)

- **Shape:** no seam change at all; UI/publishers stat the growing staging file.
- **Fatal flaws:** (1) couples every consumer to the default fetcher's private staging
  name — an injected `fetch` (the seam's whole purpose) may stage anywhere; (2) no
  `Content-Length` → no total, percent never computable; (3) polling cadence invents a
  second, coarser truth next to the heartbeat. **Rejected:** dishonest-by-default for
  injected fetchers, and structurally glued to an implementation detail the F-11 cleanup
  owns.

### Option C — Enqueue the install as a bridge job (`kind="install"`) so it rides executor status

- **Shape:** a new job kind reusing registry/executor/monitor visibility for free.
- **Fatal flaws:** (1) the executor is **serial** — an install behind a 3-hour upscale
  queue (or blocking the queue ahead of one) is Eric's 17 minutes again, this time as a
  queued job; (2) it leaks into `/history`, `jobs.json`, `MAX_GUI_BATCH`-adjacent
  accounting, the startup sweep, and every job-shape pin in the suite — for what is a
  *GUI utility action*, not media work; (3) it invites a remote-trigger path (a stdio
  MCP client could enqueue it), eroding N4. **Rejected:** wrong ownership; a download is
  not a job.

### Option D — New MCP tool / monitor endpoint to report or trigger model ops

- **Rejected outright** by N2/N4 and the bridge's binding spec (stdio-only, "no network
  listener"); a reporting endpoint is redundant once the additive status keys land
  (Option C of the visibility engagement, same reasoning), and a trigger endpoint
  violates read-only by construction.

### Comparison

| Criterion | A (recommended) | B (file-poll) | C (bridge job) | D (new endpoint) |
|---|---|---|---|---|
| Honest with injected fetch | ✅ | ❌ | ✅ | ✅ |
| Progress total (%) | ✅ (Content-Length) | ❌ | ✅ | ✅ |
| Queue never starved | ✅ | ✅ | ❌ serial | ✅ |
| New telemetry path | none | second truth | none | new surface |
| Hermes read-only (N4) | ✅ structural | ✅ | ⚠️ triggerable | ❌ |
| Cross-platform | ✅ stdlib | ✅ | ✅ | ✅ |
| Effort | low | low | medium | medium |

**Selected: Option A.**

---

## 5. Architecture Decision Records

### ADR-1 — Detection: existence-contract (`marker + model.bin`) for the UI; sha256 stays at delivery

- **Problem:** How does the UI answer "installed or not" cheaply enough to refresh at
  1 Hz, without weakening the integrity guarantee?
- **Decision:** Detection mirrors `resolve_model_dir`'s fast path **verbatim**:
  `(<cache>/<tag>/.complete).is_file() and (<cache>/<tag>/model.bin).is_file()`.
  Integrity is enforced where it already is — the download's sha256 pin
  (`MODEL_SHA256`, `transcribe.py:745-749`) and F-11's self-healing re-fetch when the
  marker/bin pair is desynced. No periodic re-hash of 1.5 GB.
- **Alternatives considered:** full sha256 per check (~seconds of disk I/O per paint —
  disqualifying); hash-once-and-cache (a second truth with its own invalidation bug
  farm); marker-only (lies on the F-11 debris shape the core already handles).
- **Advantages:** zero disagreement possible between the indicator and the resolver —
  they read the same two files with the same rule.
- **Disadvantages:** a *manually planted* corrupt `model.bin` with a fresh marker is
  "installed" until engine construction fails honestly at run time — **pre-existing
  shipped behavior**, pinned by tests; unchanged, not worsened.
- **Consequences:** the indicator helper is ~10 LoC of pure predicate, testable without
  any network.
- **Revisit criteria:** an explicit user-triggered "Verify integrity" action (future
  evolution; would reuse `_sha256_file` on click, never on a timer).
- **Dependencies:** `model_cache_dir`, `MODEL_SHA256`, `DEFAULT_MODEL` — all existing.

### ADR-2 — State model: `installed | not-installed | downloading | unknown`, additive `transcription_model` keys (per-job + envelope)

- **Problem:** What is the single honest shape surfaced in BOTH the UI and `/status`,
  including the idle-queue case (no per-job payloads exist to ride)?
- **Decision:** One block, two additive placements:

  ```json
  "transcription_model": {
    "tag": "medium",
    "state": "installed" | "not-installed" | "downloading" | "unknown",
    "cache_dir": "<absolute path>",
    "bytes_done": 734003200 | null,
    "bytes_total": 1531000000 | null,
    "file": "model.bin" | null,
    "error": null
  }
  ```

  - **Bytes fields are null unless `state == "downloading"`** (honest: no download, no
    numbers). `bytes_total` is null mid-download when the server sent no
    `Content-Length` — the fraction is then honestly uncomputable.
  - **`unknown`** covers the unreadable-cache/OSError case (never a guessed
    `not-installed` — a permissions failure is not "missing").
  - **`tag`** is the *effective* tag: the job's recorded pin for `subs` records
    (`options["model"]`), else `DEFAULT_MODEL` — that is not a guess;
    `resolve_model_dir(None)` resolves to `DEFAULT_MODEL` by the module's own rule, so
    the block names the model the run *would* fetch.
  - **Per-job** (`_status_payload`, tools.py): derived live-first (in-flight
    `progress["model_download"]`), else the board (ADR-4), else disk. Derived at read:
    the payload computes the truth, never persists a state string that can rot.
  - **Envelope** (`status_payload` in monitor.py, beside `queued_count`): the same
    block machine-scoped — GUI-install board, then any active job's in-flight
    `model_download`, else disk for `DEFAULT_MODEL`. Solves "queue is idle, is the
    model there?" for Hermes without job choreography.
- **Alternatives considered:** nesting inside the visibility `transcription` block
  (rejected: that block's `{"device", "device_requested"}` shape just landed with
  Karen's pins; a machine-scoped fact inside a job-scoped device echo muddies both);
  per-job only (rejected: idle-queue Hermes visibility, F4's spirit); a separate
  `/model` endpoint (rejected: ADR-1 of the visibility engagement — no endpoint forks).
- **Tradeoffs:** a machine fact duplicated per job is mild redundancy; accepted for
  payload shape stability and because the task names the per-job payload explicitly.
- **Revisit criteria:** a GUI model picker (then `tag` resolution gains a QSettings
  hop — `QSETTINGS_TRANSCRIPTION_KEYS` is already defined-but-unused
  (config.py:79-84)); any consumer needing `small`/`large-v3` idle state explicitly.
- **Dependencies:** none beyond the ADR-3 channel and ADR-4 board.

### ADR-3 — Progress plumbing: optional third positional on the fetch seam, conditional-arity at the call site

- **Problem:** The fetch seam has no byte channel; every existing test fake implements
  `fetch(url, target_path)` and the suite must stay green unmodified (N5).
- **Decision:** Contract change to `fetch(url, target_path, on_progress=None)` where
  `on_progress(done_bytes, total_or_None)` fires per copied chunk:

  ```python
  # _download_file — the discipline pin:
  if on_progress is not None:
      fetch(url, part, on_progress)   # 3-arg: progress requested
  else:
      fetch(url, part)                # 2-arg: the legacy shape, untouched
  ```

  - `_default_fetcher` replaces `shutil.copyfileobj` with an explicit 1 MiB loop that
    calls `on_progress(done, size_hint)` after each chunk (`size_hint` from
    `Content-Length`, else `None`); all existing guards (post-hoc size check, `.tmp`
    staging, BaseException cleanup) unchanged.
  - Signatures gain the optional parameter and forward it:
    `resolve_model_dir(pin, *, state_root=None, fetch=None, on_download_progress=None,
    on_lock_wait=None)`; `transcribe_source_to_srt` passes its (previously decorative)
    `on_download_progress` into `resolve_model_dir`; `run_subs_job` needs **no change**
    (it already accepts and forwards both — subs_job.py:113-115, 190-192).
  - Publishers (jobs.py, run.py) wrap the callback: update
    `progress["model_download"]`, set `progress["step"] = "downloading transcription
    model (model.bin)"` (file named honestly), `state.touch()` + `record.touch()`
    **per chunk** (throttled ≤ 4 Hz publisher-side — the touch is the heartbeat fix),
    and **pop `model_download` on the first post-resolve step** so the fact clears
    when transcription moves on.
- **Alternatives considered:** keyword-only param always passed (breaks every 2-arg
  fake — N5 violation); a module-global progress pub (implicit state, test-poisoning);
  Option B's file polling (rejected wholesale); passing a context object (over-built
  for one tuple).
- **Disadvantages:** conditional arity is a subtle contract — mitigated by making it
  the documented discipline with a dedicated test pair ("2-arg fake never receives a
  third arg"; "3-arg fake receives monotonic chunks").
- **Consequences:** `transcribe_source_to_srt`'s dead parameter becomes load-bearing
  with zero signature churn for the CLI/MCP callers; Mode A's direct resolve
  (run.py:2516) passes the same callback shape.
- **Revisit criteria:** multi-file aggregated progress (support files are
  size-pinned KBs; `model.bin` dominates — per-file reporting is honest enough);
  resumable `.part` offsets (today a `.part` resume is all-or-nothing short-circuit;
  byte-level HTTP `Range` resume is a separate feature).
- **Dependencies:** none new (stdlib `urllib` only).

### ADR-4 — Install action: GUI title-stack pill + Install button on QThreadPool; in-memory board for idle visibility; structurally GUI-only

- **Problem:** Where does "Install" live, on what concurrency, and how does `/status`
  see an install that runs while the queue is idle (no job exists to carry progress)?
- **Decision:**
  - **Placement:** a QLabel pill + small `Install` QPushButton added to the existing
    title stack **directly beside the monitor pill** (`gui.py:1026-1030`). Rationale:
    the task's "Setup screen" does not exist as a separate surface — the controls
    frame/title stack *is* the settings surface; the monitor pill is the proven chrome
    for a machine-state fact with an action affordance. The Plan view was considered
    and rejected: model presence is global machine state, not per-plan input, and the
    pill must be visible *before* any plan exists.
  - **Concurrency:** `model_install.py`-style internals stay in `gui.py`: a
    `QThreadPool`-scheduled `QRunnable` + `QObject` signal pair — **the GPU-meter
    pattern** (`gpu_meter.py:144-150`), the GUI's only background idiom — calling
    `resolve_model_dir(tag, state_root=default_state_dir(), on_progress=…)` with the
    signal emitting throttled updates back to the Qt thread. NOT the `JobExecutor`
    (Option C rejected). No cancel button in v1: the fetcher's BaseException cleanup
    plus `.part` semantics make interruption safe; a stopped download resumes (F-11's
    staging discipline). App-quit mid-install is bounded by the urllib 30 s op timeout.
  - **Idle visibility (`/status`):** a `ModelInstallBoard` — one in-memory slot on
    `BridgeServices` (constructed in `build_services`; soft-read via `getattr` in
    tools/monitor so foreign fakes keep working) holding the GUI install's
    `{state, tag, bytes_done, bytes_total, file, error, updated_at}` under a
    `threading.Lock`. In-memory only — exactly the `set_live` discipline, no file, no
    mirror, no second truth on disk. When the GUI dies, the board dies — correct: a
    dead install is not downloading.
  - **Read-only enforcement:** no new MCP tool (the 9-tool registry untouched), no new
    monitor route (`do_GET` only), no `enqueue` path. **N4 is satisfied by the absence
    of code**: there is no callable remote surface for install — Hermes can only read
    `transcription_model` state.
- **Advantages:** discoverable (visible at launch, pre-plan); honest during batches
  (pill reflects in-run downloads via the same tick's payload); never starves the
  queue; testable headlessly (the runnable's body is a plain function call +
  injectable fetch).
- **Disadvantages:** a GUI-initiated download competes for bandwidth with nothing else
  in-process (fine) but is invisible to *other* processes' indicators (accepted: the
  board is process-local by design; cross-process installs are the stdio server's runs,
  which surface via the job mirror).
- **Revisit criteria:** if Eric ever installs from the stdio/CLI surface, promote a
  shared on-disk install-leases file (deliberately rejected now: a file is a mirror);
  cancel-support if field demand appears (`.part` resume already makes that kind).
- **Dependencies:** PyQt6 (existing), `default_state_dir` (existing).

### ADR-5 — Cross-process/cross-thread download concurrency: per-tag lockfile, progress-driven heartbeat, degrade-open

- **Problem:** Today every in-run download serializes on the single executor thread,
  but the GUI install worker runs *outside* the executor — two writers could race the
  same `<tag>/model.bin.part` (the GUI install vs an in-flight Mode B resolve; also two
  VidLift *processes* — GUI + stdio server — hold no shared in-memory lock).
- **Decision:** One helper in `transcribe.py`, `_model_download_lock(target_dir)` — a
  context manager around the **download critical section only** (from the failed
  fast-path through `marker.write_text`):

  - Acquire: `os.open(lock_path, O_CREAT | O_EXCL | O_WRONLY)` —
    `<tag>/.download.lock` — content `pid\nstarted_at\n`. Blocking retry at 1 Hz with
    an `on_lock_wait(holder_pid, waited_seconds)` callback (mirrors `_gpu_phase_lock`'s
    label discipline; Mode A already owns `_transcribe_wait_label` to copy).
  - **Heartbeat:** the wrapped `on_download_progress` does `os.utime(lock_path)` at
    ≤ 1 Hz — an active download keeps its lock young with zero extra threads.
  - **Stale reclaim:** a lock whose mtime is older than `MODEL_LOCK_STALE_SECONDS`
    (30 min — an active writer refreshes constantly; a dead one can't) is unlinked and
    acquisition retried once. Cross-platform safe: the lock is create-close/utime/unlink,
    never held-open — no Windows sharing-violation trap.
  - **Second-acquirer fast path:** after acquiring, re-run the `.complete` check — the
    winner likely finished while we waited → return instantly (no second download).
  - **Degrade-open:** if the cache dir is unwritable for the lock itself, proceed
    *without* the lock and add a manifest/log note (`model-lock unavailable
    (degrade-open)`) — mirrors the GPU lock's honesty rule; the sha256 pin remains the
    integrity backstop regardless.
- **Alternatives considered:** `fcntl`/POSIX locks (N2 disqualifier); `filelock`
  (new third-party dependency — N1 disqualifier); in-process `threading.Lock` only
  (misses the two-process case); no lock (the race corrupts a download that sha256
  then rejects — honest but wasteful and needlessly confusing).
- **Disadvantages:** a killed-at-first-byte install can strand a lock for up to the
  stale window unless heartbeat ran once — mitigated by unlink-on-exit in the context
  manager (BaseException-safe) plus reclaim.
- **Consequences:** `resolve_model_dir` gains internal serialization for every caller
  (run, subs, GUI install) with no caller-side ceremony.
- **Revisit criteria:** byte-range resume (would move the critical section); observed
  lock contention in the field (none expected — same-tag double-fetch is rare).
- **Dependencies:** stdlib `os`, `pathlib`, `time`.

---

## 6. System Architecture & Data Flow

```
GUI (Qt thread)                        Bridge worker thread            Monitor HTTP thread
─────────────                          ────────────────────            ───────────────────
title-stack pill                       _execute_subs_job / run.py      /status envelope
  └ disk check (2 stats, 1 Hz)              │ resolve_model_dir ──┐        │ scans: board →
Install button ─► QThreadPool               │   lock acquire      │        │  jobs' progress
  └ Runnable: resolve_model_dir()           │   _download_file ──►│ fetch  │  → disk check
        on_progress ─► pyqtSignal ──────────┼─► (same callback    │  per-chunk
                                            │   shape)            │   on_progress
        ▲                                     ▼                   ▼
        └── pill/step render ◄── progress["model_download"] ◄── publisher wraps:
                                          + step label          {bytes_done,bytes_total,file}
                                          + state.touch() ─► activity_age honest
                                          + record.touch() ─► mirror (10 s) persists it
                                                                 │
ModelInstallBoard (in-memory, BridgeServices) ◄── GUI install worker when idle
        └ consumed by: _transcription_model_echo (per-job) and envelope builder
```

**Derive-at-read hierarchy (both surfaces):** in-flight live fact
(`state.progress["model_download"]`, same-process) → persisted mirror fact (record
`running`, cross-process; freshness visible via the existing `activity_age_seconds`) →
board (GUI idle install) → disk (ADR-1 predicate) → `unknown` on OSError.

**Hermes path:** `GET /status` (unchanged token gate) → envelope `transcription_model`
(always present) + per-job `transcription_model`. The Hermes `vidlift-status` script
renders one line: `subs model: installed (medium)` / `…downloading 734/1531 MB (47%)`/
`…not installed — open VidLift and click Install` / `…unknown`. No install affordance
exists on that side, structurally.

## 7. Security Design

- **AuthN/z unchanged:** additive keys only inside existing token-gated payloads;
  `/health` untouched (no model state leaks there — it is `ok, version, busy`, pinned).
- **Remote trigger surface: none added.** The bridge tool registry stays at 9; monitor
  stays `do_GET`; `StatusView._VIEW_METHODS` unchanged. Karen greps the diff.
- **Download integrity:** unchanged and pin-enforced (`MODEL_SHA256`, size pins,
  `MIN_DOWNLOAD_SIZE`, `.part`/`.tmp` cleanup). The lock never weakens verification —
  degrade-open still lands the same sha check before the marker is written.
- **The lockfile is not a security boundary** — it is a courtesy to bandwidth and
  correctness on one trusting machine; content is pid/timestamp only.
- **Cache path in payload:** `cache_dir` is a local absolute path disclosed to
  token-holding LAN readers — same disclosure class as existing `log_path`/
  `manifest_path`. Accepted; flagged for record.
- **Blast radius of a hostile `Content-Length`:** none — total is display-only; the
  sha256 pin, not the length, gates installation.

## 8. Failure Analysis

| Failure | Behavior |
|---|---|
| Network down / HF unreachable | `TranscriptionError("transcription model download failed (model.bin): …")` — install button returns to *Install*, pill shows *not installed*, GUI warning box; in-run Mode B job fails honestly, Mode A fails open (existing dispositions) |
| Truncated response | Size-mismatch raise (existing), `.part`/`.tmp` deleted, state → not-installed |
| Tampered/corrupt bytes | sha256 mismatch → delete + raise "retry to refetch" (existing pin) |
| Kill mid-download | `.tmp` cleaned (F-11); `.part` may remain — next attempt resumes the short-circuit where size-pinned, else refetches; lock goes stale in ≤ 30 min and is reclaimed |
| Second downloader (install + job, or two processes) | Loser blocks with a wait label; on acquire, fast-path returns the winner's completed cache — no double fetch |
| Cache dir unwritable (permissions) | Lock degrade-open + note; download still sha-verified; if the cache itself can't write, `TranscriptionError` names it; status surfaces `unknown` — never a fabricated `not-installed` |
| `VIDLIFT_TRANSCRIPTION_ENGINE=0` | `resolve_model_dir` refuses honestly (existing) — install surfaces that refusal verbatim |
| GUI quit mid-install | Runnable continues to its current 30 s op timeout at worst; no cancel semantics promised in v1 (documented); `.part` consistency as above |
| Reader process stale mirror | `model_download` in a persisted mirror is trusted **only while the record is `running`**; terminal records derive from disk |

## 9. Risks

- **R1 (seam subtlety):** conditional-arity fetch calls. Mitigation: documented as the
  contract + a pinned test pair; the 2-arg path is the default (no-progress callers).
- **R2 (Mode A GPU-lock dwell):** downloads already ride the GPU phase lock; now that
  they're visible, Eric will *see* the lock-wait label plus download label. No new
  dwell — but a future pass may move resolution before the lock acquire (out of scope;
  noted in revisit criteria).
- **R3 (payload growth):** one small constant block per job + envelope; scale-neutral
  (the passthrough test's served==fresh holds trivially).
- **R4 (indicator/tag skew):** orgs pinning `transcription/model=small` would see the
  GUI indicator track the *default* tag while jobs fetch the pin. Mitigation: the pill
  names its tag explicitly ("…(medium)"); the picker is a documented future evolution
  riding the already-defined-but-unused QSettings keys.

## 10. Future Evolution

- GUI model picker (`small|medium|large-v3`) consuming `QSETTINGS_TRANSCRIPTION_KEYS`
  (config.py:79-84) — the state model's `tag` resolution gains one hop, no shape break.
- Cancel-install button (the fetcher's cleanup already makes interruption safe).
- Byte-range resume (`Range` on `.part` offsets) — a fetch-seam enhancement, additive.
- Moving Mode A model resolution *before* the GPU phase lock acquire (bandwidth work
  needn't hold GPU serialization).
- User-triggered integrity re-verify (ADR-1 revisit).
- Hermes render polish (rate estimate from successive `bytes_done` samples) — additive
  client-side only.

---

## Appendix A — Scope map vs. task asks

| Task line | Answered by |
|---|---|
| Detect (F1) | ADR-1 + pill + `transcription_model.state` |
| Progress (F2) | ADR-3 channel + publishers + run-panel/stage-bar render |
| Install (F3) | ADR-4 (GUI pill/button, QThreadPool) |
| Status payload surfacing (F4) | ADR-2 (per-job + envelope, additive, `api_version` stays 1) |
| No new deps / reuse seams | ADR-3 (existing `fetch`, `MODEL_SHA256`, `model_cache_dir`) |
| Cross-platform honest | ADR-1, ADR-5 (O_EXCL/utime only); tests Linux + windows-latest |
| Hermes read-only | ADR-4 + §7 (no tool, no endpoint, no enqueue path) |
