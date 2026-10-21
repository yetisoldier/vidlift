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

## State Location (registry lanes)

The persisted job registry (`jobs.json`) lives in ONE state dir per
host, and the two deployment lanes resolve it differently: the minipc
dev lane pins `VIDLIFT_STATE_DIR=/home/yetisoldier/.vidlift` (the
`vidlift-mcp` wrapper), while the packaged Windows GUI resolves
`default_state_dir()` to `%LOCALAPPDATA%\YetiWurks\VidLift`. These are
two physically separate registries — each is the source of truth for
the jobs its own host enqueued, and there is no cross-machine sync
(lane decision deferred to v0.5.0; the honest rule meanwhile is
**enqueue and observe on the same host**): enqueue jobs through the
GUI/CLI/MCP server running on the machine whose registry you intend to
write, and query status from the same lane. An MCP-side `enqueue`
against a DIFFERENT machine's registry will not appear in that
machine's GUI and vice versa; a long-lived MCP server converges on its
OWN state dir's file via the registry's mtime reload (v0.4.5), which
works within one host, not across the two.

## Engine Boundary

VidLift keeps its file-first Real-ESRGAN adapter behind the
`EngineAdapter` contract. The product accepts media files, not discs or
titles. Disc workflow concerns such as drive handling, title selection, and
rip queues remain outside this project.
