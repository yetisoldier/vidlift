# Architecture Task — VidLift remote visibility for Hermes (Bob)

You are **Bob**, Principal Software Architect. Design the cleanest mechanism for Hermes
(Eric's personal assistant, running on the minipc on the same LAN) to get full, read-only
visibility into VidLift's running state on demand, so that when Eric asks "what's going on
with VidLift", Hermes can answer with device (CPU/GPU), per-job progress, stage, ETA,
subtitle/transcription status, and queue — WITHOUT Eric relaying a token or manifest by hand.

## Repo / current state (read this first)

`/home/yetisoldier/projects/vidlift` — Eric's Windows AI-upscale + subtitle tool. It already
has TWO introspection surfaces:

1. **LAN monitor** (`vidlift/monitor.py`), bound 0.0.0.0:8766 (currently live at
   192.168.1.53:8766):
   - `GET /health` — token-free: `{ok, version, busy}`.
   - `GET /status` — token-gated (`Authorization: Bearer <t>` or `?token=<t>`, hmac compare):
     full snapshot — `api_version`, `version`, `queue_active`, `uptime_seconds`,
     `active_jobs[]` (per-job `_status_payload`, the SAME shape the GUI's 1 Hz tick consumes:
     stage, progress, ETA, device, etc.), `queued_count`, `server_time`.
   - `GET /history` — token-gated pass-through to the bridge's history().
   - Token: baked default `MONITOR_TOKEN_DEFAULT` (a uuid4, `vidlift-mon-...`), overridable
     and persisted via the GUI's `QSettings monitor/token` key. Hermes does NOT currently
     hold this token.

2. **MCP bridge** (`vidlift/mcp_bridge/` — server.py, tools.py, registry.py, client_local.py):
   a full MCP server with 8 tools — `tool_status`, `tool_list`, `tool_manifest`, `tool_run`,
   `tool_cancel`, `tool_plan`, `tool_probe`, `tool_benchmark`. `tool_status` exposes the same
   live registry the monitor does.

## Requirement (Eric's words, distilled)

Hermes must be able to "fully see what is going on" when Eric asks — the full running state,
not just `{ok, busy, version}`. This is an on-demand, READ-ONLY, read-side capability.

## Constraints (non-negotiable)

1. **Read-only.** Hermes observes VidLift state; it must not mutate jobs, config, or files.
2. **No security regression.** The full snapshot MUST stay authenticated. VidLift's recent
   M2-equivalent tightening (token-gated /status) is correct and stays. Hermes is Eric's own
   trusted agent on the same LAN/Tailscale, so a shared credential is acceptable — but
   unauthenticated full-state exposure is NOT.
3. **Minipc reaches 192.168.1.53 over LAN** (HTTP works; ICMP is blocked). No SSH to that box.
4. **On-demand and cheap.** The "what's going on" ask should resolve to a one-shot read, not a
   standing poll or a full agent round-trip. No LLM should be needed just to render a status.

## Design directions to evaluate (research + pick, 3+ options)

- **A. HTTP monitor + shared token:** Hermes stores the monitor token in `~/.hermes/.env`
  (e.g. `VIDLIFT_MONITOR_TOKEN`), plus a small script/skill that curls `/status` + `/history`
  and renders a readable summary. Simple, but token provision is the wrinkle: the token is
  overridable in QSettings, so Hermes must learn the CURRENT token — where does it come from?
- **B. MCP bridge:** Hermes connects to VidLift's MCP server as an MCP client and calls
  `tool_status`/`tool_list`. Richer, reuses existing machinery, but needs MCP client wiring +
  auth story on Hermes's side, and may be heavier than a status read warrants.
- **C. Dedicated read-only agent endpoint/token:** VidLift gains a small, purpose-built
  read-only surface (or a separate read-scoped credential) specifically for Eric's agent, with
  the exact fields Hermes needs (including the RESOLVED transcription device — CPU vs GPU —
  which today lives in the job manifest and may not be in /status).
- **D. File-based state sharing:** VidLift writes a machine-readable state file (the manifest /
  a status.json) to a shared, Tailscale-reachable location Hermes reads directly.

For each: auth model, what's exposed, how Hermes invokes it, security posture, effort, and
whether it can also answer the specific "CPU or GPU" question (the RESOLVED transcription
device) without Hermes guessing.

## Explicitly decide / cover

- How Hermes learns the CURRENT monitor token (baked default vs QSettings override vs a
  dedicated agent credential) — this is the crux.
- Whether the RESOLVED transcription device (cuda vs cpu, per the v0.4.15.2 honest-fallback
  manifest note) is surfaced, or whether Hermes still infers it.
- The minimal Hermes-side artifact: a script and/or a skill so the ask is instant and cheap.
- Any VidLift-side change (new endpoint, new tool, token provisioning) vs a pure Hermes-side
  client that reuses what already exists.

## Deliverables (write under `.karen/` or a new `.specs/` in the vidlift repo)

1. `bob-vidlift-visibility-architecture-spec.md` — human-facing: options evaluated, ADRs,
   data flow, security design, recommendation.
2. `bob-vidlift-visibility-implementation-spec.md` — for Forge: exact files, endpoints,
   functions, the Hermes-side script/skill, provisioning steps, acceptance criteria.
3. `bob-vidlift-visibility-validation-spec.md` — for Karen: how to prove Hermes sees full
   state, that read-only holds, and that no unauthenticated full-state exposure was added.

Return a concise executive summary as your final text (recommended approach + the one-line
reason, the auth/provisioning decision, and any risk you want Eric to see before Forge builds).

You may read anything in the vidlift repo; write only your three spec files. Do not modify
source or tests.
