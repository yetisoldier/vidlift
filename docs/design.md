# VidLift v0.1 Design

Status: design doc for this repo, v0.2 work started (2026-10-03). Adapted from
the SD-file-upscaler scoping assessment written by the architect
subagent on 2026-10-02 (bluray-to-mp4
`docs/designs/sd-file-upscaler-scoping.md`, the binding spec for this
project's shape) and the bluray-to-mp4 v0.2.19 AI upscale design
(`docs/designs/v0.2.19-dvd-ai-upscale.md`, the engine design VidLift
consumes). This document restates what VidLift is on its own terms and
records the version plan.

---

## 1. What VidLift is

VidLift is a batch AI upscaler for SD video FILES. Input: the formats
of a 1990s-2000s movie shelf (AVI with XviD/DivX, MP4/M4V, MOV, MKV,
WMV, FLV, MPG/MPEG/VOB), of unknown declared resolution. Output: a
720p-frame HEVC MP4 per file, anamorphic-safe (16:9 -> 1280x720, 4:3
-> 960x720), AAC audio, text subtitles passed through.

It is a separate product from bluray-to-mp4 (which is "insert disc,
get MP4s"); VidLift is "drop files, get better MP4s." The two share
an ENGINE, not a product. VidLift has no concept of discs, rips,
drives, titles, MakeMKV, subtitle OCR, TMDB, or naming lookups: it
names its outputs from the input filename. That separation is the
whole point of existing as a sibling repo instead of an input mode or
a fork.

**Seam contract (binding):** the engine VidLift imports is the
bluray-to-mp4 v0.2.19 upscale core, whose public surface never
mentions discs, rips, drives, titles, or MakeMKV. Its pieces: an
engine adapter (`detect() -> Engine`, `run(in_chunk_dir,
out_chunk_dir, model, scale, cancel) -> frames_done`), a prepare
filter builder (probed stream + file path in; ffmpeg filter chain +
classified output fps out; `displayed_size(w, h, sar)` geometry), a
mezzanine writer (chunk dirs + fps in; video-only MKV out), an encode
planner (already media-agnostic), and a one-stage entry point
`run_upscale(input_file_path, plan, cancel) -> mezzanine_path` where
the input is a caller-supplied "prepared progressive square-pixel
source file" path. VidLift is the shell job around that seam, IF
v0.2.19 builds it that way. This is a hard gate: if the seam does not
hold, session 1 of the v0.2 work becomes a rewrite, and the plan
re-estimates.

## 2. v0.1 (now): probe and classify

Shipped in this repo as the first real code. Zero GUI dependencies,
plain Python plus ffprobe, testable headless through a runner seam
that replays canned ffprobe JSON (no real videos needed).

`vidlift probe FILE...` reads what each file actually declares and
reports it honestly:

- container, duration, codec;
- width/height, `sample_aspect_ratio`/`display_aspect_ratio`, and
  the square-pixel displayed size (`displayed_size(w, h, sar)`;
  unknown SAR ("0:1" or missing) is reported as unknown and assumed
  square, never guessed);
- `field_order` and both frame rates (`r_frame_rate`,
  `avg_frame_rate`);
- audio streams with channels; subtitle streams typed text/image
  (v0.2 passes text subs through as mov_text; image subs are listed
  as "not carried," no OCR ever);
- source-class heuristic: `xvid-divx-avi` (MPEG-4 ASP in an AVI, the
  expected common case), `dvd-mpeg2-rip` (MPEG-1/2), `h264-sd`
  (h264 at SD height), `other`;
- upscale verdict: `candidate` only when displayed height <= 576
  after square-pixel normalization; taller files get `already-hd`,
  which is a report verdict, not an upscale;
- PAL-speedup suspicion: progressive 25 fps flags a warning (European
  rips of US films commonly play ~4% fast). v0.1 suspects; v0.2
  warns in the pre-flight report and processes as-is. Retiming to
  23.976 with pitch correction is a v2 rabbit hole and is not
  coupled to launch.

The tool never invents an "original resolution"; it reads what the
file declares. "Unknown resolution" was a non-problem all along:
every decoder reports real geometry, and v0.1 proves that by
classifying from probe data alone.

The v0.1 rule that carries to every later version: **the probe and
classification core has zero GUI imports.** The classification that
v0.2's engine path needs is exactly the classification v0.1 ships.

## 3. v0.2: engine consumption + pre-flight report

This work started after bluray-to-mp4 v0.2.20 shipped with a validated
DVD AI-upscale path. VidLift does not re-run the benchmark and does not
pick models; it inherits whatever routing that pipeline blesses.

- Pinned import of the v0.2.19 upscale core (`upscale/` +
  `encode.py`) as a dependency (git submodule or path dep while both
  stay personal).
- The three-way source-type classification (progressive / hard 3:2
  telecine -> fieldmatch+decimate IVTC to 23.976 / genuine interlace
  -> bwdif at field rate) drives the prepare filter chain verbatim
  from v0.2.19's design; v0.1's `field_order` + rate fields are the
  ffprobe triage input, confirmed by the 60-second sample
  inspection. No blind deinterlace, ever.
- Square-pixel normalization before the model: `setsar=1` at the
  displayed size, then 2x upscale, then lanczos down to the
  720p-frame geometry.
- Audio: AAC per the shared channel-count bitrate table (5.1 ->
  448k, stereo -> 192k, mono -> 128k). Chapters: dropped, noted.
- **Per-file pre-flight report before committing hours:** probe
  (sub-second), classify, ONE tiny timed engine sample (~3 s of
  video) to measure real fps on this box, then projected wall clock,
  projected output size at the GB/hour target (1.5 GB/hour default,
  the pairing rule), and the honest expectation string per source
  class. This is the front door of the product because files have
  no "insert disc" moment of commitment.
- First implemented slice: `vidlift plan --target 720p --gbph 1.5
  FILE...` builds the batch plan from probe data: output geometry,
  target bitrate/size, output name, subtitle handling note, source
  expectation, and warnings. It deliberately stops before running the
  sample benchmark or encode.
- Honest expectations, stated per source class: a proper DVD MPEG-2
  rip gets "the best 720p that source can give, good, not
  transformative"; a ~700 MB XviD/DivX rip gets "clearly cleaner on a
  TV, it cannot recover detail never captured in the file"; WMV/FLV
  get "helps least, watch the sample first." The report exists so a
  NO is learned from a 3-minute sample, not a 6-hour encode.
- Batch queue UI (PyQt6): drag-drop files or pick a folder, queue in
  drop order with per-file status, Start / Start All, cancel between
  and mid-file. The queue is the product; no library, no watch list,
  no metadata editor. Watch folder: optional v2, correctly excluded.

## 4. CLI-first: the zero-touch rule

Carried over from bluray-to-mp4 run 12 with full force: a batch of
6-hour jobs is where a GUI babysitter fails at 3 AM. Every
capability VidLift grows must be drivable headless. v0.1 ships the
pattern (`vidlift probe`); v0.2 grows `vidlift-cli --target 720p
--gbph 1.5 FILE...` / `--folder DIR` with the same engine/config
resolution as the GUI, per-file exit codes, and a stderr summary.
The CLI also runs on the Linux box with no Qt, which is what keeps
the Section 1 seam honest. The v0.1 code already separates GUI-free
core (`vidlift/probe.py`) from interface (`vidlift/cli.py`); v0.2's
GUI is a client of the core, never a container for it.

## 5. Sequencing

1. v0.1 (this repo, now): skeleton + probe/classify + tests, design
   doc. Done when pytest is green twice in a fresh venv.
2. Gate: bluray-to-mp4 v0.2.20 lands with a validated upscale path. Done.
3. v0.2 session A: pre-flight plan CLI, then engine consumption; prepare chain + classification
   wiring; probe fixtures extended to recorded ffprobe JSON for
   Eric's real sample files.
4. v0.2 session B: pre-flight report + batch queue UI.
5. v0.2 session C: CLI + end-to-end validation (Linux for pipeline
   correctness; Eric's Windows box for 3090 Ti throughput truth and
   TV playback acceptance).

Sample-file ask (small, high value, from Eric): 2-3 representatives
of the actual collection: one ~700 MB XviD/DivX AVI (the expected
common case), one proper DVD MPEG-2 rip (vob or mkv), one MOV or
odd-container file. Trimmed clips are fine.