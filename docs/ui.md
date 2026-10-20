# VidLift Desktop Interface

The desktop app plans and runs batches. It lets you inspect several videos
together before committing GPU hours, then processes ready files one at a time.

## Home screen

![VidLift home screen](images/vidlift-home.png)

### Controls

- **Add Files:** select one or more videos.
- **Add Folder:** add supported videos directly inside one folder.
- **Clear:** remove the current list.
- **GB/hour:** set the approximate final size per hour of video.
- **Output Folder:** send every planned MP4 to one destination.
- **Enable subtitle OCR:** convert selected bitmap PGS/VobSub subtitles to text. Turning this off does not disable ordinary text-subtitle pass-through.
- **Plan Batch:** probe and classify every listed file.
- **Start Upscale:** process every ready item in the displayed plan.
- **Cancel Current:** stop the running item safely and leave remaining items unstarted.

Files can also be dragged into the window.

## Planned batch

![VidLift planned batch](images/vidlift-planned-batch.png)

The top-right summary shows the ready count and estimated combined output size.

The table shows:

- verdict
- source path
- detected source class
- target geometry
- exact output destination
- estimated output size
- subtitle disposition
- warnings

Select any row to read the complete file-level plan and output preview in the
right pane. The preview lists the destination filename, HEVC video geometry and
bitrate, AAC audio, subtitle disposition, and estimated size.

After planning, select **Start Upscale**. The progress bar and summary identify
the current file and completed count. Detailed phase heartbeats, commands, and
diagnostics remain in the `.vidlift-runs` log beside each source file.
