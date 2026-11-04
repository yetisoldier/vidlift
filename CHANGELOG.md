# Changelog

All notable changes to VidLift. Dates are America/Chicago.

## [0.4.17.0] - 2026-11-04

### Added

- **Mode B opt-in "replace original on success"**: the Add Subtitles flow can now
  delete the source video after a verified subtitles pass and rename the output to
  the source's name with an `.mp4` extension
  (e.g. `35 Hours.avi` → `35 Hours.mp4`). Default remains OFF — without the flag,
  the source is never touched and the output stays `{stem}.subs.mp4`.
  - **GUI**: a new checkbox beside the "Add Subtitles…" button (persisted, default
    off). Its tooltip states the naming rule and the keep-both guarantee.
  - **CLI**: `vidlift subs --replace-source`.
  - **MCP**: the `transcribe` tool accepts an optional `replace_source` boolean
    (`api_version` unchanged; absent means off for older callers and recorded jobs).
- **Sidecar rename-to-match**: on a successful replace, `{stem}.subs.srt` is renamed
  to `{stem}.srt` so the delivered pair is `{stem}.mp4` + `{stem}.srt` (player
  auto-load convention). If a different file already occupies `{stem}.srt`, the
  sidecar keeps its `.subs.srt` name and a warning is written to the job notes —
  the sidecar never blocks or destroys the media replace.
- **Collision keep-both guard**: when `{stem}.mp4` already exists as a *different*
  file beside the source, the whole replace is refused before anything is touched —
  the source, the verified output, and the unrelated file all survive, the warning
  names the refusal, and the job still succeeds. When the source itself is an
  `.mp4`, the rename is a single atomic move (safest case).

### Notes

- The unrelated sidecar-vs-burned `.srt` output-choice toggle Eric raised is a
  tracked follow-up and is **not** part of this release.

## [0.4.16.2] - 2026-11-03

### Fixed
- **Transcription works on fresh Windows installs**: Fixed a packaging defect in v0.4.16.1 where the Silero VAD model was not bundled. Transcription jobs no longer fail with `NO_SUCHFILE` errors on first run.
- **Clearer transcription failures**: Added a pre-flight check that detects missing VAD assets immediately, providing a clear error message and remedy instead of failing mid-job.
- **Release-gate hardening**: CI now automatically verifies that the VAD asset set is present and correctly named in the bundled payload to prevent future regressions.

### Added
- **Installation health check**: Added `vidlift-cli --selftest`. This command prints a JSON capability report and exits with a success/fail code, allowing users and CI to verify a healthy install before starting a job.
- **Enhanced monitor status**: The `GET /status` API now includes a `transcription_engine` block reporting the readiness of the transcription assets and package version. This is additive and does not affect older clients.

## [0.4.16.1] - 2026-11-03

### Fixed

- **Fail-path propagation in the GUI**: failures during early phases (e.g. `probe` or `transcribe`) now correctly settle on every surface. The details panel, LIVE overlay, and both status bars now reflect the `failed` state immediately, ending stale "running" labels and busy animations.
- **Failure reasons are surfaced**: the actual exception text (e.g. `{phase} command failed (exit N)`) is now carried to the user. This is implemented as an additive `error` key on history rows—round-tripping through the local client, MCP `tool_list`, and the HTTP `/history` pass-through. `api_version` remains 1; records persisted before this release load with `error: null`.
- **Version-stamp fix**: resolved a drift where `packaging/version_info.txt` numeric tuples (`filevers` / `prodvers`) remained at `0.4.16.0` while string stamps and package metadata had moved to `0.4.16.1`. All stamps now agree.

## [0.4.16.0] - 2026-11-03
 
### Added
 
- **Transcription-model management** (F1 detect, F2 progress, F3
  install, F4 surfacing): the faster-whisper `medium` model's
  presence, download progress, and one-click install are now visible
  everywhere VidLift already reports truth — no new endpoints, no new
  MCP tools, no new dependencies, `api_version` stays 1.
   - **In-run download progress** (Eric's 17-minute silent-stall fix):
     the fetch seam's contract gains an optional third positional —
     `fetch(url, target_path, on_progress=None)` with
     `on_progress(done_bytes, total_bytes_or_None)` fired per copied
     chunk by the default fetcher (Content-Length carries the total;
     a close-delimited response publishes `None` and never a
     fabricated percent). `resolve_model_dir` and
     `transcribe_source_to_srt` forward it; the Mode B publisher
     (`_execute_subs_job`) and Mode A's direct resolve publish
     `progress["model_download"] = {bytes_done, bytes_total, file}`
     into the SAME progress dict the GUI tick / `/status` / mirror
     consume, and every beat touches the live record/state so
     `activity_age_seconds` stops climbing during a long download. The
     fact is popped at the first post-resolve step; conditional-arity
     call discipline keeps every legacy 2-arg test fake passing
     unmodified.
   - **Per-tag download lock (ADR-5)**: the model-download critical
     section serializes on `<tag>/.download.lock` — O_CREAT|O_EXCL
     create-close (no Windows sharing violations), the download's own
     progress heartbeats the lock's mtime at ≤1 Hz, a lock older than
     `MODEL_LOCK_STALE_SECONDS` (30 min) is reclaimed, a
     second-arriving downloader re-runs the fast-path check after
     acquiring (no double fetch), and an unwritable cache dir degrades
     OPEN (no hang, no refusal — the sha256 pin remains the integrity
     backstop).
   - **GUI pill + Install button (ADR-4)**: a title-stack pill beside
     the monitor pill — `Subs model (medium): installed / not
     installed — first subtitles use downloads ~1.5 GB / downloading —
     X / Y (pct%) / state unknown (cache unreadable)` — plus an
     explicit Install button running `resolve_model_dir` on a
     `QThreadPool`/`QRunnable` (the GPU-meter pattern; never the
     serial JobExecutor). Install is a GUI-only action: no new MCP
     tool, no new monitor route, no enqueue kind — Hermes stays
     structurally read-only.
   - **Status surfacing (ADR-2)**: additive `transcription_model`
     block (tag, state, cache_dir, bytes_done, bytes_total, file,
     error) on every per-job `_status_payload` AND once on the
     `/status` envelope (so a watcher sees machine truth with an idle
     queue). Derived at read — in-flight job bytes → the GUI install
     board (`ModelInstallBoard`, in-memory, process-local) → the disk
     predicate (the resolve fast-path's own `.complete` marker AND
     `model.bin` rule) → `unknown` on an unreadable cache. Never
     persisted as a state string.
   - **Run-panel render**: a download beat renders
     `downloading transcription model — X / Y (pct%)` in both the
     `subtitles` (Mode A) and `transcribe` (Mode B) phases, with the
     stage bar determinate when the total is known and
     honestly-indeterminate when it is not (no new phase, no
     `_PHASE_BUSY_LABELS` churn).
- **Suite hermeticity**: the default state-root resolution
  (`benchmark.state_dir()`) lands on a per-test tmp root during tests
  — the suite no longer creates/unlinks the real
  `~/.vidlift/.gpu-phase.lock` (50 cycles per run observed) nor reads
  the dev session's warm model cache; an explicit
  `$VIDLIFT_STATE_DIR` pin still wins, and the isolated root carries
  the seeded model cache the resolve fast-path contract promises.
 
### Fixed
 
- **A 17-minute model download looked like a hang**: every download
  beat now touches the running record and state (≤4 Hz publisher
  throttle), so the GUI's stale-activity warning, the `/status`
  `activity_age_seconds`, and the 10-second mirror all stay honest
  during first-use model fetches.
- **Cancel was silently dropped outside the engine phases** (the
  §4.6 observe-fix; Eric's 2026-11-03 repro: "cancel current"/"cancel
  batch" left the job rendered `running` — `/status` read `running`
  long after the click and `/history` held a mix of `cancelled` AND
  `failed` rows): `run_file` observed the job's cancel flag only
  inside the engine adapter (upscale) and between transcription
  segments. A cancel landing during prepare, encode, verify, or a
  sidecar mux ran the (minutes-to-hours) child to completion, the run
  CONTINUED into the next phases, and the job closed `succeeded` — the
  registry transition to `cancelled` never fired, so the GUI's 1 Hz
  tick and the monitor faithfully rendered `running`. A terminated
  child (whatever the cause) surfaced as `VideoToolError`, closing the
  job `failed` and masking the cancel vocabulary. Fixed on three legs,
  all inside the documented binding semantics ("run_file's cancel
  callable observes it"; the engine adapter keeps its own discipline):
  - **`run_file` observes the flag at every boundary** (run.py): before
    each phase command, after each command returns — before the
    nonzero-code guard, so a killed child reads cancelled, never
    failed — after the engine, after verify, and before the success
    manifest. A cancelled run writes the cancelled manifest and never
    spends GPU work after the flip (a pre-cancelled run no longer
    issues the prepare command).
  - **`_subprocess_runner` is cancel-aware** (run.py): with a live
    flag, the unlimited `communicate()` wait becomes the engine
    adapter's binding child discipline (drain threads, 0.2 s poll,
    terminate -> wait 10 s -> kill -> wait 5 s) and raises
    `EngineCancelled` — the underlying ffmpeg/ffprobe child actually
    stops. Without a flag the (rc, stdout, stderr) contract is
    byte-identical.
  - **The executor forwards the flag through its tracked runners**
    (jobs.py): run/subs jobs' production children terminate on the
    flip like the engine's own do; a cancelled BENCHMARK job now
    closes `cancelled` (it used to fall through the run-error mapping
    as `failed`). Canceled state reaches the registry within the
    wind-down (≤ ~10 s worst case), and the GUI tick / monitor
    `/status` reflect it on the following read — regression tests in
    `tests/test_v0416_cancel_propagation.py` plus the GUI-tick test in
    `test_gui.py`.
- **Karen's v0.4.16 validation remediation** (2026-11-03 report; three
  honesty fixes the promised test matrices' absence had let slip,
  plus three hardening fixes — the matrices themselves are landed
  with this release, so each fix now has its cell pinned):
  - **Mode A download beats now touch the JobRecord** (D-1): run.py's
    in-run download publisher touched only the live state, so a
    first-use model download inside a run-job left the record's
    `last_activity_at` frozen while the state advanced — every
    reader's `activity_age_seconds` kept climbing during a LIVE
    download (the GUI's stale-activity warning, the stage label,
    `/status` crossed the 15-min threshold on a 17-minute fetch). The
    executor now carries the record in run_file's state_box seam and
    the publisher touches record+state per beat, mirroring the Mode B
    publisher. The model-management spec's H-2 heartbeat-parity test
    is landed (`tests/test_v0416_mode_a_honesty.py`).
  - **A failed in-run download no longer reports "downloading"** (D-2):
    Mode A's fail-open conversion popped nothing — a terminal download
    failure (network error, sha mismatch, truncation) left the
    `model_download` byte fact AND its "downloading" step label riding
    status surfaces for the rest of the run. The conversion now pops
    the fact and moves the step label on (mirroring Mode B's
    unconditional pop). The H-3 Mode A analog test is landed.
  - **Terminal run jobs echo their device** (D-3): `_transcription_echo`'s
    manifest arm read top-level `manifest["subtitles"]` but every real
    manifest stores the subtitles block at `manifest["run"]["subtitles"]`
    — Hermes's one-shot read of a finished/failed run job always saw
    `transcription.device: null`. The read now keys the real layout.
    The T-1..T-6 device-echo matrix is landed
    (`tests/test_v0416_transcription_echo.py`).
  - **The per-tag download lock can no longer spin on a mid-wait
    permission flip** (D-4): round-0 contention followed by the cache
    dir going read-only looped the OSError retry at 1 Hz forever
    (the degrade-OPEN arm fired only on round 0). A PermissionError on
    ANY round — or 3 consecutive non-FileExistsError OSErrors — now
    degrades OPEN the same way (the sha256 pin remains the backstop;
    never a hang). Probe regression tests appended to the lock matrix.
  - **The discovery card is owner-only** (D-5): the card carries the
    effective monitor token but was created world/group-readable
    (0o664 under this umask). It is now created 0o600 (and the state
    dir 0o700 when VidLift creates it), so the token no longer
    bypasses the `/status`/`/history` gate on shared machines.
  - **The discovery card's staging name is pid-unique** (D-6): the
    fixed `monitor.json.tmp` let two GUI instances sharing a state
    dir interleave one staging file — atomic `os.replace` moves only
    this writer's own completed card now.
  - The visibility spec's D-1..D-6 discovery-card matrix is landed
    (`tests/test_v0416_discovery_card.py`), the model-management
    spec's H-2/H-3 Mode A cells in
    `tests/test_v0416_mode_a_honesty.py`; no behavior claim in the
    entries above extends beyond what the landed tests pin.
 
### Known issues
- **Lock stale-reclaim spin (R-1)**: the lock stale-reclaim logic can
  spin on a read-only directory containing a stale lock; non-gating
  follow-up.


## [0.4.15.4] - 2026-11-03

### Fixed

- **Overall queue bar inflated then receded at job boundaries** (N-1,
  Medium): v0.4.15.3's measured-floor honesty regressed at the NEXT
  scale — when job N resolved, its final measured fraction (usually
  1.0) rode into job N+1's probe window, rendering the whole batch
  100% before visibly receding at the first measured tick. A resolving
  job now RETIRES its measured floor (guarded on the empty slot, so a
  delivered-twice terminal snapshot never resets a live job's truth).
- **Busy label missed the plan phase** (N-2, Low): the phase-keyed
  busy map lacked `plan` (run.py's real PHASE_PLAN — it fell back to
  the generic "estimating ...") and carried the dead `finalize` key no
  writer emits. The map is now structurally pinned to the emitted
  phase set, so a future phase addition fails the suite instead of
  silently falling back.
- **Test-file hygiene** (N-3, info): both byte-verified newline-less
  files end with a newline; the real-ffmpeg integration file carries
  the `slow` mark its name advertises; the marker is registered in
  pyproject (the 3 standing PytestUnknownMarkWarnings are dead; test
  selection is unchanged — the canonical invocation has no `-m`
  filter).
- **Registry cancel lied "not-found" across processes** (F-7): a
  second process's queued/running job is file-visible but invisible to
  a stale in-memory view. `cancel()` converges on the file's truth
  before reading (v0.4.15.3's converge machinery, one extra stat);
  `cancel_batch()` gets the identical converge inside its lock hold.
- **Missing ffprobe tracebacked the probe/plan CLI** (F-8):
  `probe_file` converts the subprocess layer's raw `FileNotFoundError`
  into its documented contract error — `ProbeError` naming the binary
  — so every consumer gets the honest named failure.
- **MCP serverInfo version stamp drifted, ungated** (F-9): the
  hand-stamped `"0.2.0"` literal is gone — `SERVER_INFO` derives from
  the package `__version__`, and the version-stamp suite pins the
  derivation.
- **Language detection decoded the whole file** (F-10): the detect
  pass consumes ~30 s of features but decoded entire 4 h sources
  (~1 GB of RAM). The new bounded-prefix decode (`DETECT_AUDIO_SECONDS`
  = 120 s, sample-exact against faster-whisper's resample chain) makes
  detection's memory a constant, asserted structurally.
- **Model-cache edges stranded debris / returned broken dirs** (F-11):
  a failed or interrupted fetch (including KeyboardInterrupt) always
  removes its `.part.tmp` staging file, and a `.complete` marker
  WITHOUT `model.bin` no longer short-circuits — resolution re-enters
  the size/sha-verified fetch path instead of returning a dir that
  fails engine construction with a confusing error.
- **Dead code swept** (F-12): seven grep-verified dead sites removed
  (`align_verify_cues`'s never-read `covering` list,
  `MAX_EXTRACT_SECONDS`, `STALE_AFTER_HEARTBEATS`, plan.py's
  `if False else`, run.py's no-op reason reassignment and dead
  `claimed_by` reassignment, subs_job.py's import-then-del pair) —
  zero functional change; pre-delete greps confirmed no test
  references any removed symbol.
- **Drop path skipped the file cap** (F-13a): the folder picker capped
  at 500 files; drag-and-drop accepted unbounded. The drop enforces
  the same cap with the same visible modal naming the limit.
- **Enqueue write failures could crash the GUI** (F-13b): a full or
  read-only state dir raises `OSError` from the `jobs.json` write —
  both enqueue paths (Start Upscale and Add Subtitles) now catch it
  and show the honest dialog ("could not record the batch: ...")
  instead of the exception escaping the Qt slot (the documented
  crash-abort class on frozen builds).
- **"Cancel Current" killed queued work** (F-13c): the old enable rule
  (active count > 0) counted QUEUED jobs, so a queued-only batch made
  the button clickable — and the click permanently converted the next
  queued job to cancelled, every word of the tooltip false. The
  window now tracks the actually-running job id; the button is enabled
  iff THIS window owns a running job and cancels only that job; the
  tooltip says exactly that (queued clearing remains Cancel Batch's
  documented surface, with its confirm dialog).
- **CLI had no headless transcription off switch** (F-14a): `run` and
  `plan` both accept `--no-transcribe`, threading through the
  absent-defaults rule (absent → config default; flag → explicit
  False) — plan renders the same truth a `run --no-transcribe`
  performs.
- **Plan hardcoded the transcription model** (F-14b): the literal
  `"medium"` silently defeated the config constant; plans now derive
  the model from `TRANSCRIPTION_MODEL_DEFAULT` (pinned by a
  derivation test, not a literal test).

### Security

- **Monitor token compare is constant-time** (F-15, partial):
  `hmac.compare_digest` on both token paths (Bearer header, query
  param) — behavior byte-identical. The DEFERRED halves and their
  revisit criteria: flipping the bind default (0.0.0.0 → localhost)
  would silently break the documented LAN-watch workflow on upgrade,
  and per-install token generation needs a pairing/UX design; revisit
  when repo visibility changes, a wide-audience packaged ship happens,
  any write-capable monitor endpoint is ever proposed, or Eric calls
  it. Rotation-by-release remains the documented interim.

### CI

- **The Windows build workflow derives its version** rather than
  retargeting stamps: all five version literals now derive from
  `$env:VIDLIFT_VERSION` (written from pyproject before every use) —
  the drift class that cost two releases (0.4.15.2's stale literals,
  0.4.15.3's retarget) is dead; a repo-side gate structurally forbids
  version literals in the workflow, so a bump touches exactly four
  files (pyproject, `__init__`, version_info, CHANGELOG).

## [0.4.15.3] - 2026-11-02

### Fixed

- **Mode B (Add Subtitles) bitmap-subs remux** (F-1, HIGH): the designed
  Mode B source class — files whose only subtitle tracks are bitmap
  (PGS/VobSub) — transcribed for minutes-to-hours and then died at the
  remux: the blanket `-map 0` routed the bitmap track (and mkv
  attachments) into the mp4, where `-c:s mov_text` forces a bitmap→text
  transcode ffmpeg categorically refuses. The remux now maps an explicit
  allowlist (`-map 0:v -map 0:a -map 1`): the output's subtitle track IS
  the transcription; bitmap tracks, attachments, and data streams stay
  in the untouched source. Under `--force` source text tracks are no
  longer duplicated into the output ("transcribe anyway" — the source
  keeps its original track).
- **`--replace-source` cross-filesystem data loss** (F-2): the copy onto
  the source truncated it at open, so a mid-copy failure (ENOSPC, NAS
  drop) could leave the source holding partial data. The copy is now
  staged at a temp sibling of the source, SHA-256-verified against the
  verified output, and atomically renamed into place — a failed copy
  strands only the temp; the source and the verified output always
  survive.
- **Registry lost update between processes** (F-3): `save()` wrote the
  in-memory view without converging, so a stale-view save (the stdio
  MCP server's progress mirror, another agent session) wholesale-reverted
  newer rows — terminal jobs resurrected to `running`, freshly enqueued
  jobs dropped (the "false active job" wedge at runtime). `save()`,
  `enqueue()`, and `transition()` now converge on the file's truth
  before writing, under one lock hold; a save can only ever land on top
  of converged truth.
- **Stage bar pinned at a stale 100% through encode/verify** (F-4):
  progression payloads merge (never replace), so the upscale's final
  frame counters rode every later phase's snapshot and the GUI rendered
  them — and the "measuring — counting frames…" busy label — regardless
  of phase. Frame counters are now honored only in the upscale phase;
  the busy pulse names the actual phase ("encoding …", "verifying …",
  …); the overall queue bar renders last-measured truth so it never
  visibly recedes at a phase change.
- **`Add Subtitles` output naming on non-4-char extensions** (F-5): the
  GUI's `path[:-4]` slice mangled `.ts`/`.divx`/`.mpeg` stems (movie.ts
  → movi.subs.mp4) and silently disabled the collision pre-flight for
  those files. Every surface now reuses the one naming derivation
  (`subs_output_path`).
- **Plan view promised transcription the run wouldn't do** (F-6): the
  subtitle note re-derived its selection under the locked default
  instead of the checkbox the plan was built with. The resolved
  transcription flag now threads into the note (CLI/MCP plan renders
  included), the GUI passes the checkbox at plan time, and the plan
  render mirrors a disabled checkbox for still-transcribed plans.
- **Manifest claimed CUDA transcriptions that never happened** (R1): on
  a CPU-resolved device whose engine construction needed the honest
  one-shot CPU retry, the fallback hook rewrote the manifest's device to
  `cpu (cuda unavailable)`. The device echo is now keyed on the actual
  resolution — only a real CUDA fallback earns that string.
- **Mode B results could not state where transcription ran** (D4): the
  transcription result, subs job result, and the subs executor's result
  dict now carry a `device` field (resolved by the default factory;
  `cpu` whenever a fallback fired; empty when a foreign engine factory
  ran — honest unknown, never a guess).

## [0.4.15.2] - 2026-11-02

### Fixed

- **Transcription: automatic CPU fallback when CUDA fails** (v0.4.15.2
  design): the device probe is an honest CLAIM ("the driver exposes a
  CUDA device"), but library rot, VRAM OOM at construction or on the
  first decode, and an explicit `--device cuda` pin on a cuda-less box
  slipped past it and detonated the transcription with a raw CTranslate2
  traceback after the expensive phases had already run. The engine now
  falls back to the documented CPU path (int8, ~1x realtime) EXACTLY
  ONCE in those cases — with a stderr `WARNING:` line carrying the
  exception, and the manifest's transcription block recording
  `cuda unavailable — fell back to cpu (<exception type>)` plus an
  honest `device: cpu (cuda unavailable)`. Explicit `--device cpu`
  pins still fail loudly on their own failure (nothing below CPU);
  explicit cuda pins fall back like auto (the job the user asked for
  is a subtitle, and the honest note names the pin's failure). No new
  device values: `auto` already meant "cuda when it works".

## [0.4.15.1] - 2026-11-02

### Fixed

- **'Add Subtitles…' false "batch running" block**: the guard blocked the
  picker whenever the window held batch ids even with nothing running
  (planned-but-not-started files read as an active batch). The planned-files
  state is not a batch — standalone subtitles are independent of the upscale
  batch slate, so the picker now opens any time; a dialog appears only when a
  live subtitles job over the SAME file would collide, and picked files append
  cleanly while an upscale batch is still running (the serial executor orders
  everything).
- **Startup sweep for stale job records**: a fresh app start could block every
  guarded click (Clear/Start/cancel) with "Wait for the current upscale to
  finish or cancel it first." while the GPU sat idle — the persistent registry
  carried 'queued'/'running' leftovers from a crashed session and the active-
  job count read them as live. The GUI's startup now sweeps stale queued AND
  running records to `failed` ("stale job from previous session (startup
  sweep)") before the first active-count read; the swept jobs stay visible as
  failed history rows, never deleted. The stdio MCP server keeps its restart
  semantics (queued jobs enqueued by an agent resume when it restarts).

## [0.4.15] - 2026-11-02

### Added

- **Denoise prep pass (opt-in, off by default)**: an hqdn3d light/strong
  filter appended LAST in the prepare chain (after scale, setsar,
  deinterlace, telecine — before FFv1). Choose `off`/`light`/`strong`
  in the GUI (3-state combo, QSettings-persisted), `--denoise
  {off,light,strong}` on `vidlift run` (refused on `subs`), or the
  MCP `run` tool's `denoise` option. OFF stays byte-identical to the
  v0.4.14 chain; the choice is recorded in the job state/manifest and
  folded into resume-claim identity, so a run started OFF never
  claims a denoised scratch (and vice versa) — v0.4.14 checkpoints
  read as OFF. No auto-detection: the pass only runs when chosen.
- **Single-word hold-cap**: a single-word cue held >2.5 s past its
  word is capped to word end + the standard 120 ms readability hold —
  but ONLY when the audio right after the word measures quiet (RMS
  < 0.01 peak-normalized on the job's scratch wav). The measured
  guard keeps real dramatic one-word holds untouched, and without
  audio access the cap never trims. Fixes Karen's 0.4.14 WARN
  (Firefly cue 16 "can't": 8.28 s stuck hold from a smeared word-end
  stamp); trim counts surface in the job notes.

### Fixed

- **Flaky merge gate**: `test_subs_job_executes_through_executor`
  failed ~1-of-4 runs — the executor's mirror-save throttle started
  at 0.0 monotonic, leaving the first run's mirror save unthrottled;
  the throttle now initializes at construction time.
- **Release-gate network use**: the v0.4.14 gate could hit the real
  Whisper model download in-test when the cache was cold (a ~37 s
  network fetch that also cannot complete in the systemd unit
  without a CA bundle). The test contract is now explicitly
  no-network: model seeds are guaranteed and an autouse guard fails
  any test that opens an external connection.

### Removed

- **`--burn` flag**: burn-in is permanently off the roadmap (Eric,
  2026-11-01). The flag was surfaced-but-not-implemented; it is now
  removed from the CLI, subcommands' help, MCP tool schemas, the GUI
  checkbox, and its dedicated test surface.

### Added

- **Transcription Subtitles (v0.4.14)**: VidLift now makes subtitles
  when a file has none, using faster-whisper (medium model default,
  sha256-pinned first-run download into `~/.vidlift/whisper/models/`,
  user pin override for large-v3 or a local model path).
- **Mode A — integrated fallback**: a file with no embedded text,
  bitmap, or sidecar subtitles has its own audio transcribed during
  the run's subtitles phase and the result is soft-muxed (mov_text,
  language-tagged) into the finished MP4. Sync is structural: the
  transcription extracts the source's `0:a:0`, the exact stream the
  encode maps untouched. On by default (Eric's locked decision); the
  GUI checkbox "Add subtitles by transcription when none exist" is
  the load-bearing kill switch and QSettings-persisted. Any
  transcription failure (missing model, failed download, no audio)
  fails OPEN: the upscale ships video+audio, a warning records it —
  an upscale never fails because subtitles could not be made.
- **Mode B — standalone `vidlift subs FILE...`**: transcribe +
  soft-mux into a NEW `{stem}.subs.mp4` beside the source at ANY
  resolution, never touching the source. B1 stream-copy remux is
  default (zero quality loss; `--burn` B2 is surfaced but returns a
  named not-implemented error this cycle). `--lang`, `--model`,
  `--device`, `--force` flags; non-zero exit on job failure.
- **MCP tool `transcribe`**: enqueue subs jobs (`paths[]`, optional
  `lang`/`model`/`device`/`force`); cancel/status/manifest/list cover
  subs jobs untouched (registry transitions are job-kind-blind);
  cancel-batch included.
- VAD prefilter + word-level timestamps + an alignment verification
  pass repair cues whose aligned midpoint drifts >250 ms; below-
  floor confidence cues are dropped (hallucination discipline);
  language auto-detect with config pin override; detected language +
  confidence + model + cue count land in the run manifest's
  `subtitles.transcription` block.
- The machine-wide GPU phase lock serializes transcription against
  any concurrent upscale (one VRAM user at a time); on filesystems
  that cannot host the lock it degrades OPEN with a warning.

## [0.4.13] - 2026-11-01

### Added

- **Cancel Batch (GUI)**: a new button immediately right of Cancel
  Current stops the running file AND cancels every queued job in the
  batch with one action — confirm before it acts ("Cancel the entire
  batch?"). Queued jobs land `cancelled` without ever starting (error:
  "batch cancelled by user"); the running job dies through the same
  cooperative terminate path as Cancel Current; the bar lands amber
  ("0/N succeeded, N cancelled") and every row shows its own truth.
  Enabled whenever this window owns a batch set — including the
  idle-queue moment between files. Closing the window keeps its
  previous semantics (unchanged by this release).
- Registry-side atomicity: the new `Registry.cancel_batch` marks/flags
  and persists under ONE lock hold, and claiming (`Registry.claim_next`,
  the executor's claim path) runs on the same lock — nothing from the
  batch can start after a Cancel Batch returns, even when a job finishes
  at the exact tick of the click.

## [0.4.12] - 2026-11-01

### Changed

- **Output filename: `<source>.720p.hevc.mp4` → `<source>.<target>.mp4`**
  (e.g. `Part Two - S01E02.720p.mp4`). The name now carries the real
  plan target instead of a hardcoded `720p.hevc` infix. NO retroactive
  renaming of past outputs — existing `.720p.hevc.mp4` files on disk
  stay as they are (policy: migration is a separate, explicit decision).

### Added

- **Plan-time output collision gate**: when the computed output name
  already exists on disk at plan time, the item plans NOT-ready with
  the warning `output exists: <name> (will not be overwritten)` —
  the encoder's `-y` must never erase a pre-existing file silently.
  Block-until-forced; no force flag ships this cycle (resolution is a
  user decision: move/remove the file, then re-plan).
- **Run-time collision guard**: a cheap `os.path.exists` check on the
  planned output immediately before encode fails THAT single job (not
  the batch) with a clear error if a file appeared between plan and
  run — closing the plan→run race the plan-time gate cannot see.

## [0.4.11] - 2026-10-29

### Fixed

- **GPU meter console-window flash on Windows (v0.4.10 regression)**:
  the top-bar GPU meter's nvidia-smi probe bypassed the v0.4.4
  console-suppression convention (vidlift/_proc.py: any new
  external-command call MUST route its subprocess kwargs through
  `popen_kwargs()`), so the probe's raw `subprocess.Popen` spawned a
  visible conhost window twice per second —Eric reported it live on
  NINJABASE2 minutes after upgrading. The probe now spreads
  `**_proc.popen_kwargs()` into its Popen call like every other
  external command (a pure no-op off Windows). Regression tests added
  (`tests/test_gpu_meter_no_window.py`): a cross-platform source
  contract that the `_probe` Popen call carries the popen_kwargs
  spread, plus Windows-only behavioral proofs of the
  STARTF_USESHOWWINDOW + CREATE_NO_WINDOW spawn suppression.

## [0.4.10] - 2026-10-28

GUI truth-and-instrumentation patch (Eric's direct request): a GPU
meter in the top bar, an honest batch progress bar, and the fresh-run
default made explicit.

### Added

- **GPU meter (top bar)**: a fixed-size (~180x54) Task-Manager-style
  widget — dark panel, faint grid, filled lavender area chart of the
  last ~90 samples, "GPU" label top-left, current utilization top-right.
  Samples NVIDIA utilization at ~2 Hz via
  `nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits`,
  off the UI thread (QThreadPool worker) with a hard 2 s child timeout
  (terminate on expiry). Multi-GPU: the MAX utilization across devices
  with a small `x2`/`x3` device-count hint. When nvidia-smi is absent,
  fails, or times out: renders "GPU n/a" and never crashes, never
  logs, never stalls — it is read-only instrumentation and cannot
  interfere with an active upscale (new module `vidlift/ui/gpu_meter.py`,
  zero new dependencies).

### Fixed

- **Batch progress bar truth (R2)**: terminal-but-not-succeeded jobs
  no longer lied. The bar previously counted ANY terminal state into
  `run_done`, so a cancelled 1/1 batch rendered a full green
  "1/1 completed" bar — contradicting the row table's yellow
  cancelled row. Now `summarize_batch(states) -> BatchSummary`
  (pure, in `vidlift/run.py`) does the counting — succeeded, failed,
  cancelled, pending — and the bar renders truthfully: "1/2
  succeeded, 1 cancelled" (amber chunk), "1/2 succeeded, 1 failed"
  (red chunk), "2/2 succeeded" (green chunk). Row-table colors are
  untouched.

### Changed

- **Fresh-run default (R3)**: made explicit — the GUI Start path
  rides `resume=self.resume_claim.isChecked()` into
  `run_file(resume=...)` (v0.4.9's explicit contract), and `resume`
  keeps its DEFAULT-OFF run_file default. A normal Start is a clean
  start: no retained scratch from a previous run of the same source
  is claimed unless the opt-in "Resume retained work" checkbox is on
  (a new unit test pins the default and the explicit ride-through).

## [0.4.9] - 2026-10-28

Resume of retained work plus anti-freeze hardening, per the binding
design `docs/designs/v0.4.9-resume-and-observability.md` (Bob,
approved by Eric 2026-10-28).

### Added

- **Resume (opt-in, all four surfaces)**: a failed, cancelled, or
  killed run leaves its retained scratch (prepared mezzanine + engine
  workdir + checkpoint) intact under the runs directory; a later run
  of the SAME file with resume enabled claims it after re-proving
  every identity (source path/size/mtime, plan incl. profile and OCR
  decision, prepared duration + decode count) — any mismatch means a
  clean prepare, never a wrong-file stitch. Surfaces: GUI "Resume
  retained work" checkbox (default OFF, persisted under QSettings
  `resume`), CLI `vidlift run FILE --resume`, Python API
  `run_file(..., resume=True)`, MCP `run` options `resume` (boolean;
  absent -> False = v0.4.8 behavior). The claim is atomic
  (`.claimed-by-<run>` sentinel); a verified claiming run removes the
  claimed scratch; a failing claiming run RETAINS it (chunks
  accumulate across attempts; the newest proving set wins). v0.4.7
  checkpoints are never silently trusted: without identity facts the
  set is GC-eligible but not claimable.
- **Scratch GC** (`vidlift runs prune [--dry-run]`, plus automatic
  GC at GUI/server startup): per source, keep the newest 2 sets with
  completed chunks; everything older past a 7-day floor, and ANY set
  with zero completed chunks (triage debris), is removed. Rejected
  mezzanines, logs, and manifests are never GC'd. The prune command
  reports every decision (`no_checkpoint`, `zero_completed_chunks`,
  `older_than_newest_2_per_source`).
- **Anti-freeze (design §2)**:
  - GPU phase lock: the upscale chunk loop holds a machine-wide
    `.gpu-phase.lock` (per state dir, shared GUI+MCP+CLI) with a
    holder pid; a crashed holder's lock is swept; waiters surface
    "waiting for GPU — held by pid N for Xs" instead of stalling
    silently; on filesystems refusing atomic locks VidLift degrades
    open with a logged WARNING (never a hang).
  - Progress mirror: the MCP job registry persists a
    `progress_snapshot` (progress/current_phase/last_activity) at
    most every 10 s, so the GUI (a separate process) sees live
    progress even when job threads run in another process.
  - Stale-activity warning (D4): a running MCP job with no activity
    for 900 s renders "running (no activity N min)" (yellow) in the
    GUI and carries a computed `activity_age_seconds` in the MCP
    status payload — observability ONLY, never an automatic action.
  - v0.4.8's monitor behavior is unchanged; v0.4.9 adds visibility.

### Fixed

- The claim layer reads the checkpoint's nested `source` identity
  block as the engine writes it.
- A bounded GPU-lock wait no longer overshoots its deadline by a
  full heartbeat.
- `vidlift runs prune` accepts both the FILE (source-dir) form and
  `--log-dir`, and exits with a clean error for missing directories.

## [0.4.8] - 2026-10-26

The anime upscale profile (`realesr-animevideov3-x2`) for DVD-era
cartoon rips — the seam deliberately deferred at v0.4.0, now landed
per the binding design `docs/designs/v0.4.8-anime-upscale-profile.md`
(Bob, approved by Eric 2026-10-26).

### Added

- **The "anime" upscale profile on all four surfaces** (all manual,
  all defaulting to "live-action"):
  - CLI: `vidlift run --upscale-profile {live-action,anime}`.
  - GUI: an "Upscale profile" combo on the control strip
    ("Live action (general x2plus)" / "Anime / cartoons
    (animevideov3-x2)"), persisted under QSettings key
    `upscale_profile` and disabled while a batch runs.
  - Python API: `run_file(..., profile="anime")` (additive keyword;
    the default behavior is unchanged).
  - MCP bridge: `run` options gained `upscale_profile` (string;
    absent → "live-action"; an unknown value is refused outright at
    the CLI/GUI seams, but at the Python-API and MCP seams it fails
    open — the job succeeds as an honest no-AI re-encode whose
    manifest/log name the profile, the same shape as an unknown
    `engine` value there).
  - No auto-detection, by design (D3): every anime run is the user's
    stated intent; VidLift never guesses content type.
- **The model, vendored and hash-pinned like x2plus** (D1):
  `packaging/models/realesr-animevideov3-x2.{param,bin}` (3,173 B /
  1,247,368 B — a 1.25 MB lane next to x2plus's 33 MB), pinned in
  `MODEL_ARTIFACTS` and verified at first use per profile. Native-2x
  behavior verified 2026-10-26 against the actual released engine
  binary (64x64 → exactly 128x128; 65x47 → exactly 130x94). The
  Windows installer workflow copies the pair, asserts both sha256
  digests at build time, and validates both files in the installed
  payload.
- **Honest expectation note for anime runs** (D6): when the anime
  profile is selected, the CLI run-report and the GUI detail pane
  append "Anime upscaling model: strong fit for flat fills and line
  art (common in cel animation); not for live-action content." after
  the plan's own expectation — plan expectations themselves are
  unchanged (the profile is a run knob, not a plan knob).
- **Validation surface (Karen's gate list, b13-b22):** registry + hash
  pinning + plumb-through tests for every surface, the GUI
  no-overlap geometry assertion for the new combo, the MCP schema
  static check, the workflow hash-assert static check, the
  v0.4.7 cleanup contract pinned under the anime profile, and a
  real-content gate fixture:
  `tests/fixtures/media/fixture-cartoon.vob` — a 4 s, 640x480 4:3
  MPEG-2 (q:v 6) VOB cut from Fleischer's *Superman: The Mechanical
  Monsters* (1941), public domain (US non-renewal; the Internet
  Archive copy carries a CC0 dedication) — see
  `tests/fixtures/media/LICENSE.md`. The checked-in 4 s fixture is
  under the frame clock's 10 s audio-extent sanity minimum, so it can
  never take a real AI pass: it deterministically exercises the
  fail-open path; the real-engine e2e used a 10.5 s derivation from
  the same source. Suite: 458 tests.

### Changed

- **GUI layout detail:** the remote-monitor checkbox's grid span
  narrowed from (2, 0, 1, 7) to (2, 0, 1, 5) to free the row-2 cells
  the profile combo occupies (columns 5-6); `cancel_button` stays
  untouched at (1, 7), pinned by the same no-overlap geometry test
  pattern as v0.4.7's fix.

### Fixed

- None.

Three Eric-ordered irritant fixes after the smooth v0.4.6 ship:
output naming, success cleanup, and opt-in replace-source.

### Fixed (validation r2, Karen's gate run)

- **CLI replace-source report is honest about the outcome** (gate
  defect D1): `_cmd_run` printed `source replaced:` whenever the
  flag was set and the paths differed, even when the replace had been
  refused — Karen's live probe (read-only source dir) caught the
  false claim. The line now prints only when the replace actually
  happened (`state.output_path == state.file_path`, which
  `run_file` sets only inside the successful replace branch); a
  refused replace gets an explicit `source NOT replaced: both files
  kept` notice instead, while the upscale still reports success.
  Pinned both ways: success prints the line
  (`test_cli_prints_success_line_only_on_actual_replace`), refusal
  prints no success line plus the honest notice
  (`test_cli_refused_replace_prints_no_false_success_line` — driven
  by a real refused run through `run_file`, not a mock).
- **GUI: the replace-source checkbox no longer collides with the
  Cancel button** (gate defect D2): the checkbox spanned grid
  columns 5-7 and clipped under `cancel_button` at (1, 7); it now
  spans columns 5-6 (`gui.py`). Pinned by an offscreen no-overlap
  geometry assertion (`test_v047_replace_checkbox_no_overlap_with_cancel`).
- **Success cleanup sweeps the engine mezzanine** (gate defect D3):
  `<run-id>.prepared.upscaled.mkv` — the intermediate both engines
  build beside the prepared file via
  `Path(prepared_path).with_suffix(".upscaled.mkv")` — was escaping
  the post-success sweep (a real-measured 1.2 MB leak for a 2 s
  clip; gigabytes for a movie-length FFV1 mezzanine). Added to
  `_SUCCESS_SCRATCH_EXTENSIONS` by exact run-id namespaced path,
  never globbed; the rejected-mezzanine retention path renames to
  `<run-id>.prepared.rejected-<ts>.mkv` before success cleanup can
  run, and fail-open encodes never create an upscaled sibling, so
  retained diagnostics cannot be eaten. Pinned:
  a successful run leaves NO mezzanine
  (`test_success_deletes_engine_mezzanine_karen_r2_d3`), a failed
  run retains it (`test_failed_run_retains_engine_mezzanine`).

### Changed

- **Output filename drops the `vidlift.` infix.** Planned outputs are
  now `<source-stem>.720p.hevc.mp4` (was `...vidlift.720p.hevc.mp4`),
  beside the source unless `--output-dir` is set. All pinned tests and
  the docs smoke command updated; README now states the naming
  explicitly (v0.4.7 naming decision: the extension chain already says
  "vidlift enough").

### Added

- **Success cleanup ("when a run is completed successfully we need to
  ensure the files used for the conversion are properly removed").**
  After `verify_output` passes, `run_file` removes the run's scratch
  under the log root: `<run-id>.prepared.mkv`,
  `<run-id>.engine-work/`, `<run-id>.ocr-work/` — by exact run-id
  namespace, never globbing, so a concurrent run under the same log
  root cannot lose its own artifacts (`run.py::cleanup_run_artifacts`,
  wired only on the verified-success path). Retention contracts are
  untouched: the run log and manifest are the durable record and stay;
  nothing is deleted on a failed or cancelled run (clause-e engine
  retention intact); caches are NOT temp — benchmark caches under
  `~/.vidlift` stay; sidecar `.srt` files and the `.ocr/` diagnosis
  kit live beside the OUTPUT and stay. Cleanup failures log a WARNING
  and keep the success — cleanup is best-effort, never a new failure
  mode.
- **Opt-in delete-and-replace of the source** ("I want an option to
  have it delete and replace the source file when the upscale is
  complete"). `replace_source` is strictly opt-in, default OFF:
  after a verified run, the source video is deleted and the verified
  output moves into the source's original path — media library
  replaces its file in place, same name, new pixels. Guardrails: the
  replace only runs AFTER `verify_output` passed; never when the
  output already IS the source path (no-op); same-filesystem moves
  are one atomic `os.replace`; cross-filesystem goes copy-first with
  a SHA-256 verify before the original output copy is removed (a
  hash mismatch keeps both originals — a verified copy never
  destroys the only good data); ANY replace failure (source vanished,
  permission, cross-fs copy error) logs `WARNING: replace-source
  failed; keeping both files` and leaves the verified run untouched —
  it can never crash the GUI or fail the job.
  Surfaces: `run_file(replace_source=...)`, CLI `--replace-source`
  (the run report gains a `source replaced:` line stating the file's
  new location), GUI checkbox "Replace original on success (deletes
  the source video)" with the honest irreversible-warning tooltip
  (persisted via QSettings key `replace_source`), and the MCP bridge
  `run` options object gains boolean `replace_source` (schema
  validated; absent reads as False).

## [0.4.6] - 2026-10-20

Read-only HTTP status API (binding design:
`docs/designs/v0.4.6-status-api.md`, D1 lane d). The GUI process
serves a small stdlib HTTP surface reading the SAME
registry/executor the GUI tick reads — one truth, two readers, no
mirror. Remote enqueue/cancel stays out of scope (minipc-dev-only
through the stdio MCP bridge).

### Added

- **`GET /health`** (token-free liveness: `ok`, `version`, `busy`),
  **`GET /status`** (token-gated: `api_version: 1` + the bridge's own
  `_status_payload` per active job — live `progress` dict with
  `step`/`frames_done`/`frames_total`/`chunk_*`/`phase_note`, honest-
  null `eta_seconds`, `log_tail`), **`GET /history`** (token-gated
  pass-through to the registry's history rows). Anything else 404;
  any non-GET verb 501 — read-only by construction (the handler
  defines only `do_GET`, and the server holds a three-method
  `StatusView` read facade, never the client's write surface).
  Served from `vidlift/monitor.py` (stdlib `ThreadingHTTPServer`,
  daemon thread); idempotent `start_server`/
  `start_server_with_fallback`/`stop_server` with the Windows port-
  walk fallback (WinError 10013, Hyper-V/WSL reservations) and a
  `lan_ip()` helper. Bind defaults 0.0.0.0:8765, **on by default**
  (sibling bluray-to-mp4 precedent); fresh baked
  `MONITOR_TOKEN_DEFAULT` (never the sibling's token); override via
  QSettings `monitor/*` keys (`vidlift/config.py`).
- **GUI wiring (~40 LOC):** "LAN monitoring" checkbox (persists via
  QSettings), header pill (`Monitor on: <lan-ip>:<port>` / `Monitor
  off`), daemon-thread start at window construction (startup failure
  non-fatal — pill names it), `stop_server()` in `closeEvent` and
  `aboutToQuit`. `VIDLIFT_MONITOR_AUTOSTART=0` skips the bind per
  launch (tests/headless).
- **README "Remote monitoring" section** with the agent curl recipe
  and the token-retrieval path.

### Fixed

- **The live status payload never carried the runner's `progress`
  dict** (`vidlift/mcp_bridge/tools.py::_status_payload`). v0.4.5's
  GUI consumed `snapshot["progress"]` (stage labels, counters,
  `phase_note`) but the bridge payload omitted the key — GUI-only
  tests masked the gap; the API made the omission observable. The
  runner's own `state.progress` now rides the payload verbatim
  (additive key, schema-v2 convention). No other bridge change.

### D1 resolution

> **D1 resolved by lane (d).** With the read-only status API shipped,
> cross-machine *visibility* — the entire reason options (a)
> (Windows-side MCP server) and (b) (one-way registry sync) were on
> the table — is served by one curl against the GUI host. (a) and (b)
> retire to "unnecessary unless remote enqueue is ever needed."

## [0.4.5] - 2026-10-20

Progress honesty (binding design:
`docs/designs/v0.4.5-progress-honesty.md`). Additive patch over
v0.4.4; nothing touches a live run — everything activates at next GUI
launch. No protocol or CLI behavior change.

### Fixed

- **Log tail renders the TOP of its buffer** (D-tail). The run log
  widget now pins to the tail on every tick, a user scroll-up keeps
  their position (inspect-inspecting mode), scrolling back to the
  bottom (within an 8 px tolerance) re-engages the pin, a job-set
  change force-pins once, and identical tail text never touches the
  document (no scrollbar jitter on the 1 Hz tick).
- **SD prepared-count probe emitted no progress signal for its whole
  duration** (D-count). The decode-count pass now writes an honest
  heartbeat log line every 30 s: `prepared-count-probe: heartbeat
  t=<n>s (decode-count pass; no per-frame signal until ffprobe
  summary)` — phase named, elapsed named, no fabricated percent. The
  engine's step touch (`last_activity_at`) is unchanged.
- **"estimating ..." gave no motion cue on long stages** (D-eta). The
  count-pass busy label now reads `measuring — counting frames...`
  (naming what is being measured); the stage label renders
  `upscaling — counting frames (this can take 10-40 min on SD sources;
  no per-frame ETA exists yet)` during the count pass and
  `upscaling chunk 3/12` once chunks flow; encode/verify surface
  their heartbeat seconds; the ETA appears the moment the first
  chunk's counters land. `_eta_seconds` math unchanged.
- **Registry staleness across long-lived MCP sessions** (D2). The
  registry stat-checks `jobs.json`'s mtime on every read and reloads
  (merge-by-job-id, in-memory `_live` boxes and cancel flags
  preserved) when another process wrote it — a server started before
  an enqueue now sees the job without a restart. Soft-fail on corrupt
  files at reload time (startup `load()` stays hard).

### Docs

- `docs/development.md`: the Observability Heartbeat Rule — no phase
  may run more than 30 s without an observable signal (log line,
  step-label change, or progress update).
- `docs/architecture.md`: the registry-lane "State location" note —
  enqueue and observe on the same host; the cross-machine union
  (D1) is a v0.5.0 decision.

## [0.4.4] - 2026-10-20

GUI/status/MCP unification (binding design:
`docs/designs/v0.4.4-gui-status-mcp-unified.md`). The desktop app
becomes a view over the MCP bridge's persistent job registry: a
server started after the run sees GUI-started jobs via
`list`/`status`/`manifest`; live cross-process `cancel` is v0.4.5.

### Added

- **Unified job path for GUI runs.** Start enqueues one tracked
  JobRecord per ready file through the bridge's registry/JobExecutor
  (`vidlift.mcp_bridge.client_local`); the serialized single-worker
  rule is unchanged, and a server started after the run sees GUI
  jobs exactly like its own. The old in-thread `UpscaleWorker` is
  retired.
- **Engine selector in the GUI** with the exact three in-tree choices
  (`auto` / `ncnn` / `stub`), persisted to QSettings; a stale saved
  value reads as `auto`. The choice rides the job's options into the
  existing honest `select_engine` machinery — no GUI-side resolution.
- **Run panel with proven progress behavior** (port of
  bluray-to-mp4's UI discipline): stage labels naming what is running
  (probe/prepare/upscaling — frames X/Y/encode/verify/subtitles), the
  `EtaProgressBar` busy/ETA contract (duplicate progress lines never
  blank the ETA), animated activity dots for legitimate 0% windows, a
  1 Hz elapsed/forecast timer with a single writer per time label, a
  read-only runner-log tail, and an overall-queue bar.
- **Honest batch forecast.** "Total remaining" comes only from the
  live measured ETA plus benchmark-cache constants for files ahead
  (stub-sourced constants are policy-forbidden in the headline); no
  measured constant means "…", never a guess.
- **Windows child-process console suppression**
  (`vidlift/_proc.py::popen_kwargs`): ffmpeg/ffprobe/realesrgan/
  seconv/tesseract children spawned by the packaged GUI no longer
  flash console windows (`CREATE_NO_WINDOW` + hidden `STARTUPINFO`;
  an empty dict on other platforms).
- **Header version pill** beside the title (a second persistent
  version surface) and the muted "1. Add files → 2. Plan → 3. Start"
  helper line; toolbar verbs read left-to-right in that order.

### Changed

- GUI Cancel and MCP `cancel` land on the same claim-time registry
  CancelFlag; closing the window cancels through the same path and
  waits up to 30 s before the documented last-resort terminate.
- `vidlift mcp-server` and the GUI construct their registry/executor
  through one factory (`mcp_bridge.server.build_services`), and both
  run the same restart sweep (a window closed mid-run surfaces as
  `failed` with the restart error on next launch).
- Packaged GUI builds pin their state dir to the per-platform
  app-data location so the GUI and agents share one registry by
  default (`run_gui.py`; `QSettings` organization/app named before
  the window constructs).

## [0.4.3] - 2026-10-20

### Added

- Visible version badge in the desktop interface.
- Explicit **Start Upscale** and safe **Cancel Current** controls for serial GUI batches.
- Exact output destination column and detailed output-content preview.
- User control for enabling or disabling bitmap subtitle OCR while preserving text-subtitle handling.
- Batch progress showing the current file and completed count.

### Fixed

- The frozen Windows planner now uses its bundled ffprobe instead of launching a bare PATH command.
- Planning and setup failures stay inside the GUI as actionable messages instead of terminating the window.

## [0.4.2] - 2026-10-20

### Added

- Windows 11 x64 installer built and validated on GitHub Actions.
- Installed GUI and CLI executables with bundled FFmpeg/ffprobe,
  Real-ESRGAN ncnn/Vulkan plus the verified x2plus model, and unattended
  subtitle OCR tools.
- Frozen-app tool discovery for all bundled runtime dependencies.
- Installer SHA-256 checksum published beside the installer.

### Verification

- Full Python suite runs on Windows before packaging.
- CI silently installs the exact generated setup executable into a clean
  directory, verifies every bundled payload, starts the installed GUI,
  and smoke-tests the installed CLI and ffprobe.

### Known limitation

- The installer is not Authenticode-signed, so Windows SmartScreen may
  display an Unknown publisher warning.

## [0.4.1] - 2026-10-20

Subtitle support end to end: probe → selection (pass-through or OCR) →
mov_text embed + `.srt` sidecar. The port of bluray-to-mp4's
field-proven subtitle machinery (v0.2.16 mov_text embed, v0.2.27
unattended seconv+Tesseract OCR, v0.2.29 stale-file/truncated-write
hardening, v0.2.30 quality reporting + `.ocr` diagnosis kit) into
VidLift's seams. Binding design: `docs/designs/v0.4.1-subtitles.md`.
Honest provenance: the machinery shape and its failure history
(GUI-wizard babysitting, stale-SRT poisoning, truncated pass-2
writes, silent poor-quality OCR) are bluray-to-mp4's field lessons
(read-only reference at bluray-to-mp4 e62a80e); VidLift v0.4.1 ships
the PORT re-derived against VidLift's runner-seam architecture.
The Linux OCR lane is new orchestration, validated on this box
against real ffmpeg 8.0.1 + tesseract 5.3.4 with a real PGS sample
(per-packet seeking and default vsync were measured, found wrong,
and rejected — see the dev-box lane below).

### Added

- **Subtitle probe + selection (B.1/B.2/B.7).** The probe carries
  per-stream `index`, `kind` (text/image), and language for every
  subtitle stream. `select_subtitle_track` applies the
  Eric-approved selection philosophy: English-preferred, text beats
  bitmap, first-index tie-break, single track ever. Disk sidecars
  are discovered (same-stem `.srt`; `.sub`+`.idx` VobSub pair) and
  adopted only as the candidate of last resort — embedded truth
  wins, and an unadopted sidecar is named in the plan note so the
  disk file never surprises anyone.
- **Text-subtitle pass-through (B.3).** A selected text track is
  muxed into the shipped MP4 as soft `mov_text` through the temp-
  then-replace atomic mux; the `eng`/`und` language-tag honesty rule
  never stamps `eng` on an unconfirmed stream.
- **Bitmap OCR, both lanes (B.4).** PGS (`hdmv_pgs_subtitle`) and
  DVD VobSub (`dvd_subtitle`) extract losslessly (`.sup` raw;
  VobSub to single-track MKV) and OCR through the engine lane:
  Windows ships the ported seconv two-pass flow (raw Tesseract pass,
  FixCommonOcrErrors pass with timing-paired counting, pinned
  output names, stale purges before each pass, truncated-write raw
  fallback); the Linux lane is a showinfo timeline probe + a
  single-pass `image2` render (`-fps_mode passthrough`, one PNG per
  decoded frame in container time order) + per-PNG Tesseract
  (`--psm 6`) + cue assembly from the container's own frame
  timestamps. Both lanes share everything downstream of "SRT text
  in memory": normalize → `<stem>.srt` sidecar + mov_text embed.
- **MicroDVD re-timing (B.6.1).** MicroDVD's frame-numbered cues
  convert to wall-clock through the v0.4.0 DERIVED stamp token —
  the only re-timing edge in this milestone; bitmap pairs need none
  (their wall clock comes from the container).
- **Fail-open everywhere (B.8).** No usable OCR tool or an OCR
  hard failure never blocks the run: the video+audio ship, the
  manifest records `failed-open` with the named tool and fix, and
  the `.ocr/` diagnosis kit (extracted track + raw pass-1 SRT,
  size-capped 150 MB) is written on success AND failure for
  offline re-tuning without re-ripping.
- **Quality reporting (B.9).** The honest v0.2.30 line ("N% of M
cues clean") plus the low-cue-count and Tesseract-accuracy
  spot-check advisories, reporting only, never gating.
- **Manifest + status (B.7).** A `subtitles` block (status,
  engine, language, cue count, warnings, sidecar path) in the run
  state and manifest; the phase sits between verify and done; the
  verifier records expected-vs-actual subtitle presence as a
  non-gating warning.

### Changed

- `vidlift run` output for a file with a selected subtitle now
  writes `<stem>.srt` next to the MP4 (KEEP contract) and a soft
  mov_text track inside it (disposition contract).
- The tesseract lane's frame timeline regex tolerates ffmpeg's
  space-padded `pts:` fields (found while validating against real
  ffmpeg 8.0.1 output: sub-7-digit pts values are right-padded and
  were silently dropped by the first regex).

### Verification

- New boundary series S1-S19 + F1 (tests/test_subtitles_v041.py,
  87 tests), all seam-stubbed, pinning: selection rules, sidecar
  adoption, argv shapes of both lanes (showinfo null-sink probe,
  single-pass image2 with `-fps_mode passthrough`, per-PNG
  `--psm 6`), cue assembly semantics (blank=closes, same-text=
  continues, 3 s tail hold, 500 ms floor), render-count
  cross-check, zero-cue fail-open with kit retention, mux
  atomicity, MicroDVD token conversion, manifest/status shapes,
  and the run-level fail-open contract.
- Full suite: 315 passed, 0 failed (previously 305 passed with 6
  stale-lane failures — the S13/zero-cue tests still asserted the
  rejected per-packet/overlay approach and were rewritten to the
  validated showinfo/image2 design).
- Known gaps (unchanged, by design): the seconv lane is exercised
  through seam stubs only (Windows-only binary); real-media PGS +
  VobSub end-to-end ran on this box at implementation time but is
  not a CI fixture.

## [0.4.0] - 2026-10-19

Decode-truth clock, frame-exact chunking, gate/heartbeat/resume: the
port of bluray-to-mp4's v0.2.33 (frame-exact chunking) and v0.2.34
(derived stamp rate) field lessons into VidLift's file-based
architecture. Binding design: `docs/designs/v0.4.0-decode-truth-chunking.md`.
Honest provenance: the field evidence (the .33/.34 lessons, the Mamma
Mia drift shape, the under-reported-audio snap band) is bluray-to-mp4's,
from its own field runs, recorded in its design docs —
`bluray-to-mp4/docs/designs/v0.2.33-frame-exact-chunking.md` and
`bluray-to-mp4/docs/designs/v0.2.34-derived-stamp-rate.md` (read-only
reference at bluray-to-mp4 e62a80e). VidLift v0.4.0 ships the PORT:
the concepts were re-derived against VidLift's seams (prepared-file
numerator, shipped-audio `a:0` denominator), not copied code.

### Added

- **Decode-truth stamp clock (B.1/B.2).** The mezzanine's stamp rate
  is now DERIVED: the prepared file's decode-counted frame total
  (`ffprobe -count_frames`, authoritative; metadata `nb_frames` only as
  a flagged fallback) over the shipped audio's measured extent (the
  source's `a:0` stream duration, matching the encode's `-map 1:a:0`),
  snapped to a canonical rate set (24000/1001, 24, 25, 30000/1001, 50,
  60000/1001) at 0.1% or refused pre-GPU. Container `r/avg_frame_rate`
  fields are demoted to diagnostic-only — v0.3.0's "frame-clock
  discipline" (never default the metadata) is superseded: metadata
  hygiene cannot catch a consistently lying clock family.
- **Cross-provenance snap-band defense (B.2, the Karen finding).**
  When the source carries both an audio-stream extent and a format
  duration, both derivations must snap to the same canonical token;
  every derivation is additionally cross-checked against the prepared
  file's own measured duration. An under-reported audio extent (the
  few-tenths-percent lie that lands in the 23.976-vs-24 band) moves
  one provenance's snap and not the other's — the disagreement is the
  signature, and the refusal names both provenances and both snaps.
- **Frame-index chunking (B.3).** Chunks address the prepared file by
  FRAME INDEX (`select='between(n,a,b)'` + `-vsync 0`), never by
  `-ss/-t` wall-time windows (timestamp addressing does not land on
  frame boundaries; per-window jitter accumulates). Every chunk asserts
  its EXACT frame count in and out; a chunk that silently drops or
  duplicates a frame can no longer assemble a partial mezzanine.
- **Two reassembly gates (B.4).** The mezzanine must decode-count to
  EXACTLY the prepared total (integer equality), and its duration must
  match the shipped-audio extent at max(2.0 s, 0.1%). The old gate M
  (mezzanine-vs-prepared format duration) is demoted to a diagnostic
  record — it cannot see a consistent lie; gate P keeps its pre-GPU
  refusal and its record now labels its reference `format_duration`.
- **Rejected-mezzanine retention (B.5).** A gate trip after reassembly
  RENAMES the mezzanine to `.rejected-<ts>.mkv` and writes a
  diagnostic JSON (both derivations, snap results, chunk plan, gate
  operands) — never deletes. The fail-open reason carries both paths.
- **Heartbeat + step labels (B.6).** Long phases (decode-count probes,
  concat, encode, full-decode verify) no longer read as a frozen
  `last_activity_at`: the engine fires step labels through a new
  `on_step` seam (concat, gate probes), the runner pumps a heartbeat
  (touch every 5 s, throttled log lines every 30 s) around opted-in
  long commands, and `progress.step` names what is running. The MCP
  bridge needed no protocol change — the fields were already in
  `status`; they are now true during the previously-blind windows.
- **Per-chunk resume (B.7).** A `chunk_state.json` checkpoint in the
  engine workdir records completed chunks (fsynced per chunk). A
  retried run proves params + decode-counted total + stamp token
  against its own fresh derivation and re-uses the completed GPU
  chunks; any mismatch, missing file, wrong size, or truncated
  segment deletes the workdir wholesale. No partial trust.
- **Manifest schema v2 additive fields (B.8):** `frame_gates`, `resume`,
  `rejected_mezzanine`, the `mezzanine_vs_audio` duration-gate record,
  and the frame-clock provenance dict (derived raw rate, audio
  provenance, cross-provenance results, diagnostic container fields).

### Changed

- Progress totals are the decode-counted frame total, never the
  ceil(duration × container fps) estimate.
- `EngineAdapter.run` gained an optional `on_step` kwarg (additive);
  `EngineResult.frame_clock` is now the derivation provenance dict.

### Verification

- New boundary series E1-E15 (engine adapter) and R1-R4 (runner and
  bridge), all seam-stubbed, with the field-proven fixtures: 163,509
  frames / 6,818.4 s → 24000/1001; 5,453.0 s → 30000/1001 control; the
  under-reported-audio band fixture → cross-provenance refuse.
- Known gaps unchanged (gated on real hardware): GPU benchmark
  constants, visual frame spot-check vs source, Windows packaged-path
  discovery. FFV1 decode-count wall-time (minutes on a feature-length
  prepared file) is accepted by design and to be measured on the
  first real run; the hybrid-seek extraction optimization remains
  explicitly deferred (out of this milestone per the design doc).

## [0.3.0] - 2026-10-08

Major milestone: real-engine adapter, measured benchmark, and the full MCP bridge.

### Added

- **Real AI upscale engine (ncnn/Vulkan, RealESRGAN x2plus).** The engine
  adapter (`vidlift/engine_ncnn.py`) runs the verified x2plus ncnn artifacts
  (sha256-pinned at load time, refusal before extraction on mismatch) with
  the bluray-to-mp4 validation lessons ported clause-by-clause: frame clock
  from the probed `avg_frame_rate` (never a default), duration sanity gates
  at max(0.5 s, 0.01%) that fail open to a synchronized original-resolution
  encode, extent-derived explicit tiling (`-t 0` is forbidden and tested),
  and native scale 2 only. The pinned regression includes the measured
  15.050 s Mamma Mia drift shape, which now rejects at the mezzanine gate.
- **Measured benchmark + honest ETAs.** `vidlift benchmark` measures a real
  slice through the engine on the current machine; plan ETAs are derived
  only from measured data and carry their provenance. No measurement, no
  wall-clock ETA.
- **Sample-based interlace classification.** Prepare arms choose between
  progressive, hard 3:2 telecine, and genuine interlace using decoded
  samples instead of container flags alone.
- **Full MCP bridge (stdio).** `vidlift mcp-server` exposes the whole runner
  state contract to AI agents: probe, plan, benchmark, run, status, cancel,
  list, manifest. Crash-safe JSON job registry with atomic writes and a
  restart sweep that marks orphaned running jobs as failed — never silently
  succeeded. Honesty fields (`engine_simulated`, fail-open reasons) flow
  through byte-verbatim. Local machine only; no network listener.
- **Manifest schema v2** (additive): engine identity, model hashes, frame
  clock, gate results, full-decode verification, benchmark provenance.

### Fixed

- Runner now passes its progress callback to the engine adapter without a
  TypeError (caught by the first real end-to-end smoke run; regression
  test added).

### Verification

- Test suite grew from 72 to 200 passed (1 pre-existing PyQt6 GUI skip on
  this headless box). Boundary series b0-b12 (engine adapter) and t1-t9
  (MCP bridge) all enforced and passing. Real x2plus pass verified
  end-to-end through software Vulkan: 160x90 in, exactly 320x180 out of
  the network, final 720p HEVC output verified.
- Known gaps gated on real hardware (3090 Ti): GPU benchmark constants,
  visual frame spot-check vs source, Windows packaged-path discovery.

## [0.2.0] - 2026-10-03

- Probe/classify core, batch pre-flight planner, PyQt planning workbench,
  single-file CLI runner with stub engine and full observability contract.

## [0.1.0] - 2026-10-02

- Initial skeleton: ffprobe classification core, CLI stub, design docs.
