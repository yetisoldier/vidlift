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

## `vidlift gui`

Open the PyQt planning workbench.

## Script Aliases

- `vidlift`: primary command
- `vidlift-cli`: same CLI entry point, reserved for unattended automation naming
- `vidlift-gui`: opens the GUI directly
