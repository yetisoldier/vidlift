# VidLift UI

The UI is a focused batch planning workbench. It exists to make the pre-flight decision fast: which files are worth running, how big the outputs will be, and which warnings matter.

## Layout

- Top summary: queued/ready count and total planned size.
- Control strip: add files, add a folder, clear, set GB/hour, choose output folder, plan batch.
- Batch table: verdict, file, source class, target geometry, estimated size, subtitle note, and warnings.
- Details pane: selected-file explanation in plain text for easy copying.

## Supported Input

The UI accepts file picker selection, folder selection, and drag/drop. Folder adds include common video extensions only.

## Design Intent

The interface is intentionally quiet and operational. VidLift is an hours-long batch tool, so the UI prioritizes scanning, comparison, and trustworthy warnings over decoration.

## Not Yet In The UI

- Start/Cancel encoding controls
- Sample benchmark timing
- Per-file progress
- Preset management

Those belong with the encode runner rather than the planner-only release.
