# Validation Specification — VidLift Transcription-Model Management

**For:** Karen (expand freely — this is the baseline, not the ceiling)
**Author:** Bob
**Companions:** `bob-vidlift-model-management-architecture-spec.md`,
`bob-vidlift-model-management-implementation-spec.md`
**Feature:** F1 detect · F2 download progress · F3 in-UI install · F4 status-payload
surfacing — with a fake `fetch` seam throughout; NEVER a real download, NEVER the
network beyond 127.0.0.1, NEVER the real model.

---

## 1. What Must Be Proven

1. **P1 — Detection honesty.** The UI and `/status` report `installed` /
   `not-installed` / `unknown` from the SAME predicate `resolve_model_dir` uses
   (`.complete` marker **and** `model.bin`, at `<state_root>/whisper/models/<tag>/`).
   No case may report `installed` when the resolver would re-fetch, nor
   `not-installed` when the cache is merely unreadable (that is `unknown`).
2. **P2 — Progress truth.** A model download produces per-chunk byte callbacks
   (`done` monotone nondecreasing, final `done` == actual size; `total` ==
   `Content-Length` when sent, `None` otherwise), lands as
   `progress["model_download"]` on the running job, keeps the heartbeat advancing
   (`activity_age_seconds` does NOT climb across a simulated slow download), clears
   when resolve returns, and renders as a byte label in the GUI run panel in BOTH
   Mode A (`subtitles` phase) and Mode B (`transcribe` phase).
3. **P3 — Install action.** The GUI Install button runs the download off-thread
   (QThreadPool), throttled-progress the whole way, serializes against concurrent
   downloaders via the lock, ends `installed` on success and `not-installed` +
   warning + re-enabled button on failure — all without touching the JobExecutor.
4. **P4 — Remote surfacing, additively.** Per-job `_status_payload` and the `/status`
   envelope carry `transcription_model` with the documented key set;
   `api_version` remains 1; `/health` is unchanged; every pre-existing key is
   unchanged (the visibility block `transcription` keeps its exact shape).
5. **P5 — Read-only Hermes.** No code path outside the GUI triggers an install: no
   new MCP tool, no new monitor route/verb, no new enqueue kind; the Hermes render is
   a pure read.
6. **P6 — No regressions, cross-platform.** The full suite passes **unmodified**
   (conditional-arity seam discipline) on Linux and windows-latest; the lock uses no
   POSIX-only primitives.

---

## 2. Seam & Progress Matrix (fake-fetch driven)

| # | Scenario | Expected |
|---|----------|----------|
| M-1 | Legacy 2-arg fake fetch (`def fetch(url, target): …`) through `resolve_model_dir` with NO progress consumer, cold cache | works exactly as before — never receives a third argument (record call arity; assert every call len==2); marker + bin land; suite's F-11 tests stay green untouched |
| M-2 | 3-arg fake fetch with progress consumer | per-chunk callbacks observed: ≥2 beats for a multi-chunk fake, `done` strictly monotone, final `done` == file size, `total` forwarded as given (including `None`) |
| M-3 | Default fetcher against a **127.0.0.1 `http.server`** serving N known bytes WITH `Content-Length` | callback beats sum to exactly N; `total == N` on every beat; `.tmp` staging cleaned; final file byte-identical (conftest's `_no_network` permits loopback — verify route stays loopback-only) |
| M-4 | Default fetcher against loopback server with NO `Content-Length` (chunked) | beats arrive with `total is None`; final size check skipped (existing semantics); file correct; the payload/GUI never fabricate a percent |
| M-5 | Default fetcher: server truncates (Content-Length N, delivers <N) | `TranscriptionError` truncation wording preserved (existing pin), `.tmp` removed, no marker written, state resolves `not-installed` |
| M-6 | Fake fetch writes wrong bytes | sha256 mismatch → delete + "retry to refetch" raise (existing pin, new path); `model_install_state` reports `not-installed` afterward |
| M-7 | `.part` short-circuit | a staged `model.bin.part` of the size-pinned support-file shape (or pre-existing complete bin flow) behaves exactly as the current contract (F-11 pins green) |
| M-8 | Progress throttling in publishers (jobs/run wrappers) | faster-than-4 Hz callback floods collapse to ~4 Hz progress-dict churn (beat count bounded); final beat always lands |

## 3. Detection & State Derivation Matrix

| # | Scenario | Expected |
|---|----------|----------|
| D-1 | Cold state dir (nothing) | `model_install_state("medium")` → `not-installed`; exact key set `{tag, state, cache_dir, bytes_done, bytes_total, file, error}`; bytes/file/error all `None` |
| D-2 | Warm cache (marker + bin fabricated) | `installed`; `cache_dir` names the real path |
| D-3 | Debris: marker WITHOUT `model.bin` (F-11 shape) | `not-installed` — indicator and resolver agree (resolver would re-fetch) |
| D-4 | Debris inverted: `model.bin` WITHOUT marker | `not-installed` |
| D-5 | Cache dir unreadable (monkeypatch `Path.is_file` to raise OSError) | `state == "unknown"` — NEVER fabricated `not-installed` |
| D-6 | Board `downloading` snapshot vs cold disk | board wins: `downloading` with bytes carried |
| D-7 | Board `fail(msg)` snapshot vs cold disk | disk truth `not-installed` + `error == msg` |
| D-8 | Board `succeed` but disk cold (lying board) | disk truth `not-installed` — the board never overrides the file system for the terminal states |
| D-9 | Board snapshot for a DIFFERENT tag than queried | ignored entirely |
| D-10 | Live job `progress["model_download"]` present | per-job payload shows `downloading` with those bytes REGARDLESS of disk |
| D-11 | Cross-process: record `running`, only `progress_snapshot` mirror carries `model_download` | per-job payload shows `downloading` (mirror truth); freshness visible via `activity_age_seconds` |
| D-12 | Terminal record (succeeded/failed) whose last mirror still carries `model_download` | mirror NOT trusted for terminal records — derive from disk |
| D-13 | Tag resolution: subs record with `options["model"]="small"` | block's `tag == "small"` and disk check targets the `small` cache dir |
| D-14 | Tag resolution: subs record with an unknown pin (a PATH per resolve semantics) | block reports the DEFAULT tag's machine state (documented honest rule — path-pinned models need no installer) |
| D-15 | run/benchmark-kind records | `tag == DEFAULT_MODEL`, same derivation (no per-kind lies) |

## 4. Status-Contract Matrix (`/status`, per-job, envelope)

| # | Scenario | Expected |
|---|----------|----------|
| S-1 | Envelope, idle, cold cache | `transcription_model.state == "not-installed"` at TOP level of `/status`; `api_version == 1` |
| S-2 | Envelope, GUI install in flight (fake board downloading) | envelope `downloading` with bytes — visible with ZERO jobs in the system |
| S-3 | Envelope, active job mid-download | envelope mirrors the job's in-flight block (job truth beats idle disk) |
| S-4 | Per-job, live subs job with model_download in live state | per-job block `downloading` + bytes (D-10 through the real `_status_payload`) |
| S-5 | Per-job, terminal subs job, warm cache | `installed` |
| S-6 | **`api_version` stability** | `monitor.API_VERSION == 1`; the vidlift_version_note additive convention unchanged |
| S-7 | `/health` | still token-free, still EXACTLY `{ok, version, busy}` — no model key |
| S-8 | Additivity guard | a pre-change payload fixture's keys ∪ `{"transcription_model"}` == new key set, no renames, no removals; the visibility `transcription` block keeps EXACTLY `{device, device_requested}` |
| S-9 | Served == fresh passthrough | `test_status_payload_shape_live_passthrough` semantics hold with the new key (served entry == fresh `_status_payload` modulo `activity_age_seconds`) |
| S-10 | Soft-read tolerance | a fake services object WITHOUT `model_install_board` does not 500 the envelope or the per-job payload (honest degrade to disk derivation) |
| S-11 | Foreign-state tolerance | a fake RunState lacking `progress`/`subtitles` shapes still yields a well-formed `transcription_model` block (defensive getattr, mirroring the `_transcription_echo` discipline) |

## 5. GUI Matrix (qtbot, fake fetch — never the network)

| # | Scenario | Expected |
|---|----------|----------|
| G-1 | Construct MainWindow with cold state dir | pill: `Subs model (medium): not installed — first subtitles use downloads ~1.5 GB`; Install button visible + enabled |
| G-2 | Construct with warm cache | pill: `Subs model (medium): installed`; button HIDDEN |
| G-3 | Click Install, fake fetch feeds 3 progress beats then completes (fabricate marker+bin via the fake) | button disables/`Downloading…` during flight; pill byte counts render (`fmt(d)`/`fmt(t)` `(pct%)`); final: `installed`, button hidden; `QThreadPool` used (assert no executor interaction — `client.services.executor` job count unchanged, no record created) |
| G-4 | Fake fetch raises `TranscriptionError("…check network access")` | warning `QMessageBox` shown with the error text and the retry wording; pill back to `not-installed`; button re-enabled; `_subs_model_installing` cleared (second click works) |
| G-5 | Double-click during flight | exactly one runnable (the in-flight guard holds) |
| G-6 | Progress with `total=None` | pill renders `… (total size unknown)` — never a fabricated percent |
| G-7 | Lock-wait signal mid-install (fake fetch blocks once with an existing fresh lock fabricated by the test) | pill surfaces the waiting state; completes after the lock frees |
| G-8 | Run-panel render: live snapshot, phase `transcribe`, `progress["model_download"]` with totals | `run_stage_label` returns `downloading transcription model — X / Y (Z%)`; stage bar receives `set_value` (not busy) |
| G-9 | Same, phase `subtitles` (Mode A) | identical label behavior (the branch covers both phases) |
| G-10 | Same, `bytes_total is None` | label `… (total size unknown)`; stage bar stays busy-pulsed |
| G-11 | Pill reflects an IN-RUN download (live job publishing model_download) while a slate is live | pill/step truth agrees with the 1 Hz payload (the board covers idle installs; in-run truth arrives via the tick) |
| G-12 | GUI does NOT touch widgets off-thread | progress/finished arrive via signals only (code inspection + a qtbot signal spy asserting emissions happen; the runnable body contains no widget references by construction — grep the diff) |

## 6. Lock Matrix (cross-platform — must pass on windows-latest)

| # | Scenario | Expected |
|---|----------|----------|
| L-1 | Cold acquire | `.download.lock` created with pid+timestamp content; released (unlinked) on exit |
| L-2 | Held lock, second `resolve_model_dir` call | waiter blocks, `on_lock_wait(pid, seconds)` fires; when the winner fabricates marker+bin and releases, the waiter re-runs the fast path and returns WITHOUT downloading (fetch must not be called — assert call count 0) |
| L-3 | Stale lock (fabricate, backdate mtime > `MODEL_LOCK_STALE_SECONDS`) | reclaimed unilaterally; acquisition proceeds; no exception |
| L-4 | Unwritable cache dir (chmod/mocking O_EXCL create to raise OSError) | degrade-OPEN: download proceeds without lock, no hang, no raise from the lock itself; sha256 verification still gates completion |
| L-5 | Exception mid-download (fake fetch raises) | lock released in `finally` (no stranded fresh lock); `.tmp` cleaned (F-11); state `not-installed` |
| L-6 | Heartbeat | a fake long download with progress beats refreshes lock mtime (assert `st_mtime` advances); with `on_download_progress=None` the internal no-op heartbeat still refreshes |
| L-7 | Windows semantics | no `fcntl`/signals anywhere in the diff (grep); lock never held open across operations (create-close/utime/unlink only) |

## 7. Heartbeat / "Never Mistaken for a Hang" (Eric's original symptom)

| # | Scenario | Expected |
|---|----------|----------|
| H-1 | Simulated slow Mode B download (fake fetch sleeping between chunks, driven through `_execute_subs_job` with a stub engine_factory and the `transcription_engine_on` fixture) | across the download window, `record.last_activity_at` keeps advancing (per-beat touch); a reader's `activity_age_seconds` stays small — contrast-pin: BEFORE this feature the same shape climbed unbounded |
| H-2 | Same shape, Mode A (run.py publisher) | identical heartbeat behavior via `state.touch()` |
| H-3 | Download completes | `progress["model_download"]` popped at the first post-resolve step; subsequent payloads carry no stale byte counts; `progress["step"]` moves on (`transcribe-language`…) |
| H-4 | Mirror persistence | within one mirror cadence, `progress_snapshot["progress"]["model_download"]` is on disk (registry jobs.json) for a cross-process reader (D-11's persistence half) |
| H-5 | Run panel during a real-ish fake download | label renders byte counts (G-8/G-9) — no "frozen step" window between `transcribe-resolve` and first byte beyond connect latency |

## 8. Read-Only / Security Expectations (assert all)

- R-1: the bridge tool set is unchanged (9 tools; no `install`/`model` tool registered).
- R-2: monitor routing table unchanged (`/health`, `/status`, `/history`; everything else 404; non-GET 501).
- R-3: `test_status_view_cannot_write` and every read-only-construction pin pass unmodified.
- R-4: no endpoint, tool, file, or enqueue kind OUTSIDE gui.py reaches `resolve_model_dir`'s download path with the new parameters (grep the diff: the only new callers are the GUI install worker, `_execute_subs_job`'s pass-through, and run.py's pass-through).
- R-5: `transcription_model.cache_dir` disclosure is the same class as existing `log_path`/`manifest_path` (token-gated) — and does NOT appear in `/health`.
- R-6: the discovery card (`monitor.json`, visibility ADR-2) is unaffected by this feature (it stays the provisioning artifact; no model keys leak into it).
- R-7: Hermes script contains no install/trigger code path — its model line is a pure render of served state.

## 9. Performance Expectations

- Disk predicate = 2 `stat` calls per refresh (1 Hz while a slate is live; evented otherwise) — measure: no visible tick cost.
- Publisher throttled ≤4 Hz; mirror cadence unchanged (10 s); `/status` response growth is one small constant block per job + envelope.
- sha256 verification cost unchanged (once per download, post-completion — ADR-1).
- No new threads beyond the QThreadPool install worker (exists only during an install).

## 10. Regression Gate (must pass, unmodified)

- Full suite green with ZERO edits to existing tests, sources outside the spec'd
  hunks, or fixtures — explicitly re-run: `test_v04154_transcribe_core.py` (F-10/F-11,
  incl. marker/bin and staging-debris pins), `test_v04152_cuda_fallback.py`,
  `test_v0414_subs_job.py`, `test_v0414_mode_b_gui*.py`, `test_monitor.py`,
  `test_monitor_gui.py`, `test_progress_mirror.py` (M1-M6), `test_mcp_bridge.py`,
  `test_gui.py`, `test_stale_activity.py`, and the visibility block's tests.
- CI: Linux AND windows-latest legs both green (lock cross-platform proof).

## 11. Acceptance Criteria (ship when ALL hold)

1. P1-P6 proven (§2-§9 pass).
2. Eric's repro inverted end-to-end on a dev build: fresh state dir → pill says
   *not installed*; click Install → byte-counting pill → *installed*; Mode B job on a
   cold cache → visible byte-progress label, healthy `activity_age_seconds`; on a warm
   cache → no download signal at all.
3. `/status` (token-gated) shows `transcription_model` per job AND in the envelope
   with `api_version == 1`; Hermes `vidlift-status` prints the model line for idle
   and busy servers.
4. Two concurrent downloaders (GUI install + a fabricated second resolver; and the
   two-process shape via a second python instance against the same state dir)
   serialize: one downloads, the other waits and fast-paths; the cache ends valid.
5. Karen's independent expansion finds no remote install trigger, no new mutation
   surface, and no fabricated-percent or fabricated-`not-installed` lie anywhere.

## 12. Known Honest Gaps (NOT failures — set expectations)

- **Total-unknown downloads** (no `Content-Length` from HF): the UI/payload honestly
  render bytes-without-percent. (HF sends Content-Length today; the None path is the
  defensive arm.)
- **GUI pill tag** tracks the configured default (`medium`); a job pinned (CLI/MCP)
  to `small`/`large-v3` surfaces correctly *per job* but does not retarget the idle
  indicator (named explicitly in the pill text — architecture ADR-2/R4).
- **In-flight install has no cancel button** (v1): closing the window lets the
  runnable run to its next op timeout; `.part`-level debris is resumable/cleaned on
  the next attempt (architecture ADR-4).
- **A board `downloading` state is process-local**: a second VidLift process cannot
  see the GUI's idle install as `downloading` (it sees `not-installed` until the
  marker lands). Cross-process visibility exists only for job-carried downloads (the
  mirror). Accepted tradeoff, documented for revisit.
- **Stale-lock reclaim window** (30 min) means a killed-at-first-byte download can
  serialize-block a second attempt for up to that window when NO heartbeat ever ran
  (pathological; heartbeat normally keeps the lock young within the first MB).
