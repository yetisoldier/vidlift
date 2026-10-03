# VidLift Roadmap

## Done

- Probe/classify core with canned ffprobe tests.
- CLI probe command.
- Batch pre-flight planner.
- CLI plan command.
- PyQt planning workbench.
- Documentation suite for usage, CLI, UI, development, architecture, and design.

## Next

1. Wire the planner into the validated bluray-to-mp4 upscale/encode path.
2. Add a tiny sample benchmark step so the plan can estimate wall-clock time on the current machine.
3. Add start/cancel controls and per-file progress to the UI.
4. Validate against 2-3 real collection samples: XviD/DivX AVI, DVD MPEG-2 rip, and an odd MOV/MP4/WMV container.
5. Package the Windows app after the runner path is proven.

## Deferred

- Watch folders
- Metadata lookup
- Library management
- Image subtitle OCR
- PAL speedup correction
- 1080p output targets
