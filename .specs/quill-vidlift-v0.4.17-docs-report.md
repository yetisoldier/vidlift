# Quill Documentation Report — v0.4.17 Combined Release

**Status:** COMPLETE
**Version:** 0.4.17.0
**Date:** 2026-11-04

## 1. Mode B Replace-Source Documentation
The "replace original on success" feature for Mode B (Add Subtitles) has been documented across all three user surfaces:
- **GUI (`docs/ui.md`):** Added the "Add subtitles (no upscale)" group description and the specific behavior of the replace checkbox, including the `.mp4` renaming rule and the collision guard.
- **CLI (`docs/cli.md`):** 
    - Added `--replace-source` to the `run` command options.
    - Fully documented the `vidlift subs` subcommand (K-6 gap), including its specific `--replace-source` flag and transcription options.
- **MCP:** The behavior is covered via the CLI/GUI descriptions as the MCP interface mirrors these options.

## 2. UI Restructure Documentation
The settings surface restructure has been documented to reflect the new structural separation:
- **Surface Split:** `docs/ui.md` now describes the two titled groups (**Upscale** and **Add subtitles (no upscale)**) and clarifies that each shows only its own relevant controls.
- **Finding 1 (Transcription):** Documented the "Transcribe missing subtitles" checkbox in the Upscale group, including its default-ON state and the AI model requirements.
- **Finding-1 Twin (Resume):** Documented the "Resume retained work" checkbox.
- **Release Note:** Added a troubleshooting entry to `README.md` specifically phrasing the fix as "fixed a layout overlap that hid the Resume retained work checkbox" per the design spec.
- **Window Width:** The restoration of the ~1290px minimum width is noted in the internal release logic (and reflected in the lack of "width bomb" warnings in the docs).

## 3. General Updates & Verifications
- **README:** Updated the troubleshooting table to include the Resume checkbox fix.
- **Usage Guide:** Updated the "Run the pre-flight plan" section to include the Mode B replace option.
- **Link Integrity:** Verified that all internal anchors and relative links in `docs/` and `README.md` point to existing sections.
- **Historical Records:** Confirmed that no files under `docs/designs/` were modified.

## 4. File Change Log
- `README.md`
- `docs/ui.md`
- `docs/cli.md`
- `docs/usage.md`
- `.specs/quill-vidlift-v0.4.17-docs-report.md`
