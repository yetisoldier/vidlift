# Hermes Brief — v0.4.17: opt-in "replace original on success" for Mode B (Add Subtitles)

**Author:** Hermes (orchestrator)
**Date:** 2026-11-04
**Topic:** Mode B ignores the "replace original on success" preference; add a Mode B opt-in that deletes the source and renames the output to the source's stem with a `.mp4` extension.

## Observed behaviour (verbatim)

Eric ran "Add Subtitles" on `J:/Complete/The Adventures of Pete & Pete (1993) - S03E01 - 35 Hours.avi`
and reported:

> "I do need a change though, even though replace original on success was selected, it didn't remove the original copy"

Then, on the desired end-state:

> "Delete the original and rename the [output] to take the exact filename, but don't include the .avi (or any other old extension)"

The job itself succeeded (state `succeeded`, output `.../35 Hours.subs.mp4`, no error, v0.4.16.2).

## Live telemetry / evidence

- `/history` row for `vl-20261104-911b48ae`: `state=succeeded`, `output_path=J:/Complete\The Adventures of Pete & Pete (1993) - S03E01 - 35 Hours.subs.mp4`.
- The original `.../35 Hours.avi` remains on disk (never touched).

## Root cause (code facts, file:line)

This is a **design boundary, not a regression**. The "replace original on success" preference
(`replace_source`) is wired into **Mode A (upscale)** only:

- `vidlift/run.py:1910` — `replace_source: bool = False` is a parameter of `run_file` (the upscale pipeline).
- `vidlift/run.py:2964-2974` — the only caller of `replace_source_with_output()`; this is the Mode A success path.
- `vidlift/run.py:1683-1796` — `replace_source_with_output()` deletes the source and `os.replace`s the output over the source's exact path (so the output takes the source's filename verbatim, extension included).
- `vidlift/gui.py:605-618` — the checkbox lives in the upscale settings panel; its label/tooltip say "after a verified **upscale**" and "move the finished 720p MP4 into its original path".
- `vidlift/gui.py:661` — "Mode A upscale flow is the ONLY surface: Mode B (subs) has no prepare chain".

**Mode B ("Add Subtitles") is source-blind by design** and never consults `replace_source`:

- `vidlift/subs_job.py` — the whole module; docstring line 8: "never touching the source (source-blind safety)".
- `vidlift/subs_job.py:97-117` — `run_subs_job()` signature has **no** `replace_source` parameter.
- `vidlift/subs_job.py:70-77` — `subs_output_path()` returns `{stem}.subs.mp4` beside the source.
- `vidlift/gui.py:1528` — the Mode B row table hard-codes a `"keep original"` column; there is no replace toggle in the Mode B surface.

So the checkbox Eric checked (in the upscale panel) is simply not consumed by the job he ran.

## Desired behaviour (Eric's spec — normative)

Add an **opt-in** "replace original on success" to Mode B, defaulting OFF, mirroring Mode A's
guardrails (only on a verified success; a replace failure must never fail the job or destroy the
only good data; on failure/cancel keep BOTH files).

On a verified subs success with the option enabled:

1. **Delete the original source file** (e.g. `...35 Hours.avi`).
2. **Rename the output** from `{stem}.subs.mp4` to `{stem}.mp4` — the source's exact stem, but with a
   `.mp4` extension. **Never** carry over the old extension (`.avi`/`.mkv`/whatever).

Concrete: `The Adventures of Pete & Pete - S03E01 - 35 Hours.avi`
→ produces `...35 Hours.subs.mp4`
→ replace → deletes the `.avi`, leaves `...35 Hours.mp4`.

## Decisions Bob must make (enumerated)

1. **Sidecar `.srt` handling.** Mode B writes `{stem}.subs.srt` next to the output. On replace,
   should it be renamed to `{stem}.srt` to match the renamed mp4 (recommended, so the pair stays
   in sync), or left as `{stem}.subs.srt`? Eric did not specify; recommend rename-to-match and note
   it in the report.
2. **Target path collision.** `{stem}.mp4` may already exist as a *different* file when the source is
   e.g. `.avi` and a sibling `{stem}.mp4` is already present (or when the source itself is `.mp4` and
   `{stem}.mp4` == source). Define the guard: refuse (and keep both files, log a warning) rather than
   overwrite an unrelated file. The source-is-already-`.mp4` case: target == source path, so delete
   source then `os.replace(output, target)` re-creates the same name — confirm this is safe/intended.
3. **Reuse vs. re-implement the replace routine.** Mode A's `replace_source_with_output()` assumes the
   output moves onto the *source's exact path* (extension included). Mode B needs *rename to stem + `.mp4`*
   instead. Decide whether to (a) add a dedicated `replace_subs_output(source_path, output_path, ...)`
   with the same atomicity discipline (temp-sibling + SHA-256 + `os.replace`), or (b) generalise the
   existing helper. Keep the Mode A path byte-identical (don't regress upscale replace).
4. **Surface.** The Mode A checkbox is in the upscale panel and does not apply to Mode B. Mode B needs
   its own opt-in control in the "Add Subtitles…" surface (and it must be QSettings-persisted like the
   Mode A one), plus the CLI `vidlift subs` flag and the MCP `subs` job kind option.
   - CLI: mirror `--replace-source` on `vidlift subs`.
   - MCP: `vidlift/mcp_bridge/jobs.py:247` + `schemas.py:51` already carry `replace_source` for upscale;
     the `subs` job kind needs the same option plumbed through `run_subs_job`.
5. **Failure discipline.** Identical to Mode A: a replace failure is a logged warning that keeps the
   verified success (and keeps both files), never a job failure. Cancellation never replaces.
6. **Extension policy.** Confirm the rename is always to `.mp4` regardless of source container, and
   that this is surfaced in the tooltip/help text ("the output is renamed to the original name with an
   .mp4 extension; the original file is deleted").

## Constraints / invariants

- `api_version` stays constant; no new endpoints.
- The existing upscale (Mode A) replace behaviour is untouched.
- Mode B's source-blind default (option OFF) is unchanged — existing behaviour is the default.
- Never delete the source unless the subs remux has already verified (`succeeded` in the runner) and the
  output file exists on disk.

## Scope of this brief

Mode B replace-source feature only. Do not fold in the unrelated `.srt`-toggle request Eric raised
earlier (separate sidecar-vs-burned mode) — track it as a follow-up, not part of this release.
