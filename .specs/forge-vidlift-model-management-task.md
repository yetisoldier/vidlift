# Implementation Task — VidLift model management + hermeticity (Forge)

You are **Forge**, Senior Software Engineer. Implement Bob's transcription-model-management
design, plus one test-hygiene fix. Stay strictly inside the repo (no `~/.hermes`, no
`~/.vidlift` access — external_directory is denied; use `tmp_path` fixtures for state dirs).

## Repo

`/home/yetisoldier/projects/vidlift` — Eric's Windows AI-upscale + subtitle tool.

## Read first (authoritative)

- `.specs/bob-vidlift-model-management-implementation-spec.md` — exact files, functions,
  order, acceptance criteria.
- `.specs/bob-vidlift-model-management-architecture-spec.md` — design + ADRs + risks.

## Interpreter (CRITICAL)

`/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3, pytest 9.1.1). Never system python.

    cd /home/yetisoldier/projects/vidlift && ~/.venvs/vidlift/bin/python -m pytest -q

## Scope

### A. Model management (per Bob's spec — the four additive moves)

1. **Open the fetch seam:** `fetch(url, target_path, on_progress=None)` — optional third
   positional `on_progress(done_bytes, total_bytes_or_None)`, invoked per chunk by the
   default fetcher. Thread `fetch → _download_file → resolve_model_dir →
   transcribe_source_to_srt → run_subs_job` (the last hop's params already exist in
   `subs_job.py:113-115`, merely forwarded today). **Conditional-arity discipline** so every
   existing 2-arg test fake keeps passing untouched.
2. **Publish into the heartbeat:** in-run publishers record
   `progress["model_download"] = {"bytes_done", "bytes_total"}` and **touch the live
   state/record per chunk** so `activity_age_seconds` stops climbing (this is the fix for
   Eric's 17-min silent-stall symptom).
3. **UI indicator + Install:** a title-stack pill beside the monitor pill —
   `Subs model (medium): installed / not installed — Install` — plus an explicit Install
   button. Install runs on a `QThreadPool`/`QRunnable` (the GPU-meter pattern) calling
   `resolve_model_dir`; NEVER the serial JobExecutor. No new MCP tool / endpoint (Hermes
   stays structurally read-only).
4. **Surfaced state:** the model state (installed / not-installed / downloading / error,
   honest) lands in the additive `_status_payload` keys. Never bump `api_version`.

Follow Bob's lockfile design (per-tag O_EXCL + mtime heartbeat + 30-min stale reclaim,
degrade-open on unwritable dirs) for download concurrency. No install-cancel button in v1.

### B. Hermeticity fix (test hygiene)

The discovery-card path currently writes the REAL `~/.vidlift/monitor.json` during the
suite (observed mtime mid-run). Fix the discovery-card tests (and any monitor/GUI test
that triggers `_write_monitor_discovery`) to use an isolated state dir (a `tmp_path`
fixture / monkeypatched state-dir resolution), never the real `~/.vidlift/`. Verify with
`git status` + a state-dir-mtime check that the suite no longer touches `~/.vidlift/`.

## Hard rules

1. Read-only Hermes surface (install is GUI-only; Hermes observes model state only).
2. No security regression to the monitor `/status` token gate.
3. Cross-platform honesty (Windows release target; tests on Linux + windows-latest).
4. Follow Bob's spec, don't redesign; escalate ambiguity.

## Verify before finishing

- FULL suite green with the venv interpreter; report exact pass/fail/skip counts.
- Confirm no test writes to `~/.vidlift/` (state-dir isolation holds).

## Report (final text)

- Files changed (one-line each).
- The new test(s): model-state detection, download-progress publication (activity-age stops
  climbing), install action, conditional-arity seam, and state-dir hermeticity.
- Test counts (before/after).
- Any spec deviation or ambiguity.
