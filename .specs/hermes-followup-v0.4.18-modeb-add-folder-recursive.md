# Follow-up: "Add Folder" (recursive) for Add Subtitles (Mode B)

**Author:** Hermes (orchestrator) — **Date:** 2026-11-04
**Source:** Eric (2026-11-04): wants folder-add for subtitle transcription, mirroring the
upscale "Add Folder", including nested/recursive traversal so a whole show (all seasons) can
be queued at once. Outputs must land back in each source's original folder.
**Severity:** feature request — fold into the next build/release.

## Current state (verified in-tree)

- **Mode B "Add Subtitles…" is file-only** — `choose_subs_files` uses
  `QFileDialog.getOpenFileNames` (`gui.py:1683`). There is no folder picker.
- **Mode A "Add Folder" is NON-recursive** — `add_folder` uses `Path(folder).iterdir()`
  (`gui.py:1558-1562`), which reads only the folder's immediate children, not subdirectories.
  (So even the upscale side doesn't currently do nested traversal.)
- **Outputs already land beside each source** in both modes (`subs_output_path` derives from
  `os.path.dirname(source_path)`), so the "back to the original folder" requirement is
  structurally satisfied — the folder-add just must NOT flatten the structure. A nested add
  naturally produces `season 1/episode.mp4` → `season 1/episode.subs.mp4` (or, with
  replace-source, `season 1/episode.mp4`).

## The feature

1. **Mode B "Add Folder" button** beside "Add Subtitles…", mirroring Mode A's
   `add_folder`: a directory picker that collects recognized video files.
2. **Recursive traversal** — walk nested subdirectories (e.g. `os.walk` / `Path.rglob`) so a
   whole show's folder tree queues at once. Decide whether recursion is always-on or a
   checkbox ("include subfolders") — recommend a toggle, default ON, since Eric's stated use
   case is "a whole show at once."
3. **Preserve the source directory structure** — outputs go beside each source (already the
   default); the folder-add must not relocate or flatten. No change to `subs_output_path`.
4. **`MAX_FILES` guard** applies to the recursive total (mirror `gui.py:1563-1569`), and the
   warning should name the count so a huge tree fails fast rather than silently truncating.

## Open decisions for Bob

1. **Recursion scope.** Upgrade only Mode B, or also make Mode A's existing `add_folder`
   recursive for parity (Eric's "even nested folders" reads as applying to the new feature,
   but the same helper would serve both)? Recommend a shared recursive collector used by both
   buttons, with the toggle surfaced on both.
2. **Toggle default.** Recursion ON vs OFF by default, and whether it's a persistent QSettings
   preference.
3. **Duplicate/symlink handling** in a recursive walk (symlinked dirs could loop; dedupe by
   resolved path).

## Related (same next-build backlog)

- `hermes-followup-v0.4.18-srt-sidecar-suppression.md` (both-mode sidecar suppression)
- `hermes-followup-v0.4.18-cleanup-scratch-on-success.md` (Mode B `.subs-work` cleanup)
- `hermes-followup-v0.4.18-ui-title-position.md` (title position + Karen F-1 test)
