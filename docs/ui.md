# VidLift Desktop Interface

The desktop app plans and runs batches. It lets you inspect several videos
together before committing GPU hours, then processes ready files one at a time.

v0.4.4: runs are tracked jobs in the SAME persistent registry the
`vidlift mcp-server` uses, so anything an AI agent can see over MCP —
`list`, `status`, `manifest`, and `cancel` — covers GUI-started runs
too. One source of truth, two readers.

## Home screen

![VidLift home screen](images/vidlift-home.png)

A muted helper line above the controls names the canonical order:

> 1. Add files → 2. Plan → 3. Start. Planning never writes to disk.

### Controls

- **Add Files:** select one or more videos.
- **Add Folder:** add supported videos directly inside one folder.
- **Clear:** remove the current list.
- **GB/hour:** set the approximate final size per hour of video.
- **Output Folder:** send every planned MP4 to one destination.
- **Upscale engine:** choose the AI pass. `Auto (recommended)` tries the
  Real-ESRGAN ncnn/Vulkan engine and falls back to the stub when the
  binary, models, or GPU probe fail — recording the exact reason in the
  manifest. `Real-ESRGAN ncnn/Vulkan (GPU)` forces the real engine and
  names a missing setup as an error. `Stub (no AI; encode-only
  fallback)` ships a verified re-encode with no AI pass, on purpose.
  The choice persists across launches; it is fixed while a batch runs
  (cancel to change it).
- **Enable subtitle OCR:** convert selected bitmap PGS/VobSub subtitles to text. Turning this off does not disable ordinary text-subtitle pass-through.
- **Plan Batch:** probe and classify every listed file.
- **Start Upscale:** enqueue every ready item as a tracked job.
- **Cancel Current:** stop the running job safely through the same
  cancel path an MCP agent uses; it can take a few seconds while the
  engine exits cleanly.

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

## Run panel

After **Start Upscale** a run card appears with:

- the current **stage label** naming what is actually running (probe,
  prepare, `upscaling — frames X/Y`, encode, verify full decode,
  subtitles), driven by the runner's own phase and step facts;
- an animated activity-dots indicator plus the elapsed timer, so a
  legitimate 0% window (GPU probe, first chunk) never reads as frozen;
- a **Current file** bar with a measured ETA when the benchmark cache
  has a constant for this geometry bucket (duplicate progress lines
  never blank the ETA; no measured constant means "estimating", never
  a fake percentage);
- an **Overall queue** bar across every enqueued job;
- a read-only **log tail** rendered from the runner's own log; the
  full log stays in `.vidlift-runs` beside each source file;
- the head **Total remaining** estimate across the whole batch, shown
  only from measured constants and the live ETA. When a file ahead has
  no measured constant the honest reading is "…", never a guess.

Jobs run strictly one at a time. A five-file start is five serial
tracked jobs; Start re-enables when the last one reaches a finished
state. Cancelled, failed, and succeeded jobs all remain in the
registry history and resurface on the next launch (a window closed
mid-run surfaces as `failed` with the restart error — never a silent
"succeeded").

Fail-open parity: when the AI pass fails mid-run (or the plan chose the
stub), the output row reads "completed (no AI upscale)" — the manifest
and the MCP `status` payload carry the same honesty fields the CLI
shows.

## Monitor via MCP

Any local MCP agent sees the same registry the GUI writes to:

- `list` — queued, running, and finished job ids (include GUI runs).
- `status {job_id}` — the full runner state contract: current phase,
  progress frames, log tail, engine honesty fields, ETA when measured.
- `manifest {job_id}` — the run manifest verbatim.
- `cancel {job_id}` — the same cooperative cancel the GUI's button
  uses; both fronts land on one flag.

The registry lives in one state directory per machine
(`$VIDLIFT_STATE_DIR/jobs.json`, default `~/.vidlift` in development
and the per-platform app-data dir in packaged builds), so the packaged
GUI and an agent-side server share job history by default.

The `vidlift mcp-server` stdio server runs against the same registry
files; nothing in the GUI duplicates its state.