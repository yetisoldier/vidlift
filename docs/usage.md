# VidLift User Guide

## 1. Install on Windows 11

1. Open the [latest GitHub release](https://github.com/yetisoldier/vidlift/releases/latest).
2. Download **VidLift-<version>-Windows-x64-Setup.exe**.
3. Optionally download the matching **.sha256** file.
4. Run the setup program and accept the default per-user install location.
5. Launch VidLift from the Start menu.

VidLift installs under:

    %LOCALAPPDATA%\Programs\VidLift

The installer includes every runtime dependency. You do not need Python,
FFmpeg, Real-ESRGAN, Subtitle Edit, or Tesseract separately.

### SmartScreen notice

The installer is not yet Authenticode-signed. Windows may show **Windows
protected your PC** or **Unknown publisher**.

To continue, select **More info**, confirm the filename matches the GitHub
release, and select **Run anyway**.

### Verify the installer checksum

In PowerShell:

~~~powershell
Get-FileHash .\VidLift-0.4.2-Windows-x64-Setup.exe -Algorithm SHA256
Get-Content .\VidLift-0.4.2-Windows-x64-Setup.exe.sha256
~~~

The two hexadecimal hashes must match exactly.

## 2. Plan files in the desktop app

Launch VidLift. The opening screen is intentionally simple:

![VidLift home screen](images/vidlift-home.png)

### Add source videos

Use any of these methods:

- drag one or more video files into the window
- select **Add Files**
- select **Add Folder** to add supported videos from one folder

Recognized extensions include AVI, DivX, FLV, M4V, MKV, MOV, MP4, MPEG,
MPG, TS, VOB, and WMV.

### Set the output size target

**GB/hour** controls the approximate final file size. The default is 1.5 GB
per hour. A two-hour video would therefore target roughly 3 GB.

Higher values preserve more encode detail but create larger files. This setting
does not change the 720p output geometry.

### Choose an output folder

Select **Output Folder** if every planned MP4 should go to one destination.
Leave it empty to place each output beside its source.

### Run the pre-flight plan

Select **Plan Batch**.

![VidLift planned batch](images/vidlift-planned-batch.png)

The table reports:

- **Verdict:** ready or skipped
- **File:** source path
- **Source:** detected source class
- **Target:** planned output dimensions
- **Size:** estimated final size
- **Subtitles:** selected subtitle handling
- **Warnings:** interlace, PAL-speedup, or other cautions

Select a row to see the complete explanation in the details pane.

The GUI currently plans batches. It does not yet start or cancel the upscale
queue.

## 3. Run one file from PowerShell

Open PowerShell and define the installed CLI path:

~~~powershell
$vidlift = "$env:LOCALAPPDATA\Programs\VidLift\vidlift-cli.exe"
& $vidlift --version
~~~

Run a real AI upscale:

~~~powershell
& $vidlift run "D:\Videos\movie.avi" --output-dir "D:\Videos\Upscaled" --engine ncnn
~~~

Use quotes around paths containing spaces.

### Useful run options

- **--gbph 1.5** sets the approximate output size per hour.
- **--output-dir PATH** writes the final MP4 to a chosen folder.
- **--log-dir PATH** puts logs and manifests in a chosen folder.
- **--engine ncnn** requires the real AI engine and reports a clear error if it
  cannot start.
- **--engine auto** uses ncnn when its GPU probe succeeds; otherwise it records
  the fallback honestly.
- **--engine stub** deliberately skips AI and is intended for pipeline testing.

## 4. Queue a bulk upscale

VidLift's MCP interface accepts multiple input paths. It creates one job per
file and processes them FIFO through a single worker so the jobs do not compete
for the GPU.

Example request to Jarvis:

> Queue every supported video in D:\Videos\To Upscale through VidLift. Use
> D:\Videos\Upscaled for output and notify me when the batch finishes.

You can also provide an explicit list of files. Jarvis can plan the batch,
start it, report status, cancel a job, and inspect each final manifest.

The installed CLI currently starts one file per command. The GUI plans multiple
files but does not start the queue.

## 5. Understand the pipeline

Each job passes through these phases:

1. **Probe:** inspect streams, geometry, frame rates, duration, and subtitles.
2. **Plan:** choose output geometry, bitrate, subtitle disposition, and warnings.
3. **Prepare:** deinterlace, inverse-telecine, or normalize the source as needed.
4. **Upscale:** process source frames through Real-ESRGAN ncnn/Vulkan.
5. **Encode:** write a 720p HEVC MP4 with audio.
6. **Verify:** inspect the container, geometry, duration, and full decode.
7. **Subtitles:** pass text subtitles through or attempt OCR for bitmap tracks.
8. **Done:** write the final manifest and close the job.

Long phases write heartbeat activity. A job can remain on one phase for a long
time without being stuck.

## 6. Find outputs and logs

By default, the final MP4 is written beside the source. If an output folder was
specified, it is written there instead.

The run record is stored in a **.vidlift-runs** folder and includes:

- **.log:** phase transitions, commands, errors, and heartbeat activity
- **.manifest.json:** input, plan, engine, timings, output paths, subtitle
  result, and verification evidence

When OCR produces a subtitle, VidLift writes an SRT beside the MP4 and embeds a
soft subtitle track where supported. OCR failures do not discard a successfully
completed video.

## 7. Troubleshooting

### Windows says Unknown publisher

The installer is unsigned. Confirm you downloaded it from this repository's
release page and verify its SHA-256 checksum before selecting **Run anyway**.

### VidLift reports no Vulkan GPU

Update the graphics driver. Real-ESRGAN requires a working Vulkan-capable GPU.
Remote desktop and virtual machines can hide Vulkan support even when the
physical GPU supports it.

### A file is skipped

Open the row in the desktop planner or run **vidlift-cli probe FILE**. Common
causes are no video stream, already-HD geometry, unreadable media, or a missing
file.

### Output is too large or too small

Adjust **GB/hour**. For example:

~~~powershell
& $vidlift run "D:\Videos\movie.avi" --gbph 1.2 --engine ncnn
~~~

### The job appears stuck

Check the latest log in **.vidlift-runs**. Heartbeat lines and a changing
last-activity time indicate useful work. AI upscale and final verification can
each take a long time.

### Subtitles are missing

Bitmap subtitle OCR is best effort. Inspect the manifest's **subtitles** block
and the retained **.ocr** diagnosis folder. The video is allowed to finish even
when OCR fails.

### Cancel a queued or running MCP job

Ask Jarvis to cancel the named VidLift job. Cancellation is cooperative and may
take a short time while the current external process exits cleanly.

## 8. Uninstall

Open **Settings → Apps → Installed apps**, find **VidLift**, and select
**Uninstall**. Source videos, completed outputs, logs, and manifests are not
removed.
