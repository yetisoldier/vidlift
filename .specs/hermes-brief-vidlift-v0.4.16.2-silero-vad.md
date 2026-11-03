# Diagnostic Brief — VidLift v0.4.16.1: silero_vad_v6.onnx missing from frozen build (Bob)

**Author:** Hermes (orchestrator), 2026-11-03 ~15:05 CST
**Repo:** `/home/yetisoldier/projects/vidlift` @ `88a7116` (tag `v0.4.16.1`, released today)
**Ask:** design the fix for the packaging defect described below. This is a DESIGN task: produce an
architecture + implementation spec for v0.4.16.2. Do not implement.

---

## 1. Executive summary

The **shipped Windows installer (v0.4.16.1) cannot transcribe any file.** Every job that reaches the
`transcribe` phase dies with:

```
NoSuchFile: [ONNXRuntimeError] : 3 : NO_SUCHFILE : Load model from
C:\Users\yetis\AppData\Local\Programs\VidLift\_internal\faster_whisper\assets\silero_vad_v6.onnx failed:
Load model ... failed. File doesn't exist
```

Root cause is in the **PyInstaller spec files**, not in the application logic and not in the fail-path
fix that v0.4.16.1 shipped. `faster_whisper`'s VAD needs its bundled ONNX model at runtime; the frozen
tree does not contain it because neither spec collects `faster_whisper` package data.

This blocks the primary user-facing capability of the product. It is a **P0 for the installer build**.

---

## 2. Observed evidence (live, from the monitor HTTP API)

Monitor endpoint: `http://100.99.94.125:8766` (Tailscale) = `http://192.168.1.53:8766` (LAN),
Bearer token `vidlift-mon-0e3f142b-cdee-4fdb-819a-7f65e81c92cd`.

`GET /health` → `{"ok": true, "version": "0.4.16.1", "busy": false}`

`GET /status` (transcription block on the live broken run):
```json
"transcription": {"device": null, "device_requested": "auto"},
"transcription_model": {"tag": "medium", "state": "installed",
  "cache_dir": "C:\\Users\\yetis\\AppData\\Local\\YetiWurks\\VidLift\\whisper\\models\\medium",
  "bytes_done": null, "bytes_total": null, "file": null, "error": null}
```

`GET /history` — 84 rows: **46 succeeded, 23 failed, 15 cancelled**. Four of the failures are this
defect, on both `.avi` and `.mkv` inputs, i.e. **container-agnostic**:

| finished | file | error |
|---|---|---|
| 14:48:08 | `The Adventures of Pete & Pete (1993) - S03E01 - 35 Hours.avi` | silero_vad_v6.onnx missing |
| 13:26:53 | `Class of 1999 II - The Substitute (1994) Bluray-1080p.mkv` | silero_vad_v6.onnx missing |
| 13:10:40 | `The Adventures of Pete & Pete (1993) - S03E13 - Saturday.avi` | silero_vad_v6.onnx missing |
| 13:09:54 | `The Adventures of Pete & Pete (1993) - S03E02 - The Trouble With Teddy.avi` | silero_vad_v6.onnx missing |

**A test run in progress as this was written contradicts the "always fails" reading and must be
designed for:** job `vl-20261103-3ef5b3a8`
(`J:/Class of 1999 II - The Substitute (1994) Bluray-1080p.mkv`, state `running`, phase `transcribe`)
has been **alive for 370+ seconds with `transcription.device = null` and `transcription_model.error =
null`**. The earlier failure on the *same file* (13:26:53) died immediately at the silero load. So the
outcome is **not deterministic**: sometimes the VAD asset resolves, sometimes it does not.

Non-errors in history, for completeness (NOT this defect): 11 "batch cancelled by user", 9 "server
restarted during run", 7 "stale job from previous session", 2 prepare failures (exit 1, no output),
1 `cublas64_12.dll` not found (separate GPU/CUDA packaging concern), 3 cancelled mid-chunk.

---

## 3. Code and build facts (verified in the repo)

### 3.1 The VAD call site — `vidlift/transcribe.py`

```python
class WhisperEngine(TranscribeEngine):
    def __init__(self, model_path, *, device="auto", cpu_threads=4, vad_on=True):
        from faster_whisper import WhisperModel  # deferred: test seam rule
        self.vad_on = vad_on
        compute = "int8_float16" if device == "cuda" else "int8"
        self.model = WhisperModel(model_path, device=device,
                                  compute_type=compute, cpu_threads=cpu_threads)

    def transcribe_segments(self, audio_path, *, language, on_segment=None):
        segments, _info = self.model.transcribe(
            audio_path, language=language or None,
            vad_filter=self.vad_on,                                 # <-- triggers silero load
            vad_parameters={"min_silence_duration_ms": 300},
            word_timestamps=True,
            no_speech_threshold=0.6, compression_ratio_threshold=2.4,
            condition_on_previous_text=False,
        )
```

Notes that matter for the design:
- `vad_on` is already a constructor parameter — **the application has an off switch for VAD today**,
  it is simply never wired to anything user-facing. Any graceful-degradation design should build on
  this existing seam rather than inventing a new one.
- `from faster_whisper import WhisperModel` is deferred into `__init__` by an explicit in-repo rule
  ("deferred: test seam rule") so the test suite never imports the package. **The fix must not break
  that rule** — the Linux suite passing green is exactly why this shipped broken.

### 3.2 The build specs — `vidlift_gui.spec` (and `vidlift_cli.spec`)

```python
a = Analysis(
    ["run_gui.py"],
    pathex=["."], binaries=[], datas=[],
    hiddenimports=["vidlift", "vidlift.gui", "vidlift.plan", "vidlift.probe"],
    hookspath=[], runtime_hooks=[], excludes=["tkinter", "matplotlib"],
)
```

`datas=[]`. There is **no `collect_data_files("faster_whisper")`**, no `collect_all`, no
`hookspath` entries, and **no `grep -rn "silero_vad"` hits anywhere in the repo** — the asset is not
named, pinned, or verified by any file in the tree. `vidlift_cli.spec` has the same `datas=[]` shape.
Dependencies (`pyproject.toml`): `PyQt6>=6.7`, `faster-whisper>=1.2`, `av<15`.

### 3.3 Why the suite cannot catch it

- The Linux gate (`~/.venvs/vidlift/bin/python`, 3.12.3) imports `faster_whisper` from site-packages,
  where `assets/silero_vad_v6.onnx` **does** exist. 921 tests pass.
- No test asserts anything about the frozen tree's contents, and no test constructs `WhisperEngine`
  with `vad_filter` exercised against a frozen asset path.
- The CI build does have a "Validate installed artifact" step, but evidently it does not launch a
  transcription. **The release gate therefore cannot see this class of defect.**

### 3.4 The non-determinism to explain

The same `.mkv` failed at 13:26:53 (silero missing) yet is running at 15:00+ with no error. Candidate
explanations to investigate and pick between (do not assume — the spec should require evidence):
1. `faster_whisper` resolves `assets/` relative to its own module path; under PyInstaller `_internal`
   the path differs between a one-folder run and a run where a different code path/`sys._MEIPASS`
   resolution is used.
2. A leftover `assets` directory from a previous (v0.4.15.x) install in
   `C:\Users\yetis\AppData\Local\Programs\VidLift\_internal\faster_whisper\` — an in-place upgrade can
   leave the old asset behind, so some runs find it and a fresh install does not.
3. The running job is stalled before the VAD load rather than past it (`device: null` throughout, no
   phase step reported), and will fail on the same asset shortly. **The spec must include a
   determination step, not a guess.**

---

## 4. What Bob must produce

Architecture + implementation spec for **v0.4.16.2**, delivering:

1. **Bundle the asset correctly.** Collect `faster_whisper` package data (the whole `assets/`
   directory, not a guessed single filename) in BOTH specs, with a documented mechanism for keeping
   the list correct if `faster_whisper` renames the asset in a future release. Pin/record the expected
   asset set rather than hardcoding one filename that will rot (note the asset is already
   version-suffixed: `silero_vad_v6.onnx`, which a prior release may have shipped as `silero_vad.onnx`).
   Consider whether a PyInstaller hook file is cleaner than inline `collect_data_files`.
2. **Fail loudly at build time, not at the user's first job.** Add a build/release-gate assertion that
   the frozen tree contains the required `faster_whisper/assets/*.onnx` files. Decide where it belongs:
   the CI "Validate installed artifact" step, a `--selftest` CLI surface, or both. State exactly what
   the gate runs and what it prints on failure.
3. **Graceful degradation at runtime (design decision required).** If the VAD asset is genuinely
   absent, decide between: (a) hard fail with a clear, actionable message naming the missing file and
   the remedy, (b) automatically retry the job with `vad_on=False` and record that honestly in the
   manifest/status, or (c) preflight-check the asset before a run starts and refuse with a clear
   reason. Give 3 options with trade-offs and an ADR-style recommendation. Whatever is chosen must be
   **honest** (no silent quality loss without a recorded note) and must reuse the existing `vad_on`
   constructor parameter rather than adding parallel machinery.
4. **Detectability in the status contract (additive only).** `api_version` must stay 1. If a new key
   is warranted (e.g. a model/VAD readiness field), keep it additive, mirroring how
   `transcription_model` was added in v0.4.16.0. Say precisely what Hermes can query to answer "is
   this install capable of transcribing right now".
5. **A regression test that would have caught this.** The Linux suite cannot see the frozen tree, so
   propose something that CAN: an assertion over the spec files themselves (that the asset collection
   is present and covers the expected filenames), a packaging-level test that runs the collector and
   checks the resulting file list, or a CI-step contract test. Be concrete; the existing suite must
   stay green and **no pre-existing test may be modified**.

## 5. Constraints

- Additive only to the status contract; `api_version` stays 1.
- No new dependencies. No new endpoints. No new MCP tools.
- The deferred-import rule in `transcribe.py` must survive: the test suite must continue to never
  import `faster_whisper`.
- Existing suite must pass unmodified (currently 921 passed, 2 skipped on
  `~/.venvs/vidlift/bin/python`, 3.12.3, pytest 9.1.1).
- Out of scope for this spec (note them, do not design them): the separate
  `cublas64_12.dll` not found error seen once in history, and Karen's K-1 stale-overlay edge.

## 6. Deliverables

Write to `/home/yetisoldier/projects/vidlift/.specs/`:
- `bob-vidlift-v0.4.16.2-architecture-spec.md` — ADRs, the 3 options for §4.3, the non-determinism
  determination from §3.4, rationale.
- `bob-vidlift-v0.4.16.2-implementation-spec.md` — module-by-module, binding names, exact spec-file
  deltas, the gate's exact command and output, test strategy.
- `bob-vidlift-v0.4.16.2-validation-spec.md` — what Karen must prove, including how she verifies the
  asset lands in a frozen tree from Linux where she cannot run the Windows build.

Return a concise executive summary with your recommendation.
