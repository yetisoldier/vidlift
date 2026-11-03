# Architecture Specification — VidLift Remote Visibility for Hermes

**Author:** Bob (Principal Software Architect)
**Date:** 2026-11-03
**Status:** Recommended — ready for Forge
**Task:** `.specs/bob-vidlift-visibility-task.md`

---

## 1. Executive Summary

Hermes (Eric's assistant on the minipc) must answer "what's going on with VidLift?" —
device (CPU/GPU), per-job progress, stage, ETA, subtitle/transcription status, queue —
as a one-shot, read-only, authenticated read, with no manual token relay per ask.

**Recommendation: Option A — the existing HTTP monitor (0.0.0.0:8765+), consumed by a
tiny Hermes-side stdlib script — extended with two small additive VidLift changes:**

1. A **monitor discovery card** (`<state_dir>/monitor.json`) written by the GUI whenever
   the monitor's serving state resolves. It carries the *effective* bind host/port (the
   port-fallback mechanism makes the real port unknowable a priori — the deployment is
   currently on 8766, not the default 8765) and the *effective* token. This is the
   durable answer to "how does Hermes learn the CURRENT monitor token": one pairing copy
   into `~/.hermes/.env`, re-done only on deliberate rotation, with 401s as the
   deterministic re-pair signal.
2. An **additive `transcription` block in the per-job status payload**
   (`_status_payload` in `mcp_bridge/tools.py`) surfacing the RESOLVED transcription
   device (`cuda` / `cpu` / `cpu (cuda unavailable)` / honest `null`) — the one fact the
   task names that `/status` does not reliably expose today. Additive keys never bump
   `api_version` (repo's documented convention).

No new network surface. No MCP client. No file-sharing infrastructure. No standing
poll. No LLM in the ask path.

**One-line reason:** the HTTP monitor already serves exactly the truth Hermes needs from
the exact process that owns it (the repo's "one truth, two readers" discipline); the
only real gaps are credential/address provisioning and one additive honesty key — both
cheaper and safer than any parallel surface.

---

## 2. Requirements

### 2.1 Functional

| ID | Requirement |
|----|-------------|
| F1 | Hermes can obtain the full running state on demand: per-job state, stage (`progress["step"]`, `current_phase`), progress (frames/segments), ETA (honest-null), queue depth/order, log tail. |
| F2 | The answer includes device facts: upscale engine facts (`engine_simulated`) and the **resolved transcription device** (cuda vs cpu, including the honest `"cpu (cuda unavailable)"` fallback wording) — surfaced, not inferred. |
| F3 | The ask resolves to a one-shot read (seconds, no LLM, no standing poll). |
| F4 | Hermes's credential never silently desyncs from VidLift's effective token/port; desync is deterministically detectable (401) and recoverable via one explicit re-pair step. |

### 2.2 Non-functional (constraints, from the task — non-negotiable)

- **N1 Read-only.** Hermes observes; it must never mutate jobs, config, or files.
- **N2 No security regression.** Full state stays authenticated; `/status`'s token gate
  stays; no unauthenticated full-state exposure is introduced. (Also: no endpoint is
  added that weakens the empty-token open mode's semantics.)
- **N3 Transport.** Minipc → 192.168.1.53 over LAN HTTP works. ICMP blocked, **no SSH**
  to the Windows box. Tailscale mesh available.
- **N4 Cheap and on-demand.** One-shot read; no polling infrastructure, no LLM to render.

---

## 3. Current-State Analysis (verified in source)

### 3.1 HTTP monitor (`vidlift/monitor.py`)

- `ThreadingHTTPServer`, `GET /health` (token-free: `ok, version, busy`),
  `GET /status` and `GET /history` (token-gated; Bearer header **or** `?token=`,
  `hmac.compare_digest`).
- Read-only **by construction**: only `do_GET` defined (any other verb → 501); the
  server's only window on state is the `StatusView` facade (three bridge read methods);
  `test_status_view_cannot_write` pins the shape.
- `/status` envelope: `api_version: 1`, `version`, `queue_active`, `uptime_seconds`,
  `active_jobs[]` (each the bridge's `_status_payload` — the SAME shape the GUI's 1 Hz
  tick consumes), `queued_count`, `server_time`.
- Token: baked `MONITOR_TOKEN_DEFAULT` (`vidlift-mon-…`), overridable via QSettings
  `monitor/token`; empty stored token = open mode. Rotation-by-release is the
  documented interim; constant-time compare shipped (v0.4.15.4 F-15); per-install token
  generation explicitly **deferred** pending "a pairing/UX design (the watching client
  must learn the token)" — *this engagement is exactly that pairing design.*
- Bind `0.0.0.0`, default port 8765 with a 10-attempt fallback walk
  (`start_server_with_fallback`); the GUI persists the actual port. **Live deployment
  is on 8766** — the port Hermes must use is not statically knowable.

### 3.2 MCP bridge (`vidlift/mcp_bridge/`)

- **Stdio-only by binding design** (`docs/designs/mcp-bridge.md` § 2: "No network
  listener"). One server per client session, newline-framed JSON-RPC over stdin/stdout.
- 9 tools registered (`probe, plan, benchmark, run, transcribe, status, cancel, list,
  manifest`); `tool_status`/`tool_list` read the same registry the HTTP monitor reads.
- For same-machine consumers it re-attaches to the persisted registry
  (`$VIDLIFT_STATE_DIR/jobs.json`) and rebuilds status via `progress_snapshot` mirrors.

### 3.3 The device-truth inventory (where "CPU or GPU" actually lives today)

| Fact | Where it lives | Visible via HTTP `/status` today? |
|------|----------------|-----------------------------------|
| Upscale engine simulated vs real GPU | `RunState.engine_simulated` → per-job payload | ✅ Yes (live + manifest reconstruction) |
| Mode A (upscale) resolved transcription device | `subtitles_block["transcription"]["device"]` (run.py:2571–2584), incl. fallback `"cpu (cuda unavailable)"` (v0.4.15.3 R1 wording) | ⚠️ **Only in a narrow window**: live jobs whose subtitles phase already completed expose it via `payload["subtitles"]`; *during* transcription it is absent; finished jobs do **not** copy `subtitles` into the payload |
| Mode B (`subs` job) resolved transcription device | `SubsJobResult.device` → `record.result["device"]` (jobs.py:421, v0.4.15.3 D4) | ❌ **No** — `_status_payload` only surfaces `result` for `benchmark` jobs (tools.py:523) |
| Requested device pin | `record.options["device"]` (subs), plan transcription block (run) | ❌ Not in payload |
| Finished-job manifest | `manifest["subtitles"]["transcription"]["device"]` | ❌ Manifest readable in-process only; `/history` returns a `manifest_path` Hermes cannot reach (no SSH/share) |

**Conclusion:** the CPU/GPU question is *answerable in principle* from one process (the
GUI's bridge), but the HTTP surface does not serve it reliably. A small additive honesty
key closes this; no new surface is needed.

---

## 4. Options Evaluated

### Option A — HTTP monitor + shared token (Hermes script curls `/status` + `/history`)

- **Auth model:** Hermes stores the effective monitor token (`~/.hermes/.env`,
  `VIDLIFT_MONITOR_TOKEN`), sends `Authorization: Bearer` (never the `?token=` query —
  query strings leak into logs/history).
- **Exposed:** everything F1 needs today; F2 after the additive `transcription` block.
- **Invocation:** one stdlib script, two GETs, ~1 s.
- **Security posture:** token gate unchanged (N2 preserved); LAN-plaintext HTTP (accepted
  repo tradeoff; Tailscale raises the bar); read-only by construction satisfies N1
  structurally — the client cannot reach a write verb that does not exist.
- **The wrinkle (the crux of the task):** token *provisioning*. The effective token is
  QSettings-overridable and the port is fallback-shifted; Hermes must learn the CURRENT
  pair. Solved by the discovery card (§5, ADR-2): zero-touch when the baked default is
  in force, one pairing copy otherwise/after rotation, 401-driven deterministic re-pair.
- **Effort:** VidLift ~60 LoC + tests; Hermes ~150 LoC stdlib script + skill.
- **CPU/GPU answer:** ✅ after additive key — surfaced, not inferred.

### Option B — MCP bridge (`tool_status`/`tool_list` as an MCP client)

- **Structurally disqualified for this ask.** The bridge is stdio-only *by binding
  design* ("No network listener, no second implementation"). From the minipc, a stdio
  server is unreachable without either SSH (explicitly unavailable, N3) or building a
  NEW network transport for MCP — which contradicts the binding spec and creates a
  parallel network surface duplicating what the HTTP monitor already serves from the
  same reader.
- A *local* MCP client works only if Hermes ran on the Windows box — it does not.
- **Auth model:** none exists (stdio = implicit trust); a network wrapper would need a
  new auth story = strictly worse than A on N2.
- **Effort:** high (MCP client wiring on Hermes + network wrapper + auth), for a
  read-side ask the HTTP monitor already answers.
- **CPU/GPU answer:** same data as A, heavier path.

### Option C — Dedicated read-only agent endpoint / read-scoped credential

- A second endpoint (e.g. `/agent/status`) or a second accepted credential scoped
  "read-only". Note first: **every monitor endpoint is already read-only** — a
  read-scoped credential buys nothing over the existing token; the write surface it
  would exclude does not exist (do_GET only).
- A separate endpoint with "exactly the fields Hermes needs" forks the payload contract
  the GUI/HTTP/MCP all share — the repo has repeatedly paid (version-stamp drift,
  schema forks) for second truths, and its discipline is "additive keys land
  automatically, one shape". The device-field gap is closed by an additive key in the
  ONE payload (which is what we recommend), not by a second payload.
- A *dedicated agent credential* (token set instead of token singleton) was considered
  for credential hygiene (Hermes's loss ≠ other watchers' loss). Rejected for now:
  rotates the auth model the repo deliberately kept simple, and the baked-token
  tradeoff is already documented. Kept as a future option if a second watcher or a
  rotation cadence appears (ADR-2, revisit criteria).
- **Effort:** medium; value: none beyond A once the additive key lands.

### Option D — File-based state sharing (status.json / manifest to a Tailscale-reachable share)

- **No transport exists:** no SSH, no verified SMB/Syncthing between minipc and the
  Windows box (N3). Standing up a file-share/sync daemon *solely* to read state the
  LAN already serves over HTTP is pure operational complexity.
- **Philosophy violation:** a periodically-written `status.json` is a *mirror of the
  truth with a staleness window* — precisely what monitor.py's header forbids ("no
  parallel store, no mirror, no stale window"). The task's own honesty culture (ETA
  honest-null, progress mirrors) argues against it.
- **One honest sub-case survives:** the *discovery card* (`monitor.json`) — a tiny,
  write-on-change provisioning file, NOT a status mirror — is how the effective
  token/port crosses to Hermes once. That is config provisioning, not state mirroring,
  and it is part of the recommended A-variant.
- **Effort:** high (new sync infrastructure), **CPU/GPU answer:** only as good as the
  writer cadence.

### Comparison

| Criterion | A. HTTP+token | B. MCP bridge | C. Agent endpoint | D. File sharing |
|---|---|---|---|---|
| Read-only (N1) | structural ✅ | stdio trust | structural ✅ | read-only file ✅ |
| Auth (N2) | unchanged ✅ | needs new model ⚠️ | unchanged ✅ | share ACLs ⚠️ |
| Works over N3 transport | ✅ | ❌ (no SSH) | ✅ | ❌ (no share) |
| Second truth/mirror risk | none | none (but needs wrapper) | fork risk | staleness window ❌ |
| CPU/GPU surfaced | ✅ (additive key) | ✅ | ✅ | ⚠️ cadence-bound |
| Effort | low | high | medium | high |
| One-shot cheap ask | ✅ | heavy handshake | ✅ | ✅ |

**Selected: Option A** (with the additive device key and the discovery-card
provisioning — i.e., the minimal C-flavored deltas folded into A).

---

## 5. Architecture Decision Records

### ADR-1 — Transport: existing HTTP monitor, not MCP-over-network, endpoint fork, or file sharing

- **Problem:** How does Hermes read full VidLift state cross-machine, read-only, on demand?
- **Decision:** Consume the existing token-gated HTTP `/status` (+`/health`, optional
  `/history`) from a Hermes-side stdlib script.
- **Alternatives considered:** B (MCP client) — disqualified by stdio-only binding + no
  SSH; C (dedicated endpoint) — forks the payload contract; read-scope credential buys
  nothing on a writeless surface; D (file sharing) — no transport exists, mirror
  violates the no-stale-window rule.
- **Advantages:** zero new listeners; single payload contract (GUI 1 Hz, HTTP, MCP all
  one shape); read-only by construction; auth unchanged.
- **Disadvantages:** plaintext LAN HTTP (pre-existing, documented); shared static token
  (pre-existing, documented, `hmac.compare_digest`-compared).
- **Tradeoffs:** we accept the repo's documented interim (baked token, rotation-by-release)
  rather than redesigning auth for a private single-user LAN deployment.
- **Consequences:** Hermes depends on monitor uptime and the token/port pair (→ ADR-2).
- **Revisit criteria:** any write-capable monitor endpoint proposed (repo's own trigger,
  config.py); wide-audience packaged ship; a second watcher needing separate credentials.
- **Dependencies:** none new.

### ADR-2 — Credential & address provisioning: monitor discovery card + one-time pairing, 401-driven re-pair

- **Problem (the crux):** Hermes must learn the CURRENT effective token and port without
  Eric relaying them per ask. Token is QSettings-overridable; the port is
  fallback-shifted (live deployment proves it: 8766 ≠ 8765 default). QSettings lives on
  the Windows box; Hermes has no SSH/file access to read it.
- **Decision:** The GUI writes a **discovery card**, `<state_dir>/monitor.json`
  (atomic temp+replace), every time the monitor's serving state resolves (startup,
  toggle, fallback-port persist):

  ```json
  {
    "schema": 1,
    "enabled": true,
    "bind_host": "0.0.0.0",
    "port": 8766,
    "lan_base_url": "http://192.168.1.53:8766",
    "token": "vidlift-mon-…",
    "token_source": "default|qsettings",
    "version": "0.4.15.x",
    "written_at": 1730000000.0
  }
  ```

  Hermes's operator (Eric) copies this file's values into `~/.hermes/.env`
  ONCE per pairing event. Re-pair triggers: (a) deliberate rotation, (b) first-time
  QSettings override — both detected deterministically when the script sees 401 while
  `/health` answers. When the baked default is in force and unrotated, Hermes's shipped
  baseline config needs **zero** action.
- **Alternatives considered:**
  - *Bake the default token into the Hermes skill forever*: works until
    rotation-by-release or a QSettings override, then silently desyncs with no discovery
    path. Kept as the card's `token_source: default` fast path, not the mechanism.
  - *Token-free pairing endpoint on the monitor*: hands the secret to anyone on the LAN —
    violates N2 outright.
  - *Localhost-only provisioning endpoint*: unreachable from the minipc by definition.
  - *Multiple accepted tokens (baked + per-agent stable credential)*: defensible hygiene,
    but changes the auth model the repo deliberately keeps simple; parked under C's
    revisit criteria.
  - *Eric paste-relay*: always available as the degenerate fallback; the card makes it a
    file copy, not transcription of a uuid.
- **Advantages:** durable across restarts and port fallback; single source of truth for
  "what is the monitor NOW"; re-pair is rare and detection is deterministic; the card
  doubles as the ops answer to "which port is it on".
- **Disadvantages:** the token now exists in one more file on the Windows box (state
  dir, same trust zone as the QSettings store — user-profile private; not a new
  exfiltration channel); requires discipline: never commit it, never log it (the GUI
  already keeps the token out of window chrome, § 4.4 of the v0.4.6 design — the card
  extends, not breaks, that rule).
- **Tradeoffs:** a provisioning file over a protocol handshake — justified because there
  is no handshake channel (no SSH) and the trust domain is a single user on one LAN.
- **Consequences:** Hermes-side `.env` is the credential store (0600, gitignored);
  VidLift-side card writer is ~30 LoC in the GUI's existing start/toggle paths.
- **Revisit criteria:** per-install token generation (the repo's own deferred item);
  Tailscale-ACL-scoped binds (would allow weaker tokens over tailscale0); any second
  watcher.
- **Dependencies:** `client_local.default_state_dir()` (existing), atomic-write
  discipline (repo convention).

### ADR-3 — Surfacing the resolved transcription device: additive `transcription` block in `_status_payload`

- **Problem:** "CPU or GPU for transcription" is knowable (§ 3.3) but not served:
  Mode B terminal jobs never expose `record.result["device"]`; Mode A finished jobs
  don't echo the manifest's `subtitles.transcription.device`; live Mode A exposes it
  only post-transcription. Hermes would otherwise *infer* — unacceptable (the repo's
  anti-lie rule).
- **Decision:** Add ONE additive nested key to every per-job `_status_payload`:

  ```json
  "transcription": {
    "device": "cuda" | "cpu" | "cpu (cuda unavailable)" | null,
    "device_requested": "auto" | "cuda" | "cpu" | null
  }
  ```

  Resolution chain (honest-null everywhere a fact was not recorded):
  1. live Mode A: `state.subtitles.transcription.device` when the subtitles phase has
     recorded it; `null` mid-transcription (honest: not yet decided/final);
  2. Mode B: `record.result["device"]` on terminal records (`""` → `null`, the D4
     honest-unknown); `device_requested` from `record.options["device"]`;
  3. finished/failed Mode A (no live state): manifest echo
     `manifest["subtitles"]["transcription"]["device"]`;
  4. jobs without transcription: `null`/`null` (subtitles status `none` etc.).
- **`api_version` stays 1** — additive keys never bump it (monitor.py's own header;
  the v0.4.1 subtitles-block precedent).
- **Alternatives considered:** new endpoint (ADR-1, rejected); Hermes infers from
  progress steps/`engine_note` (violates the anti-lie rule); surface only in a new
  agent payload (contract fork).
- **Disadvantages:** mid-transcription Mode B still reports `null` until completion —
  the *honest* answer, since the v0.4.15.3 D4 settlement defines the run device at
  completion (the fallback wrapper may flip it in flight). Documented to Eric as such.
- **Revisit criteria:** a future in-flight resolved-device seam in `run_subs_job`
  (would let live Mode B flip from `null` to the resolved value additively — no
  contract change).
- **Dependencies:** none beyond existing fields; ~20 LoC in `tools.py`.

### ADR-4 — Hermes-side artifact: one stdlib script + env config + skill; no LLM, no poll

- **Problem:** the ask must be instant and cheap (N4) and survive Hermes upgrades.
- **Decision:** `~/.hermes/bin/vidlift-status` (python3, stdlib only): reads
  `~/.hermes/.env` (`VIDLIFT_MONITOR_URL`, `VIDLIFT_MONITOR_TOKEN`), GETs `/health`
  then `/status` (Bearer), prints a compact human summary (queue state; per-job: file
  name, kind, state, stage label, frames/segments progress %, ETA or honest "unknown",
  device line, queue position; recent history when asked). A Hermes **skill**
  ("vidlift-status") shells the script and relays output verbatim. Exit codes:
  0 ok, 3 unreachable, 4 auth (re-pair hint printed).
- **Alternatives considered:** Hermes LLM tool-use with a raw HTTP tool (nondeterministic,
  costs tokens, N4 fail); standing poller/push cache (violates on-demand; stale answers).
- **Dependencies:** python3 on minipc (present); `~/.hermes/.env` (existing pattern,
  task-named).

---

## 6. System Architecture & Data Flow

```
Eric: "what's going on with VidLift?"
        │
        ▼
Hermes (minipc)                  VidLift Windows box (192.168.1.53)
┌────────────────────┐           ┌─────────────────────────────────┐
│ skill vidlift-status│          │ GUI process                     │
│   └► vidlift-status │  HTTP    │  ├─ LocalBridgeClient (registry)│
│       script        │─────────►│  ├─ MonitorServer :8766         │
│   reads ~/.hermes/.env         │  │   └─ StatusView (3 reads)     │
└────────────────────┘           │  ├─ JobExecutor (1 worker)      │
       ▲                         │  └─ state_dir/monitor.json      │
       │ (pairing, rare)         │      (written on start/toggle)  │
       └─────────────────────────┘
                                 Registry ← same object, one truth
```

Request flow per ask:
1. skill → script;
2. `GET /health` (token-free): if unreachable → "VidLift monitor is off/unreachable"
   (exit 3); `busy` previews the answer;
3. `GET /status` with Bearer: 200 → render summary; 401 → "token stale — re-pair from
   monitor.json" (exit 4), never silently fall back to the query-string token;
4. (optional, `--history`) `GET /history` for the last few finished jobs (state, ok,
   engine_simulated — `manifest_path` is noted as remote-only and NOT fetched).

One-shot. No daemon, no cache, no LLM in the rendering path.

## 7. Security Design

- **AuthN:** unchanged — Bearer token, `hmac.compare_digest` (v0.4.15.4 F-15). The
  script sends the header only; `?token=` is deliberately unused.
- **Read-only:** structural (do_GET only); the Hermes artifact contains no enqueue/
  cancel code paths to misuse.
- **Credential storage:** VidLift-side card lives in the user-profile state dir (same
  trust zone as QSettings; never logged, never in window chrome, never committed).
  Hermes-side `.env` mode 0600, gitignored.
- **Pre-existing caveat flagged, not worsened:** QSettings token stored **empty** +
  the default `0.0.0.0` bind means open full state on the LAN (the code comments call
  this "local-only open", which is inaccurate for a 0.0.0.0 bind). The discovery card
  records `token_source`; the Hermes script **warns loudly** if `/status` answers 200
  with no Authorization header, giving Eric a detection canary for accidental open
  mode. Recommended (non-blocking) hardening for a later pass: refuse-bind or
  localhost-force when token is empty.
- **Blast radius:** token compromise ⇒ read-only state disclosure on the LAN/Tailscale;
  no job control, no filesystem write. Rotation = QSettings/Bob-spec'd constant change
  + one re-pair (401 already detected deterministically).
- **Deferred (repo-consistent, with revisit criteria):** Tailscale-only bind; TLS
  (self-signed, for the Tailscale case where it is essentially redundant); per-install
  tokens (now superseded by ADR-2's pairing design if ever promoted).

## 8. Failure Analysis

| Failure | Behavior |
|---|---|
| Monitor off / GUI not running | `/health` unreachable → script exit 3, honest "monitor not reachable" message |
| Stale Hermes token (rotation/override) | `/health` 200 + `/status` 401 → exit 4 + re-pair instruction; no silent fallback |
| Port re-fallback after reboot | Hermes hits wrong port → connect error; re-pair from the card (card is rewritten on every start) |
| Reader-side exception in monitor | existing honest 500 with error name (`_serve`) |
| VidLift mid-upgrade (version skew) | additive-only contract ⇒ script ignores unknown keys; missing new keys render as "unknown" |
| Registry wedged mid-write | monitor reads lock-snapshotted views (existing discipline) |
| Empty-token open mode (accidental) | script canary warning (§ 7) |

## 9. Risks

- **R1 (pairing drift):** Eric overrides the token in QSettings and forgets to re-pair.
  Mitigation: 401 detection + one-file re-pair; documented in the skill's error path.
- **R2 (card staleness):** state dir is per-user; a crash between port change and card
  write is possible. Mitigation: card written immediately after the server's actual port
  is known (same lines that persist `monitor/port` today).
- **R3 (contract skew):** Hermes parses `_status_payload` fields. Mitigation:
  additive-only rule pinned by `api_version` + the repo's schema-v2 convention; Karen's
  validation includes a payload-shape assertion (validation spec T-series).

## 10. Future Evolution

- In-flight Mode B device seam (`run_subs_job` → RunState) upgrades live `null` to the
  resolved value — additive, no contract change.
- Per-watcher credentials if a second agent appears (Option C's parked half).
- Tailscale-ACL bind and/or mTLS if the box ever leaves the private-LAN assumption.
- The same discovery-card pattern generalizes to the sibling bluray-to-mp4 monitor.
