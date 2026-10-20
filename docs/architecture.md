# VidLift Architecture

VidLift keeps product concerns separated from the shared upscale engine.

## Layers

1. `vidlift.probe`: ffprobe wrapper, stream parsing, source classification, and SD candidacy.
2. `vidlift.plan`: deterministic batch planning from classified reports.
3. `vidlift.engine_ncnn`: real Real-ESRGAN ncnn/Vulkan adapter,
   frame-exact chunking, measured frame clock, resume state, and duration gates.
4. `vidlift.run`: GUI-free single-file pipeline (probe -> plan -> prepare ->
   upscale -> encode -> verify -> subtitles) with phase, log tail/path,
   last-activity timestamp, command lines, outputs, timings, verification,
   cancellation, and a per-run manifest.
5. `vidlift.mcp_bridge`: persistent FIFO job registry and bulk queue over the
   same runner.
6. `vidlift.cli`: headless interface for probe, plan, benchmark, run, and MCP.
7. `vidlift.gui`: PyQt planning workbench that consumes the same plan objects.

The runner is CLI-first and headless. The MCP bridge adds batching without
duplicating pipeline logic. Runner state exposes phase, log paths,
last-activity time, cancellation, and artifacts so health is judged by
activity rather than an invented progress percentage. External tools are
resolved explicitly with named setup errors; packaged builds discover the
bundled FFmpeg, Real-ESRGAN, and OCR tools beside the frozen executables.

## Core Rules

- Probe and plan modules have no GUI imports.
- The GUI never owns business logic that the CLI cannot run.
- VidLift accepts files, not discs or titles.
- Output naming is derived from the input filename.
- The tool reports source limits honestly instead of promising invented detail.

## Engine Boundary

VidLift keeps its file-first Real-ESRGAN adapter behind the
`EngineAdapter` contract. The product accepts media files, not discs or
titles. Disc workflow concerns such as drive handling, title selection, and
rip queues remain outside this project.
