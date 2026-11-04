# Follow-up (next build): treat "already has subtitles" as a SKIP, not a FAIL

**Author:** Hermes (orchestrator) — **Date:** 2026-11-04
**Source:** Eric, v0.4.18.0 batch run (2026-11-04): re-adding a folder re-queued already-subtitled
files, which surfaced as red "failed" rows. Two S03E01 "35 Hours" entries failed — one
"already has a usable text subtitle track", one "output already exists (never overwritten)".
**Severity:** UX/quality-of-life — fold into the next build.

## The problem

The Mode B "already has a usable text subtitle track" guard and the "output already exists"
guard both raise `SubsJobError` **at run time**, so a file that is genuinely already done shows
as a red **failure** in the batch — alarming, when the correct behavior is "nothing to do,
skip."

Compounding it: the recursive collector (`collect_video_files`) filters only by extension, so it
re-queues files that already carry a text subtitle track — including VidLift's OWN outputs
(both the old `.subs.mp4` keep-original form AND the `.mp4` form produced by replace-source,
which renames away the `.subs` infix but is still a `.mp4` the walk picks up).

Eric's rename point (2026-11-04): replace-source eliminates the `.subs.mp4` leftovers going
forward, but the renamed `.mp4` is still re-queued and still fails the same guard — so the
real fix must be "already has subtitles → skip", not a filename carve-out.

## The fix (two layers, one goal)

1. **Plan-time "already has subtitles → skip" (the load-bearing fix).** At the pre-flight/plan
   stage (GUI `_subs_file_rows` + the equivalent CLI/MCP pre-check), probe the source and, when
   it already has a usable embedded text subtitle track, mark the row **"skip — already has
   subtitles"** instead of enqueuing it to fail at run time. The `source_has_usable_subtitles`
   predicate already exists (`subs_job.py`) — surface it earlier. `--force` remains the override
   for a genuine re-transcription.
2. **Collector fast-path (belt-and-suspenders).** `collect_video_files` may additionally exclude
   `*.subs.mp4` (VidLift's own keep-original output) so re-adding a folder never re-queues its own
   results. Do NOT treat the `.subs.mp4` exclusion as sufficient on its own — the plan-time skip
   is what also catches the renamed `.mp4` and externally-subtitled files.

## Open decisions for Bob

1. **Where the skip verdict lives.** Reuse `source_has_usable_subtitles` at plan time, or a
   lighter probe? (The predicate needs a `probe`/`classify` result — confirm the plan path has it
   cheaply, or add a targeted probe.)
2. **Distinct status vs. existing "blocked".** A new "skip (already has subtitles)" status cell
   vs. folding into the existing "blocked — output already exists" wording. Recommend distinct,
   so "already done" and "would overwrite" read as different things.
3. **Batch summary.** Skipped rows should count as "skipped" in the batch summary, not inflate the
   failed count.

## Related (same next-build backlog)

- `hermes-followup-v0.4.19-test-coverage.md` (F-1 path-guard test, F-7 100-file pin, D-2 Bob's-eyes)
