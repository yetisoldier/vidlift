# Sequencing & Forge Implementation Brief

Status: binding recommendation 2026-10-08. Author: Bob (architect).
Context: Eric approved the chain Bob designs -> Forge implements ->
Karen validates on 2026-10-08. This document is deliverables 3 and 4:
which milestone ships first, and the ordered work packages Forge
implements with the acceptance tests Karen enforces.

---

## A. Sequencing verdict

**Engine adapter FIRST. MCP bridge SECOND, shipping initially against
the stub-tested, real-engine-backed runner.**

Concretely:

1. Real-engine adapter + benchmark step (roadmap items 1 and 4,
   `docs/designs/real-engine-adapter.md`) land first, on the stub's
   already-proven pipeline, with the real engine validated on Eric's
   Windows box as the milestone's hardware gate.
2. MCP bridge (roadmap item 2, `docs/designs/mcp-bridge.md`) lands
   second. By construction it works on day one against whatever engine
   the runner defaults to — including the stub — because the bridge is
   a 1:1 mapping with no pipeline logic. Karen validates the full tool
   surface headless on the stub path; the real-engine path is the same
   tools with different manifests, already covered by the adapter
   milestone's tests.

Dependencies that force this order:

- **The benchmark tool has no honest existence without the engine.**
  The bridge's `benchmark` tool is the measured-ETA contract (clause
  c). Measured under the stub, it returns pipeline-machinery numbers
  that must be labeled `simulated`; the tool is only worth exposing
  once the real adapter exists — otherwise the bridge ships its most
  novel tool in the one configuration where its numbers are unusable
  for real ETAs, and trains its first consumers to distrust it.
- **The bridge inherits the adapter's honesty surface.** Manifest v2
  (`fail_open_reason`, `frame_clock`, `duration_gates`,
  `model_hashes_verified`) is defined by the adapter design; the
  bridge spec passes those fields through verbatim. Building the
  bridge first means a protocol revision the moment the adapter
  lands; building the adapter first means the bridge freezes against
  a stable schema. (Manifest v2 is additive, so the risk is churn,
  not breakage — but churn at a protocol boundary is exactly the
  v0.2.32 failure class this project is organized to avoid.)
- **The runner seam is already the bridge's whole job.** Status,
  cancel, manifest, and log-tail plumbing exist in `RunState` today
  and are engine-agnostic by construction; the engine swap changes
  nothing in `registry.py`/`server.py`/`tools.py`. The reverse is not
  true: the adapter's ETA and fail-open data flows are new pipeline
  surface the bridge must expose.
- **Risk value.** The real engine is the project's highest-risk item
  (hours-long GPU jobs, chunk lifecycle, drift gates). Shipping it
  first converts the bridge milestone into low-risk pure plumbing on
  a stable runner. Shipping the bridge first produces a validated
  bridge over an engine nobody has ever run.

Why the bridge-on-stub question does not flip the verdict: the bridge
*can* ship on the stub (its acceptance suite is deliberately
GPU-free), and operationally that is exactly what happens — Karen's
environment has no GPU, so her validation of the bridge uses the stub
path regardless. The ordering claim is about which milestone is built
and hardware-validated first, not about which engine the bridge is
tested against. The bridge does not wait for Eric's Windows
validation; it waits for the adapter's schema and seam, which land in
the adapter milestone's headless-complete portion (P1–P3 below).

Timeline note (effort, from the designs): the adapter is ~3 work
packages; the bridge is ~2 packages; both are small compared to a
misordered protocol revision.

---

## B. Forge implementation brief

Ordered work packages. Each package lists: file scope (what Forge may
touch), what it builds, and the acceptance tests Karen will be told to
enforce. Tests live in `tests/` and must satisfy the v0.2.32 rule:
each test provably executes (the per-module canary pattern from
mcp-bridge.md t9 / real-engine-adapter.md b0).

### P1 — Real-engine adapter core

File scope: `vidlift/engine_ncnn.py` (new),
`vidlift/tools_realesrgan.py` (new), `tests/test_engine_ncnn.py`
(new).

Builds:

- `NcnnVulkanEngine(EngineAdapter)` with the Section-3 model table
  (live-action only: `-n realesrgan-x2plus -s 2`), Section-6 tile
  derivation (extent + 64, ceil to 64, floor 512, `-t 0` forbidden),
  Section-7 chunk loop (extract `-ss/-t`, engine, ffv1 segment at the
  prepared clock, concat), `_run_process` port (drain threads,
  terminate/wait-10/kill-5, 0.2 s cancel poll),
  `UpscaleError`/`EngineCancelled`.
- Section-4 frame clock: probe the prepared file, strict
  `avg_frame_rate` → `r_frame_rate` rational-string resolution, never
  a default; `-framerate` uses that exact string; manifest
  `frame_clock` record.
- Section-4.2 gates P and M with `max(0.5 s, 0.01%)` tolerances and
  the unknown-reference refusal.
- Section-9 resolution + once-per-process x2plus sha256 verification
  (hashes pinned in the design doc's table).
- Workdir retention: deleted on success, kept on failure/cancel.

Karen enforces (from real-engine-adapter.md Section 10): b0, b1, b2,
b3, b4, b5, b9, b10, b11 — all headless with fake runways and canned
ffprobe JSON. Hardware gate for the milestone (Eric, not Karen): one
real file end-to-end on the 3090 Ti with Gate P/M passing and the
manifest v2 fields present.

### P2 — Runner integration and manifest v2

File scope: `vidlift/run.py`, `vidlift/cli.py`,
`tests/test_run.py` (extend), `tests/test_cli.py` if it exists by
then (else extend test_run.py).

Builds:

- Engine selection: `--engine {auto,stub,ncnn}` on `vidlift run`,
  `auto` = ncnn when resolution + lazy GPU probe-frame succeeds else
  stub with note; `ncnn` forces and names failures. Stub remains the
  test/CI/fail-open engine.
- Fail-open: `UpscaleError` in the upscale phase logs the warning,
  encodes from `prepared_path`, and completes the run;
  `EngineCancelled` is a cancellation outcome, not a failure.
- Manifest schema v2 additive fields (real-engine-adapter.md Section
  11), passed verbatim; `engine_simulated` describes shipped pixels.
- Full decode pass in verify (`ffmpeg -v error -i out -f null -`) as
  part of this milestone (b12).
- `RunState.progress` plumbing (`frames_done/frames_total`) fed by the
  adapter's progress seam; ETA computed only from the P3 benchmark
  cache, else null.

Karen enforces: b7 (fail-open end-to-end on a fake failing engine),
b8 (highest-boundary promise sweep), b12; existing test_run.py suite
stays green byte-for-byte on the stub (manifest v1 consumers must not
break — v2 is additive).

### P3 — Measured benchmark step

File scope: `vidlift/benchmark.py` (new), `vidlift/cli.py` (`vidlift
benchmark FILE`), `vidlift/plan.py` (additive `attach_estimate`),
`tests/test_benchmark.py` (new).

Builds: `run_benchmark` through the real seam (prepare + one slice
through the active engine, engine-phase timed), `BenchResult`, the
append-only `benchmarks.json` cache keyed
(engine, model, geometry bucket), `estimate_eta` returning
`frames × seconds_per_frame` or None. Stub-derived constants are
stamped `engine_simulated: true` with the not-an-AI-measurement note.

Karen enforces: b6 (ETA honesty: none-without-cache, simulated-labeled
under stub, exact arithmetic under a cached bucket); t7-adjacent
coverage for the CLI surface (`vidlift benchmark` prints provenance).

### P4 — Sample-based classification (deferred slice of item 1)

File scope: `vidlift/classify_sample.py` (new), `vidlift/run.py`
(prepare command builder only), `tests/test_classify_sample.py`
(new).

Builds: the 60-second `idet`-driven three-way classification
(progressive / hard 3:2 telecine → fieldmatch+decimate to 24000/1001 /
genuine interlace → bwdif at field rate + `fps=60000/1001`), driving
the prepare filter chain. The reference chains in bluray-to-mp4's
v0.2.19 design Section 3.3 are adopted verbatim. Until P4 lands, the
probe flag drives bwdif (today's behavior) — P4 changes prepare, never
the seam.

Karen enforces: new boundary tests for each of the three arms
(canned idet statistics fixtures; asserting the exact filter string in
the recorded prepare command and the resulting prepared-clock that P1
re-probes), plus a regression test that unflagged progressive sources
get NO deinterlace filter. P4 is ordered after P1–P3 but may ship in
the same milestone if validation allows; the milestone is not blocked
on it, and the roadmap text for item 1 keeps it listed until it lands.

### P5 — MCP bridge

File scope: `vidlift/mcp_server.py`, `vidlift/mcp_bridge/` (new
package per mcp-bridge.md Section 5.1: `server.py`, `tools.py`,
`registry.py`, `jobs.py`, `schemas.py`), `vidlift/__main__.py`
(`vidlift mcp-server` entry), `tests/test_mcp_bridge.py` (new).
No changes to probe/plan/run/engine modules.

Builds: the eight tools (probe, plan, benchmark, run, status, cancel,
list, manifest) exactly per mcp-bridge.md Section 4; stdio-only
transport; the JSON registry with atomic writes, restart sweep
(running → failed with the restart error), single-worker concurrency;
manifest v2 pass-through. Registration as `mcp.servers.vidlift`
happens only after Karen signs off.

Karen enforces (from mcp-bridge.md Section 8): t1–t9 — tool-surface
schema test, 1:1 mapping proof, async lifecycle, both cancel paths,
persistence + restart sweep, honesty flow-through (byte-identical
`engine_simulated`/`engine_note`; null ETA without a measured
constant), stub-benchmark labeling, per-code error tests, and the
execution-canary meta-test. All headless against the stub engine.

### Hand-off checklist (Eric, hardware Karen lacks)

Recorded in the designs as flagged gaps; Eric runs these on his
Windows box at the adapter milestone's end: u1 real 3090 Ti benchmark
constants via `vidlift benchmark` on one XviD-class and one
MPEG-2-class sample; u2 visual spot-check of engine frames vs source
frames on 2–3 real collection files (clause e triage rule: defects
checked against source pictures before the engine is blamed); u3
packaged-path tool discovery smoke test. These close the loop for
roadmap item 6 validation and gate the "measured ETA" user-facing
claims.

---

## C. What was deliberately left out of scope

- The anime engine profile (`realesr-animevideov3-x2` + a mode
  option): the model table and hash-pinning rule cover it, but no
  UI/CLI/MCP mode switch ships this milestone; enabling it later is a
  one-row table change plus its own validation.
- RVE/TensorRT primary lanes from the sibling's three-lane strategy:
  VidLift ships ncnn-only this milestone; the engine seam admits a
  later lane without runner changes.
- Batch queue and UI start/cancel wiring (roadmap item 5): comes after
  the bridge; the bridge's job registry is its natural substrate.
- MCP over any network transport, authentication, or multi-user
  semantics: permanently out per the bridge spec's binding non-goals.
- Retry-with-backoff inside the runner (roadmap item 3 lists retry
  under "observability hardening"): single cooperative cancel +
  fail-open is the contract this milestone; retry is a later,
  separately designed item.
- The 1080p target, image-subtitle OCR, PAL speedup correction,
  metadata lookup, watch folders: roadmap "Deferred" list, untouched.
