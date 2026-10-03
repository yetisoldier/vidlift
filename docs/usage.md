# VidLift Usage

VidLift is currently a pre-flight tool. It probes source files, classifies them, and builds an honest 720p HEVC MP4 plan before the future encode runner spends hours on a batch.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e .
```

VidLift needs `ffprobe` on `PATH` for real files.

## Probe Files

```bash
.venv/bin/vidlift probe movie.avi clip.mov
```

The probe report shows container, codec, duration, displayed geometry after sample-aspect-ratio handling, audio channels, subtitle type, source class, and whether the file is an upscale candidate.

## Plan A Batch

```bash
.venv/bin/vidlift plan --target 720p --gbph 1.5 movie.avi clip.mov
```

The planner reports:

- ready/skip verdict
- planned output path
- target frame geometry
- video/audio bitrate plan
- estimated output size
- subtitle handling note
- source-specific quality expectation
- warnings for PAL speedup or interlace

Use `--output-dir DIR` to send all planned MP4s to one folder.

## Open The UI

```bash
.venv/bin/vidlift gui
# or
.venv/bin/vidlift-gui
```

The UI is a planner workbench. Add files or a folder, set the GB/hour target, choose an optional output folder, and run `Plan Batch`. It does not encode yet.

## Current Limits

- No upscale/encode runner in this repo yet.
- No OCR for image subtitles.
- PAL speedup is reported, not corrected.
- Chapters are not preserved in the planned MP4 output.
