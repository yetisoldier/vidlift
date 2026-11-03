# Architecture Specification — VidLift v0.4.16.2: VAD asset packaging defect

**Author:** Bob (Principal Software Architect)
**Date:** 2026-11-03
**Status:** Recommended — ready for Forge
**Task:** `.specs/hermes-brief-vidlift-v0.4.16.2-silero-vad.md`
**Target release:** v0.4.16.2 (hotfix; `api_version` stays 1; the shipped installer must transcribe)

---

## 1. Executive Summary

The v0.4.16.1 Windows installer cannot transcribe: every job that reaches the
`transcribe` phase dies with `NO_SUCHFILE ... _internal\faster_whisper\assets\
silero_vad_v6.onnx`. Verified root cause, end to end:

1. **faster-whisper resolves its VAD model from its own package directory.**
   `faster_whisper/utils.py::get_assets_path()` (installed 1.2.1) is literally
   `os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")` — no
   `sys._MEIPASS` branch, no fallback. `faster_whisper/vad.py:291` joins that dir
   with the hardcoded leaf `silero_vad_v6.onnx` and hands it to
   `onnxruntime.InferenceSession` (`vad.py:310`). Under PyInstaller the module's
   `__file__` resolves to `<_internal>\faster_whisper\utils.py`, so the loader
   looks in `<_internal>\faster_whisper\assets\` — a directory that only exists
   on disk if the build PUT package data there.
2. **The build never puts it there.** Both `vidlift_gui.spec` and
   `vidlift_cli.spec` carry `datas=[]`, and git history shows **both specs have
   been byte-identical since `f810724` ("release: add Windows 11 installer
   packaging")** — no shipped Windows build has ever contained the asset. `grep
   silero` across the repo: zero hits.
3. **Nothing in the release path can see it.** The Linux gate (921 green) never
   imports `faster_whisper` (the documented test-seam rule — which is exactly why
   the suite is fast and hermetic), and the CI "Validate installed artifact" step
   checks tools, runs `--version`, and launches the GUI for 5s — but never asserts
   the frozen Python-payload tree and never exercises transcription.

**Recommendation — five moves, layered, all additive:**

- **M1 — Bundle the package data** (both specs, `collect_data_files("faster_whisper")`,
  whole-package). This is the actual fix; everything below is defense in depth.
- **M2 — One honest checker family** in `vidlift/transcribe.py`: an import-free
  filesystem report (`vad_asset_report`, `find_spec`-based with a frozen `_MEIPASS`
  fallback, resolver injectable for tests) and an authoritative build-gate report
  (`selftest_report`, which makes the frozen binary itself load the asset through
  faster-whisper's own code path). Plus `VadAssetMissingError(TranscriptionError)`.
- **M3 — Runtime honesty:** a VAD preflight at the top of `transcribe_source_to_srt`
  (raises the named error before any ffmpeg/model work; Mode A fail-open and Mode B
  job-failure dispositions come **for free** through existing `except TranscriptionError`
  arms), a TOCTOU backstop inside `WhisperEngine.transcribe_segments`, and a carve-out
  so the CUDA-fallback wrapper's tier-2 decode arms stop mislabeling an honest
  `TranscriptionError` as a device fault (today a missing asset on a CUDA box would
  trigger a wasted CPU rebuild and a *dishonest* "cuda unavailable" manifest note).
- **M4 — Build/release gates:** the frozen CLI gains `--selftest` (JSON, exit 0/1);
  CI runs it against the dist tree **and** the installed payload, plus a
  name+size parity check of `_internal\faster_whisper\assets` against the build
  venv's `faster_whisper\assets` (self-updating against upstream asset renames).
- **M5 — Detectability:** additive envelope key `transcription_engine` on the
  monitor's `GET /status` (machine-scoped, derived-at-read, honest-null; mirrors the
  v0.4.16.0 `transcription_model` envelope precedent). `api_version` stays 1.

Plus **M0 — the regression net**: one NEW test file `tests/test_v04162_vad_packaging.py`
(spec-text contracts, workflow-gate contracts, a pinned asset-record tripwire that
runs on Linux against the dev venv *without importing faster-whisper*, and unit
coverage of preflight/carve-out/selftest/envelope). No pre-existing test is modified.

**One-line reason:** the defect is a packaging omission, so the fix is a packaging
change — but a packaging change alone ships unverifiable, so v0.4.16.2 also teaches
the *frozen binary itself* to answer "can you transcribe" and makes CI ask it.

---

## 2. Requirements

### 2.1 Functional (brief §4, mapped)

| ID | Requirement (brief §) | Where it lands |
|----|------------------------|----------------|
| F1 | Bundle `faster_whisper` package data — whole `assets/` dir, both specs, rename-proof **(§4.1)** | `vidlift_gui.spec`, `vidlift_cli.spec` (`collect_data_files`); rename immunity via the parity gate (F2) + pinned-record tripwire (M0) |
| F2 | Fail loudly at build time, not at the user's first job **(§4.2)** | New CLI `--selftest` + two workflow insertion points (post-build, post-install) + suite contract tests |
| F3 | Graceful runtime degradation decision **(§4.3)** | ADR-3: **preflight-refuse with a named, actionable error**; TOCTOU backstop; no silent quality loss |
| F4 | Status-contract detectability, additive only **(§4.4)** | `transcription_engine` envelope key on monitor `/status`; field contract in §6.5 |
| F5 | A regression test that would have caught this **(§4.5)** | `tests/test_v04162_vad_packaging.py` (the ONLY test file touched — it is new) |

### 2.2 Non-functional (constraints — non-negotiable)

- **N1 Additive status contract only; `api_version` stays 1** (monitor.py:52-55
  convention; the v0.4.1 subtitles-block and v0.4.16 `transcription_model` precedents).
- **N2 No new dependencies. No new endpoints. No new MCP tools.** (`--selftest` is a
  CLI flag, precedented by v0.4.15.4's `--no-transcribe`.)
- **N3 The deferred-import rule survives.** The suite must continue to never import
  `faster_whisper`. Every new suite-visible path is import-free (find_spec + fs) or
  env-guarded (the selftest's real import is skipped under
  `VIDLIFT_TRANSCRIPTION_ENGINE=0`, which conftest pins suite-wide at
  `tests/conftest.py:41-43`).
- **N4 Existing suite passes unmodified.** Currently 921 passed, 2 skipped on
  `~/.venvs/vidlift/bin/python` (3.12.3, pytest 9.1.1).
- **N5 Honest-null culture.** Unknown is `null`, never fabricated (the
  `model_install_state` OSError→"unknown" discipline, transcribe.py:1300-1304).
- **N6 Reuse the existing `vad_on` seam.** No parallel machinery; the constructor
  parameter is the only switch.
- **N7 Out of scope (noted, not designed):** the `cublas64_12.dll` GPU/CUDA packaging
  failure (one history row), Karen's K-1 stale-overlay edge, and the ≥400 s
  language-detect stall observed live on job `vl-20261103-3ef5b3a8` (§5.4).

---

## 3. Current-State Analysis (verified in source, 2026-11-03)

### 3.1 The resolution path and why it breaks frozen

```
faster_whisper.vad (1.2.1)
  L10   from faster_whisper.utils import get_assets_path
  L291  path = os.path.join(get_assets_path(), "silero_vad_v6.onnx")   # leaf hardcoded upstream
  L310  self.session = onnxruntime.InferenceSession(path, ...)         # load here
faster_whisper.utils
  get_assets_path() = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
```

- **Frozen (one-folder GUI):** `utils.__file__` → `<install>\_internal\faster_whisper\
  utils.py` (the `.pyc` actually lives in the PYZ archive; the path is virtual).
  `get_assets_path()` therefore names `<install>\_internal\faster_whisper\assets` —
  exactly the directory PyInstaller's data collection would have populated, byte for
  byte, had the spec asked for it. The observed error path matches this derivation
  literally. **The collector destination and the package's own resolver coincide by
  construction** — collecting the data is sufficient; no runtime path patching, no
  hook trickery.
- **Frozen (one-file CLI):** datas extract to the runtime `_MEIPASS` dir under the
  same relative shape; the same argument holds. Bundling both specs is correct in
  both layouts.
- **Dev/Linux:** site-packages really contains it
  (`…/site-packages/faster_whisper/assets/silero_vad_v6.onnx`, 1,245,151 bytes,
  sha256 `4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2` —
  verified on the dev box today). The suite green-lit a broken frozen build because
  the suite's world genuinely has the asset; the frozen world never did.

### 3.2 The build

- `vidlift_gui.spec:11` and `vidlift_cli.spec:10`: `datas=[]`. Both specs: one commit
  each, `f810724`, unchanged since. **No shipped installer ever carried the payload.
  Transcription shipped in v0.4.14; every Windows install since has been
  structurally unable to run it.**
- `pyproject.toml`: `faster-whisper>=1.2` (floor, no ceiling) — upstream MAY rename
  the asset in a future release; the design must not pin one leaf name anywhere the
  release path depends on (F1).
- `packaging/vidlift.iss` `[Files]` uses `ignoreversion recursesubdirs` with **no
  `[InstallDelete]`** — upgrades overwrite but never remove stale payload. (Relevant
  to hypothesis 2, §3.4 below; hygiene improvement noted as future work, §10.)
- PyInstaller/pyinstaller-hooks-contrib ship **no upstream `hook-faster_whisper`**;
  the community fix for this exact failure class is explicit data collection. Forge
  must verify at implementation time (`pip show pyinstaller-hooks-contrib` + hooks
  dir listing on the CI runner); the design is deliberately immune either way —
  explicit spec-side collection never relies on hook-discovery variance.

### 3.3 Where the error surfaces today (and how it lies)

- `onnxruntime.capi.onnxruntime_pybind11_state.NoSuchFile` derives from **Exception**
  directly (verified MRO on onnxruntime 1.31.1) — it is an ordinary broad-catchable.
- Mode A (`run.py:2714-2765`): `transcribe_source_to_srt`'s `TranscriptionError` →
  converted to `SubtitleOcrError` → fail-open ("upscale ships without subtitles,
  warning names the failure"). Mode B (`subs_job.py:177-196`): the error propagates →
  job fails with the verbatim message. The history rows show exactly this.
- **The dishonesty gap:** on a CUDA-resolved box, the first decode runs inside
  `_FallbackEngine.transcribe_segments`'s tier-2 gate (transcribe.py:651-688), whose
  broad catch triggers a CPU rebuild for ANY non-cancel exception — including a
  missing-asset error, which then (a) wastes a full CPU engine build, (b) records a
  manifest note claiming *"cuda unavailable — fell back to cpu (NoSuchFile)"* — the
  failure was never a device problem — then (c) fails identically on CPU (same missing
  file), so the user pays the lie AND the failed job. The factory's own docstring
  (transcribe.py:428-430) already states the invariant — *"TranscriptionError … are
  NOT device guesses — they raise through on every arm"* — but the tier-2 decode arms
  don't implement it yet. v0.4.16.2 makes the code match its own documentation.

### 3.4 The status contract seams (verified)

- v0.4.16.0 precedent: `transcription_model` — per-job via `_transcription_model_echo`
  (`mcp_bridge/tools.py:481-524`) and machine-scoped on the monitor envelope
  (`monitor.py:137-177`, derived-at-read, soft `getattr` chains so foreign fakes
  degrade instead of 500ing). `api_version` did not bump.
- The `/status` envelope is Hermes's surface; the GUI consumes the bridge client
  directly. An install-scoped capability fact (the frozen payload either has the
  asset or it does not — per machine, per install) belongs on the **envelope**,
  exactly where v0.4.16.0 put the machine model truth. Per-job duplication buys
  nothing and invites desync claims.

---

## 4. Determination of the brief's non-determinism question (§3.4 of the brief)

**Verdict: hypothesis 3 — the "contradicting" run never reached the VAD load.** The
outcome within one installed tree is deterministic by construction; no evidence of
run-to-run asset variance exists, and hypothesis 2 has no mechanism in this repo's
history. Evidence, not assumption:

**E1 — Live telemetry (polled during this review).** Job `vl-20261103-3ef5b3a8`
(the same `Class of 1999 II` file that failed at 13:26:53) at poll time:
`state=running`, `current_phase=transcribe`, **`progress.step="transcribe-language"`**,
`transcription.device=null`, `transcription_model.error=null`, `log_tail=[]`,
`last_activity_at` ≈ `started_at` + 67 s, then silent (413 s of age at poll, still
climbing). The core's step order is fixed in source (transcribe.py:2050-2076):
`transcribe-resolve` → `transcribe-language` → **`transcribe-run`** → `transcribe-align`
→ `transcribe-write`. The silero load happens inside `transcribe-run`'s engine call
(`model.transcribe(vad_filter=True)` → `vad.py` load, §3.1); `detect_language` touches
no VAD code path. **The run is parked one step BEFORE the asset is touched. It has
proven nothing about the asset resolving.**

**E2 — Structural impossibility of within-install variance.** `get_assets_path()` is a
pure function of the frozen module's `__file__` (§3.1). A given install's
`_internal\faster_whisper\assets` either exists or it doesn't, for every run, from
first boot to uninstall. "Sometimes resolves" cannot come from path resolution,
`_MEIPASS` timing, or phase ordering. Only a **change to the install tree between the
failing runs and the surviving run** could produce mixed outcomes on one box.

**E3 — Hypothesis 2 has no shipped mechanism.** The brief's candidate 2 (leftover
assets from a v0.4.15.x install) would require a previous payload that contained the
asset, and Inno's no-delete upgrade to preserve it. But the specs have carried
`datas=[]` since their only commit (`f810724`) — **no shipped build ever placed the
asset there to be left over.** A leftover could only originate from a non-shipped
artifact (hand-built/local staging install). That is checkable box-side, below.

**Falsifiable prediction (for Hermes, recorded for the record):** when job
`vl-20261103-3ef5b3a8` exits `transcribe-language`, it fails at `transcribe-run`
within seconds with the identical `NO_SUCHFILE … silero_vad_v6.onnx`. If it instead
transcribes successfully, then the install tree on that box DOES contain the asset —
immediately compare `C:\Users\yetis\AppData\Local\Programs\VidLift\_internal\
faster_whisper\assets\*` file timestamps against the installer's write time; a hit
older than the v0.4.16.1 install means a non-shipped build artifact existed on that
box (the only remaining explanation), and §3.2-E3 stands.

**Separate observation (flagged, out of scope per N7):** ≥400 s of heartbeat silence
during `transcribe-language` sits at the edge of plausibility (detect is bounded to a
120 s audio prefix, `DETECT_AUDIO_SECONDS=120`; `medium` int8 CPU is documented ~1×
realtime; a 1080p-source decode + first-pass model warm-up can stretch toward several
minutes, but this merits a watch). This is NOT the silero defect — detect precedes it —
and the existing stale-job machinery (history shows "server restarted during run" and
"stale job" rows working as designed) is the safety net. No design here; Hermes should
keep the job under observation for its terminal state per the prediction above.

*Record-keeping note:* the monitor was reachable and serving during the evidence
gathering above (E1), but a final poll after these specs were drafted timed out on
both the Tailscale and LAN endpoints (GUI stopped / host asleep / link drop — cause
unknown from here). The falsifiable prediction therefore remains OPEN and is handed
to Karen verbatim as validation item V-8 — including its "if it succeeded instead"
branch and the exact on-box filesystem check — so the determination is settled with
box-side evidence, not inference, before the v0.4.16.1 install is upgraded.

---

## 5. Architecture Options

The bundling fix (F1) and the build gates (F2) are mandatory in every option — a
runtime strategy without a shipped asset is a strategy for a defect that should not
exist. The options differ in **runtime posture** when the asset is absent anyway
(corrupted install, partial uninstall, future packaging regression, foreign engine
paths).

### Option 1 — Build-only (baseline)

Bundle + gates. Runtime unchanged: a missing asset surfaces as onnxruntime's
`NoSuchFile` whenever the first VAD decode runs (after audio extraction, model
resolve/download, engine construction, language detection — i.e., possibly minutes
in), Mode B job fails with a raw stack-flavored message; on CUDA boxes the fallback
wrapper mislabels it (§3.3).

- **+** smallest diff; **−** slowest, least honest failure; defect recurss in any
  payload-integrity scenario the gates miss (user deletes `_internal` debris,
  antivirus quarantine, partial install).

### Option 2 — Preflight-refuse + named hard fail + TOCTOU backstop (RECOMMENDED)

Bundle + gates + a VAD preflight at the top of `transcribe_source_to_srt` (runs
before ANY work: before ffmpeg extraction, before a potential 1.5 GB model download).
Missing asset → `VadAssetMissingError(TranscriptionError)` naming the resolved assets
dir, the expectation ("the bundled silero VAD ONNX set"), and the remedy (repair /
reinstall; "the installer payload is defective" wording when frozen). Mode A's
existing arm converts it to the honest fail-open subtitle-skip; Mode B fails the job
with the same words. WhisperEngine wraps the decode with a directory-matched
TOCTOU backstop; `_FallbackEngine` decode arms re-raise `TranscriptionError` instead
of mislabeling as device faults. `vad_on=False` runs skip the check (true minimal
dependency truth — a no-VAD run needs no VAD asset).

- **+** fails ONE job at second 0 with the remedy; honest by construction; zero
  quality ambiguity; preflight is one `find_spec` + directory listing (µs–ms);
  dispositions ride existing mode arms — no new plumbing in run.py/subs_job.
- **−** a defective install produces failed/skipped transcriptions until repaired
  (vs. degraded-but-completed under option 3); the message must be maintained as
  carefully as code.

### Option 3 — Auto-degrade: retry with `vad_on=False`, honestly recorded

Bundle + gates + on asset-missing, rebuild/re-run with the existing `vad_on=False`
seam, appending a manifest note ("VAD asset missing — ran without VAD") and a reason
in the result notes.

- **+** jobs complete on defective installs; uses the sanctioned seam (N6).
- **− REJECTED as default:** silently converts a packaging defect into
  **library-wide subtitle quality drift**. The sync/quality stack was measured WITH
  silero: the zero lead-in rationale cites the VAD's 400 ms speech pad
  (transcribe.py:1445-1456), the zero-cue guard and the music-region drop floors are
  calibrated against VAD-segmented input, and whisper's hallucination profile without
  VAD on long quiet/media beds is materially worse. A note in a manifest Eric may
  never open does not make unrequested quality loss acceptable at batch scale ("never
  silently" is honored, technically, but the honesty here is a *warning on a decision
  the product had no right to make*). If ever wanted, it is a user OPT-IN (`--no-vad`
  style pin), not a silent fallback — noted in §10 as future work.

### Option 4 — Runtime repair (download the ONNX on demand)

Fetch the asset at runtime from a pinned URL with a sha256 pin, like model.bin.

- **− REJECTED:** new runtime network dependency for release integrity; the pinned
  digest still rots with upstream renames; the frozen payload is the product — a
  build that needs runtime self-repair has already failed. Violates the spirit of
  N2's "no new machinery."

### Decision matrix

| Criterion | 1: build-only | 2: preflight (★) | 3: auto-degrade | 4: runtime repair |
|---|---|---|---|---|
| Time-to-honest-signal on defect | minutes | **seconds** | minutes | minutes |
| Honesty (no mislabels) | poor (§3.3) | **strong** | medium (note-only) | strong |
| Quality guarantee | preserved | **preserved** | **violated silently-ish** | preserved |
| Diff size | tiny | small | small | medium |
| New failure modes | none | none | degraded output at scale | network/pin rot |

**Recommendation: Option 2**, with Option 1's contents as its foundation and Option 3
explicitly demoted to a possible future opt-in flag.

---

## 6. Recommended Architecture (system design)

### 6.1 M1 — Bundle the package data (both specs)

```python
from PyInstaller.utils.hooks import collect_data_files
...
datas=collect_data_files("faster_whisper"),
```

Whole-package collection (not `subdir="assets"`, not a hardcoded leaf tuple): today
the only data is `assets/`, but whole-package is rename- AND relocation-immune against
upstream data additions (weights, configs) at zero cost (~1.2 MB). The GUI one-folder
COLLECT already rides `a.datas`; the CLI one-file EXE already embeds `a.datas` —
no further spec surgery. Mechanism trade-offs (inline vs hook file) → **ADR-2**.
Keeping the asset LIST correct across upstream renames → **ADR-6** (dynamic parity
gate + pinned-record tripwire; neither hardcodes a leaf name in the release path).

### 6.2 M2 — One honest checker family (`vidlift/transcribe.py`)

Two tiers, one home, both seams injectable; the suite's import rule (N3) governs
their shape:

- **`vad_asset_report(resolver=None) -> dict`** — the CHEAP, import-free filesystem
  truth. Default resolver: `importlib.util.find_spec("faster_whisper")` →
  `submodule_search_locations[0] / "assets"` (works dev and frozen — PyInstaller's
  FrozenImporter implements `find_spec`; **verified claim status:** dev-verified,
  frozen-verified by the CI selftest gate rather than by argument); frozen fallback:
  `Path(sys._MEIPASS) / "faster_whisper" / "assets"`. States: `present` /
  `missing` (dir absent or zero `*.onnx`) / `unknown` (package unlocatable, OSError —
  never claim missing on uncertainty; N5). Carries `assets_dir`, `onnx_files`,
  `onnx_count`, `package_version` (via `importlib.metadata`, import-free),
  `resolver_source`, `detail`, `checked_at`. **Never imports faster_whisper.**
- **`selftest_report() -> dict`** — the AUTHORITATIVE capability proof, run by the
  frozen binary: real deferred import, `faster_whisper.utils.get_assets_path()` (the
  package's OWN resolver — ground truth, immune to our derivation drifting from
  upstream's), `*.onnx` listing, a cross-check that our report agrees on the dir,
  and a minimal real load smoke (`get_speech_timestamps` over a 1 s synthetic silence
  buffer) proving the ONNX *loads*, not merely exists. Honors the engine-off env
  guard with an explicit `{ok: False, skipped: ...}` shape — the suite can call it
  with the real checker and deterministically get the skip shape while never
  importing the package (N3); CI runs it unguarded, where `ok` MUST be literal `true`.
- **`VadAssetMissingError(TranscriptionError)`** — the named condition. Message
  template names the resolved dir, the expectation, and the remedy; frozen builds add
  "the installer payload is defective — reinstall/repair; report if it persists."
  Never names one hardcoded leaf as the requirement (F1): "the bundled silero VAD
  ONNX model set (`assets/*.onnx`, e.g. `silero_vad_v6.onnx` for faster-whisper
  1.2.x)" — self-dating, rot-aware.

### 6.3 M3 — Runtime honesty

- **Preflight (core seam):** first statement of `transcribe_source_to_srt`:
  skip iff `_engine_disabled_by_env()` **or** `vad_on is False`; else compute (or take
  the injected `vad_report` dict) and raise `VadAssetMissingError` iff
  `state == "missing"`; `unknown` proceeds (lenient-on-probe-fault; the engine's own
  load decides, and the backstop names it). Placement before `os.makedirs(workdir)`:
  zero side effects on refusal. **Dispositions ride existing arms** — Mode A's
  `except TranscriptionError` → honest fail-open subtitle-skip (the upscale ships,
  the warning names the packaging defect); Mode B propagates → job failed, verbatim
  reason. Neither caller is edited. Suite-green proof by construction: guard-on skips
  (conftest pins the guard suite-wide); guard-off + stub-factory tests hit the real
  fs resolver, which is `present` on any correctly provisioned dev/CI box and
  `unknown`-lenient otherwise — never a new failure surface. Forge MUST verify this
  claim by running the full suite before AND after (§Impl-Spec, M2 exit criteria).
- **TOCTOU backstop (engine seam):** `WhisperEngine.__init__` captures
  `self._vad_assets_dir` via the SAME deferred import block (`from faster_whisper.utils
  import get_assets_path` beside `WhisperModel` — the seam rule is untouched).
  `transcribe_segments` wraps its materialization: an exception whose message contains
  the known assets dir path is re-raised as `VadAssetMissingError` (mid-run deletion,
  AV quarantine between preflight and decode). Matching on the resolved dir string —
  not on onnxruntime's exception TYPE (whose hierarchy varies by version; verified
  `NoSuchFile` is a direct `Exception` subclass on 1.31.1) — is the version-stable
  predicate, and it is the honest one: a fault naming the assets dir IS an asset fault.
- **Tier-2 carve-out (fallback wrapper):** both decode arms of `_FallbackEngine`
  gain `isinstance(exc, TranscriptionError)` beside the existing `EngineCancelled`
  re-raise — honest refusals raise through, never triggering the mislabeled CPU
  rebuild of §3.3. This aligns the code with the invariant the factory's docstring
  already states (transcribe.py:428-430). Audit requirement: Forge confirms no
  existing test drives a `TranscriptionError` through the decode arms expecting a
  rebuild (the v0.4.15.2 tests drive `RuntimeError`/OOM-shaped fakes via the `ctor=`
  seam; see Impl Spec).

### 6.4 M4 — Build/release gates

Three independent failure surfaces, so no single gate's gap re-ships this class:

1. **Suite contracts (run on every OS incl. Linux dev, pre-build):** spec-text
   contracts (collector present in BOTH specs); workflow-text contracts (the gates
   below EXIST in the YAML — b20-style, `tests/test_windows_packaging.py:92-128`
   precedent); pinned asset-record tripwire against the dev venv via `find_spec` + fs
   (import-free — N3) with documented refresh policy (ADR-6).
2. **Post-build, pre-installer (CI, fastest frozen feedback):** after
   `pyinstaller`, run `dist\vidlift-cli.exe --selftest` (exit 1 kills the build
   BEFORE Inno Setup runs) and assert `dist\VidLift\_internal\faster_whisper\assets`
   has name+size parity with the build venv's `faster_whisper\assets` (dynamic —
   immune to upstream renames; prints both listings on mismatch).
3. **Post-install (CI, the shipped artifact itself):** extend the existing
   "Validate installed artifact" step: `$install\_internal\faster_whisper\assets`
   parity (same dynamic rule) **and** `& "$install\vidlift-cli.exe" --selftest` —
   the frozen binary self-reporting through faster-whisper's own loader. This is the
   step that definitively answers "the installed payload can transcribe."

**Exact commands, output contract, and failure text: Implementation Spec §5.**

### 6.5 M5 — Status-contract detectability (additive)

Monitor `GET /status` envelope gains **`transcription_engine`** (machine-scoped,
sibling to the v0.4.16.0 `transcription_model` envelope block, derived-at-read via
`vad_asset_report()` with a soft try/except → honest `unknown` shape on probe fault
— the monitor's no-500 rule, monitor.py:209-213):

```json
"transcription_engine": {
  "package": "faster-whisper",
  "package_version": "1.2.1",
  "vad_asset": "present",
  "assets_dir": "…\\_internal\\faster_whisper\\assets",
  "onnx_files": ["silero_vad_v6.onnx"],
  "onnx_count": 1,
  "resolver_source": "find-spec",
  "ready_for_transcription": true,
  "detail": null,
  "checked_at": 1793739337.5
}
```

`ready_for_transcription`: `true` on `present`, `false` on `missing`, **`null` on
`unknown`** (N5). `/health` gains nothing (its documented liveness scope).
`api_version` stays **1**; per-job payloads and every MCP tool shape are untouched.
**Hermes's exact capability query:**
`GET /status → .transcription_engine.ready_for_transcription == true
AND .transcription_model.state == "installed"` — payload asset + model, both true,
means "this install can start a transcription now."

### 6.6 Data flow (defect-path before → after)

```
BEFORE (v0.4.16.1):  enqueue → probe/plan → extract+audio → model resolve (maybe 1.5 GB)
                     → engine build → language detect → transcribe → 💥 NoSuchFile
                     (CUDA boxes: + one wasted CPU rebuild + a dishonest manifest note)

AFTER  (v0.4.16.2):  enqueue → transcribe phase entry → PREFLIGHT (µs)
                     ├─ asset present → extract → resolve → build → detect → transcribe ✅
                     └─ asset missing → VadAssetMissingError at second 0
                          ├─ Mode B: job failed, reason names dir + remedy
                          └─ Mode A: fail-open; upscale ships; warning names dir + remedy
        …and none of it should ever fire, because M1 ships the asset and M4 proves it
        at build time (dist tree), install time (installed payload), and run time
        (--selftest / /status envelope).
```

### 6.7 Security & integrity

Read-only additions (report + envelope key) on an already token-gated surface; no new
endpoints/tools/deps (N2). The parity gate compares names+sizes (not hashes) between
two build-time-known trees — both derive from the same pinned-by-pip wheel install;
integrity of the ASSET bytes is inherited from pip's wheel hashes, and load-validity
is proven by the selftest smoke. No secrets in selftest output (paths + versions
only). Selftest makes no network calls (the conftest network guard needs no
exception; smoke input is synthetic silence).

### 6.8 Failure analysis (residual)

| Residual failure | Behavior after fix |
|---|---|
| GUI (one-folder) payload fine, CLI (one-file) extraction temp unwritable | selftest error shape `ok:false` names it; jobs fail with the named preflight/TOCTOU error, not a stack |
| Asset deleted between preflight and decode (TOCTOU) | backstop re-raises `VadAssetMissingError` with the same message family |
| AV quarantines the ONNX post-install | preflight reports `missing` on next run; envelope `vad_asset:"missing"`; Hermes sees it before enqueueing |
| `find_spec` fails under a future PyInstaller | resolver falls back to `_MEIPASS` derivation; failing that, `unknown` — never a false "missing"; CI selftest (real loader) remains the authoritative gate and would catch resolver rot |
| faster-whisper 1.3 renames the asset | pip resolves it into the build venv; dynamic parity gate passes (both sides move together); **suite tripwire fails loudly** on the dev box with refresh instructions; selftest immune by construction (uses upstream's loader) |
| Engine-off env var set in production by accident | preflight & selftest report the honest skip/disabled shape rather than hanging |

### 6.9 Observability & performance

Build: three printed gate surfaces (dist parity listing, installed parity listing,
selftest JSON one-liner) — grep-able CI output. Runtime: preflight adds ≤ a few ms
(one `find_spec` + one listdir, no cache needed at run-cadence → always fresh);
envelope key computed per `/status` poll at the same µs cost, no caching (freshness
beats cleverness for install-tree truth). Selftest runtime ≈ faster-whisper import +
~1 s smoke (CI-only, human-triggered otherwise). No per-tick model hashing anywhere,
consistent with the v0.4.16 design's deliberate no-rehash stance.

---

## 7. Architecture Decision Records

### ADR-1 — Collection mechanism: whole-package `collect_data_files`

- **Problem:** frozen builds lack `faster_whisper`'s data payload.
- **Decision:** `datas=collect_data_files("faster_whisper")` in both specs.
- **Alternatives:** (a) `subdir="assets"` narrowing; (b) explicit
  `[(site_packages_path, "faster_whisper/assets")]` tuple; (c) `collect_all`
  (binaries+hiddenimports too).
- **Advantages:** PyInstaller's canonical data seam; destination
  (`faster_whisper/…` under the distroot/`_internal`) coincides EXACTLY with
  `dirname(utils.__file__)/assets` (§3.1) — no runtime patching; whole-package is
  immune to upstream adding data files elsewhere in the package.
- **Disadvantages:** collects any future package data blindly (bounded: pure fs
  copy of a small package); `collect_all` rejected — it sweeps binaries/hiddenimport
  guesses we neither need nor want to audit; (b) rejected — hardcodes the leaf dir
  (F1's rot constraint).
- **Tradeoffs:** slightly wider net than `subdir="assets"` for ~0 bytes today.
- **Rationale:** minimum mechanism that cannot drift from the resolver's own rule.
- **Consequences:** both specs carry one identical line; the CLI one-file runtime
  extracts ~1.2 MB to `_MEIPASS` per launch (negligible).
- **Revisit when:** faster-whisper ships a working PyInstaller hook upstream
  (verify each CI run; even then, explicit collection stays — hooks are discovery
  variance, §3.2).
- **Dependencies:** PyInstaller (already the build tool); pinned-record tripwire
  (ADR-6) watches upstream drift.

### ADR-2 — Inline spec collection vs a repo hook file

- **Problem:** two specs must collect identically; hook indirection possible.
- **Decision:** inline `collect_data_files` line in EACH spec; the new contract
  test asserts both (drift pinned by test, house-style — b20/version-stamp
  precedent).
- **Alternatives:** (a) `packaging/pyinstaller/hook-faster_whisper.py` +
  `hookspath=["packaging/pyinstaller"]` in both specs; (b) a shared helper module
  imported by both specs.
- **Advantages of inline:** explicit-over-implicit at the exact file a reviewer
  audits; spec execution needs no import machinery beyond PyInstaller's own utils;
  hook caching/staleness classes vanish; grep-visible.
- **Disadvantages:** one duplicated line (both files), mitigated by the contract
  test failing the moment either spec loses it.
- **Rationale:** the hook's value (auto-application) is exactly its audit cost
  (invisibility); the repo's drift-guard idiom is contract tests over visible text.
- **Consequences:** `hookspath=[]` stays empty; test `test_specs_collect_faster_
  whisper_datas` becomes the single-source enforcer.
- **Revisit when:** a second frozen dependency needs data collection — then the
  shared-helper alternative earns its import machinery.
- **Dependencies:** the new contract test file (M0).

### ADR-3 — Runtime posture on a genuinely missing asset (brief §4.3, 3 options)

- **Problem:** if the asset is absent at runtime anyway, what does a run do?
- **Decision:** **(c) preflight-refuse** at core entry, with the **(a) hard-fail
  message** as its refusal body, plus the TOCTOU backstop; **(b) auto
  `vad_on=False` rejected** as a default.
- **The honest framing:** (a) and (c) are not rivals — (c) IS (a), moved in front of
  the work, with a checker. The real decision is **fail-when vs degrade**, and on
  degrade the whole measured subtitle-quality stack is invalidated (§5 Option 3):
  lead-in-zero tuning, no-speech drop floors, music-region signatures all presuppose
  silero with `speech_pad_ms=400`. "Honestly noted" degraded output across a user's
  batch is still degraded output they didn't ask for.
- **Advantages:** honest signal at second 0; remedy is repair, not acceptance;
  zero quality ambiguity for Karen to re-measure; the existing `vad_on` seam stays
  the ONLY switch (N6), used here as the preflight's own skip predicate.
- **Disadvantages:** defective install ⇒ refused transcriptions until repaired
  (accepted: that IS the honest state of the install).
- **Tradeoffs:** rejects "always completes" (Option 3's one virtue) in favor of
  "never lies about capability."
- **Consequences:** Mode A converts the refusal to fail-open subtitle-skip through
  the EXISTING arm (no caller edits); Mode B fails the job verbatim; the envelope
  key explains the install before anyone enqueues.
- **Revisit when:** field evidence shows defective-install-but-wants-anything users
  — then a deliberate OPT-IN (e.g. a `vad off` config pin) designed then, not
  smuggled now.
- **Dependencies:** M2 checker family; `VadAssetMissingError`.

### ADR-4 — Gate placement: suite + post-build + post-install, and the `--selftest` CLI surface

- **Problem:** "where does the release-gate assertion live — CI step, selftest, or
  both?"
- **Decision:** **both**, plus static contracts in the suite (3 surfaces, §6.4).
  The dynamic authority is the frozen CLI's `--selftest`, because only the frozen
  binary reporting through faster-whisper's OWN resolver+loader proves
  resolver↔payload and payload↔loader agreement on the target OS.
- **Alternatives:** (a) CI-only tree assertions (no selftest): can't prove the
  package's runtime RESOLUTION agrees — a future faster-whisper `_MEIPASS`-aware
  resolver could move the target and tree asserts would pass while runtime fails;
  (b) selftest-only: loses the cheap name/size parity forensics (the "which file is
  missing" answer) and the suite-side early tripwire; (c) GUI-side selftest button:
  human-gated, hence not a gate.
- **Tradeoffs:** three gate surfaces to maintain vs one; accepted — each catches a
  class the others structurally cannot (§6.4).
- **Consequences:** workflow gains ~20 pwsh lines (version-literal-clean; the
  existing `test_workflow_has_no_version_literals` and b20 split-tests keep passing —
  additions contain no dotted version shapes); `--selftest` prints
  `vidlift-selftest {json}`, exit 0 iff `ok is true`.
- **Revisit when:** the CI gains a real transcription smoke fixture — the selftest
  then composes with it rather than standing alone.
- **Dependencies:** M2 `selftest_report`; CLI flag.

### ADR-5 — Status key shape: envelope-only, `transcription_engine`, no composite state string

- **Problem:** how Hermes asks "can this install transcribe right now," additively.
- **Decision:** machine-scoped envelope key on monitor `/status`; report shape
  verbatim + `ready_for_transcription` (true/false/**null**-on-unknown); per-job
  payloads and MCP tool shapes unchanged; `api_version` 1.
- **Alternatives:** (a) per-job echo (mirrors `transcription_model`): rejected —
  install-scoped truth per-job invites desync claims; the model block is per-job
  because a JOB pins the model tag, while NO job pins the payload tree; (b) a
  composite `state: ready|degraded` string: rejected — two booleans' worth of truth
  in one enum strings away the audit trail; fields compose, strings rot; (c)
  extending `/health`: rejected (its documented scope is liveness).
- **Consequences:** Hermes's capability answer = two-key conjunction (§6.5); GUI may
  consume the same checker later for a badge (future, §10) — not required here.
- **Revisit when:** a second install-scoped capability fact appears (e.g. the CUDA
  runtime probe result for the cublas gap, N7) — then a generalized `install`
  envelope object absorbs both.
- **Dependencies:** monitor `status_payload`; soft-degradation discipline.

### ADR-6 — Recording the expected asset set: pinned record in the suite, dynamic parity at build

- **Problem:** "pin/record the expected asset set" without hardcoding a leaf name
  that rots (brief §4.1; the leaf is version-suffixed upstream: `_v6` today).
- **Decision:** TWO records with disjoint jobs. (1) **Build/release path: dynamic
  parity** — the CI gate derives the expected set from the build venv's installed
  `faster_whisper/assets` (names+sizes) and requires the frozen tree to match 1:1.
  Self-updating on renames BY CONSTRUCTION; the release path hardcodes no leaf.
  (2) **Suite: pinned tripwire** — `EXPECTED_VAD_ASSETS` in the new test file
  (currently one entry: `silero_vad_v6.onnx`, 1,245,151 bytes, sha256 `4cbf54…17d2`
  — recorded from faster-whisper 1.2.1 on this date), asserted import-free
  (`find_spec`+fs) against the dev venv, `pytest.skip` when the package is absent.
- **Advantages:** the release can't rot (it compares two live installs); the suite
  screams FIRST and LOUDLY on the dev box the day upstream renames — with the doc'd
  refresh step beside the constant ("update from the new wheel when bumping the
  floor; never edit to silence").
- **Disadvantages:** pinned record is human-refreshed (by design — a human SHOULD
  look when the dependency's payload changes); name+size parity (not hashes)
  between venv and tree is weaker than hashing (accepted: same-wheel provenance §6.7).
- **Revisit when:** faster-whisper's floor moves in pyproject — refresh procedure is
  written beside the constant.
- **Dependencies:** none (both records are files/text).

### ADR-7 — `_FallbackEngine` decode arms re-raise `TranscriptionError`

- **Problem:** tier-2's broad catch mislabels engine-layer honest refusals (today
  hypothetical; post-M2 real: `VadAssetMissingError`) as device faults — wasted CPU
  rebuild + dishonest "cuda unavailable" manifest note (§3.3).
- **Decision:** `isinstance(exc, (EngineCancelled, TranscriptionError))` gates the
  re-raise in both arms; comment cites the factory docstring's stated invariant
  (transcribe.py:428-430) it now enforces.
- **Advantages:** code matches its own documentation; the note never lies about
  device truth; one-line-class diff.
- **Disadvantages:** none identified — an engine raising `TranscriptionError` has
  already said "not a device guess"; rebuilding can't fix a non-device fault.
- **Revisit when:** an engine-layer `TranscriptionError` variant ever DOES encode a
  device-recoverable condition (none exists; the type's contract is refusal).
- **Dependencies:** M2's named error; Forge's audit of `test_v04152_cuda_fallback.py`
  (decode-arm fakes raise RuntimeError-family — Impl Spec lists the check).

---

## 8. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| `find_spec` under the CURRENT frozen build resolves differently than dev | low (FrozenImporter implements it) | frozen fallback to `_MEIPASS` derivation; **the CLI selftest uses faster-whisper's real loader, so the gate stands even if both our resolvers rot**; Karen's frozen verification (Validation Spec V-6) |
| Suite non-hermeticity complaint (preflight touches real site-packages when guard is off) | medium (review taste) | documented: read-only listing; `unknown`-lenient; conftest untouched; the alternative (gating on factory provenance) provably misses Mode A (run.py:2720 always injects a real-builder lambda) — core preflight + guard-skip is the only shape that covers both modes without caller edits |
| Workflow edits trip the version-literal or b20 contract tests | low | no dotted version shapes in added YAML; additions appended INSIDE existing steps (b20's `- name:` splits unaffected); the suite itself is the verifier |
| CLI hiddenimports miss something the selftest newly touches | low | CLI spec already hidden-imports the vidlift chain; `numpy`/`onnxruntime`/`ctranslate2` arrive transitively via faster_whisper's graph (modulegraph sees the function-level imports); the post-build gate runs the real binary and would fail loudly if not |
| `--selftest` exit-code semantics collide with argparse `--version` | low | flag (not subcommand), dispatched first in `main()`; suite pins parse + dispatch mapping with a monkeypatched report |
| Karen can't run Windows builds from her Linux box | n/a (addressed head-on) | Validation Spec V-9/V-10: installer-extraction verification on Linux (`innoextract`/`7z`) over the CI artifact + gate-output audit + the runtime envelope on the Windows box |

---

## 9. Implementation plan (milestones; details in the Implementation Spec)

Each milestone is independently testable and must leave the suite at 921+N green.

- **M1 — Bundle + spec contracts:** both spec files; contract tests red→green on
  Linux immediately (no PyInstaller needed — text contracts).
- **M2 — Core honesty:** `VadAssetMissingError`, `vad_asset_report`,
  `selftest_report`, core preflight, engine TOCTOU backstop, tier-2 carve-out; unit
  tests (guard-on skip, missing→raise before extraction, unknown-lenient,
  `vad_on=False` skip, backstop match predicate, carve-out no-rebuild).
- **M3 — Surfaces:** `vidlift-cli --selftest`; monitor envelope key; their tests
  (monkeypatched report; foreign-fake degradation; `api_version == 1` pin).
- **M4 — Workflow gates:** two YAML insertions; workflow-shape tests.
- **M5 — Release:** version stamps to 0.4.16.2 (pyproject + `vidlift/__init__.py` +
  `packaging/version_info.txt` per the existing release dance; CI stamps the PE
  resources), CHANGELOG/release-notes hook (Quill), dispatch the Windows build,
  hand Karen the artifact + this spec set.

Forge needs no architectural judgment calls beyond M2's suite-green verification;
every name, message template, command, and output contract is bound in the
Implementation Spec.

---

## 10. Future evolution (noted, NOT designed)

- **GUI readiness badge** consuming `vad_asset_report` via the bridge services layer
  (the monitor envelope is Hermes's; the GUI reads the bridge client — a
  services-side exposure is a small additive follow-up if Eric wants the red badge).
- **Installer hygiene:** `[InstallDelete]` on `{app}\_internal` (modeled in §3.2:
  no shipped build ever left VAD debris, but the next stale-payload class will use
  the same door; pair with the K-1 stale-overlay work Karen already tracks).
- **Opt-in no-VAD transcription** (`--no-vad` pin through the EXISTING `vad_on`
  seam) IF field evidence ever asks for degraded-but-completed runs (ADR-3).
- **cublas64_12.dll GPU payload gap** (N7): likely a sibling spec-side collection
  class (CUDA DLLs are `binaries`, not `datas`) — bundle-inventory audit candidate,
  deliberately out of this hotfix's blast radius.
- **Workflow hygiene:** the PyInstaller/pyinstaller-hooks-contrib toolchain install is
  unpinned (`pip install pyinstaller`) — the same "derive, never drift" philosophy as
  the version stamps argues for pinning the build toolchain; separate proposal.
- **Housekeeping:** `mcp_bridge/tools.py:704` comment says "v0.4.17 (fail-path…)" for
  the feature that shipped as 0.4.16.1 — label drift for Quill, one-line fix.

---

## 11. Out of scope (restated from N7, with routing)

1. `cublas64_12.dll` not found (one history row) — GPU/CUDA payload audit, §10.
2. Karen's K-1 stale-overlay edge — pairs naturally with the `[InstallDelete]`
   hygiene item, §10.
3. The `vl-20261103-3ef5b3a8` language-detect silence (>400 s) — watch for the
   falsifiable prediction in §4; if it proves a NEW stall class, that's its own
   diagnostic cycle (the anti-stale machinery already bounds the blast radius).

*Verification basis: vidlift @ 88a7116; faster-whisper 1.2.1
(`~/.venvs/vidlift/lib/python3.12/site-packages/faster_whisper`); onnxruntime
1.31.1; live monitor polls of the v0.4.16.1 install on the Windows box during this
review.*
