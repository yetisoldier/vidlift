# Docs Task — VidLift v0.4.16.0 (Quill)

You are **Quill**, Technical Documentation Writer. Update the docs for the v0.4.16.0 release,
which is validated (Karen PASS) and ready to ship.

## Repo

`/home/yetisoldier/projects/vidlift`

## What shipped in v0.4.16.0 (write about these, accurately)

1. **Monitor visibility for Hermes** — a discovery card (`<state_dir>/monitor.json`, atomic,
   carries the EFFECTIVE monitor token + actual port + `token_source`) so Hermes can discover
   the current token; plus an additive `transcription` device block in the `/status` payload
   (device / device_requested). `api_version` stays 1; `/status` stays Bearer-token-gated.
2. **Transcription-model management** — the UI shows whether the faster-whisper model is
   installed (via sha256 pin) with an explicit **Install** button (runs off the serial job
   queue); real download progress is published into the heartbeat (fixes the silent
   `transcribe-language` stall — `activity_age_seconds` no longer climbs during a download);
   model state also surfaces in the status payload.
3. **Cancel fix** — `cancel current` / `cancel batch` now reliably stop the running job and
   reflect `cancelled` in the UI/registry/status (previously a cancelled job could keep
   showing as running).
4. **Honesty fixes** — Mode A (upscale) downloads now also touch the job record (parity with
   Mode B); a failed download no longer leaves a stale "downloading" fact; terminal run jobs
   correctly echo the resolved device.

## Read first

- The source: `vidlift/transcribe.py`, `run.py`, `gui.py`, `mcp_bridge/{tools,jobs,server}.py`,
  `monitor.py`, `ui/model_install.py`.
- The specs in `.specs/` (Bob's visibility + model-management specs, Karen's validation +
  re-validation reports) for exact behavior and the R-1 follow-up.
- Existing `README.md` and `CHANGELOG.md`.

## Deliverables

1. **README** — add a concise "Monitor / status visibility" + "Transcription model
   management" section (discovery card, `/status` device echo, install + progress). Keep it
   accurate; do not overstate (no install via Hermes — Hermes is read-only here).
2. **CHANGELOG** — a `[0.4.16.0]` entry. Forge already drafted one during remediation — verify
   every claim is accurate against the code, fix any overstatement, and add a short
   "Known issues" note for R-1 (lock stale-reclaim can spin on a read-only dir + stale lock;
   non-gating follow-up).
3. **Release notes** — `.specs/release-notes-v0.4.16.0.md` for the GitHub/Gitea release.

Do not modify source or tests. Return a concise summary of what you changed.
