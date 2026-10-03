# VidLift Roadmap

## Done

- Probe/classify core with canned ffprobe tests.
- CLI probe command.
- Batch pre-flight planner.
- CLI plan command.
- PyQt planning workbench.
- Documentation suite for usage, CLI, UI, development, architecture, and design.
- Single-file CLI runner: `vidlift run FILE` drives one file through
  probe, plan, prepare, upscale, encode, verify with the full
  observability contract (phase, log tail + log file path,
  last-activity timestamp, command lines, output paths, phase
  timings, verification result, per-run artifact manifest), explicit
  missing-tool errors, and output verification (exists, decodes,
  geometry matches plan). The upscale phase uses a stub engine
  through the `EngineAdapter` seam because the bluray-to-mp4
  file-first entry point does not exist yet; manifests record
  `engine_simulated: true`. Real-engine integration is the next
  milestone's work, not batch/UI.

## Next

VidLift stays file-first: SD files in, 720p HEVC MP4 out. The runner
must not inherit disc/rip concepts from the shared bluray-to-mp4
engine; anything disc-shaped stays behind the engine seam. The
runner pipeline, observability, and verification are now proven
headless with a stub engine; the remaining gap before batch/UI work
is the real engine.

1. **Real engine adapter.** Implement `EngineAdapter` against the
   bluray-to-mp4 file-first seam once that sibling ships it (its
   validated path is chunk-directory/disc-shaped today). Includes
   the deferred sample-based classification (progressive / hard 3:2
   telecine / genuine interlace) and the full decode pass during
   verification. Until then, the runner's stub makes honest
   re-encodes and never claims an AI pass it did not do.
2. **Runner observability hardening.** The state contract exists from
   day one (phase, log tail and path, last-activity timestamp,
   command lines, outputs, timings, verification, manifest, cancel).
   Still to add where the platform allows: child process and GPU
   activity reporting, and retry. Cancel exists at the engine seam.
3. Add a tiny sample benchmark step so the plan can estimate
   wall-clock time on the current machine.
4. Build the batch queue on top of the proven single-file runner,
   then wire UI start/cancel and per-file status to the same runner
   state.
5. Validate against 2-3 real collection samples: XviD/DivX AVI,
   DVD MPEG-2 rip, and an odd MOV/MP4/WMV container.
6. Package the Windows app after the runner path is proven.

## Packaging Discipline

Before the Windows app is packaged:

- Discover external tools (ffmpeg, ffprobe, Real-ESRGAN) with
  explicit not-found errors; never fall back to a silent PATH guess
  mid-run.
- Honor bundled ffmpeg and Real-ESRGAN paths in the packaged app.
- Smoke-test the full single-file runner path on Windows, not just
  the planner.
- Verify output as part of each run, not as a packaging
  afterthought.

## Deferred

- Watch folders
- Metadata lookup
- Library management
- Image subtitle OCR
- PAL speedup correction
- 1080p output targets
