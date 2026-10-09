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

   **Real-engine acceptance criteria (ported from bluray-to-mp4,
   binding).** Every clause gates the milestone; none is advisory.

   a. Frame-clock discipline. Whatever reassembles or re-stamps
      frames after the engine uses the probed clock (avg_frame_rate
      with r_frame_rate fallback), never a default. A duration
      sanity gate compares mezzanine vs source; deviation beyond
      max(0.5 s, 0.01%) raises and fails open to a synchronized
      original-resolution encode. Regression tests pin 23.976 /
      25 / 29.97 inputs and the measured 15.05 s Mamma Mia drift
      shape.
   b. Explicit tiling, never auto. Tile size derives from input
      extent (704 for the 640x360 class); a test asserts `-t 0`
      can never appear in an engine command line. Model scale flag
      matches the network's native scale; `-s 2` on an x4 network
      is corruption, not downsampling.
   c. Measured ETAs only. Plan time estimates come from a real
      benchmark chunk on the current machine (item 3), never from
      theory. No ETA rather than a projected one.
   d. Fail open, never lie. A failed upscale stage may close a job,
      but the manifest and final report state exactly what shipped
      and why (engine used, engine_simulated, fail-open reason).
      Ported from the OCR babysit lesson.
   e. Diagnose from source frames first. Engine output defects are
      triaged against the source pictures before the engine is
      blamed (the v0.2.26 lesson: the defect was in the disc).
   f. Boundary tests that actually run. GUI/PyQt tests must be
      proven executed in the test env (no silent module skips);
      every user-visible promise (output filename, final geometry,
      sync, manifest claims) has one test at the highest boundary
      it crosses. Ported from the v0.2.32 filename-handoff escape.
   g. Model provenance. The adapter consumes the verified x2plus
      ncnn artifacts from the bluray-to-mp4 repo
      (packaging/tools/models/) rather than re-deriving them;
      hashes checked at bundle time.
2. **Full MCP bridge.** Local stdio MCP server exposing the whole
   runner state contract (probe, plan, benchmark, run, status,
   cancel, list, manifest) to AI agents, 1:1 onto the same pipeline
   code the CLI/GUI use. Draft sketch: docs/designs/mcp-bridge.md.
   Registered as `mcp.servers.vidlift` only after validation.
3. **Runner observability hardening.** The state contract exists from
   day one (phase, log tail and path, last-activity timestamp,
   command lines, outputs, timings, verification, manifest, cancel).
   Still to add where the platform allows: child process and GPU
   activity reporting, and retry. Cancel exists at the engine seam.
4. Add a tiny sample benchmark step so the plan can estimate
   wall-clock time on the current machine.
5. Build the batch queue on top of the proven single-file runner,
   then wire UI start/cancel and per-file status to the same runner
   state.
6. Validate against 2-3 real collection samples: XviD/DivX AVI,
   DVD MPEG-2 rip, and an odd MOV/MP4/WMV container.
7. Package the Windows app after the runner path is proven.

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
