# VidLift Architecture

VidLift keeps product concerns separated from the shared upscale engine.

## Layers

1. `vidlift.probe`: ffprobe wrapper, stream parsing, source classification, and SD candidacy.
2. `vidlift.plan`: deterministic batch planning from classified reports.
3. `vidlift.cli`: headless user interface for probe and plan.
4. `vidlift.gui`: PyQt planning workbench that consumes the same core plan objects.
5. Future runner: imports the validated bluray-to-mp4 upscale/encode path.

   The runner is CLI-first: it exists and is proven headless
   (single file first, batch second) before the GUI attaches to it.
   Runner state exposes phase, log paths, last-activity time,
   cancel/retry, and per-run artifacts so health is judged by
   activity, not progress percent alone.

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
