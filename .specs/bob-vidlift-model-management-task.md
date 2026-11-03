# Architecture Task — VidLift transcription-model management (Bob)

You are **Bob**, Principal Software Architect. Design the transcription-model management
feature for VidLift, to ship in the NEXT build alongside the visibility work already
specced.

## Context / the problem this solves

Eric ran a Mode B "add subtitles" (faster-whisper transcription) run and watched it sit
with **no UI signal** for ~17 minutes while the model was (almost certainly) downloading
silently — the job showed `current_phase=transcribe`, `progress.step=transcribe-language`,
and `activity_age_seconds` climbing with zero indication of a download. He had no way to
know whether the model was present, downloading, or missing, and no way to install it.

## Requirement (Eric's words, consolidated)

1. **Detect** — an explicit UI indication of whether the transcription model is present
   (installed) or not.
2. **Progress** — while the model is downloading, show real progress, so a long fetch is
   never mistaken for a hang.
3. **Install** — if the model is NOT present, an explicit in-UI way to fetch/install it
   (not merely a "missing" message).
4. This state must also surface in the status payload (the per-job `_status_payload` the
   monitor `/status` serves), so Hermes can report it remotely.

## Read first

- `/home/yetisoldier/projects/vidlift/vidlift/transcribe.py` — the transcription core:
  `DEFAULT_MODEL = "medium"`, `KNOWN_MODELS = ("small","medium","large-v3")`,
  `MODEL_SHA256` (sha256-pinned model.bin), `model_cache_dir`, `model_download_url_for`,
  and the injectable `fetch` seam ("Every model download goes through the injectable
  ``fetch`` seam"). Understand exactly how a model is detected as present (file + sha256
  at the cache path) and how download + progress currently work (or don't).
- `.specs/bob-vidlift-visibility-architecture-spec.md` + `-implementation-spec.md` — the
  visibility work already specced (discovery card + additive `transcription` block). The
  model-management state should land in the SAME additive-key convention (never bump
  `api_version`).
- The GUI (`vidlift/gui.py`, and the Setup/status surfaces) to decide where the indicator
  and install action belong.

## Design decisions to make (with ADRs)

- **Detection:** how "present" is defined (file existence vs full sha256 verification vs
  a cheaper existence-only check for the UI, with sha256 re-check before use). Reuse the
  existing `MODEL_SHA256` pins.
- **State model:** a small model-state enum surfaced in both the UI and the status payload
  — e.g. `installed` / `not-installed` / `downloading` (+ progress fraction/bytes when
  downloading) / `unknown` / `error`. Honest-null where unknown.
- **Install action:** where it lives in the UI (Setup screen vs a status row vs the Plan
  view), what it triggers (the existing `fetch` seam / `model_download_url_for`), and how
  it reports progress + completion/failure. No new dependency; reuse the fetch seam.
- **Progress plumbing:** how download progress is threaded to the UI and the status
  payload (the fetch seam may not currently expose progress — design the minimal change).
- **Remote surfacing:** the additive `transcription` block (or a sibling `model` block) in
  `_status_payload` so Hermes sees installed/downloading/progress/missing.

## Constraints

- No new dependencies. Reuse `fetch`, `model_cache_dir`, `MODEL_SHA256`.
- Cross-platform honesty (Windows release target; tests on Linux + windows-latest).
- Additive status keys only; never bump `api_version`.
- Read-only Hermes surface: Hermes must not be able to trigger an install (install is a
  GUI action), only observe model state.

## Deliverables (write under `.specs/` in the vidlift repo)

1. `bob-vidlift-model-management-architecture-spec.md` — options, ADRs, state model, UI
   placement, data flow, security.
2. `bob-vidlift-model-management-implementation-spec.md` — for Forge: exact files,
   functions, the state enum, the UI wiring, the fetch/progress plumbing, acceptance
   criteria.
3. `bob-vidlift-model-management-validation-spec.md` — for Karen: how to prove
   detect/progress/install all work and surface correctly, with a fake `fetch` seam.

Return a concise executive summary (recommended state model + UI placement, the install
action design, any risk for Eric). You may read anything in the repo; write only your three
spec files.
