# Fix Task — VidLift v0.4.16 Karen WARN remediation (Forge)

You are **Forge**, Senior Software Engineer. Fix Karen's three Medium defects and four Lows
from her validation report, land the missing test matrices, and re-run the suite green.

## Authoritative sources

- `.specs/karen-vidlift-v0.4.16-validation-report.md` — full defect detail + evidence.
- `.specs/bob-vidlift-model-management-{implementation,validation}-spec.md` and
  `.specs/bob-vidlift-visibility-{implementation,validation}-spec.md` — the promised matrices.

## Interpreter

`/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3, pytest 9.1.1). Never system Python.

    cd /home/yetisoldier/projects/vidlift && ~/.venvs/vidlift/bin/python -m pytest -q

## Fixes (all small; Karen's line refs are advisory — locate by symbol, not line)

### D-1 (Medium) — Mode A download beats never touch the JobRecord
`run.py` `_on_model_download` closure references only `state`, so a first-use model download
inside an upscale (Mode A) job lets `record.last_activity_at` freeze and `activity_age_seconds`
climb, tripping the 15-min stale-activity warning. Fix: thread a record-touch into Mode A's
publisher (mirror `jobs.py` Mode B's `record.touch()`), or derive `activity_age_seconds` from the
live state when one exists. Then land the missing **H-2 Mode A heartbeat parity test**.

### D-2 (Medium) — Mode A fail-open leaves a stale `model_download` fact
On download failure (network/sha/truncated), the `model_download` byte fact is never cleared —
the pop runs only on the success path. Status then reports "downloading" for a dead download.
Fix: pop the fact in the fail-open conversion (`except TranscriptionError` arm), mirroring Mode
B's unconditional pop. Land the **H-3 Mode A analog test**.

### D-3 (Medium) — `_transcription_echo` manifest arm reads the wrong key path
It reads top-level `manifest["subtitles"]`; real manifests store it at
`manifest["run"]["subtitles"]`. Terminal run jobs always echo `device: null`. Fix (one line):
`manifest.get("run", {}).get("subtitles")`. Land the missing **T-5 device-echo test**.

### D-4 (Low) — per-tag download lock spins forever on a mid-wait permission flip
`except OSError` arm retries unconditionally after round 0; degrade-OPEN fires only when
`rounds == 0`. Fix: treat `PermissionError` as degrade-OPEN on any round, or bound consecutive
non-FileExistsError OSErrors. Add a probe-regression test.

### D-5 (Low) — discovery card written 0o664 (world/group-readable token)
Create `monitor.json` `0o600` (consider `0o700` state dir). It carries the effective monitor
token, so it must not be group/world-readable on shared machines.

### D-6 (Low) — discovery card staging name fixed (`monitor.json.tmp`), races two GUI instances
Use a pid-unique staging name before `os.replace`.

### D-7 (Low) — missing test matrices (the root cause of D-1/D-3)
Land the missing matrices the specs promised but Forge never delivered: (a) visibility spec
**D-1..D-6 discovery-card matrix** (zero tests exist), (b) **T-1..T-6 device-echo matrix**, and
(c) model-management **H-2 Mode A parity test**. Karen's probe harnesses in `.karen/v0416-probes/`
show the exact shapes to pin.

## Constraints

- Do NOT redesign; these are localized fixes. Preserve all Karen-verified-working behavior
  (conditional-arity seam, lock create-close/heartbeat/stale-reclaim, cancel fix, hermeticity,
  `api_version` additivity).
- Keep the version stamp 0.4.16.0; update CHANGELOG only to note the honesty fixes (not to
  overstate).

## Verify

- FULL suite green with the venv interpreter; report exact pass/fail/skip counts (repeat once
  for determinism).
- Confirm each of D-1..D-6 is addressed and D-7's matrices are now present + green.

## Report (final text)

- Per-defect: one line on the fix + the test that now pins it.
- Test counts (before/after).
- Any deviation or ambiguity.
