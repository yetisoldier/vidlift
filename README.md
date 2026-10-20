# VidLift

VidLift converts older standard-definition video files into clean 720p HEVC
MP4s using a local Real-ESRGAN ncnn/Vulkan upscale pipeline.

It supports common AVI, DivX, Xvid, MPEG, MP4, MOV, MKV, WMV, FLV, TS, M4V,
and VOB inputs. VidLift probes each file, chooses safe geometry and
deinterlacing behavior, preserves usable audio and subtitles, runs the AI
upscale, encodes the final MP4, and verifies the result.

## Download for Windows 11

1. Open the [latest release](https://github.com/yetisoldier/vidlift/releases/latest).
2. Download **VidLift-<version>-Windows-x64-Setup.exe**.
3. Run the installer.
4. Leave **Create a desktop shortcut** selected if you want quick access.
5. Launch **VidLift** from the Start menu or desktop.

The installer includes VidLift, FFmpeg/ffprobe, Real-ESRGAN ncnn/Vulkan,
the verified x2plus model, and unattended subtitle OCR tools. Python is not
required.

> The installer is currently unsigned. Windows SmartScreen may display an
> **Unknown publisher** warning. The release includes a SHA-256 checksum file
> so you can verify the download.

## Plan a batch

Open VidLift, then drag video files into the window or select **Add Files**
or **Add Folder**. Choose an optional output folder, decide whether bitmap
subtitle OCR should run, and select **Plan Batch**.

![VidLift home screen](docs/images/vidlift-home.png)

The plan shows which files are ready, source type, output geometry, exact
output filename, estimated size, subtitle handling, and warnings. Select a row
to see an output preview covering the destination, HEVC video, AAC audio,
subtitles, and estimated size.

![VidLift planned batch](docs/images/vidlift-planned-batch.png)

Select **Start Upscale** to process every ready file serially. VidLift shows the
current file and completed count while keeping the window responsive. Select
**Cancel Current** to stop safely.

## Run an upscale

Open PowerShell and run:

~~~powershell
$vidlift = "$env:LOCALAPPDATA\Programs\VidLift\vidlift-cli.exe"
& $vidlift run "D:\Videos\movie.avi" --output-dir "D:\Videos\Upscaled" --engine ncnn
~~~

VidLift performs:

1. source probe and classification
2. pre-flight plan
3. deinterlace/prepare when required
4. Real-ESRGAN AI upscale
5. 720p HEVC MP4 encode
6. full verification
7. subtitle pass-through or OCR when possible

## Bulk upscaling

The OpenClaw/MCP interface accepts multiple paths in one request and queues one
job per file through a single FIFO worker. This prevents multiple upscales from
fighting for the same GPU.

Example request to Jarvis:

> Queue every supported video in D:\Videos\To Upscale through VidLift and
> report when the batch finishes.

The native CLI currently starts one file per command. The desktop GUI and MCP
interface both process bulk queues serially so jobs do not compete for the GPU.

## What you receive

For each successful job, VidLift writes:

- a verified 720p HEVC MP4
- an SRT sidecar when a subtitle was selected or recovered
- a run log with commands, phases, and heartbeat activity
- a JSON manifest with the input, plan, engine, timing, outputs, and
  verification result

Run records are stored in **.vidlift-runs** beside the source unless a different
log directory is supplied.

## Hardware and storage

- Windows 11 x64
- Vulkan-capable GPU and current graphics driver for AI upscaling
- enough free space for temporary decoded frames and the final MP4
- local storage is strongly recommended for long runs

## Documentation

- [User guide](docs/usage.md)
- [Desktop interface](docs/ui.md)
- [Command-line reference](docs/cli.md)
- [Troubleshooting](docs/usage.md#troubleshooting)
- [Development](docs/development.md)
- [Architecture](docs/architecture.md)
- [Roadmap](docs/roadmap.md)

## Current limitations

- The CLI runs one file per command; bulk queueing is available through the GUI and MCP.
- GUI progress reports the current file and completed count; detailed phase activity remains in the run log.
- Output target is currently 720p.
- PAL speedup is reported but not corrected.
- Chapters are not preserved.
- OCR is best effort and fails open so a subtitle problem does not discard a
  completed video.

Copyright © 2026 YetiWurks LLC. All rights reserved.
