# Hermes Brief — v0.4.18: follow-up batch (recursive folder-add, sidecar suppression, scratch cleanup, UI polish)

**Author:** Hermes (orchestrator)
**Date:** 2026-11-04
**Base:** v0.4.17.0 is RELEASED (tag `v0.4.17.0`, both forges). This batch is a NEW release, v0.4.18.0.
**Audience:** Bob (design). Eric has approved this batch as a new build.

## Scope (four workstreams, one release)

### WS1 — Recursive "Add Folder" for BOTH modes
Eric wants folder-add for Add Subtitles (Mode B) mirroring upscale, with NESTED/recursive
traversal, AND wants the existing upscale "Add Folder" made recursive too (confirmed 2026-11-04).
Detail: `.specs/hermes-followup-v0.4.18-modeb-add-folder-recursive.md`.
- Mode B currently file-only (`choose_subs_files` → `getOpenFileNames`, gui.py:1683).
- Mode A `add_folder` is non-recursive (`Path(folder).iterdir()`, gui.py:1558).
- **Eric's confirmed decisions:** recursion on BOTH modes; outputs stay beside each source
  (preserve nested structure — already the default, just don't flatten); `MAX_FILES` guard on
  the recursive total; shared recursive collector preferred; watch symlink loops.

### WS2 — Sidecar `.srt` suppression (both modes, all subtitle branches)
Eric: never burn-in (already true), subtitles muxed toggleable into the MP4 (already true), and
a NEW opt-in to suppress the redundant `.srt` sidecar.
Detail: `.specs/hermes-followup-v0.4.18-srt-sidecar-suppression.md`.
- Covers Mode B (`{stem}.subs.srt`) AND Mode A (every subtitle branch: transcription, OCR,
  adopted sidecar — Mode A currently KEEPS the sidecar by design, run.py:2802 "KEEP disposition").
- On mux failure the `.srt` must remain as the fallback deliverable (never leave the user empty).
- Interaction with replace-source (no sidecar to rename when suppression is ON).

### WS3 — Scratch cleanup on success
Eric: after a successful run, remove leftover scratch files/folders.
Detail: `.specs/hermes-followup-v0.4.18-cleanup-scratch-on-success.md`.
- Mode A already cleans up (`cleanup_run_artifacts`). The gap is Mode B's `<output>.subs-work/`
  directory (Karen K-5). Success-only, best-effort, path-guarded.

### WS4 — UI polish (title position + Karen F-1 test)
Detail: `.specs/hermes-followup-v0.4.18-ui-title-position.md`.
- Raise the `QGroupBox` titles ("Upscale" / "Add subtitles (no upscale)") slightly off the border
  (Eric's Windows screenshot feedback; tune on Segoe UI).
- Add Karen F-1's exhaustiveness assertion (Mode B group holds exactly its three controls).

## Version
Bump to **0.4.18.0** — all four `packaging/version_info.txt` values (strings + tuples) plus
`__init__.py` and `pyproject.toml`, CHANGELOG `[0.4.18.0]` section. (Forge: do NOT double-bump;
this is a single release covering all four workstreams.)

## Invariants
- `vidlift/run.py` (Mode A engine) byte-identical unless a workstream deliberately and
  justifiably touches it — flag any such change for Eric. (WS2 touches Mode A *subtitle
  handling* in run.py by necessity — enumerate those touches explicitly.)
- `api_version` stays 1; new options are additive absent-defaults (the `replace_source` precedent).
- New QSettings keys follow the house pattern; no renames/migration of existing keys.

## Deliverable
Write `.specs/bob-vidlift-v0.4.18-implementation-spec.md` with the same rigor as prior specs:
per-workstream change table (file:line, bindings, exact signatures, LoC), exact strings/tooltips,
a combined test plan, and a Karen validation baseline. Answer every open decision in the four
follow-up notes. State deviations explicitly.
