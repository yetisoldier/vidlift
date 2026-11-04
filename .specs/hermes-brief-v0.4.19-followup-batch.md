# Hermes Brief — v0.4.19: cancel-fix + skip + device indicator + test coverage

**Author:** Hermes (orchestrator)
**Date:** 2026-11-04
**Base:** v0.4.18.0 is RELEASED (both forges). This is a new release, v0.4.19.0.
**Audience:** Bob (design). Eric approved the full batch ("have Bob work on all the items").

## Scope (four workstreams, one release)

### WS1 — HIGH: cancel must be observed during pre-transcription steps
Detail: `.specs/hermes-followup-v0.4.19-cancel-pre-transcribe.md`.
Eric hit this live: a job wedged at `transcribe-language` (detect_language hang after the GPU
was freed mid-load) could NOT be cancelled — `should_cancel` is only read inside `_on_segment`
(`transcribe.py:2429`) and the post-mux tail, never during `transcribe-resolve` (model load) or
`transcribe-language` (detect_language). Fix: add `should_cancel` observes to the
pre-transcription steps (before/around detect_language and model resolution) raising
`EngineCancelled`; confirm both modes + the executor's `EngineCancelled → cancelled` mapping.

### WS2 — plan-time "already has subtitles" → SKIP, not FAIL
Detail: `.specs/hermes-followup-v0.4.19-already-subtitled-skip.md`.
`source_has_usable_subtitles` and "output already exists" currently raise `SubsJobError` at RUN
time, so already-done files show red "failed". Move the check to PLAN/pre-flight time and mark
them "skip — already has subtitles" (distinct from "blocked — output exists"). Collector
fast-path: exclude `*.subs.mp4` (VidLift's own outputs). `--force` remains the override.

### WS3 — CPU/GPU device indicator in the UI
Detail: `.specs/hermes-followup-v0.4.19-ui-device-indicator.md`.
Surface the resolved transcription device (GPU/CUDA vs CPU) on the in-progress job. Key nuance:
`device` is `None` until transcription actually starts (during `transcribe-language`/model load),
so show "resolving…" (or the requested value) before then, and surface an honest "CPU (GPU
unavailable)" when `auto` falls back. Both modes.

### WS4 — test-coverage gaps from Karen's v0.4.18 validation
Detail: `.specs/hermes-followup-v0.4.19-test-coverage.md`.
- F-1: add a stale-`.subs-work`-with-custom-workdir test (path-guard coverage).
- F-7: pin the 100-file Mode B enqueue acceptance.
- D-2: Bob's decision on the `test_v0417_failpath_fix.py` double — conftest-shared vs per-file
  defaulted-kwarg (resolve deliberately, not as a one-off ripple).

## Version
Bump to **0.4.19.0** — all four `packaging/version_info.txt` values (strings + tuples) plus
`__init__.py` and `pyproject.toml`, CHANGELOG `[0.4.19.0]` section. (Forge: no double-bump.)

## Invariants
- `api_version` stays 1; new options additive absent-defaults.
- `vidlift/run.py` (Mode A) byte-identical unless a workstream deliberately touches it — WS1
  touches Mode A's transcription path (`transcribe.py` and/or `run.py`'s transcription call) —
  enumerate any run.py touch explicitly.
- No new QSettings keys unless a workstream requires one; no renames/migration.

## Deliverable
Write `.specs/bob-vidlift-v0.4.19-implementation-spec.md` with the usual rigor: per-workstream
change table (file:line, bindings, exact signatures, LoC), exact strings, a combined test plan,
and a Karen validation baseline. Answer every open decision in the four notes. State deviations
explicitly.
