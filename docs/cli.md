# VidLift Command-Line Reference

The Windows installer places the CLI at:

    %LOCALAPPDATA%\Programs\VidLift\vidlift-cli.exe

PowerShell convenience:

~~~powershell
$vidlift = "$env:LOCALAPPDATA\Programs\VidLift\vidlift-cli.exe"
~~~

## Version and help

~~~powershell
& $vidlift --version
& $vidlift --help
& $vidlift run --help
~~~

## Probe files

~~~powershell
& $vidlift probe "D:\Videos\movie.avi" "D:\Videos\clip.mov"
~~~

The report includes container, codecs, duration, displayed geometry, frame
rates, audio, subtitles, source class, and upscale verdict.

Exit codes:

- **0:** every file probed successfully
- **1:** one or more files failed
- **2:** command-line usage error

## Plan files

~~~powershell
& $vidlift plan --target 720p --gbph 1.5 --output-dir "D:\Videos\Upscaled" "D:\Videos\movie.avi" "D:\Videos\clip.mov"
~~~

Options:

- **--target 720p:** output frame target
- **--gbph NUMBER:** approximate output gigabytes per hour; default 1.5
- **--output-dir PATH:** shared output directory
- **--no-transcribe:** disables Mode A audio transcription / subtitle transcription for that run or plan

Planning does not start an upscale.

## Run one file

~~~powershell
& $vidlift run "D:\Videos\movie.avi" --output-dir "D:\Videos\Upscaled" --engine ncnn
~~~

The command runs probe, plan, prepare, upscale, encode, verification, and
subtitle handling.

Options:

- **--target 720p:** output target
- **--gbph NUMBER:** approximate output gigabytes per hour
- **--output-dir PATH:** final MP4 destination
- **--log-dir PATH:** run log and manifest directory
- **--engine auto|ncnn|stub:** engine selection
- **--ffmpeg PATH / --ffprobe PATH:** override bundled binaries
- **--realesrgan PATH:** override the bundled ncnn executable
- **--realesrgan-models PATH:** override the bundled model directory
- **--no-transcribe:** disables Mode A audio transcription / subtitle transcription for that run or plan

Engine behavior:

- **ncnn:** require the real AI engine
- **auto:** use ncnn when tool resolution and the GPU probe succeed; otherwise
  record the fallback
- **stub:** deliberately bypass AI for pipeline testing

Exit codes:

- **0:** output completed and verified
- **1:** media, pipeline, or verification failure
- **2:** missing required external tool

## Add subtitles (Mode B)

~~~powershell
& $vidlift subs "D:\Videos\movie.avi" --lang en --device auto
& $vidlift subs "D:\Videos\movie.avi" --replace-source
~~~

Transcribes the file's own audio and soft-muxes the result into a NEW file
beside the source as `{stem}.subs.mp4` (plus a `{stem}.subs.srt` sidecar). No
upscale and no re-encode — the video and audio streams are copied. The source
file is never modified by default.

Options:

- **--lang ISO:** pin the transcription language (ISO 639-1, e.g. `ko`); default auto-detect
- **--model TAG:** whisper model tag or path (default `medium`; first use downloads it)
- **--device auto|cuda|cpu:** transcription device (default `auto`)
- **--force:** transcribe even when a usable text subtitle track already exists
- **--replace-source:** after a verified pass, delete the source video and rename
  the output to the source's name with an `.mp4` extension (e.g. `movie.avi` →
  `movie.mp4`). If a file of that name already exists, both files are kept
  (default: off).
- **--ffmpeg PATH / --ffprobe PATH:** override the bundled binaries

A refused replace (for example, a name collision) still exits **0** — it is a
successful job with a warning, never a failure.

## Benchmark

~~~powershell
& $vidlift benchmark "D:\Videos\sample.avi" --duration 30 --engine ncnn
~~~

This measures a short sample on the current machine and records a local timing
constant for honest ETA calculations.

Options:

- **--duration SECONDS:** sample duration, clamped to 10 through 120
- **--engine auto|ncnn|stub**
- **--work-dir PATH**
- the same FFmpeg and Real-ESRGAN override options as **run**

## Open the GUI

~~~powershell
& $vidlift gui
~~~

The Start menu shortcut launches the same planning interface directly.

## Start the MCP server

~~~powershell
& $vidlift mcp-server
~~~

Optional:

- **--state-dir PATH:** registry and benchmark state directory

The MCP bridge provides batch queueing, status, cancellation, manifests, and
benchmarks to compatible clients such as OpenClaw.

## Bulk behavior

The CLI accepts multiple files for **probe** and **plan**, but **run** starts one
file per command. Bulk upscaling is provided by the MCP bridge, which creates
one job per input and processes the queue FIFO through one worker.
