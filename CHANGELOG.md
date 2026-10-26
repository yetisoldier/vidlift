# Changelog

All notable changes to VidLift. Dates are America/Chicago.

## [0.4.7] - 2026-10-26

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
