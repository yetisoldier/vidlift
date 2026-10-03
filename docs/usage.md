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

## Run One File

The runner drives a single file end-to-end: probe, plan, prepare,
upscale, encode, verify.

```bash
.venv/bin/vidlift run --output-dir /media/upscaled movie.avi
```

Each run writes its output MP4 (verified for existence, decodability,
and planned geometry), a run log with every command line, and an
artifact manifest (plan, commands, outputs, phase timings,
verification result) into the run-log directory.

Honest status: the upscale phase currently uses a stub engine, so the
output is a real re-encode of the prepared source rather than an AI
upscale. The bluray-to-mp4 file-first engine seam does not exist yet;
when it ships, the same runner gains the real adapter with no
pipeline changes. Missing ffmpeg/ffprobe are explicit setup errors
naming the tool.

## Open The UI

```bash
.venv/bin/vidlift gui
# or
.venv/bin/vidlift-gui
```

The UI is a planner workbench. Add files or a folder, set the GB/hour target, choose an optional output folder, and run `Plan Batch`. It does not encode yet.

## Current Limits

- The runner's upscale phase is a stub (no Real-ESRGAN pass yet);
  outputs are real re-encodes at the planned bitrate/geometry, and
  manifests record `engine_simulated: true`.
- Sample-based progressive/IVTC/interlace classification is deferred
  until the real engine adapter; the probe's interlace flag drives
  the deinterlace decision.
- Verification checks existence, container decodability, and
  geometry; a full decode pass is deferred with it.
- Batch queue: not yet; the runner is single-file by design for now.
- No OCR for image subtitles.
- PAL speedup is reported, not corrected.
- Chapters are not preserved in the MP4 output.
