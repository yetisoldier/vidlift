# Follow-up (HIGH priority): cancel is not observed during pre-transcription steps

**Author:** Hermes (orchestrator) — **Date:** 2026-11-04
**Source:** Eric, live: clicked "Cancel Current" twice, the running subs job stayed running.
**Severity:** HIGH — real defect; a job wedged at language detection cannot be cancelled in-app.

## Root cause

`should_cancel` is observed ONLY inside `_on_segment` (`vidlift/transcribe.py:2429`), which fires
during the segment-by-segment transcription loop (`transcribe-run`). It is NOT observed during:

- **`transcribe-resolve`** (`transcribe.py:2398-2416` — `resolve_model_dir` + engine factory), or
- **`transcribe-language`** (`transcribe.py:2418-2421` — `detect_language(...)`).

So a job that hangs at `detect_language` (e.g. the model load wedged after the GPU was freed
mid-load, or a slow/hung CUDA context) never reaches `_on_segment`, the `CancelFlag` flips but
nothing reads it, and the job stays `running` forever. Cancel Current / Cancel Batch have no
effect in that window.

## Evidence (live)

- S03E03 "The Good, the Bad, and the Lucky.avi": `state=running`, `step=transcribe-language`,
  920s elapsed, `last_activity` 914s ago — a hang, not slowness.
- `cancel_upscale` (`gui.py:2522`) → `client.cancel(job_id)` → registry `CancelFlag` flips, but
  the running job's only `should_cancel()` reads are `transcribe.py:2429` (`_on_segment`) and the
  post-mux tail block in `subs_job.py` — neither is reached during `detect_language`.

## The fix (for Bob/Forge)

Add `should_cancel` observes to the pre-transcription steps so a cancel flips the job to
`cancelled` (or aborts the step) even during model resolution and language detection, e.g.:

- a check immediately **before** `detect_language` (and/or inside the resolve/model-load block),
- raising `EngineCancelled` (the same lazy import + message pattern as `_on_segment`).

Confirm both modes (Mode B via `run_subs_job`'s `should_cancel` seam; Mode A transcription via
`run_file`'s equivalent) honor the new observe. Also confirm the executor's
`EngineCancelled → cancelled` mapping (`jobs.py:271` and `:495`) still closes the job cleanly.

## Immediate remediation (for Eric, pre-fix)

Quit VidLift to kill a wedged job (the in-app cancel cannot reach it while stuck at
`transcribe-language`). Re-add the folder after restart; already-subtitled files will surface as
"already has subtitles"/"output exists" until the plan-time-skip follow-up ships.

## Related (same next-build backlog)

- `hermes-followup-v0.4.19-already-subtitled-skip.md` (plan-time skip)
- `hermes-followup-v0.4.19-ui-device-indicator.md` (CPU/GPU indicator)
- `hermes-followup-v0.4.19-test-coverage.md` (F-1/F-7/D-2)
