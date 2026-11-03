# Validation Task — VidLift v0.4.16 full change set (Karen)

You are **Karen**, adversarial Principal QA Validator. Validate the ENTIRE uncommitted VidLift
change set for the v0.4.16 release and give a release-gate verdict. You are read-only: do NOT
modify any tracked file or any source/test (evidence under `.karen/` is allowed).

## Repo / interpreter

`/home/yetisoldier/projects/vidlift`; interpreter `/home/yetisoldier/.venvs/vidlift/bin/python`
(3.12.3, pytest 9.1.1). Never system Python (3.14 hard-crashes the Qt suite).

    cd /home/yetisoldier/projects/vidlift && ~/.venvs/vidlift/bin/python -m pytest -q

## What was implemented (four workstreams, one release)

1. **Base visibility** — monitor discovery card (`<state_dir>/monitor.json`, atomic,
   carries effective token + actual port + `token_source`) + additive `transcription` block
   in `_status_payload`. Specs: `.specs/bob-vidlift-visibility-{architecture,implementation,validation}-spec.md`.
2. **Model management** — fetch seam opened with conditional-arity `on_progress`,
   `progress.model_download` published into the heartbeat, UI model-state pill + Install
   button (QThreadPool QRunnable), additive status keys. Specs:
   `.specs/bob-vidlift-model-management-{architecture,implementation,validation}-spec.md`.
3. **Cancel fix** — `cancel current`/`cancel batch` now propagate the state transition to
   the registry + UI. No Bob spec; validate adversarially. Task:
   `.specs/forge-vidlift-cancel-fix-task.md`.
4. **Hermeticity** — discovery-card tests use an isolated state dir; the suite must not
   touch the real `~/.vidlift/`.

## Validation focus (adversarial)

- **Run the full suite** with the venv interpreter; confirm 895 passed / 2 skipped and
  repeat for determinism.
- **Security of the discovery card:** verify `monitor.json` carries only non-secret-safe
  fields (token is the EFFECTIVE monitor token — confirm it's not the app's other secrets;
  `token_source` is honest; atomic write; file perms). Confirm the monitor `/status` remains
  Bearer-token-gated and the empty-token → open-bind canary exists.
- **Model management correctness:** verify detection (installed/not via sha256 pin), the
  conditional-arity `on_progress` does NOT break existing 2-arg fakes, download progress
  stops `activity_age_seconds` from climbing, the Install action runs off the serial
  JobExecutor, and the status keys are additive (no `api_version` bump).
- **Cancel fix:** adversarially test cancel-current stops a running job and updates state;
  cancel-batch cancels queued + running; the UI tick reflects the transition; no
  running/cancelled race. Probe the two documented bounded windows (cancel during model
  download; cross-process cancel) and confirm they degrade safely rather than hang.
- **Hermeticity:** prove the suite leaves `~/.vidlift/` byte-identical.
- **Cross-platform honesty:** no Windows-only assumptions that would break windows-latest CI.

## Deliverable

`.karen/` evidence + a `.specs/karen-vidlift-v0.4.16-validation-report.md` with:
- Verdict (PASS / WARN with fixes / FAIL) + risk/confidence.
- Test counts (deterministic, repeated).
- Every defect found, severity (Critical/High/Medium/Low), and the falsification attempt.
- Explicit statement of whether the release gate is met.

Do not modify tracked files; escalate any Critical/High you cannot resolve to the report.
