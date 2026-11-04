# Hermes Findings — v0.4.17 follow-up: upscale panel subtitle-control UX

**Author:** Hermes (orchestrator)
**Date:** 2026-11-04
**Related:** hermes-brief-v0.4.17-modeb-replace-source.md

## Finding 1 — "Add subtitles by transcription" checkbox is never rendered

The Mode A transcription toggle exists in code but is never placed in the layout, so it is
invisible and effectively locked ON.

Evidence:
- `vidlift/gui.py:643` — `self.transcribe_subs = QCheckBox("Add subtitles by transcription when none exist")`,
  wired to `_transcribe_subs_changed` (line 1192), default `True` (line 646-650).
- Its state IS read at `gui.py:1760`, `1792`, `2174` (`transcription_enabled=self.transcribe_subs.isChecked()`).
- The settings grid (`gui.py:1290-1328`) adds `subtitle_ocr`, `replace_source`, `resume_claim`,
  `add_subs_button`, and the denoise combo — but there is **no `grid.addWidget(self.transcribe_subs, ...)`**
  anywhere in the file (grep for `transcribe_subs` shows only creation + the toggle handler, no placement).

Net effect: the user cannot see or change the transcription default; transcription fires on every
upscale whose file has no subtitle track and has audio (`plan.py:272`).

## Finding 2 — upscale-panel subtitle controls are incoherent

What the upscale panel surfaces today, and what each actually does:

- **"Enable subtitle OCR"** — visible; applies only to bitmap (PGS/VobSub) tracks.
- **"Add subtitles by transcription when none exist"** — INVISIBLE (Finding 1); applies only when no
  track exists at all.
- **"Replace original on success (deletes the source video)"** — visible; applies ONLY to Mode A
  upscale (`run.py:2964`), silently ignored by Mode B ("Add Subtitles…"), which is source-blind
  (`subs_job.py`). This mismatch is the exact confusion Eric hit on 2026-11-04.
- **"Add Subtitles…"** — a separate button launching Mode B, which has its own surface with a
  hard-coded "keep original" column and none of the upscale-panel controls.

## Recommended design direction (for Bob's redesign task)

Separate the upscale surface from the subtitle surface so each shows only its own controls:

1. Upscale panel: engine, profile, denoise, GB/hour, replace-original (upscale), and the subtitle
   *carry* policy (OCR + transcribe) — with the transcribe toggle actually RENDERED and its
   always-on default made visible/controllable.
2. Subtitles panel (Mode B): its own "replace original on success" (per the v0.4.17 brief), its own
   model/language controls, and no leakage of upscale-only options.
3. Move "Replace original on success" into whichever surface(s) actually consume it, so a control is
   never shown where it is ignored.

This is a distinct piece of work from the replace-source point-fix; queue as its own Bob design task
after the current feature lands (or fold into the same release at Bob's judgment — do not widen the
current Forge/Karen cycle).
