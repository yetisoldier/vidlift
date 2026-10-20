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

## Windows 11 installer

Download the latest `VidLift-<version>-Windows-x64-Setup.exe` from
[GitHub Releases](https://github.com/yetisoldier/vidlift/releases).
The installer includes the GUI, CLI, FFmpeg/ffprobe, the Real-ESRGAN
ncnn/Vulkan engine with VidLift's verified x2plus model, and the
unattended subtitle OCR tools. Python is not required.

The installer is currently unsigned, so Windows SmartScreen may show
an **Unknown publisher** warning. The matching `.sha256` file on the
release verifies the download.

## Status

**v0.4.2.** What exists today:

- The probe/classify core: `vidlift probe FILE...` inspects any
  video file with ffprobe and reports resolution, SAR, displayed
  geometry, field order, frame rates, codec, container, duration,
  audio channels, subtitle types, a source-class guess, and a
  PAL-speedup suspicion.
- The batch pre-flight planner: `vidlift plan FILE...` turns probe
  data into target geometry, bitrate/size estimates, output names,
  subtitle notes, and honest expectations before a long upscale run.
- The single-file runner: `vidlift run FILE` drives one file
  end-to-end (probe, plan, prepare, upscale, encode, verify) with
  full observability: a run log with every command line, phase
  timings, last-activity timestamps, explicit missing-tool errors,
  output verification, and a per-run artifact manifest. The upscale
  phase uses a stub engine today (the real file-first engine adapter
  is deferred until bluray-to-mp4 ships it); manifests record
  `engine_simulated: true` so nothing overclaims.
- The PyQt planning workbench: `vidlift gui` gives the same planner a
  batch-table interface with drag/drop, output-folder selection, and
  selected-file details.
- The documentation suite under [docs/](docs/).

The real ncnn/Vulkan AI engine and FIFO bulk queue are available through
the CLI/MCP runner. The desktop GUI currently plans batches but does not
yet expose start/cancel controls for the upscale queue.

## Quick start (what you can actually run today)

Requires Python 3.12+ and ffprobe on PATH (ffmpeg too, for `run`).

```
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/vidlift --version
.venv/bin/vidlift probe some_movie.avi another_clip.mov
.venv/bin/vidlift plan --target 720p --gbph 1.5 some_movie.avi
.venv/bin/vidlift run some_movie.avi
.venv/bin/vidlift gui
```

For development:

```
python -m venv /tmp/vidlift-venv
/tmp/vidlift-venv/bin/pip install -e .
QT_QPA_PLATFORM=offscreen /tmp/vidlift-venv/bin/python -m pytest -q
```

## Documentation

- [Usage](docs/usage.md)
- [CLI reference](docs/cli.md)
- [UI guide](docs/ui.md)
- [Development](docs/development.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)
- [Design](docs/design.md)

## Planned shape (v0.2 and beyond)

- Real engine adapter: when bluray-to-mp4 ships its file-first seam,
  `vidlift.run.EngineAdapter` gets one implementation and the runner
  changes zero lines. Sample-based source classification and a full
  decode pass during verification come with it.
- Per-file pre-flight report before any hours-long job: probe,
  classify, one tiny timed engine sample, then projected wall clock,
  projected output size, and an honest expectation string.
- Batch queue with per-file status over the proven single-file runner;
  the queue is the product.
- CLI for unattended runs, from day one (`vidlift-cli`): hours-long
  jobs must never depend on a GUI babysitter at 3 AM.
- Output: 720p-frame HEVC MP4 (16:9 -> 1280x720, 4:3 -> 960x720),
  anamorphic-safe, AAC audio, text subtitles passed through.

See [docs/design.md](docs/design.md) for the full plan.