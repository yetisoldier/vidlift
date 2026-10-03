# VidLift Architecture

VidLift keeps product concerns separated from the shared upscale engine.

## Layers

1. `vidlift.probe`: ffprobe wrapper, stream parsing, source classification, and SD candidacy.
2. `vidlift.plan`: deterministic batch planning from classified reports.
3. `vidlift.run`: single-file GUI-free run pipeline (probe -> plan ->
   prepare -> upscale -> encode -> verify) with the full observability
   contract: phase, log path/tail, last-activity timestamp, command
   lines, output paths, phase timings, verification result, and a
   per-run artifact manifest. The upscale phase goes through the
   `EngineAdapter` seam; today a `StubEngine` passthrough satisfies
   it because the bluray-to-mp4 file-first seam does not exist yet,
   and every manifest records `engine_simulated: true`.
4. `vidlift.cli`: headless user interface for probe, plan, and run.
5. `vidlift.gui`: PyQt planning workbench that consumes the same core plan objects.

The bluray-to-mp4 upscale/encode path is consumed only through the
`EngineAdapter` seam, never imported directly. The runner is
CLI-first and proven headless, single-file first (batch and UI come
later as clients of the same `RunState`). Runner state exposes
phase, log paths, last-activity time, cancel/retry, and per-run
artifacts so health is judged by activity, not progress percent
alone. External tools are discovered explicitly (`find_tools`) with
named missing-tool errors before any phase starts; there is no
silent PATH fallback mid-run, and packaged builds pass bundled
ffmpeg/ffprobe paths through the CLI flags or environment overrides.

## Core Rules

- Probe and plan modules have no GUI imports.
- The GUI never owns business logic that the CLI cannot run.
- VidLift accepts files, not discs or titles.
- Output naming is derived from the input filename.
- The tool reports source limits honestly instead of promising invented detail.

## Shared Engine Boundary

The sibling bluray-to-mp4 project owns the validated AI upscale path. VidLift should consume that path through media-agnostic functions: prepare progressive square-pixel source, run upscale, encode HEVC MP4, and carry text subtitles where possible.

If that boundary starts requiring disc concepts, keep VidLift separate and add an adapter rather than importing disc workflow assumptions.

The runner must not inherit disc/rip pipeline concerns (drive
handling, title selection, rip queues). If a runner feature only
makes sense for discs, it does not belong in VidLift.
