# VidLift

Batch AI upscaler for SD video files: it takes the obscure 1990s-2000s
movie shelf (AVI/XviD/DivX rips, MPEG-2 DVD rips, early MP4/MOV files,
WMV, FLV) and turns each file into a clean 720p HEVC MP4.

VidLift is a private, personal tool for Eric's own collection. It is
the sibling of bluray-to-mp4: a different product (drop files, get
better MP4s) that shares the same AI upscale engine core. It knows
nothing about discs, drives, or ripping.

**License:** none. This is a private repo and a personal tool, so no
formal license is attached; nothing here is intended for distribution.

## Status

**Design phase.** What exists today (v0.1.0):

- The probe/classify core: `vidlift probe FILE...` inspects any
  video file with ffprobe and reports resolution, SAR, displayed
  geometry, field order, frame rates, codec, container, duration,
  audio channels, subtitle types, a source-class guess, and a
  PAL-speedup suspicion.
- The design document: [docs/design.md](docs/design.md).

What does NOT exist yet: the AI upscale engine, any encoding, and any
GUI. The engine is being built as bluray-to-mp4 v0.2.19 (an
importable, disc-agnostic core); VidLift consumes it in v0.2. No
promises about output quality until the benchmark lands.

## Quick start (what you can actually run today)

Requires Python 3.12+ and ffprobe on PATH.

```
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/vidlift --version
.venv/bin/vidlift probe some_movie.avi another_clip.mov
```

For development:

```
python -m venv /tmp/vidlift-venv
/tmp/vidlift-venv/bin/pip install -e .
QT_QPA_PLATFORM=offscreen /tmp/vidlift-venv/bin/python -m pytest -q
```

## Planned shape (v0.2 and beyond)

- Per-file pre-flight report before any hours-long job: probe,
  classify, one tiny timed engine sample, then projected wall clock,
  projected output size, and an honest expectation string.
- Batch queue with per-file status; the queue is the product.
- CLI for unattended runs, from day one (`vidlift-cli`): hours-long
  jobs must never depend on a GUI babysitter at 3 AM.
- Output: 720p-frame HEVC MP4 (16:9 -> 1280x720, 4:3 -> 960x720),
  anamorphic-safe, AAC audio, text subtitles passed through.

See [docs/design.md](docs/design.md) for the full plan.