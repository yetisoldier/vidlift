# VidLift MCP Bridge (binding spec)

Status: binding 2026-10-08. Author: Bob (architect), polished from the
2026-10-08 draft sketch. Implements roadmap "Next" item 2. Forge
implements against this document without further design decisions;
Karen validates against Section 8's acceptance tests.

## 1. Intent

Expose the VidLift runner as a local MCP (Model Context Protocol)
server so AI agents (Jarvis via OpenClaw; same registration pattern as
REA's `rea-mcp` wrapper, registered as `mcp.servers.vidlift`) can drive
upscales end to end: probe, plan, benchmark, launch, watch, cancel,
and read manifests. "Full bridge" means the entire single-file runner
state contract is reachable over MCP, not a status toy.

## 2. Non-goals (binding)

- No network listener. Stdio transport only, local machine only. The
  server is spawned by the MCP client as a child process and speaks
  MCP framing on stdin/stdout.
- No second implementation. Every tool maps 1:1 onto the same pipeline
  code the CLI and GUI use (`vidlift.probe`, `vidlift.plan`,
  `vidlift.run`, and the benchmark step from roadmap item 4). The bridge
  contains no pipeline logic of its own. (The v0.2.32 filename-handoff
  lesson: a duplicated source of truth is how reviewed state gets
  discarded between boundaries.)
- No new policy inside the bridge. Decisions stay in the pipeline:
  verdicts, geometry, bitrates, fail-open behavior, and honesty fields
  are computed in `vidlift.*` and passed through verbatim.
- No authentication/authorization layer beyond "local stdio child
  process." Registration as `mcp.servers.vidlift` happens only after
  Karen validates; the server is never exposed publicly.

## 3. Transport and process model

- **Transport:** MCP over stdio (JSON-RPC 2.0 messages, newline- or
  header-framed per the MCP spec revision OpenClaw pins). The bridge
  logs nothing to stdout except protocol frames; all diagnostics go to
  stderr and to per-run log files.
- **Process lifetime:** one server per MCP client session. The server
  holds the job registry and any live run threads; when the client
  disconnects (stdin EOF), the server stops accepting new tool calls
  but MUST NOT kill running jobs — jobs belong to the registry, not
  the session, and a later server instance re-attaches through the
  persisted registry (Section 6).
- **Concurrency model:** single-threaded MCP message loop;
  `run` returns immediately (async job model, Section 5). Actual
  pipeline work happens on bounded worker threads owned by the job
  registry (default max 1 concurrent run; the runner is
  hours-per-file, GPU-bound, and a second concurrent run is a
  misconfiguration, not parallelism). Non-run tools (`probe`, `plan`,
  `status`, `list`, `manifest`) execute inline in the message loop;
  they are sub-second and never block on a running job.
  `benchmark` is the exception: it is short (~30–60 s) but still runs
  as a tracked job so `status`/`cancel` apply uniformly.

## 4. Tool surface (binding)

All tools return MCP content blocks; structured payloads are JSON
objects inside a `text` content block, UTF-8. Every payload below is a
contract: field names and types are fixed; new fields may be appended
in future versions but existing fields never change meaning. All error
returns use MCP's `isError: true` with a human-readable message plus a
machine-readable `code` field (Section 7).

### 4.1 `probe`

Input:

```json
{ "paths": ["string, required, 1..N absolute or relative file paths"] }
```

Output:

```json
{
  "reports": [
    {
      "path": "string",
      "ok": true,
      "report": {
        "container": "string",
        "duration_seconds": "number|null",
        "video": {
          "codec_name": "string",
          "width": "int", "height": "int",
          "sample_aspect_ratio": "string",
          "display_aspect_ratio": "string",
          "field_order": "string",
          "r_frame_rate": "number|null",
          "avg_frame_rate": "number|null",
          "interlaced_flagged": "bool"
        } | null,
        "audio": [{"codec_name": "string", "channels": "int", "lang": "string"}],
        "subtitles": [{"codec_name": "string", "kind": "text|image|other", "lang": "string"}],
        "source_class": "xvid-divx-avi|dvd-mpeg2-rip|h264-sd|other",
        "displayed_width": "int",
        "displayed_height": "int",
        "upscale_verdict": "candidate|already-hd|no-video",
        "pal_speedup_suspected": "bool"
      }
    } | { "path": "string", "ok": false, "error": "string" }
  ]
}
```

Per-file isolation: one unreadable file never fails the batch; it gets
`ok: false` in its entry. Maps to `vidlift.probe.probe_and_classify`
per path.

### 4.2 `plan`

Input:

```json
{
  "paths": ["string, required"],
  "options": {
    "target": "720p (only supported value; default)",
    "gb_per_hour": "number, default 1.5",
    "output_dir": "string|null, default null (beside each input)"
  }
}
```

Output:

```json
{
  "files": [
    {
      "path": "string",
      "ok": true,
      "plan": {
        "verdict": "ready|already-hd|no-video",
        "output_path": "string",
        "output_geometry": [1280, 720],
        "video_bitrate_kbps": "int",
        "audio": {"channels": "int", "bitrate_kbps": "int", "label": "string"},
        "estimated_output_bytes": "int|null",
        "expectation": "string (the honest per-source-class string, verbatim)",
        "subtitle_note": "string",
        "warnings": ["string, e.g. PAL speedup, interlace flags"]
      }
    } | { "path": "string", "ok": false, "error": "string" }
  ],
  "summary": {
    "runnable_count": "int",
    "total_estimated_bytes": "int|null",
    "cannot_know": ["string — what the plan cannot state honestly, e.g. wall-clock time without a benchmark"]
  }
}
```

Maps to `build_batch_plan` over per-path `probe_and_classify`. The
`cannot_know` list is mandatory and non-empty today: without a measured
benchmark on this machine there is no honest wall-clock ETA (roadmap
clause c), and the bridge must say so rather than project one.

### 4.3 `benchmark`

Input:

```json
{
  "sample_path": "string, required — a real file on this machine",
  "duration_seconds": "number, default 30, clamped to [10, 120]"
}
```

Output (job-shaped; returns immediately):

```json
{ "job_id": "string", "kind": "benchmark" }
```

On completion the job's result (readable via `status`/`manifest`):

```json
{
  "sample_path": "string",
  "duration_seconds": "number",
  "engine": "string (engine class name, e.g. StubEngine or the real adapter)",
  "engine_simulated": "bool",
  "frames_processed": "int",
  "wall_seconds": "number",
  "frames_per_second": "number",
  "per_plan_constant": {
    "seconds_per_output_second": "number — upscale cost per second of source content at this file's geometry/class"
  },
  "notes": ["string — honest caveats, e.g. 'measured on stub engine; real-engine numbers pending'"]
}
```

Semantics (binding, roadmap roadmap clauses c + item 4):

- The benchmark extracts and processes one short chunk on THIS machine
  through the same engine seam a real run uses; it never computes from
  theory tables.
- Under the stub engine the measured number is a pipeline-machinery
  number, not an AI number; `engine_simulated: true` and `notes` must
  say exactly that. An ETA derived from a stub benchmark is allowed
  only if labeled `simulated` in every surface that shows it.
- Results are cached per (engine class, model, geometry bucket) in the
  registry so plan-time ETAs can quote "measured 2026-10-09, x2plus,
  640x360 class: N s/s" instead of re-running.

### 4.4 `run`

Input:

```json
{
  "paths": ["string, required, 1..N"],
  "options": {
    "target": "720p",
    "gb_per_hour": "number, default 1.5",
    "output_dir": "string|null",
    "ffmpeg": "string|null — explicit tool path (packaged builds)",
    "ffprobe": "string|null",
    "log_dir": "string|null"
  }
}
```

Output:

```json
{
  "jobs": [
    { "path": "string", "job_id": "string|null", "ok": true } |
    { "path": "string", "job_id": null, "ok": false, "error": "string" }
  ]
}
```

Semantics:

- Each path becomes one job in the registry, state `queued`. The tool
  returns after enqueue, not after the run; workers pick jobs up in
  submission order, one at a time (Section 3).
- A path that fails synchronous pre-validation (missing file,
  unreadable) gets `ok: false` immediately; pipeline failures after
  enqueue are reported through `status`/`manifest`, not here.
- `run` never blocks the message loop. This is the anti-babysitter
  contract (design.md section 4 zero-touch rule): the agent launches,
  then polls or is re-invoked later; nothing requires the MCP session
  to stay open.

### 4.5 `status`

Input:

```json
{ "job_id": "string, required" }
```

Output (the full runner observability contract, verbatim from
`RunState.to_dict()` plus queue position):

```json
{
  "job_id": "string",
  "state": "queued|running|succeeded|failed|cancelled",
  "queue_position": "int|null (null once started)",
  "file_path": "string",
  "current_phase": "probe|plan|prepare|upscale|encode|verify|done|null",
  "started_at": "number|null", "finished_at": "number|null",
  "last_activity_at": "number|null",
  "log_path": "string|null",
  "log_tail": ["string, up to 40 lines, newest last"],
  "commands": [["string"]],
  "phases": [{"name": "string", "started_at": "number", "finished_at": "number|null", "duration_seconds": "number|null"}],
  "engine_simulated": "bool",
  "engine_note": "string|null",
  "verification": "object|null (VerificationResult.to_dict())",
  "manifest_path": "string|null",
  "error": "string|null",
  "eta_seconds": "number|null — ONLY non-null when a measured benchmark constant exists for this engine/geometry class AND the run is in the upscale or encode phase; otherwise null, never projected"
}
```

Honesty rules (binding): `engine_simulated` and `engine_note` flow
through from the runner unchanged (roadmap clause d). `eta_seconds` is
null unless measured (clause c); the bridge never invents an ETA.

### 4.6 `cancel`

Input:

```json
{ "job_id": "string, required" }
```

Output:

```json
{ "job_id": "string", "state": "cancelling|cancelled|not-found|already-finished" }
```

Semantics (binding):

- Cooperative cancel through the runner seam: the registry flips the
  job's cancel flag; `run_file`'s `cancel` callable observes it. The
  upscale phase's child-process discipline (terminate, wait 10 s,
  kill) is the engine adapter's responsibility, identical to
  bluray-to-mp4's `_run_process` contract.
- Cancelling a queued job removes it without starting. Cancelling a
  finished or unknown job is a reported non-event, not an exception.
- A cancelled job's manifest is still written (the runner's failure/
  cancel manifest path) and states cancellation as the outcome. A
  cancel is not a failure and is not reported as one.

### 4.7 `list`

Input:

```json
{
  "limit": "int, default 20, clamped to [1, 200]",
  "include_history": "bool, default true"
}
```

Output:

```json
{
  "queued": [{"job_id": "string", "file_path": "string", "submitted_at": "number"}],
  "running": [{"job_id": "string", "file_path": "string", "current_phase": "string"}],
  "history": [
    {
      "job_id": "string", "file_path": "string",
      "state": "succeeded|failed|cancelled",
      "finished_at": "number", "ok": "bool",
      "engine_simulated": "bool",
      "output_path": "string|null",
      "manifest_path": "string|null"
    }
  ]
}
```

### 4.8 `manifest`

Input:

```json
{ "job_id": "string, required" }
```

Output: the parsed JSON content of the run's manifest file, verbatim,
plus envelope:

```json
{
  "job_id": "string",
  "manifest_path": "string|null",
  "manifest": "object|null — the full per-run artifact manifest exactly as written by the runner, including run.engine_simulated, run.engine_note, and any fail-open notes"
}
```

Binding: the bridge MAY NOT filter, rename, or redact manifest fields.
Fail-open notes and `engine_simulated` cross the MCP boundary
unchanged (draft constraint; roadmap clause d). If the manifest file
does not exist yet (job queued/running before manifest time), return
`manifest: null` with `manifest_path: null`, not an error.

## 5. Architecture (binding)

### 5.1 Module layout

One new module, no changes to existing pipeline modules:

```
vidlift/mcp_server.py        — entry point, MCP framing, dispatch
vidlift/mcp_bridge/
    __init__.py
    server.py                — stdio loop, JSON-RPC handling, tool dispatch
    tools.py                 — thin adapters: MCP args -> vidlift.* calls -> MCP payloads
    registry.py              — persisted job registry (Section 6)
    jobs.py                  — job objects, worker thread runner, cancel flags
    schemas.py               — input validation for each tool (the Section 4 shapes)
```

`vidlift.__main__` gains `vidlift mcp-server` (stdio server entry) so
packaged builds expose it with the same tool-discovery environment as
the CLI. Server startup resolves no media tools; tool discovery happens
per job exactly as `run_file` does today (explicit paths/env/PATH,
named `MissingToolError`), so a bridge misconfiguration surfaces as a
job error with the tool named, not a silent hang.

### 5.2 Dispatch and validation

Every incoming call: JSON-RPC decode → `schemas.py` validates the
input shape (unknown fields rejected, types checked) → `tools.py`
adapter → `vidlift.*` call → serialize the Section 4 payload. Adapter
functions are one screenful each and contain no logic beyond mapping;
any policy that sneaks in here is a Section 2 non-goal violation.

### 5.3 Log handling

- Bridge diagnostics (protocol errors, dispatch failures) go to stderr
  only — stdout is reserved for MCP frames.
- Run logs stay where the runner already puts them
  (`INPUT/.vidlift-runs/` or `--log-dir`); `status.log_tail` reads
  from the runner's in-memory/on-disk log, it does not duplicate it.
- The bridge itself keeps a small rotating log
  (`~/.vidlift/mcp-server.log` or a `--log-dir`-sibling) for
  server-level events: startup, client connect/disconnect, job
  lifecycle transitions. Rotating, capped (e.g. 5 MB), plain text.

### 5.4 Sequencing inside the bridge

Allowed transitions are enforced in the registry, not by caller
discipline: `queued → running → {succeeded, failed, cancelled}`;
`queued → cancelled`. `benchmark` jobs share the same machine.
Illegal tool orders (e.g. `manifest` before enqueue) are impossible by
construction — `job_id` values only exist for enqueued jobs.

## 6. Job registry persistence (binding)

- Location: `$VIDLIFT_STATE_DIR/jobs.json` where `VIDLIFT_STATE_DIR`
  defaults to `~/.vidlift` (Windows: `%LOCALAPPDATA%\VidLift`). JSON,
  not SQLite: the registry is small (hundreds of records), human-
  inspectable, and a corrupt-file recovery story ("delete the file,
  history is gone, media outputs are unaffected") beats a database
  for this scale.
- Atomic writes: write-to-temp + `os.replace`. The registry is the
  bridge's only mutable shared state; guarded by one lock.
- Contents per job: `job_id` (`vl-<yyyymmdd>-<short-uuid>`), kind
  (`run|benchmark`), input paths + options, state, timestamps, log/
  manifest/output paths, terminal error if any, and the benchmark
  cache table (Section 4.3).
- Restart semantics: on startup the bridge reads the registry. Jobs
  found in `running` state when no live thread owns them (i.e. the
  previous server died mid-run) are marked `failed` with error
  "server restarted during run; outcome unknown, see log" — honest,
  never silently "succeeded" and never silently dropped. Queued jobs
  across a restart resume automatically.
- A run that finishes while no server is up is impossible by
  construction (workers die with the server), so orphaned-but-done
  detection is not required; the `running → failed` sweep covers the
  real failure mode.

## 7. Error and honesty semantics (binding)

Machine-readable error codes in every `isError` payload:

| code | meaning |
|---|---|
| `invalid_input` | schema validation failed; message names the field |
| `probe_failed` | per-file probe error (carried in the per-file entry, not a tool-level error unless all files fail) |
| `plan_failed` | plan error (e.g. unsupported target) |
| `missing_tool` | `MissingToolError` surfaced; message names the tool |
| `not_found` | unknown `job_id` |
| `state_conflict` | e.g. cancel on a finished job (soft; see 4.6) |
| `internal` | anything else; message plus stderr pointer |

Honesty invariants (ported lessons, all testable):

1. `engine_simulated` and `engine_note` appear byte-identical in
   `status` and `manifest` payloads (roadmap clause d; fail-open
   reasons likewise when the engine fails open under the real
   adapter).
2. `eta_seconds` is null without a measured benchmark constant
   (clause c). A benchmark measured under the stub engine may only
   populate `per_plan_constant` with `engine_simulated: true` and a
   note saying so.
3. `plan.cannot_know` is present and non-empty (the plan knows it
   cannot state wall-clock time yet).
4. A failed upscale under the real adapter closes the job with the
   fail-open encode outcome clearly stated in the manifest; the
   bridge passes that through (clause d).
5. Every user-visible promise the bridge makes (the job launches, the
   output path, the manifest claims) has a Section 8 test at the MCP
   boundary (clause f; the v0.2.32 rule).

## 8. Acceptance tests (binding, Karen-enforceable, no GPU required)

All tests run headless with the stub engine and faked tool runners;
none requires a GPU or real video files beyond the existing synthetic
fixtures. GPU-dependent validation is explicitly out of scope for these
tests and flagged as a gap (g5 below).

- t1. Tool surface completeness: a schema test enumerates the eight
  tools and asserts the registered input/output shapes match Section 4
  exactly (field names, types, required-ness). Executes
  server-to-tools dispatch in-process over a stdio pipe pair.
- t2. 1:1 mapping: for `probe` and `plan`, assert the tool output
  equals what `vidlift.probe`/`vidlift.plan` produce for the same
  fixture, field by field — proving the bridge adds no policy
  (Section 2).
- t3. Async run lifecycle: `run` returns immediately with a job id;
  a stub-engine run on a synthetic input reaches `succeeded`;
  `status` during the run shows a live phase and non-decreasing
  `last_activity_at`; `manifest` after completion contains
  `engine_simulated: true` and the runner's own manifest bytes
  (byte-equal to the on-disk file).
- t4. Cancel: cancel a queued job (state `cancelled`, never runs);
  cancel a running job (cooperative cancel fires; the stub's
  pre-frame check raises; state `cancelled`, manifest written with a
  cancellation outcome, not a failure).
- t5. Registry persistence: enqueue a job, stop the server, restart
  with the same state dir — queued job still listed. Kill a server
  with a job in `running` (simulate by registry fixture), restart —
  job appears `failed` with the restart error string, never
  `succeeded`.
- t6. Honesty flow-through: with a fake engine whose note is
  "sentinel note" and `simulated=True`, assert `status`/`manifest`
  carry that exact string and flag (clause d at the boundary). Assert
  `eta_seconds` is null with no benchmark cached (clause c).
- t7. Benchmark honesty: under the stub engine, `benchmark` completes
  and its result carries `engine_simulated: true` plus a note that
  the numbers are stub numbers; a subsequent `plan`-adjacent ETA
  (when wired) is labeled simulated. Asserts the no-theory-ETA rule
  with only stub machinery.
- t8. Error codes: one test per Section 7 code reachable without a
  GPU (`invalid_input`, `probe_failed`, `plan_failed`,
  `missing_tool`, `not_found`, `state_conflict`), asserting both
  `isError` and the code string.
- t9. Boundary-rule execution proof (clause f): the boundary test
  suite fails if the MCP server module is absent — i.e. tests import
  and drive the real server over real stdio pipes, no silent skips;
  each test asserts its own execution (a canary assertion in each
  test body that a module-level counter incremented, checked in a
  session fixture).

Flagged gaps (hardware Eric has / Karen lacks):

- g1. Real-GPU run through the MCP bridge (x2plus on the 3090 Ti):
  validated on Eric's Windows box as part of the engine-adapter
  milestone's real-file validation, not in Karen's env.
- g2. Throughput truth of `benchmark` on the real engine: same env
  dependency. Karen validates the stub path and the honesty labels;
  Eric's box supplies the real constants.

## 9. Risks and mitigations

- R1. Stdio deadlocks from chatty child processes: the runner's
  process discipline (drain threads, no stdin) is inherited by the
  bridge unchanged; the bridge never proxies an interactive process.
- R2. Registry corruption on crash mid-write: atomic replace +
  per-field presence validation on load; on unrecoverable corruption
  the registry is reset with a stderr notice (media outputs are
  never affected — worst case is lost history).
- R3. MCP spec drift: the bridge pins the MCP library version in
  `pyproject.toml` and validates against OpenClaw's pinned revision;
  the registration step (`mcp.servers.vidlift`) happens only after
  Karen's validation of the live handshake.
- R4. Long sessions holding stale state: `status` always reads live
  runner state; the only cached data is the benchmark table, which is
  append-only and timestamped.
- R5. Agent-side misuse (launching 50 runs): the single-worker cap
  makes this a queue, not a fork bomb; `list()` shows queue depth;
  rate limiting beyond that is out of scope (local trust model).
