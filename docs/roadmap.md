# VidLift Roadmap

## Done

- Probe/classify core with canned ffprobe tests.
- CLI probe command.
- Batch pre-flight planner.
- CLI plan command.
- PyQt planning workbench.
- Documentation suite for usage, CLI, UI, development, architecture, and design.

## Next

VidLift stays file-first: SD files in, 720p HEVC MP4 out. The runner
must not inherit disc/rip concepts from the shared bluray-to-mp4
engine; anything disc-shaped stays behind the engine seam. The
current gap is the first real execution milestone, and it is
GUI-free: prove one file end-to-end from the CLI before the UI
attaches to anything.

1. **Single-file CLI runner.** `vidlift run FILE` drives one file
   end-to-end through the validated bluray-to-mp4 upscale/encode
   seam: probe, plan, prepare, upscale, encode, verify. No GUI
   dependency and no batch logic in this milestone; the batch queue
   and the UI become clients of the runner afterward.
2. **Runner observability.** Carried from bluray-to-mp4/Ripper
   experience: a progress percent alone is insufficient for
   hours-long jobs. From day one the runner exposes:
   - the current phase (probe, plan, prepare, sample, upscale,
     encode, verify);
   - a live log tail and the run's log file path;
   - a last-activity timestamp, so health is judged by activity,
     not by percent alone;
   - child process and GPU activity where the platform allows;
   - cancel and retry;
   - output verification after encode (output exists, decodes, and
     matches the planned geometry);
   - an artifact manifest per run (inputs, plan, command lines,
     outputs, timings).
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
