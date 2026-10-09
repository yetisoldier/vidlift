# VidLift MCP Bridge (draft sketch)

Status: draft 2026-10-08, written from the bluray-to-mp4 ported
lessons. The architect (Bob) polishes this into binding requirements
and architecture before any implementation.

## Intent

Expose the VidLift runner as a local MCP (Model Context Protocol)
server so AI agents (Jarvis via OpenClaw, same pattern as REA's
`rea-mcp` wrapper registered as `mcp.servers.rea`) can drive
upscales end to end: probe, plan, benchmark, launch, watch, cancel,
and read manifests. "Full bridge" means the entire single-file
runner state contract is reachable over MCP, not a status toy.

## Non-goals

- No network listener; stdio transport only, local machine.
- No second implementation: every tool maps 1:1 onto the same
  pipeline code the CLI and GUI use. One source of truth (the
  v0.2.32 filename-handoff lesson: a duplicated source of truth is
  how the reviewed title got discarded between boundaries).
- No new policy inside the bridge: decisions stay in the pipeline.

## Draft tool surface (Bob to finalize)

- `probe(paths)` -> per-file classification reports (candidate /
  already-hd, PAL-speedup suspicion, honest unknowns).
- `plan(paths, options)` -> batch plan incl. per-file output paths,
  target geometry, estimated size; states what it cannot know.
- `benchmark(sample_path)` -> measured chunk timing on this machine
  (ported lesson 3: measured, never theoretical).
- `run(paths, options)` -> job id(s); async, returns immediately.
- `status(job_id)` -> phase, progress, last-activity timestamp, ETA
  (only when derived from measured chunks), log tail + log path,
  verification result.
- `cancel(job_id)` -> cooperative cancel through the runner seam.
- `list()` -> queue plus recent history.
- `manifest(job_id)` -> the full per-run artifact manifest with
  `engine_simulated` and fail-open notes verbatim (ported lesson 4).

## Constraints

- Persisted job registry (JSON or SQLite) so a server restart does
  not orphan running jobs or history.
- Manifest honesty fields flow through MCP unchanged.
- Registered locally as `mcp.servers.vidlift` only after Karen
  validates; never exposed publicly.
- Windows-friendly: must run under the same packaged tool discovery
  rules as the app (explicit not-found errors, bundled-tool paths).