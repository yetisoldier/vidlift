# Release plan — v0.4.17 combined (Eric's directive)

**Author:** Hermes (orchestrator) — **Date:** 2026-11-04
**Directive (Eric, 2026-11-04):** "after Karen's validation, don't do a release, start the UI
work right away and only release when that is also done."

## Scope of the combined release (ONE release, two workstreams)

1. **Mode B replace-source** (in flight): opt-in "replace original on success" for Add
   Subtitles — checkbox (GUI) + `--replace-source` (CLI) + `replace_source` (MCP); delete
   source and rename output to `{stem}.mp4` (never the old extension); sidecar → `{stem}.srt`;
   collision/cancel/failure keep-both guards. Spec: `bob-vidlift-v0.4.17-implementation-spec.md`;
   impl: Forge (report `forge-vidlift-v0.4.17-fix-report.md`), pending Karen validation.
2. **UI restructure** (queued, next): see `hermes-findings-v0.4.17-subtitle-ux.md` — separate the
   upscale surface from the subtitle-only (Mode B) surface so each shows only its own controls;
   render the currently-invisible "Add subtitles by transcription" checkbox (Finding 1) and make
   its always-on default visible/controllable; stop showing Mode A's "replace original" where it
   is ignored.

## Sequencing (auto-flow, no Eric gate between stages)

1. Karen validates workstream 1 (receives Bob's full spec per the 2026-11-04 process rule).
   - Karen recycle loop up to 5 cycles → Bob (redesign) or Forge (fix).
2. **NO release. NO commit of a release tag.** Workstream 1 stays uncommitted-in-tree (or a
   checkpoint commit is fine; just no release cut).
3. Bob designs the UI restructure (new spec, feeding off `hermes-findings-v0.4.17-subtitle-ux.md`
   and the current GUI state incl. the new `subs_replace_source` checkbox at (2,8)).
4. Forge implements; Karen validates (full Bob spec supplied).
5. Quill docs pass (mandatory) covering BOTH workstreams.
6. **Single combined release** — one version bump (0.4.17.0 already staged in the tree by
   workstream 1; Bob/Forge must NOT double-bump — confirm the final version covers both), commit,
   push to GitHub AND Gitea, cut the release, hash-verify the published artifact.

## Invariants

- The version is already `0.4.17.0` in the working tree (uncommitted). Treat 0.4.17.0 as the
  combined-release version unless Bob argues for 0.5.0 (semantic-minor, since a UI restructure is
  user-visible) — that call is Bob's during the UI design, and if changed, Forge must restamp all
  four `packaging/version_info.txt` values (strings + tuples) plus `__init__.py` and `pyproject.toml`.
- Do NOT cut a release or push a release tag until workstream 2's Karen pass AND Quill docs are done.
- Both workstreams keep `vidlift/run.py` (Mode A) byte-identical unless the UI restructure explicitly
  and deliberately touches Mode A surface code — flag any such change to Eric.
