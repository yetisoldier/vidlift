# Follow-up: optional suppression of the `.srt` sidecar (soft-mux only, never burn-in)

**Author:** Hermes (orchestrator) — **Date:** 2026-11-04
**Source:** Eric (2026-11-04), clarifying the subtitle-output contract.
**Severity:** feature request — fold into the next build/release.

## Eric's standing requirement (normative)

1. **NEVER burn subtitles into the video pixels.** (Already true — Mode B soft-muxes a
   `mov_text` track; the burn-in path was permanently removed in v0.4.15.)
2. **Subtitles MUST be encoded into the MP4 as a toggleable track** (soft-mux), so they can
   be switched on/off in the player. (Already true.)
3. **NEW — a way to turn OFF the `.srt` sidecar.** When the subtitle mux into the MP4
   succeeded, the sidecar is redundant, so Eric wants an option to not write it.

## The feature

An opt-in "suppress `.srt` sidecar" switch on all three surfaces (GUI checkbox + CLI flag +
MCP option), consistent with the `replace_source` precedent (absent-defaults everywhere,
`api_version` unchanged).

- Applies to **Mode B** (Add Subtitles) and, for symmetry, **Mode A** transcription
  (`transcribe_subs` writes a sidecar too).
- Interaction with **replace-source**: when sidecar suppression is ON, there is no sidecar
  to rename on a replace — the replace helper's sidecar step becomes a no-op. Confirm the
  ordering/guard handles this cleanly.

## Open decisions for Bob

1. **Default value.** Current behavior always writes the sidecar. Eric considers it
   redundant, so a defensible default is **suppress (no sidecar)**; the alternative is
   keep-writing-by-default with the toggle opt-in. Recommend suppress-by-default for Mode B
   (the sidecar is only useful if the mux fails or the user wants the raw text), but let
   Bob weigh backward-compat vs. Eric's stated preference.
2. **Naming.** Mirror the `replace_source` / `transcribe_subs` QSettings-key pattern
   (e.g. `write_srt_sidecar` default True, or `suppress_srt` default False — pick the
   polarity that makes the default obvious).
3. **Failure surface.** If the mux fails, the `.srt` is the only deliverable — the
   suppression flag must NOT leave the user with nothing on a mux failure (keep the
   sidecar as the failure artifact regardless of the flag).

## Supersedes

This corrects the earlier "sidecar-vs-burned `.srt` toggle" wording in the brief and
CHANGELOG — Eric does NOT want a burned-in option at all. The real ask is sidecar
suppression, captured here.
