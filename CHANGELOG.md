# Changelog

All notable changes to VidLift. Dates are America/Chicago.

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
