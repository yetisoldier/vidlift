# VidLift Desktop Interface

The desktop app is a batch planning workbench. It lets you inspect several
videos together before committing GPU hours to an upscale.

## Home screen

![VidLift home screen](images/vidlift-home.png)

### Controls

- **Add Files:** select one or more videos.
- **Add Folder:** add supported videos directly inside one folder.
- **Clear:** remove the current list.
- **GB/hour:** set the approximate final size per hour of video.
- **Output Folder:** send every planned MP4 to one destination.
- **Plan Batch:** probe and classify every listed file.

Files can also be dragged into the window.

## Planned batch

![VidLift planned batch](images/vidlift-planned-batch.png)

The top-right summary shows the ready count and estimated combined output size.

The table shows:

- verdict
- source path
- detected source class
- target geometry
- estimated output size
- subtitle disposition
- warnings

Select any row to read the complete file-level plan in the right pane.

## What the GUI does not do yet

The desktop app does not currently start, cancel, or monitor upscale jobs. Use
the installed CLI for a single file or the OpenClaw/MCP interface for a bulk
queue. These controls are planned for a future GUI release.
