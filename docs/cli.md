# VidLift CLI Reference

## `vidlift --version`

Print the installed VidLift version.

## `vidlift probe FILE...`

Probe one or more video files with `ffprobe` and print the classified source report.

Exit codes:

- `0`: all files probed successfully
- `1`: at least one file failed to probe
- `2`: command-line usage error

## `vidlift plan FILE...`

Build a pre-flight plan for one or more files.

Options:

- `--target 720p`: output frame target. Only `720p` exists today.
- `--gbph NUMBER`: target gigabytes per hour. Default: `1.5`.
- `--output-dir DIR`: plan outputs into a shared directory instead of beside each input.

Example:

```bash
vidlift plan --target 720p --gbph 1.5 --output-dir /media/upscaled *.avi
```

Exit codes:

- `0`: all files planned successfully
- `1`: at least one file failed to probe
- `2`: invalid plan options or command-line usage error

## `vidlift run FILE`

Run ONE file end-to-end through the runner pipeline: probe, plan,
prepare, upscale, encode, verify. This is the first execution
milestone; batch logic and UI controls arrive later as clients of
this same runner.

Options:

- `--target 720p`: output frame target. Only `720p` exists today.
- `--gbph NUMBER`: target gigabytes per hour. Default `1.5`.
- `--output-dir DIR`: where the output MP4 goes (default: beside the input).
- `--ffmpeg PATH` / `--ffprobe PATH`: explicit tool paths (packaged
  builds pass bundled binaries here). Environment fallbacks:
  `VIDLIFT_FFMPEG`, `VIDLIFT_FFPROBE`, then `PATH`. A missing tool is
  an explicit error naming the tool, never a silent guess.
- `--log-dir DIR`: where run logs/manifests go
  (default: `INPUT_DIR/.vidlift-runs`).

What each run leaves behind:

- the output MP4 (verified to exist, decode, and match planned geometry);
- a `.log` file with every command line and phase transition (the log
  tail is the health source, not a progress percent);
- a `.manifest.json` artifact manifest: inputs, plan, command lines,
  outputs, phase timings, verification result, and engine status.

**Current limitation (honest):** the upscale phase runs a stub
engine, not Real-ESRGAN. bluray-to-mp4's validated path is
chunk-directory based (disc-shaped); its file-first seam does not
exist yet. A stub run is a REAL ffmpeg re-encode of the prepared
source at the planned bitrate/geometry — the pipeline machinery,
observability, and verification are real; the AI pass is not. Every
manifest records `engine_simulated: true`. Also deferred until the
real adapter lands: the 60-second sample inspection that classifies
progressive / hard 3:2 telecine / genuine interlace (the probe's
interlace flag drives the deinterlace decision today), and a full
decode pass during verification (the encode's own `-loglevel error`
exit plus an ffprobe container/geometry check stand in for it).

Example:

```bash
vidlift run --gbph 1.5 --output-dir /media/upscaled movie.avi
```

Exit codes:

- `0`: run completed and the output verified
- `1`: run failed (probe, plan verdict, command failure, or verification)
- `2`: missing external tool (a setup problem, not a media problem)

## `vidlift gui`

Open the PyQt planning workbench.

## Script Aliases

- `vidlift`: primary command
- `vidlift-cli`: same CLI entry point, reserved for unattended automation naming
- `vidlift-gui`: opens the GUI directly
