# Follow-up (next build): show CPU vs GPU for the in-progress transcription job

**Author:** Hermes (orchestrator) — **Date:** 2026-11-04
**Source:** Eric (2026-11-04): wants the UI to show whether a running job is actively using CPU
or GPU. Motivated by the GPU-contention episode (LM Studio held the 3090 Ti, transcription fell
back to CPU and slowed ~5-10x, invisible until diagnosed via the monitor API).
**Severity:** feature/observability — fold into the next build.

## The data already exists

- The monitor `/status` payload already carries `transcription.device` and
  `transcription.device_requested` (the latter is `auto|cuda|cpu`).
- `SubsJobResult.device` already mirrors the transcription's resolved device fact
  (`""` when a foreign factory ran — honest unknown).
- The gap is purely UI: the in-progress detail/progress surface does not render this field, so
  Eric cannot tell CPU vs GPU at a glance.

## The feature

Surface the resolved transcription device on the in-progress job's detail/status surface, e.g.:

- **GPU (CUDA)** — when the engine resolved to CUDA.
- **CPU** — when it fell back to CPU.
- **auto — resolving…** (or the requested value) — while `device` is still `None`/empty (the
  pre-transcription phase, e.g. `transcribe-language`), because the device is only known once
  transcription actually starts.

## Nuances for Bob

1. **The device is resolved late.** During `transcribe-language` (detect_language) and model load,
   `device` is still `None` — the UI must show the *requested* value or a "resolving" state, not
   a misleading "CPU" before the fallback actually happened. (This is precisely why the
   contention was invisible: `device_requested: auto` stayed `auto` while the real device was
   silently CPU.)
2. **Surface the fallback honestly.** When `device_requested: auto` resolves to CPU (GPU
   unavailable/contended), the UI should say so — ideally a hint like "CPU (GPU unavailable)"
   rather than a bare "CPU", so Eric knows the speed hit is GPU contention, not a normal path.
3. **Both modes.** Mode B (subs) reports it via `SubsJobResult.device`; Mode A (upscale
   transcription) should surface the same fact from its transcription result. Confirm both
   surfaces.
4. **Also the queue/plan view?** At minimum the in-progress job; a queued job can only show the
   requested device, so keep queued rows as "auto" unless the design calls for more.

## Related (same next-build backlog)

- `hermes-followup-v0.4.19-already-subtitled-skip.md` (plan-time skip)
- `hermes-followup-v0.4.19-test-coverage.md` (F-1/F-7/D-2)
