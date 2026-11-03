# Validation Specification — VidLift v0.4.16.2: VAD asset packaging fix

**Author:** Bob (Principal Software Architect)
**Date:** 2026-11-03
**Audience:** Karen — this is your baseline; expand it. Every check names its
environment (Linux dev box / CI log / Windows box) so the platform split is
unambiguous up front.
**Companions:** `bob-vidlift-v0.4.16.2-architecture-spec.md`,
`bob-vidlift-v0.4.16.2-implementation-spec.md` (binding names/output contracts).
**Baseline evidence (this defect, v0.4.16.1):** 4 history rows failing with
`NO_SUCHFILE … _internal\faster_whisper\assets\silero_vad_v6.onnx` across `.avi`
and `.mkv`; running repro candidate job `vl-20261103-3ef5b3a8`.

---

## 0. The one-line fix being validated

The frozen tree now carries `faster_whisper`'s `assets/` payload (both builds),
the build proves it three ways before release, and if it is ever absent at runtime
the product refuses at the front of the job with a named, actionable error instead
of dying minutes in with an onnxruntime stack — and the monitor can answer "can
this install transcribe" without running a job.

**What success looks like:** the four previously-failing files transcribe end to
end on the upgraded Windows install; the CI run shows the two new gate blocks
green; a doctored payload fails the build loudly; the suite is 921+N green with
one new test file and zero modified ones.

---

## 1. Scope

**In scope (brief §4.1–4.5):** bundling correctness in both specs; build/release
gates; runtime refusal + honesty behavior; the additive status key; the regression
net; suite hygiene (no modified tests, seam rules).

**Out of scope (verify they are NOT touched, then let them be):** the
`cublas64_12.dll` GPU packaging gap; K-1 stale-overlay; the observed >400 s
`transcribe-language` stall on job `vl-20261103-3ef5b3a8` (watch it per V-8, it is
not this defect — it sits one step BEFORE the VAD load).

**Environment key:** 🐧 = Linux dev box (`~/.venvs/vidlift/bin/python`, 3.12.3) ·
🏗️ = CI runner/log (windows-latest, workflow `build-windows-installer`) ·
🪟 = the Windows box (Tailscale `100.99.94.125`, monitor token in the Hermes brief).

---

## 2. Functional validation matrix

| ID | Requirement | Proof | Env |
|----|-------------|-------|-----|
| V-1 | Asset bundled, GUI one-folder tree (§4.1) | `_internal\faster_whisper\assets\` non-empty `*.onnx` in built+installed payload | 🏗️🪟 + 🐧 via V-9 |
| V-2 | Asset bundled, CLI one-file build (§4.1) | frozen `vidlift-cli.exe --selftest` exits 0 with `"vad_load_smoke": "ok"` | 🏗️🪟 |
| V-3 | Build fails loudly on omission (§4.2) | doctored tree → red build, named leaf in throw text (V-5) | 🐧(gate-logic) 🏗️ |
| V-4 | Runtime refusal honesty (§4.3) | preflight refuses at second 0, names dir + remedy; Mode A fail-open, Mode B job failure; no mislabeled CUDA note | 🐧(unit) 🪟(e2e, V-11) |
| V-6 | Status key additive (§4.4) | `/status` gains `transcription_engine`; `api_version==1`; bridge/MCP shapes unchanged | 🐧(suite) 🪟(curl) |
| V-7 | Regression net exists (§4.5) | `tests/test_v04162_vad_packaging.py` present, green, and demonstrably load-bearing (V-12 mutation) | 🐧 |
| V-8 | Non-determinism determination (arch §4) | falsifiable prediction check on `vl-20261103-3ef5b3a8` | 🪟/monitor |
| V-9 | Frozen-tree proof from Linux | extract the CI installer artifact on Linux, audit the payload tree | 🐧 |
| V-10 | Gate-output audit from Linux | CI run log contains the gate lines + selftest JSON | 🐧 |
| V-11 | The actual defect is gone | the four incident files transcribe; history rows change shape | 🪟 |

---

## 3. Suite-gate procedure (🐧)

1. `git fetch --tags && git checkout <v0.4.16.2 tag or PR head>`.
2. **Zero-modification proof:** `git diff --name-only 88a7116..HEAD -- tests/`
   — expect EXACTLY `tests/test_v04162_vad_packaging.py` (added). Nothing else.
3. `~/.venvs/vidlift/bin/python -m pytest -q` — expect **≥ 921+N passed, 2 skipped**,
   0 failed, 0 errors. Record N and the per-file split.
4. Focused files: `tests/test_v04162_vad_packaging.py`, `tests/test_version_stamps.py`,
   `tests/test_windows_packaging.py`, `tests/test_v0414_transcribe.py`,
   `tests/test_v04152_cuda_fallback.py`, `tests/test_monitor.py` — all green
   (these are the adjacency classes the diff touches).
5. **Seam-rule proof (N3):** the suite must still never import faster_whisper.
   Evidence comes built-in: `test_selftest_report_skip_shape_under_env_guard`
   asserts `"faster_whisper" not in sys.modules` after calling the real
   `selftest_report`. Independently spot-check: run the suite, then
   `~/.venvs/vidlift/bin/python -m pytest -q --co -q | wc` sanity pass is NOT
   enough — instead run `~/.venvs/vidlift/bin/python -X importtime -m pytest -q 2>
   /tmp/imps.log` and confirm `faster_whisper` never appears as an import line, or
   simply confirm the in-test assertions are present and passing. (Accept either
   mechanism; the invariant is what matters.)
6. **Guard matrix within the new file:** confirm presence AND green of:
   preflight-missing-raises-before-any-work (assert in the test that the fake
   runner recorded zero calls AND the workdir was not created), guard-on skip,
   `vad_on=False` skip, unknown-lenient, fallback carve-out (TranscriptionError
   re-raise, RuntimeError still rebuilds once), monitor envelope present+honest-null,
   CLI dispatch exit codes, spec/workflow text contracts, pinned-record tripwire.

**Expected suite-visible behaviors you should sanity-review while reading the new
tests** (they are intentional and documented in the impl spec §3.6/§7): preflight
performs a read-only `find_spec`+listdir of the installed `faster_whisper` package
when a test clears the engine-off guard; `unknown` outcomes never fail runs; the
pinned-record test **skips** if the package is absent. Anything beyond that
hermeticity footprint is a defect in the implementation, not in this plan.

---

## 4. Static/contract proofs (🐧, no build required)

- **V-1a spec contracts:** `grep -n 'collect_data_files("faster_whisper")'
  vidlift_gui.spec vidlift_cli.spec` → one hit each; `grep -rn silero
  vidlift_gui.spec vidlift_cli.spec .github/workflows/` → **no hits** (the release
  path never names the version-suffixed leaf; ADR-6). The suite's spec-contract
  tests encode both.
- **V-6a additive-only contract:** the `status_payload` diff adds exactly one
  envelope key and touches no existing line's contents; `API_VERSION` line
  untouched (`monitor.py:52`). `git diff` review + new envelope tests.
- **Deferral audit:** `git diff 88a7116..HEAD -- vidlift/transcribe.py` — every
  `faster_whisper` symbol import remains inside function bodies (the two new
  import sites are `WhisperEngine.__init__`'s existing deferred block +
  `selftest_report`); module-level imports unchanged.

---

## 5. CI gate proofs

### V-3/V-5 Negative proof — gate logic replay (🐧, pwsh is cross-platform)

Before trusting a red CI run, prove the gate LOGIC fires: on the Linux box with
`pwsh` installed, fabricate two dirs — `venv-assets` (copy of the dev venv's
`faster_whisper/assets`) and `dist-assets` (same minus the `.onnx`) — and replay
the impl spec §5.1 parity block with the two paths substituted. Expect the
`Compare-Object` `throw` naming the missing leaf. Variants: size-mismatch (truncate
a copy) → the size throw; zero-`.onnx` dir → the `ships no *.onnx` throw.

### V-10 Build-run audit (🐧 reading 🏗️ logs)

For the v0.4.16.2 `build-windows-installer` run, the Actions log must contain, in
the **Build GUI and CLI executables** step: `vad assets shipped: silero_vad_v6.onnx`
(name list, whatever upstream ships at build time), followed by a
`vidlift-selftest {...}` line containing `"ok": true`, `"vad_load_smoke": "ok"`,
`"report_agrees": true`. Same two proofs in the **Validate installed artifact** step
against `$install`. Absence of any of the four = release blocked, do not publish.

### V-3b Full negative (🏗️, only if you want the end-to-end red-run trophy)

Dispatch the workflow from a scratch branch whose spec omits the collector. Expect:
red at the **dist-tree gate (before Inno Setup runs — cheap)**, throw text naming
`silero_vad_v6.onnx`. This also TIMES the gate: it must fail earlier in wall-clock
than the v0.4.16.1 build's success point (i.e., you do not wait for installer
compilation to learn the payload is defective).

---

## 6. Frozen-tree verification from Linux (V-9) — the answer to "how do I prove the
payload without running the Windows build"

The CI artifact is an Inno Setup installer; Inno installers extract fine on Linux.

1. Download `VidLift-0.4.16.2-Windows-x64-Setup.exe` (+ `.sha256`) from the
   workflow artifact; verify the hash.
2. Extract: `innoextract -d extracted VidLift-0.4.16.2-Windows-x64-Setup.exe`
   (`sudo apt install innoextract` if absent; `7z x` over Inno also works in a
   pinch, innoextract is the reliable one).
3. Assert in the extracted tree:
   - `<extracted>/app/_internal/faster_whisper/assets/` exists and holds ≥1
     `*.onnx`;
   - its file set EQUALS the suite's pinned record (`silero_vad_v6.onnx`, size
     1,245,151, sha256 `4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f
     1be17d2` for faster-whisper 1.2.1) — or, if the build resolved a newer
     faster-whisper, EQUALS the names printed in the CI log's
     `vad assets shipped: …` line (V-10), which is the build-time truth. Disagreement
     between pin and log = the ADR-6 tripwire doing its job; refresh the pin and
     re-verify, do not accept silently.
   - Presence of `vidlift-cli.exe` beside it (sanity that you extracted the right
     lane).
4. You cannot EXECUTE the payload on Linux — that is precisely why the release
   requires V-10's in-build selftest (the frozen binary proving itself on Windows)
   plus V-11's box-level run. V-9 closes the "did Inno ship the tree" question;
   V-10 closes "does the tree load on Windows"; V-11 closes "does the product
   transcribe." All three, or the release does not ship.

---

## 7. Runtime behavior proofs

### V-6b Status surface (🪟 or via Tailscale from anywhere)

```
TOKEN=vidlift-mon-0e3f142b-cdee-4fdb-819a-7f65e81c92cd
curl -s -H "Authorization: Bearer $TOKEN" http://100.99.94.125:8766/status | python3 -m json.tool
```

Expect the NEW envelope key `transcription_engine` with
`"vad_asset": "present"`, `"onnx_files": ["silero_vad_v6.onnx"]`,
`"ready_for_transcription": true`, and `api_version: 1` UNCHANGED. With the queue
idle the answer must still appear (machine-scoped, not job-scoped).
**Capability assertion for Hermes:** `transcription_engine.ready_for_transcription
== true AND transcription_model.state == "installed"`.

Negative flavor (optional, 🪟): rename the asset dir on a SCRATCH copy of the
install (Inno `/DIR=` install into `%TEMP%\VidLift-scratch`, never the live one) →
`vad_asset: "missing"`, `ready_for_transcription: false`, `detail` naming the dir.
`/health` shape byte-identical to v0.4.16.1 (it gains nothing by design).

### V-2b Selftest on the box (🪟)

`& "C:\Users\yetis\AppData\Local\Programs\VidLift\vidlift-cli.exe" --selftest` →
stdout one-liner `vidlift-selftest {…}`, `"ok": true`, exit code 0
(`echo $LASTEXITCODE`). And `set VIDLIFT_TRANSCRIPTION_ENGINE=0` first →
`"ok": false` with `skipped` naming the env var, exit 1 (the honest skip shape —
the gate treats skip-as-failure by construction).

### V-4 Runtime refusal, doctored install (🪟, scratch copy only)

With the `%TEMP%\VidLift-scratch` copy missing its `assets` dir:
- **Mode B:** enqueue one `subs` job → status flips straight to failure at
  `transcribe` with error beginning `VAD model asset missing from this install:
  faster-whisper's assets dir "…"` and ending with the reinstall/report remedy.
  Timing expectation: the failure lands in **seconds** and the job log shows NO
  ffmpeg extraction line, NO model download activity — preflight precedes both.
- **Mode A:** run one upscale job with transcription on → the upscale SUCCEEDS,
  manifest/log carry the fail-open note embedding the same VAD message
  (Mode A honesty: a missing subtitle input never kills the upscale).
- **No device mislabel:** on this CUDA box the note/error must NOT contain
  "cuda unavailable"/"fell back to cpu" for this fault class (ADR-7 carve-out);
  if `engine_note` shows a fallback line paired with a VAD failure, that's a defect.
- Restore/repair by reinstalling v0.4.16.2 (also proves the remedy sentence true).

### V-11 Defect closure, live payload (🪟)

1. Upgrade in place over the broken v0.4.16.1 (the real-world path) THEN also
   fresh-install to `%TEMP%` scratch (clean-tree path). Verify
   `_internal\faster_whisper\assets\silero_vad_v6.onnx` in the live install.
2. Re-run the four incident files (the three Pete & Pete `.avi` and
   `Class of 1999 II … .mkv`) as Mode B `subs` jobs. Expect: four successes,
   cue counts > 0, sidecar `.srt` beside outputs; `transcription.device`
   populated honestly (cuda or cpu per the box), `transcription_model.state:
   installed` throughout.
3. `/history` after: the four newest rows `succeeded` with `output_path` set —
   replacing the shape of the four 0.4.16.1 failure rows.
4. Watch one run LIVE via `/status`: the `progress.step` sequence must pass
   `transcribe-resolve → transcribe-language → transcribe-run` and LEAVE
   `transcribe` (the 0.4.16.1 failures died at the step boundary).

### V-8 Prediction check on the v0.4.16.1 running job (do this FIRST, before the
upgrade wipes the repro)

Poll `GET /status` for job `vl-20261103-3ef5b3a8` on the still-broken install.
Architecture §4 predicts: `progress.step` is still `transcribe-language` or the job
has FAILED AT `transcribe-run` with the same `silero_vad_v6.onnx` NO_SUCHFILE —
i.e., the "contradicting" run never passed the VAD load. If instead the job
**succeeded** on v0.4.16.1, STOP: capture `dir` + timestamps of
`C:\Users\yetis\AppData\Local\Programs\VidLift\_internal\faster_whisper\assets\`
vs the installer's write time before upgrading — a success means that box's tree
somehow has the asset (non-shipped-build debris is the only remaining explanation;
arch §4-E3). Either outcome, record it in your report; it closes the
non-determinism question with evidence.

---

## 8. Edge cases to prove (mapped to impl-spec tests where suite-covered)

| Edge | Expected | Proof |
|---|---|---|
| Engine env-off set in production (`VIDLIFT_TRANSCRIPTION_ENGINE=0`) | preflight skips; selftest returns the skip shape, exit 1; no import of the package | 🐧 suite; 🪟 V-2b second form |
| No-VAD run (`vad_on=False`) on a defective install | preflight skips — a no-VAD run genuinely needs no VAD asset | 🐧 suite (`test_core_preflight_skips_when_vad_off`) |
| Probe fault (assets dir unreadable / package unlocatable) | report `unknown`; runs proceed (lenient); envelope `ready_for_transcription: null`; never a false "missing" | 🐧 suite |
| Asset deleted AFTER preflight, mid-run (AV quarantine sim) | decode-time backstop raises the SAME named error family via `VadAssetMissingError`; no CUDA mislabel | 🪟 manual (delete between jobs is indistinguishable at runtime; rely on 🐧 unit `_is_vad_asset_fault` + carve-out tests, then optional live: delete asset during a long transcribe on scratch install and read the failure text) |
| faster-whisper version bump renames the leaf | dynamic CI parity passes; suite pinned-record TRIPS with the refresh instructions in the constant's comment; release path ships whatever upstream named it | staged later, 🐧 (edit a scratch venv copy, never the real one) |
| Frozen `find_spec` divergence | `_MEIPASS` fallback answers; failing both → `unknown`, never false-missing; the selftest (real loader) remains authoritative | 🪟 V-2b exercises the real resolver agreement (`report_agrees: true`) |
| One-file CLI temp-extraction on every invocation | selftest still `ok`; wall time ≤ ~30 s incl. extraction | 🪟 V-2b timing |
| Monitor probe exception mid-serve | envelope still serves with `vad_asset: "unknown"` (no 500 beyond the handler's own honesty wrapper) | 🐧 suite (`vad_asset_report` raising → honest block) |

---

## 9. Non-functional expectations

- **Performance:** preflight ≤ a few ms at job start (no measurable change to
  job time; assert in V-11 logs that `transcribe` phase start-to-first-ffmpeg-line
  is unchanged modulo ms). `/status` poll cost unchanged (envelope block is one
  `find_spec`+listdir). Selftest: one-shot, ≤ ~30 s wall on the box (import +
  1 s synthetic-silence VAD smoke + one-file extraction for the CLI).
- **Security:** no new endpoints/MCP tools/dependencies. `/status` still
  Bearer-gated; `/health` unchanged; 404/501 surface unchanged (spot-check one
  bogus route and one POST). Selftest output contains paths/versions only — no
  tokens, no user file paths beyond the install dir.
- **Honesty:** every new surface honors null-over-fabrication: `unknown → null`
  for `ready_for_transcription`; `detail` explains; a probe fault is reported as
  a probe fault.
- **Compatibility:** `api_version == 1`; `/history` row shapes from v0.4.16.1
  remain parseable; a v0.4.16.1 Hermes reader ignoring `transcription_engine`
  sees a byte-compatible payload.
- **Suite hermeticity:** beyond the documented find_spec+listdir footprint of the
  preflight's default resolver (impl spec §3), no new real-state touch; conftest
  unmodified.

---

## 10. Suggested report structure (minimum sections)

1. Suite: counts, N, the zero-modified-tests `git diff` evidence, seam-rule evidence.
2. Static contracts: grep outputs.
3. Gate logic: the 🐧 pwsh replay results (V-5) + CI log excerpts (V-10, both steps).
4. Frozen tree: V-9 extraction audit (file list + hashes vs the pin).
5. Windows runtime: V-8 prediction outcome FIRST, then V-2b, V-6b, V-4, V-11 with
   `/history` row before/after excerpts.
6. Edge-case table pass/fail.
7. Deviations found + their severity; explicit go/no-go for publishing the artifact.

## 11. Acceptance criteria (validation-side mirror of impl spec §9)

Go/no-go requires ALL of: §3 suite block green with zero modified tests; §4 greps
clean; V-10 all four gate lines present in the CI log; V-9 payload audit matches
CI log names; V-2b `ok:true` + guarded skip shape; V-6b key present with
`api_version:1`; V-4 refusal texts + no-CUDA-mislabel + Mode A fail-open; V-11
four-file clean sweep on the upgraded live install; V-8 recorded (either outcome).
Any single miss = no-go, return to Forge with this document section cited.
