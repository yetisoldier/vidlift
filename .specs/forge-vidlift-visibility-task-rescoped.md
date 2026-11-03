# Implementation Task (re-scoped) — VidLift visibility, VidLift-side ONLY (Forge)

You are **Forge**, Senior Software Engineer. Implement ONLY the VidLift-side changes of
Bob's Option A design. The Hermes-side consumer script/skill is OUT OF SCOPE for you (it
lives outside this repo and another owner handles it) — do NOT attempt to read or write
anything under `~/.hermes/` or any path outside this repo.

## Repo

`/home/yetisoldier/projects/vidlift` — Eric's Windows AI-upscale + subtitle tool.

## Read first (authoritative)

- `.specs/bob-vidlift-visibility-implementation-spec.md` — exact files, functions, order,
  acceptance criteria.
- `.specs/bob-vidlift-visibility-architecture-spec.md` — design + ADRs.

## Interpreter (CRITICAL)

`/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3, pytest 9.1.1). Never system python.

    cd /home/yetisoldier/projects/vidlift && ~/.venvs/vidlift/bin/python -m pytest -q

## Scope (VidLift-side ONLY — the two additive changes)

1. **Monitor discovery card** — the GUI writes `<state_dir>/monitor.json` (atomic, marked
   secret) carrying the EFFECTIVE token, actual bind host/port (port-fallback makes the real
   port unknowable a priori — currently 8766), and `lan_base_url`, on every monitor
   start/toggle.
2. **Additive `transcription` block** in the per-job `_status_payload`
   (`mcp_bridge/tools.py`) — the RESOLVED device (`cuda` / `cpu` / `cpu (cuda unavailable)`
   / honest `null`) and `device_requested`. Additive keys only; do NOT bump `api_version`.

Do NOT implement the Hermes-side consumer. Do NOT touch anything under `~/.hermes/`,
`/home/yetisoldier/.hermes`, or any path outside this repo. All your work stays in
`/home/yetisoldier/projects/vidlift`.

## Hard rules

1. **No security regression.** `/status`'s token gate stays exactly as-is. No
   unauthenticated full-state surface. Do NOT weaken the empty-token "open" semantic.
2. **Cross-platform honesty.** VidLift's release target is Windows; new tests must pass on
   Linux AND windows-latest (`tmp_path`, `str(Path(...))`; POSIX-only fixtures need
   `skipif(sys.platform.startswith("win"))`).
3. **Follow Bob's spec, don't redesign.** Escalate ambiguity rather than inventing your own
   approach.

## Verify before finishing

- Run the FULL suite with the vidlift venv interpreter; must be green. Report exact
  pass/fail/skip counts.
- Confirm you touched no tracked file outside the intended VidLift modules + tests.

## Report (final text)

- Files changed (one-line "what and why" each).
- The exact new test(s) proving the discovery card and the `transcription` block, and that
  the `/status` token gate is unchanged.
- Test counts (before/after).
- Any spec deviation or ambiguity.
