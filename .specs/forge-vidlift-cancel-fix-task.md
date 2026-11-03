# Bug Fix Task — VidLift cancel not reflected (Forge)

You are **Forge**, Senior Software Engineer. Investigate and fix a user-reported bug, then
run the suite green.

## The bug (Eric's report + monitor evidence)

Eric clicked **"cancel current"** and **"cancel batch"** in VidLift, but the UI still shows
the job as running. The live monitor `/status` corroborates: an active job reports
`state: "running"` long after a cancel, and `/history` shows a mix — some jobs `cancelled`,
others `failed`, while a re-try sits `running`. The cancel action is not reliably stopping
the running job and/or not propagating the cancelled state to the registry + UI.

## Repo / interpreter

`/home/yetisoldier/projects/vidlift`; interpreter `/home/yetisoldier/.venvs/vidlift/bin/python`
(3.12.3, pytest 9.1.1). You have `external_directory: allow` (may read `~/.vidlift/`,
write `/tmp/`).

    cd /home/yetisoldier/projects/vidlift && ~/.venvs/vidlift/bin/python -m pytest -q

## What to do

1. **Trace the cancel path end-to-end:** GUI cancel button → cancel action →
   `mcp_bridge/tools.py::tool_cancel` / `registry.py` job-cancel → underlying process
   termination (subprocess/worker) → registry state transition → UI refresh (the 1 Hz tick).
   Find where the cancelled state is dropped: is the process not terminated? is the registry
   state not transitioned? is the UI tick not picking up the new state? is there a
   `running`-vs-`cancelled` race in the queue/registry view?
2. **Fix it** so `cancel current` and `cancel batch` (a) actually stop the running job's
   underlying work, and (b) immediately reflect `cancelled` in the registry, the monitor
   `/status`, and the UI.
3. **Cover it** with regression tests: cancel-current stops a running job and updates state;
   cancel-batch cancels every queued/running job and updates state; the UI tick reflects the
   transition. Use deterministic fakes (no real subprocess/model).

## Constraints

- If the root cause is architectural (e.g. the job executor fundamentally cannot be
  cancelled mid-run), do NOT invent a redesign — document the finding and escalate it in your
  report so it can go to Bob. Otherwise implement the fix directly.
- Follow the repo's existing conventions and cross-platform honesty (Windows release target;
  tests on Linux + windows-latest).
- Do not touch unrelated files.

## Verify

- FULL suite green with the venv interpreter; report exact pass/fail/skip counts.
- Name the exact root cause you found and the files you changed.

## Report (final text)

- Root cause (one paragraph).
- Files changed (one-line each) + the new regression tests.
- Test counts (before/after).
- Whether you fixed it directly or escalated an architectural finding.
