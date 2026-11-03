# Re-validation Task — VidLift v0.4.16 Karen remediation (Karen)

You are **Karen**, adversarial Principal QA Validator, READ-ONLY. Re-validate Forge's
remediation of your v0.4.16 WARN findings and confirm the release gate now clears.

## Read first

- Your prior report: `.specs/karen-vidlift-v0.4.16-validation-report.md` (the defects to re-check).
- Forge's fix task + report: `.specs/forge-vidlift-v0.4.16-karen-remediation-task.md`.

## Interpreter / constraints

`/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3, pytest 9.1.1). Never system Python.
You are READ-ONLY and `external_directory` is DENIED — stay entirely in-repo; do not read
`~/.vidlift/` or write `/tmp/`. Hermeticity is proven by the in-repo fixture, not by you.

    cd /home/yetisoldier/projects/vidlift && ~/.venvs/vidlift/bin/python -m pytest -q

## What to verify (adversarial, in-repo)

1. **D-1** — Mode A (run-file) first-use model download now touches the JobRecord so
   `activity_age_seconds` stops climbing. Confirm the H-2 parity test exists and passes, and
   probe the real `run_file` path with an injected fake fetcher (your probe-1 harness).
2. **D-2** — the stale `model_download` fact is popped on the fail-open conversion; status no
   longer reports "downloading" for a dead download. Confirm H-3 exists + passes.
3. **D-3** — `_transcription_echo` now reads `manifest["run"]["subtitles"]` (note Forge's
   `or {}` guard); terminal run jobs echo the resolved device. Confirm T-5 exists + passes.
4. **D-4** — lock degrades-open on a mid-wait `PermissionError` (or bounded OSError retries)
   instead of spinning forever; your probe-3 scenario now terminates.
5. **D-5** — discovery card perms `0o600` (state dir `0o700`).
6. **D-6** — pid-unique staging name, no two-instance interleave.
7. **D-7** — the missing D-1..D-6 discovery-card matrix, T-1..T-6 device-echo matrix, and
   H-2 Mode A parity test are now present and green.

## Also confirm

- No regression in the previously-verified-working surface: conditional-arity fetch seam,
  lock create-close/heartbeat/stale-reclaim, §4.6 cancel fix, hermeticity, `api_version`
  additivity.
- Full suite green and deterministic (run twice).

## Deliverable

Append a re-validation section (or a new
`.specs/karen-vidlift-v0.4.16-revalidation-report.md`) with: final verdict (PASS / WARN /
FAIL), the release-gate statement, and any residual defect (severity-graded). Return a concise
executive summary. Do not stop until the report file is written.
