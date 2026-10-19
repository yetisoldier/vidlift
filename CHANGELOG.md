# Changelog

All notable changes to VidLift. Dates are America/Chicago.

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
