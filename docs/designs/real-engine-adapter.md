# VidLift Real-Engine Adapter (binding design)

Status: binding 2026-10-08. Author: Bob (architect). Implements
roadmap "Next" item 1 and satisfies acceptance clauses a–g verbatim.
Forge implements against this document; Karen validates against
Section 9's boundary-test list. Docs only — no code changes are part
of this milestone's design phase.

Provenance: the mechanics here are a disciplined port of
bluray-to-mp4's validated v0.2.20–v0.2.29 upscale path
(`bluray_to_mp4/upscale.py`, read-only reference) re-expressed
file-first. Where this document deviates from or narrows the sibling,
the deviation is stated and reasoned.

---

## 1. Why VidLift owns the file-first implementation

The roadmap's item-1 wording anticipated consuming a file-first entry
point `run_upscale(input_file_path, plan, cancel)` from the sibling
"once that sibling ships it." It has not shipped, and two facts make
waiting on it the wrong call:

1. `bluray_to_mp4/upscale.py`'s import graph pulls `makemkv`
   (`EncodeCancelled`) and `binaries` — disc-shaped modules. Importing
   the sibling's module would drag disc vocabulary across the engine
   seam, violating the hard rule (architecture.md: "If that boundary
   starts requiring disc concepts, keep VidLift separate and add an
   adapter"). design.md section 1's own gate says: if the seam does
   not hold, this becomes a (re)implementation — that gate has fired.
2. VidLift's packaging story (PyInstaller, bundled tools) needs the
   engine code inside this repo's dependency graph, not reach across
   two sibling checkouts with separate venvs.

**Binding decision:** the engine adapter lives in VidLift as
`vidlift/engine_ncnn.py`, porting the sibling's validated mechanics
clause-by-clause (each port cites its source behavior). The sibling
repo remains the validation and artifact source (models, lessons),
never a runtime import. If the sibling later ships a clean
media-agnostic seam, a future milestone may swap the internals behind
`EngineAdapter` without touching the runner; that swap is out of
scope here.

---

## 2. Module layout and file scope

New modules (all GUI-free, same discipline as probe/plan/run):

| file | owns |
|---|---|
| `vidlift/engine_ncnn.py` | `NcnnVulkanEngine(EngineAdapter)`, model table, tile derivation, chunk extraction/reassembly, duration gates, binary/model resolution with hash verification, `UpscaleError` / `EngineCancelled` |
| `vidlift/benchmark.py` | measured benchmark step (roadmap item 4) and the measured-ETA cache the planner/CLI/GUI/MCP all read (clause c) |
| `vidlift/tools_realesrgan.py` | discovery of the engine binary + model dir, mirroring `find_tools` semantics (explicit path → env → bundled location → named missing-tool error) |

Modified files, minimal and pinned:

| file | change |
|---|---|
| `vidlift/run.py` | engine selection (`--engine` / default real once validated, stub via `--engine stub`); catch `UpscaleError` → fail-open path (Section 5); catch `EngineCancelled` → cancel outcome; manifest schema v2 fields (Section 8); pass the prepared-file probe into the engine call |
| `vidlift/cli.py` | `run --engine {auto,stub,ncnn}` flag; `vidlift benchmark FILE` subcommand; both stay thin |
| `vidlift/plan.py` | one additive function `attach_estimate(plan, estimates)` so UI/CLI/MCP render measured ETAs from `benchmark.py`'s cache; no change to existing plan math |
| `docs/roadmap.md` | mark items 1 and 4 shipped when Karen signs off |

No changes to `probe.py` — the adapter reads `FileReport` and re-probes
the prepared file itself (Section 4.1).

---

## 3. The model registry (clause b: explicit tiling, correct scale)

One table is the ONLY source of engine `-n`/`-s` arguments:

```python
MODEL_TABLE = {
    "live-action": ModelSpec(name="realesrgan-x2plus", native_scale=2),
    # "anime": ModelSpec(name="realesr-animevideov3-x2", native_scale=2),
    #   second profile; enabled when a mode option exists (out of
    #   scope this milestone; hash pinning rule identical).
}
```

Binding rules:

- v1 ships the live-action profile only: `-n realesrgan-x2plus -s 2`.
  x2plus is a true native-2x RRDBNet (pixel_unshuffle head, verified
  in the x2plus VERIFICATION-REPORT.md): `-s 2` IS its native mode.
- `realesrgan-x4plus` never appears in any VidLift code path this
  milestone. A native-4x net at `-s 2` is the v0.2.25 corruption
  case, "corruption, not downsampling" (clause b). A test asserts no
  constructed engine command line ever pairs x4plus with `-s 2`, and
  that `-s` always equals the selected model's `native_scale`.
- Geometry consequence: the prepared 640x360-class square-pixel frame
  lands at exactly 1280x720 out of the net (verified: 640x360 →
  exactly 1280x720; odd sizes follow the correct 2x rule). The
  runner's encode phase already lanczos-scales to the planned 720p
  frame, so near-2x source classes (e.g. 854x480 → 1708x960) converge
  to plan geometry there. No segment-time scaling filter is needed
  when scale == 2 (the sibling's `segment_filter` degenerates to
  `setsar=1`, which the port keeps verbatim).

---

## 4. Frame-clock discipline (clause a)

### 4.1 Where the clock comes from

The adapter never trusts the source file's rate for re-stamping,
because the prepare phase may legitimately change it (today: bwdif at
field rate for flagged interlace; later: the sample-based
classification and IVTC). Binding flow:

1. The runner's prepare phase writes `prepared.mkv` (existing
   behavior: ffv1, video-only, filters preserve timing — no `-r` in
   the prepare command).
2. At engine entry, the adapter probes the PREPARED file with ffprobe
   (same `Runner` seam as everywhere; tests replay canned JSON) and
   resolves its clock in strict order:
   `avg_frame_rate_raw` (if it parses to a positive rate) →
   `r_frame_rate_raw` (same check) → FAIL with a named
   `UpscaleError("no usable frame clock on prepared file …")`.
   **Never a default.** No `25`, no `30000/1001`, no "assume source."
3. The resolved clock is carried as the exact rational STRING ffprobe
   reported (e.g. `"24000/1001"`, not `23.976` — float re-rendering
   is how drift gets invented) and recorded in the manifest as
   `frame_clock: {value, source: "avg_frame_rate"|"r_frame_rate"}`.
4. Uses of that clock, and only these:
   - chunk reassembly: `ffmpeg -framerate <clock> -i frame_%08d.png …`
     per segment (port of the sibling's `-framerate plan.fps`);
   - expected total frame count for progress/ETA:
     `ceil(prepared_duration × rate)`;
   - the duration gates in 4.2.
5. Chunk EXTRACTION uses wall-time addressing only:
   `-ss <start> -t <chunk_seconds> -i prepared.mkv` with the prepare
   filter degenerated to identity (frames are already normalized;
   extraction is a decode+copy-to-PNG, `frame_%08d.png`). Extraction
   never re-stamps. This mirrors the sibling, which extracts with
   `-ss/-t` and applies its prepare filter at extraction because ITS
   input is the raw rip; VidLift's prepare already happened in the
   runner's prepare phase, so the filter is not applied twice.

### 4.2 Duration sanity gates (fail open)

Two gates, both failures routed through Section 5, never raised to
the user as a dead job (ported `verify_mezzanine_duration` semantics,
tolerances verbatim):

```
tolerance = max(0.5 s, 0.0001 × reference_duration)   # 0.5s + 0.01%
```

- **Gate P (prepare check), at adapter entry:** |prepared_duration −
  source_duration| must be within tolerance of the SOURCE duration.
  Catches a prepare-phase bug (e.g. a future IVTC that forgot
  decimate, or a bwdif/fps mismatch) before hours are spent. An
  unknown source duration (probe gave None) fails the gate —
  ported from v0.2.29: a gate with a zero reference accepted anything.
- **Gate M (mezzanine check), after assembly:** |mezzanine_duration −
  prepared_duration| within tolerance of the prepared duration. This
  is the Mamma Mia gate: the 15.050 s drift (0.22% — under the old
  2% gate) exceeds max(0.5 s, 0.67 s…); a test pins that exact
  drift shape (6,838 s reference, +15.050 s → REJECT) alongside the
  acceptance shapes (−0.3 s on 120 s → ACCEPT; unknown reference →
  REJECT).

Both gate failures raise `UpscaleError` naming both durations and the
tolerance, and both fail OPEN (Section 5): the job ships the
synchronized original-resolution encode, never a desynced upscale and
never no output.

### 4.3 Regression pins (clause a)

Headless, canned-clock tests, no video needed:

- 23.976 (`24000/1001`), 25, 29.97 (`30000/1001`) prepared-clock
  probes each resolve to the exact string and flow into the
  `-framerate` argument byte-identically.
- avg missing/garbage → r_frame_rate used; both missing → named error;
  a test asserts no literal default rate string can appear.
- The Gate M reject/accept matrix above, including the 15.050 s
  Mamma Mia shape.

---

## 5. Fail-open contract (clause d) and engine selection

```python
class UpscaleError(RunError): ...        # engine failed; fail open
class EngineCancelled(RunError): ...     # cooperative cancel; NOT a failure
```

Runner behavior (the pinned `run.py` change):

```
upscale phase:
    try:
        engine_result = engine.run(prepared, plan, tools, cancel=…)
    except EngineCancelled:  → phase marked cancelled, manifest written
                               with outcome "cancelled", propagate as
                               cancel (MCP state "cancelled"), not failure
    except UpscaleError as exc:  → FAIL OPEN:
        log red-level warning with exc's reason
        mezzanine_path := prepared_path
        engine_note    := f"fail open: {exc}"
        fail_open      := True
        continue to encode phase (encode from prepared == today's stub
        behavior: a synchronized original-resolution encode at planned
        bitrate/geometry — a real, verified 720p HEVC file, no AI pass)
```

Honesty fields (clause d) in manifest schema v2 (Section 8), with the
`engine_simulated` rule pinned: **it describes the shipped pixels, not
the adapter class.** Stub run: `engine_simulated: true`. Real engine,
clean pass: `false`. Real engine, fail-open: `true` PLUS
`fail_open_reason` plus `engine` naming the attempted engine — the
manifest states exactly what shipped and why. MCP/CLI/UI pass these
through verbatim.

Default engine selection: `--engine auto` (default) = ncnn when the
binary+models resolve AND a GPU probe frame succeeds (lazy, cached per
process — the sibling's Section-5 "can this machine even run it"
pattern), else stub with `engine_note` saying why. `--engine stub`
forces the stub. `--engine ncnn` forces ncnn and turns resolution
failure into a named `MissingToolError`-class error. The stub stays
fully exercised: it is the fail-open destination and the CI/Karen path.

---

## 6. Tile derivation (clause b), ported verbatim in rule

Per chunk, AFTER frame extraction (the extent is measured, never
assumed — port of `_tile_size_arg` + `_first_png_extent`):

```
extent     = max(width, height) of first frame_%08d.png in the chunk
             (PNG header read, 24 bytes, no decoder dependency)
requested  = max(512, extent + 64)          # one tile, frame plus margin
tile_arg   = ceil(requested / 64) × 64      # driver-friendly multiple
```

For the 640x360 class: extent 640 → requested 704 → tile 704 (clause
b's stated number). An explicit `tile_size` operator override (env
`VIDLIFT_ENGINE_TILE`) is honored as a FLOOR only, exactly as the
sibling does.

Binding prohibitions, both test-pinned:

- `-t 0` (driver auto-tile) can never appear in an engine command
  line — auto-tiling reintroduced the v0.2.23 seam-line artifact. A
  test scans every constructed command for the token pair and fails
  on `-t 0` or a missing `-t`.
- The tile argument is never smaller than the measured frame extent.

---

## 7. Chunk flow, workdir, cancel, progress

Port of the sibling's loop, file-first:

1. `workdir = <log_root>/<run_id>.engine-work/`; kept on
   cancel/failure for debugging, deleted on success (the sibling's
   retention contract; supports clause e diagnosis).
2. `chunks = ceil(prepared_duration / chunk_seconds)`,
   `chunk_seconds = 120` default (min 10), env-overridable for tests.
3. Per chunk: extract (`-ss/-t`, Section 4.1) → assert ≥1 frame →
   engine call
   (`realesrgan-ncnn-vulkan -i in/ -o out/ -s <native_scale> -t <tile>
   -m <models> -n <model> -f png`) → assert ≥1 output frame →
   reassemble segment (`-framerate <clock>`, ffv1, `-level 3`,
   `setsar=1`) → delete in/out dirs immediately (frame-dir peak stays
   ~1–2 GB; mezzanine grows like the sibling's, sized and deleted at
   stage end).
4. One segment → move into place; many → concat demuxer (`-f concat
   -safe 0 -c copy`) into `<run_id>.upscaled.mkv` (the mezzanine path
   the runner already expects from `EngineResult`).
5. Gate M (4.2), then return `EngineResult(mezzanine_path,
   frames_done=Σ output frames, note=None, simulated=False)`.

Process discipline: the sibling's `_run_process` contract is ported as
a private helper — drain threads on stdout/stderr, 0.2 s cancel poll,
`terminate` → wait 10 s → `kill` → wait 5 s. Cancel between chunks is
the cheap kill; cancel mid-chunk terminates the child. Partial chunk
dirs are cleaned on cancel; completed segments stay in the kept
workdir on failure.

Progress: the adapter gains the seam the runner state contract
already anticipates — `on_progress(frames_done, frames_total)` invoked
per chunk and (cheaply) inside the engine drain when the binary prints
parseable progress. The runner records it into `RunState`; `RunState`
grows `progress: {frames_done, frames_total} | None`. ETA (clause c):
the runner/MCP compute `eta_seconds` ONLY as
`remaining_frames × seconds_per_frame` from a MEASURED benchmark
constant for this engine+geometry bucket; absence → null, never
projected.

---

## 8. Measured-ETA data flow (clause c; roadmap item 4)

`vidlift/benchmark.py`:

```python
@dataclass(frozen=True)
class BenchResult:
    engine: str            # adapter class name
    engine_simulated: bool
    model: str | None      # "realesrgan-x2plus" under the real engine
    native_scale: int | None
    geometry_bucket: str   # e.g. "640x360", grouped by extent class
    duration_sliced_s: float
    frames: int
    wall_seconds: float
    seconds_per_frame: float
    measured_at: float     # epoch
    host_note: str         # e.g. "RTX 3090 Ti" / "stub"
    notes: list[str]

def run_benchmark(sample_path, *, tools, engine, slice_seconds=30,
                  workdir, runner=...) -> BenchResult
def load_estimates(state_dir) -> dict[str, BenchResult]   # keyed cache
def estimate_eta(plan, *, estimates) -> float | None      # None when no bucket
```

Semantics (binding):

- The benchmark runs the REAL seam: prepare+extract ONE slice (default
  30 s, clamped [10, 120]) through the actual engine adapter over the
  actual model, timing the engine phase. It never computes from
  throughput tables (the sibling's Section-1.1 numbers were
  explicitly "to be pinned by a probe run"; that culture carries).
- Under `--engine stub` the benchmark measures pipeline machinery and
  MUST stamp `engine_simulated: true` plus a note "stub numbers; not
  an AI-pass measurement." A stub-derived ETA may only ever be
  displayed labeled `simulated`.
- Cache: `$VIDLIFT_STATE_DIR/benchmarks.json`, keyed
  `(engine, model, geometry_bucket)`; append-only, atomic write. The
  planner (`attach_estimate`), the CLI, the GUI, and the MCP bridge
  all read THIS cache — one source of truth, matching the bridge
  spec's Section 4.3/4.5 contract.
- Plan ETA: `duration_seconds × seconds_per_frame × fps_of_plan` —
  computed as `frames_total × seconds_per_frame` and nothing else;
  no bucket → `cannot_know` entry, ETA null.

---

## 9. Model and binary artifacts (clause g)

Consumed, never re-derived. Pinned hashes from the x2plus
VERIFICATION-REPORT.md (artifacts archive:
`/home/yetisoldier/projects/x2plus-ncnn-verified/`, live copies in the
sibling at `packaging/tools/models/`):

| artifact | bytes | sha256 |
|---|---|---|
| `realesrgan-x2plus.param` | 110,995 | `f26dc4b8d7c467a86fac91f63d361a762ef5ab13ccb0521d3081b1b3152e9a6f` |
| `realesrgan-x2plus.bin` | 33,434,888 | `cfb044e3a1fe18750f54f7a3d8c2d49a296b54e2c9489c90513bbf88844e9e69` |

- Bundle time: packaging copies THESE files (byte-identity asserted
  against the table above) into `tools/realesrgan/models/`; a
  bundle-time script fails the build on any mismatch. The table lives
  in `vidlift/tools_realesrgan.py` as data with the verification
  report cited.
- Runtime: `resolve_realesrgan()` resolution order — explicit option
  (`--realesrgan`) → `VIDLIFT_REALESRGAN_NCNN` env → bundled
  `tools/realesrgan/` location → named missing-tool error listing
  what was tried (same shape as `find_tools`; never a silent PATH
  guess). Model dir resolution mirrors the sibling: explicit →
  `VIDLIFT_REALESRGAN_MODELS` → `models/` beside the binary.
- First use per process verifies both x2plus hashes once (<1 s for
  33 MB, once). A mismatch is a named `UpscaleError` BEFORE any frame
  is extracted: a corrupted or substituted model is never run
  silently. Under fail-open (Section 5) that yields the honest
  fallback, with the hash mismatch stated as the reason. Hash check
  results are recorded in the manifest (`model_hashes_verified`).
- Engine binary provenance: official Real-ESRGAN-ncnn-vulkan release
  (the verification report's engine, sha256 prefix `22c2296a…`,
  identical across the v0.2.0 and 20220424 zips); the FULL binary
  hash is pinned in the same table at integration time and recorded
  here when done. The animevideov3-x2 hashes get pinned the same way
  when the anime profile milestone opens.

---

## 10. v0.2.32-compliant boundary tests (clause f; Karen-enforceable)

Every test runs headless, no GPU; the engine binary call goes through
the runner seam. GPU-dependent validation is listed as flagged gaps
(u1–u3). A meta-test (b0) guards the guard: the suite fails if the
engine module or any of b1–b9 is collected-skipped — the v0.2.32
filename-handoff rule, "every user-visible promise has one test at the
highest boundary it crosses, and the test provably executes."

- b0. Execution canary: every boundary test increments a session
  counter; a session-scoped assertion requires counter == expected
  count, and a test asserts the engine module imports (no silent
  module skip).
- b1 (clause a). Clock flow: canned ffprobe JSON for the prepared
  file at 23.976/25/29.97; assert the exact rational string lands in
  the assembled `-framerate` argument (highest boundary: the ffmpeg
  command line, via fake runner recording).
- b2 (clause a). Fallback+refusal: avg absent → r used; both absent →
  named `UpscaleError`, job fails open, manifest carries the reason.
- b3 (clause a). Duration gates: the Section 4.3 matrix incl. the
  15.050 s Mamma Mia shape and the unknown-reference refusal.
- b4 (clause b). Model/scale pairing: the registry drives `-n realesrgan-x2plus -s 2`; constructing x4plus-at-2 is impossible through the table.
- b5 (clause b). Tiling: extent 640 → `-t 704`; `-t 0` never appears; tile < extent is impossible; explicit-env floor honored.
- b6 (clause c). ETA honesty: with no cached benchmark, `estimate_eta` returns None and the plan surface says it cannot know; with a stub-sourced constant, the ETA is labeled simulated; a real-bucket constant produces `frames × seconds_per_frame` exactly.
- b7 (clause d). Fail-open end-to-end: fake engine raises `UpscaleError("gate M: mezz 6838.0 vs prep 6853.1")` mid-run; the run COMPLETES, output is the synchronized re-encode (verified by the existing verify stage), manifest v2 shows `engine` attempted, `engine_simulated: true`, `fail_open_reason` verbatim. This is the OCR-babysit lesson as a test.
- b8 (clause f, applied to user-visible promises). Highest-boundary sweep on stub: for the existing runner promises (output filename `*.vidlift.720p.hevc.mp4`, final geometry equals plan geometry, audio mapped from the ORIGINAL file, manifest honesty fields) one test each at the command/manifest boundary — most already exist in `tests/test_run.py`; this item pins the engine-touching ones.
- b9 (clause g). Hash verification: fixture model files with correct/incorrect hashes; first use verifies, mismatch → named error before extraction, reason in manifest. Uses tiny stand-in files with computed expected hashes (the real 33 MB artifacts are hashed once in a marked-slow test Eric's box runs).
- b10. Chunk-flow shape: fake engine binary (the runner seam records argv and touches expected output PNGs); N-chunk input produces N recorded engine invocations with monotone `-ss`, one concat, in/out dirs deleted, workdir retained rules followed, Gate M executed with the probed durations.
- b11. Cancel: pre-first-frame cancel → `EngineCancelled`, state cancelled-not-failed; mid-chunk cancel via the fake runner → terminate/wait/kill sequence observed, partial chunk cleaned, manifest records cancelled.
- b12. Full decode pass verification: the runner's deferred full-decode (`ffmpeg -v error -i out -f null -`) lands WITH this milestone (roadmap item 1 includes it); test asserts it runs on the encode output and its failure fails verification.

Deferred within this milestone (stated, not hidden): the sample-based
three-way classification (progressive / hard 3:2 telecine / genuine
interlace) — roadmap item 1 lists it as part of the real-engine
milestone but it changes the PREPARE chain, not the adapter; it is
scheduled as its own work package (Forge brief P4) with its own
boundary tests (idet-fixture-driven), and until it lands the probe's
`interlaced_flagged` drives bwdif exactly as today.

Flagged gaps (Eric's hardware, Karen's env cannot supply them — each
gets the stub/simulated coverage above and an explicit hand-off
checklist for Eric):

- u1. Real x2plus throughput on the 3090 Ti (the benchmark constant
  that makes ETAs real; gates "measured ETA" user-facing claims).
- u2. Visual/spot integrity of engine output frames on real content
  (the verify stage covers structural truth; pictures get the
  sibling's v0.2.26 lesson applied — triage against source frames
  first, clause e — during Eric's real-file validation, roadmap
  item 6).
- u3. Windows packaged-path discovery (bundled tools resolution under
  PyInstaller; roadmap packaging discipline).

---

## 11. Manifest schema v2 (additive; v1 fields unchanged)

```json
"run": {
  "engine": "NcnnVulkanEngine|StubEngine",
  "engine_model": "realesrgan-x2plus|null",
  "engine_native_scale": 2,
  "engine_simulated": true,
  "engine_note": "string|null",
  "fail_open": false,
  "fail_open_reason": "string|null",
  "frame_clock": {"value": "24000/1001", "source": "avg_frame_rate"},
  "model_hashes_verified": true,
  "duration_gates": {
    "prepare_vs_source": {"prepared_s": 6853.1, "source_s": 6853.0, "tolerance_s": 0.685, "ok": true},
    "mezzanine_vs_prepared": {"mezzanine_s": 6853.2, "prepared_s": 6853.1, "tolerance_s": 0.685, "ok": true}
  },
  "benchmark_constant": {"seconds_per_frame": 1.83, "engine_simulated": false, "measured_at": 1760000000} | null
}
```

Consumers (CLI/MCP/UI) pass every field through verbatim. v1 manifests
remain valid; new fields are additive with the null/absent rule.

---

## 12. Risks

- r1. Binary drift: the ncnn binary is third-party; pinned-hash
  resolution + env overrides mitigate; a bad binary fails the lazy
  probe-frame check and falls open to stub with the reason recorded.
- r2. Chunk-boundary cadence artifacts: ffv1 segments + concat copy is
  the sibling's validated shape; Gate M plus the full-decode verify
  (b12) catch assembly damage structurally; picture-level triage is
  u2 on Eric's real files.
- r3. ETA misuse: constants are bucketed and timestamped; surfaces
  must show the measurement provenance; the bridge spec makes
  simulated labeling testable (t7 there / b6 here).
- r4. Scope creep into disc vocabulary: module review checkpoint —
  any identifier referencing discs/rips/titles in the new modules
  fails review; the ONLY disc-shaped name allowed anywhere is the
  citation comments pointing at the sibling's file (as this document
  does).
