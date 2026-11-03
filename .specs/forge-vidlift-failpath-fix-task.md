# Bug Fix Task — VidLift fail-path UI state + failure-reason surfacing (Forge)

You are **Forge**, Senior Software Engineer. Investigate and fix two related defects in the
fail path, then run the suite green.

## Repo / interpreter

`/home/yetisoldier/projects/vidlift`; `/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3,
pytest 9.1.1). Never system Python.

    cd /home/yetisoldier/projects/vidlift && ~/.venvs/vidlift/bin/python -m pytest -q

## The user's report (live, on v0.4.16.0)

Eric started a Mode-B "subtitles only" run on `The Adventures of Pete & Pete (1993) - S03E13
- Saturday.avi`. The job failed at the **"probe — reading source metadata"** phase within ~19s.
The monitor's registry is correct: `/history` shows job `vl-20261103-a2068452` **failed**, and
`/status` shows `active_jobs: []`. But the GUI is inconsistent:

- Batch summary + job table correctly show "failed" (0/1 succeeded, 1 failed).
- The **details panel** still shows "Verdict: running".
- The **"LIVE job" indicator** still shows "running — probe — reading source metadata".
- The **status bars are still animating**.

So the terminal `failed` transition propagated to the batch/table surfaces but NOT to the
details panel / live-job indicator / status bars.

## Defect 1 (primary) — fail→failed transition does not reach every UI surface

Trace how a job's terminal `failed` state flows from the registry (which is correct) into the
GUI's per-job details panel, the "live job" indicator, and the status-bar animation. Find
where the `failed` transition is dropped or not emitted for the early "probe" phase (a failure
during source-metadata reading may not publish the terminal state the details view listens
for, unlike the cancel path which v0.4.16.0 fixed). Fix it so a job that fails at ANY phase
(including probe) is shown `failed` (not `running`) everywhere, and the status bars stop.

Add a regression test: a Mode-B run whose probe/metadata step fails ends with the details view
+ live indicator + bars reflecting `failed`, not `running`.

## Defect 2 — the failure reason is not surfaced anywhere

`/history` payloads carry `state: "failed"` but no reason (`engine_note: null`, no error
field), and the GUI log-tail was empty. Neither Eric nor Hermes can see WHY the `.avi` probe
failed. Surface the failure reason minimally:

- Add an honest `error` / `failure_reason` field (or equivalent additive key) to the `/history`
  job record, populated from the existing exception text (e.g. `VideoToolError` /
  `SubsJobError` message — the `{phase} command failed (exit …)` detail already exists).
- Reflect the same reason in the GUI's failed-job details (the log-tail or the details panel).

Do NOT invent a new transport; use the existing monitor/history shape additively (do not bump
`api_version`). This is read-only visibility: Hermes must be able to SEE the reason, not act on
it.

## Constraints

- Localized; no redesign. Preserve the v0.4.16.0 cancel fix, model-management, discovery card,
  hermeticity, and `api_version` additivity.
- Cross-platform honesty (Windows release target; tests on Linux + windows-latest).

## Verify

- Full suite green with the venv interpreter; report exact counts (repeat once).
- Confirm the new fail-path regression test + the failure-reason surfacing test pass.

## Report (final text)

- Root cause of the fail-path propagation gap (one paragraph).
- Files changed + new tests (one line each).
- Test counts before/after.
- Whether you escalated any architectural concern.
