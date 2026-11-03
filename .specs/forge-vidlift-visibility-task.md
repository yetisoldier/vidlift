# Implementation Task — VidLift remote visibility for Hermes (Forge)

You are **Forge**, Senior Software Engineer. Implement Bob's design for Hermes's read-only
visibility into VidLift's running state.

## Repo

`/home/yetisoldier/projects/vidlift` — Eric's Windows AI-upscale + subtitle tool.

## Read first (authoritative)

- `.specs/bob-vidlift-visibility-implementation-spec.md` — exact files, functions, order,
  milestones, acceptance criteria. Follow it precisely; do not reinterpret.
- `.specs/bob-vidlift-visibility-architecture-spec.md` — the design + ADRs + security
  rationale.

## Interpreter (CRITICAL)

`/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3, pytest 9.1.1). Never the system
python (3.14) — it lacks `av` + `faster_whisper` and hard-crashes the Qt suite.

    cd /home/yetisoldier/projects/vidlift && ~/.venvs/vidlift/bin/python -m pytest -q

## Scope (from Bob's recommendation, Option A)

Two small additive VidLift changes, plus a Hermes-side consumer:

1. **Monitor discovery card** — the GUI writes `<state_dir>/monitor.json` (atomic, marked
   secret) carrying the EFFECTIVE token, actual bind host/port (port-fallback makes the real
   port unknowable a priori — currently 8766), and `lan_base_url`, on every monitor
   start/toggle. This is how Hermes learns the CURRENT token + port.
2. **Additive `transcription` block** in the per-job `_status_payload`
   (`mcp_bridge/tools.py`) — the RESOLVED device (`cuda` / `cpu` / `cpu (cuda unavailable)`
   / honest `null`) and `device_requested`. Additive keys; do NOT bump `api_version`.
3. **Hermes-side consumer** — a tiny stdlib script + a small skill so `hermes` can answer
   "what's going on with VidLift" as a one-shot authenticated `/status` (+ `/history`) read,
   pairing its token/port from the discovery card into `~/.hermes/.env` once, and detecting
   desync via `/health`-200 + `/status`-401.

## Hard rules

1. **Read-only.** The Hermes-side consumer observes; it must never mutate jobs, config, or
   files on the VidLift box. No write endpoints, no MCP mutation tools.
2. **No security regression.** `/status`'s token gate stays exactly as-is (Bearer header).
   Do not add any unauthenticated full-state surface. Do not weaken the empty-token "open"
   semantic — but DO include the loud open-mode canary Bob specified.
3. **Cross-platform honesty.** VidLift's release target is Windows; new tests must pass on
   both the Linux gate AND windows-latest (use `tmp_path`, `str(Path(...))`; POSIX-only
   fixtures need `skipif(sys.platform.startswith("win"))`).
4. **Follow the spec, don't redesign.** Escalate ambiguity rather than inventing your own
   approach.

## Verify before finishing

- Run the FULL suite with the vidlift venv interpreter; it must be green. Report exact
  pass/fail/skip counts.
- Confirm you touched no tracked file outside the intended modules + the Hermes-side
  script/skill location.

## Report (final text)

- Files changed (one-line "what and why" each) — VidLift side and Hermes side.
- The exact new test(s) proving the discovery card and the `transcription` block, and that
  the `/status` token gate is unchanged.
- Test counts (before/after).
- Any spec deviation or ambiguity you resolved or escalated.
