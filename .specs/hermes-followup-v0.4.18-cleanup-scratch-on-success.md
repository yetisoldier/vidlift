# Follow-up: clean up scratch files/folders after a successful run

**Author:** Hermes (orchestrator) — **Date:** 2026-11-04
**Source:** Eric (2026-11-04): "after a successful run we clean up all the extra files
that are no longer needed, including folders used in the process."
**Severity:** cleanup/hygiene — fold into the next build/release.

## Current state (what already cleans up vs. what leaks)

### Mode A (upscale) — already cleans up on success

`vidlift/run.py` has `cleanup_run_artifacts(run_id, log_root)` on the verified-success path
(v0.4.7 "success cleanup", `run.py:2955`) — removes the prepared mezzanine, engine workdir,
and OCR workdir under `log_dir/.vidlift-runs/<run_id>`. Best-effort; a cleanup failure logs
a warning and keeps the success.

### Mode B (Add Subtitles) — LEAKS a work directory

The transcription workdir passed as `workdir = output_path + ".subs-work"` (`subs_job.py`,
the `transcribe_source_to_srt` call) is left behind after every job. Karen flagged this as
**K-5** (workstream-1 validation): "the `.subs.mp4.subs-work` scratch directory is left beside
the output after every subs job — both versions, both modes, unchanged by this work."

So the concrete residue today is `<output>.subs-work/` beside the delivered output. The
`.muxing.mp4` temp (mplex) and `.replace-tmp` (replace helper) are already swept; the
`.subs-work` dir is the leak.

## The feature

After a **successful** run, remove the scratch work directory (and any other transient
files/folders the run created) so the only artifacts left are the delivered output (the
`.mp4`, and the `.srt` if the sidecar-suppression follow-up is OFF).

## Constraints (design rules for Bob)

1. **Success-only.** Cleanup must run ONLY on the verified-success path — never on failure or
   cancel, where the scratch is retained for resume-claim and diagnosis (Mode A already holds
   this discipline; Mode B must match it).
2. **Best-effort, never fatal.** A cleanup failure logs a warning and keeps the success — it
   must never fail the job or delete a non-scratch file.
3. **Path-guard.** Delete only paths the run itself created (the `.subs-work` dir beside the
   output); never a user file. Mirror Mode A's `cleanup_run_artifacts` guardrail style.
4. **Interaction with replace-source:** when replace is ON and the output is renamed, the
   `.subs-work` dir (named off the original `.subs.mp4` output path) must still be removed
   after the rename — confirm the cleanup runs against the right pre/post-replace path.

## Related (same next-build backlog)

- `hermes-followup-v0.4.18-srt-sidecar-suppression.md` (optional `.srt` suppression)
- `hermes-followup-v0.4.18-ui-title-position.md` (raise QGroupBox titles; Karen F-1 exhaustiveness test)
