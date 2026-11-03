# Implementation Specification — VidLift v0.4.16.2: VAD asset packaging fix

**Author:** Bob (Principal Software Architect)
**Date:** 2026-11-03
**Audience:** Forge — implement to this contract; deviations require an architecture
revision, not local judgment.
**Companion:** `bob-vidlift-v0.4.16.2-architecture-spec.md` (decisions + rationale),
`bob-vidlift-v0.4.16.2-validation-spec.md` (Karen's baseline).
**Baseline:** repo @ `88a7116` (v0.4.16.1). Suite at baseline: **921 passed, 2
skipped** (`~/.venvs/vidlift/bin/python`, 3.12.3, pytest 9.1.1). Every milestone's
exit gate: full suite green with **zero modifications to pre-existing tests**
(`git diff --name-only -- tests/` shows exactly ONE new file:
`tests/test_v04162_vad_packaging.py`).

Naming note: in-code comments for this work use the `v0.4.16.2` stamp. Ignore the
stale "v0.4.17 (fail-path…)" comment at `vidlift/mcp_bridge/tools.py:704` — that
feature shipped as 0.4.16.1 (label drift; Quill's housekeeping list, not yours).

---

## 1. File inventory (what changes, what appears)

| File | Action |
|---|---|
| `vidlift/transcribe.py` | **EDIT** — new checker family + exception; WhisperEngine captures `_vad_assets_dir` + TOCTOU backstop; `_FallbackEngine` decode-arm carve-out; `transcribe_source_to_srt` preflight + one new keyword param; `__all__` additions |
| `vidlift_gui.spec` | **EDIT** — one import line + `datas=` line |
| `vidlift_cli.spec` | **EDIT** — identical two-line delta |
| `vidlift/cli.py` | **EDIT** — top-level `--selftest` flag + first-dispatch arm |
| `vidlift/monitor.py` | **EDIT** — `status_payload` gains the `transcription_engine` envelope block |
| `.github/workflows/build-windows-installer.yml` | **EDIT** — two insertions (post-build gate, post-install gate) |
| `tests/test_v04162_vad_packaging.py` | **NEW — the only test file touched** |
| `pyproject.toml`, `vidlift/__init__.py`, `packaging/version_info.txt` | **EDIT (release stanza)** — 0.4.16.2 stamps (Milestone M5) |
| `CHANGELOG.md` | release-notes hook (Quill may own; Forge leaves the entry skeleton) |

Explicitly NOT touched: `run.py`, `subs_job.py` (dispositions ride existing
`except TranscriptionError` arms), `mcp_bridge/*` (no tool-shape changes), `gui.py`
(badge is future work, arch §10), `tests/conftest.py` (rules suffice as-is).

---

## 2. Milestone M1 — Bundle the package data

### 2.1 `vidlift_gui.spec` — exact delta

After line 3 (`from pathlib import Path`) add; then replace `datas=[]` (line 11):

```python
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
```

```python
    # v0.4.16.2 (ADR-1/2): faster-whisper resolves its VAD model as
    # <dirname(utils.__file__)>/assets/*.onnx at RUNTIME; under PyInstaller that
    # path is _internal\faster_whisper\assets — the exact destination this
    # collector populates. Whole-package collect: rename- and relocation-immune
    # against upstream asset changes (the leaf is version-suffixed upstream).
    # tests/test_v04162_vad_packaging.py pins this line's presence in BOTH specs.
    datas=collect_data_files("faster_whisper"),
```

### 2.2 `vidlift_cli.spec` — identical delta (insert after its line 3; replace its
line 10 `datas=[]`). Do not alter `hookspath=[]`, `hiddenimports`, or `excludes` in
either spec. (Modulegraph already sees the function-level `from faster_whisper import
WhisperModel`; faster-whisper's own transitive imports — ctranslate2, onnxruntime,
numpy, tokenizers, av — are already in the graph, as proven by the fact that the
frozen CLI today imports the package far enough to raise the asset error.)

### 2.3 M1 exit gate

`~/.venvs/vidlift/bin/python -m pytest -q tests/test_v04162_vad_packaging.py`
(the spec-contract tests in §7.1) — green on Linux WITHOUT PyInstaller installed
(they are text contracts). Full suite: 921+N, zero edits outside the new file.

---

## 3. Milestone M2 — Core honesty (`vidlift/transcribe.py`)

All additions live in ONE new section placed immediately BEFORE the
`# ---- model delivery` section marker (after `_FallbackEngine`), except the two
surgical edits in §3.4/§3.5 and the core's new statements in §3.6.

### 3.1 New names (binding)

```python
# ---- v0.4.16.2: the VAD-asset truth surface (arch spec §6.2/M2) -----------------
```

- `VAD_ASSET_PACKAGE = "faster_whisper"`
- `VAD_ASSET_GLOB = "*.onnx"` — the requirement is *a non-empty ONNX set in the
  package's assets dir*, never a hardcoded leaf (faster-whisper version-suffixes the
  leaf: `silero_vad_v6.onnx` at 1.2.1).
- `class VadAssetMissingError(TranscriptionError)` — docstring: "The frozen/dev
  install lacks faster-whisper's bundled silero VAD payload. A packaging defect,
  never a device fault; message names the resolved assets dir + remedy."
- `def _default_vad_assets_dir() -> "tuple[str | None, str]"` — returns
  `(path_or_None, source)`; source ∈ `"find-spec" | "meipass" | "unavailable"`:

  ```python
  def _default_vad_assets_dir():
      spec = None
      try:
          import importlib.util
          spec = importlib.util.find_spec(VAD_ASSET_PACKAGE)
      except Exception:  # find_spec itself must never fault the report
          spec = None
      if spec is not None and spec.submodule_search_locations:
          return (
              str(Path(list(spec.submodule_search_locations)[0]) / "assets"),
              "find-spec",
          )
      meipass = getattr(sys, "_MEIPASS", None)  # frozen runtime fallback
      if meipass:
          return (str(Path(meipass) / VAD_ASSET_PACKAGE / "assets"), "meipass")
      return (None, "unavailable")
  ```

  IMPORT-FREE on purpose (N3): the suite must never import faster_whisper; this
  derivation is how. (`sys` is already imported module-level.)
- `def _faster_whisper_version() -> "str | None"` —
  `importlib.metadata.version("faster-whisper")` (metadata, import-free), `None` on
  `PackageNotFoundError`, swallow-and-None on anything else.
- `def vad_asset_report(resolver=None) -> dict` — resolver:
  `Callable[[], tuple[str | None, str]] | None` (default `_default_vad_assets_dir`);
  tests inject. Return contract (exact keys — the monitor envelope serves it
  verbatim plus one derived key):

  ```python
  {
      "package": "faster-whisper",
      "package_version": str | None,
      "vad_asset": "present" | "missing" | "unknown",
      "assets_dir": str | None,
      "onnx_files": list[str] | None,   # sorted names; None when unknown
      "onnx_count": int | None,
      "resolver_source": str | None,    # "find-spec" | "meipass" | "unavailable" | "injected"
      "detail": str | None,             # human sentence on missing/unknown; None on present
      "checked_at": float,              # time.time()
  }
  ```

  Rules: resolver `(None, src)` or fs errors → `unknown` (detail names WHY — never
  claim "missing" on probe failure; N5, mirrors `model_install_state`'s
  OSError→"unknown" at transcribe.py:1300-1304). dir absent → `missing`
  (detail: "assets dir absent"). dir present, zero `*.onnx` → `missing`
  (detail: "no *.onnx under assets dir"). ≥1 → `present`. When the caller injects a
  resolver, `resolver_source` reports `"injected"`.
- `def _vad_missing_message(report: dict) -> str` — the ONE message template
  (preflight and backstop share it):

  ```
  VAD model asset missing from this install: faster-whisper's assets dir
  "<assets_dir or (unresolved)>" holds no *.onnx (the bundled silero VAD model
  set — e.g. silero_vad_v6.onnx for faster-whisper 1.2.x; the leaf name is
  version-suffixed upstream). Remedy: reinstall/repair this VidLift install;
  if this is the frozen Windows build, the installer payload is defective —
  please report it.
  ```

- `def selftest_report() -> dict` — the AUTHORITATIVE frozen capability check
  (CLI `--selftest` body; arch §6.2):

  ```python
  def selftest_report() -> dict:
      if _engine_disabled_by_env():
          return {
              "ok": False,
              "skipped": "engine disabled by env (VIDLIFT_TRANSCRIPTION_ENGINE=0)",
              "checks": {},
              "errors": [],
          }
      checks, errors = {}, []
      try:
          from faster_whisper.utils import get_assets_path  # deferred (N3)
          assets_dir = get_assets_path()
          checks["faster_whisper_version"] = _faster_whisper_version()
          checks["assets_dir_resolver"] = assets_dir
          onnx = sorted(p.name for p in Path(assets_dir).glob(VAD_ASSET_GLOB))
          checks["onnx_files"] = onnx
          report = vad_asset_report()
          checks["report_agrees"] = (
              report.get("state") == "present"
              and report.get("assets_dir") == assets_dir
          )
          # load smoke, not just presence: faster-whisper's OWN VAD load path on
          # 1 s of synthetic silence proves the ONNX loads, not merely exists.
          import numpy as np
          from faster_whisper.vad import get_speech_timestamps
          get_speech_timestamps(
              np.zeros(16000, dtype=np.float32),
              vad_parameters={"min_silence_duration_ms": 300},
          )
          checks["vad_load_smoke"] = "ok"
      except Exception as exc:  # the gate's honest failure channel
          errors.append(f"{type(exc).__name__}: {exc}")
      ok = (
          not errors
          and bool(checks.get("onnx_files"))
          and checks.get("report_agrees") is True
          and checks.get("vad_load_smoke") == "ok"
      )
      return {"ok": ok, "checks": checks, "errors": errors}
  ```

  Suite legality: under conftest's suite-wide guard the first branch returns the
  deterministic skip shape — no import ever happens (N3). CI/production run without
  the guard. **The gate (§5) requires literal `ok == true`** — a skip is a build
  failure there, by construction impossible on the runner.
- `__all__` additions: `"VadAssetMissingError"`, `"vad_asset_report"`,
  `"selftest_report"` (constants need no export; they're module-level for tests).

### 3.2 Design rules for M2 (bind the implementer)

1. `vad_asset_report` performs **no mutation, no network, no import of the package**;
   it is safe at 1 Hz.
2. `selftest_report` **never raises**; every failure lands in `errors[]`.
3. The env-guard skip returns `ok: False` with `skipped` set — a gate that sees
   `skipped` fails LOUDLY, never "treats skip as pass."

### 3.3 `WhisperEngine` — assets-dir capture + TOCTOU backstop

In `__init__`, extend the deferred import block (seam rule untouched — same block):

```python
def __init__(self, model_path, *, device="auto", cpu_threads=4, vad_on=True):
    from faster_whisper import WhisperModel  # deferred: test seam rule
    from faster_whisper.utils import get_assets_path  # deferred: same seam

    self.vad_on = vad_on
    self._vad_assets_dir = str(get_assets_path())
    ...
```

In `transcribe_segments`, wrap the transcribe call AND materialization loop:

```python
try:
    segments, _info = self.model.transcribe(...)
    ... existing loop ...
except Exception as exc:
    if _is_vad_asset_fault(exc, self._vad_assets_dir if self.vad_on else ""):
        raise VadAssetMissingError(
            _vad_missing_message({
                "assets_dir": self._vad_assets_dir,
            })
        ) from exc
    raise
```

with the module-level pure predicate (unit-tested directly, no faster_whisper import):

```python
def _is_vad_asset_fault(exc: BaseException, assets_dir: str) -> bool:
    """The version-stable VAD-asset predicate: the exception's message names the
    resolved assets dir. onnxruntime's NoSuchFile hierarchy varies by version
    (1.31.x: a direct Exception subclass) — matching the KNOWN path string is
    honest (a fault naming the assets dir IS an asset fault) and version-proof."""
    return bool(assets_dir) and assets_dir in str(exc)
```

### 3.4 `_FallbackEngine` — carve-out (ADR-7)

In BOTH `detect_language` and `transcribe_segments` except arms, change:

```python
if isinstance(exc, EngineCancelled) or self._note is not None:
```

to:

```python
if (
    isinstance(exc, (EngineCancelled, TranscriptionError))
    or self._note is not None
):
```

with comment: `v0.4.16.2 (ADR-7): a TranscriptionError from the engine layer
(e.g. VadAssetMissingError) is an honest refusal, never a device guess — the
factory docstring's stated invariant (see make_whisper_engine_with_fallback),
now enforced in the decode arms. No CPU rebuild, no mislabeled note.`

**Forge audit (required, record result in PR):** `grep -n "TranscriptionError"
tests/test_v04152_cuda_fallback.py` — confirm no test drives a `TranscriptionError`
through a decode arm expecting a CPU rebuild (today's decode fakes raise
`RuntimeError`/OOM-shapes; construction-arm TranscriptionError tests are
unaffected). If an unexpected dependency is found, STOP and report — that would be
an architecture revision.

### 3.5 `transcribe_source_to_srt` — preflight

Signature gains one trailing keyword-only-compatible parameter (callers pass by
keyword or not at all; positional call sites unaffected):

```python
vad_report: dict | None = None,
```

Body: insert as the FIRST statements of the function (before `os.makedirs`):

```python
# v0.4.16.2 (ADR-3): VAD preflight — refuse BEFORE any ffmpeg/model work.
# Skips under the suite-wide engine-off env guard (production never sets it,
# tests always do) and for genuine no-VAD runs (a vad_filter=False engine
# never touches the asset — true minimal dependency truth, the existing
# vad_on seam, N6). "unknown" proceeds (probe fault is not absence, N5);
# the decode-time backstop (WhisperEngine) names a mid-run disappearance.
if not _engine_disabled_by_env() and vad_on:
    report = vad_report if vad_report is not None else vad_asset_report()
    if report.get("state") == "missing":
        raise VadAssetMissingError(_vad_missing_message(report))
```

No other change to the function. Mode A (`run.py:2737`) and Mode B (propagation
through `subs_job.py`) dispositions ride the EXISTING arms — do not edit callers.

### 3.6 M2 exit gate

Full suite green; new tests §7.2-§7.4 green; the §3.4 audit recorded.

---

## 4. Milestone M3 — Surfaces

### 4.1 `vidlift/cli.py` — `--selftest`

In `build_parser()`, immediately after the `--version` argument (~line 494):

```python
parser.add_argument(
    "--selftest",
    action="store_true",
    help=(
        "check this install's transcription capability (engine package + "
        "bundled VAD asset); prints 'vidlift-selftest {json}', exit 0 pass "
        "/ 1 fail"
    ),
)
```

In `main()`, FIRST arm before the `args.command` chain (line 744):

```python
if getattr(args, "selftest", False):
    return _cmd_selftest()
```

New handler (place beside the other `_cmd_*` handlers):

```python
def _cmd_selftest() -> int:
    """v0.4.16.2 (ADR-4): the frozen binary reports its own transcription
    capability — the release gate's authoritative voice (faster-whisper's OWN
    loader proves the payload, not our derivation). Lazy import keeps cli.py
    import-light; the suite drives main() with this seam monkeypatched."""
    import json
    from .transcribe import selftest_report

    report = selftest_report()
    print("vidlift-selftest " + json.dumps(report, sort_keys=True), flush=True)
    return 0 if report.get("ok") is True else 1
```

### 4.2 `vidlift/monitor.py` — envelope key

In `status_payload`, after the existing `model_block` computation (~line 166) —
mirroring its lazy-import comment discipline:

```python
# v0.4.16.2 (ADR-5, additive): install-scoped transcription-capability truth,
# sibling to the machine transcription_model block. Derived at read (one
# find_spec + listdir, µs); a probe fault degrades to the honest "unknown"
# shape — the handler's no-500 discipline is doubled here: the envelope must
# serve even when reporting the packaging fault this key exists to expose.
from .transcribe import vad_asset_report

try:
    engine_block = vad_asset_report()
except Exception as exc:  # noqa: BLE001 - honesty about the probe itself
    engine_block = {
        "package": "faster-whisper",
        "package_version": None,
        "vad_asset": "unknown",
        "assets_dir": None,
        "onnx_files": None,
        "onnx_count": None,
        "resolver_source": None,
        "detail": f"probe failed: {type(exc).__name__}",
        "checked_at": time.time(),
    }
engine_block["ready_for_transcription"] = (
    True
    if engine_block.get("vad_asset") == "present"
    else (False if engine_block.get("vad_asset") == "missing" else None)
)
```

and add `"transcription_engine": engine_block,` to the returned dict, after the
`"transcription_model": model_block,` line. `api_version` untouched (stays 1).
Update `status_payload`'s docstring with a v0.4.16.2 note paragraph (two lines,
same register as the v0.4.16 paragraph already there). Do NOT touch `_VIEW_METHODS`,
`/health`, the handler, or `mcp_bridge/*`.

### 4.3 M3 exit gate

Suite green incl. new §7.5 tests; manual smoke (dev box):
`~/.venvs/vidlift/bin/python -c "from vidlift.cli import main; import sys; sys.exit(main(['--selftest']))"`
→ prints the skip shape (env guard honored when set) — and with
`env -u VIDLIFT_TRANSCRIPTION_ENGINE …` prints `ok: true` on the dev box
(faster-whisper 1.2.1 present, asset present, smoke runs ~1 s).

---

## 5. Milestone M4 — Workflow gates (`.github/workflows/build-windows-installer.yml`)

Two insertions. **Version-literal hygiene is a suite-enforced contract**
(`tests/test_version_stamps.py::test_workflow_has_no_version_literals`): the added
text must contain NO dotted 4-part version and NO `VidLift`-named version shape —
none of the below does (the asset set is globbed, never named).

### 5.1 Insertion A — end of "Build GUI and CLI executables" step (after the
existing CLI version smoke `if ($ver -ne …)` block, ~line 130):

```powershell
# v0.4.16.2 (ADR-4): the frozen payload must carry faster-whisper's VAD asset
# set. Two proofs, cheapest-first: (1) the one-folder GUI tree's
# _internal\faster_whisper\assets matches the build venv's faster_whisper\assets
# 1:1 by name+size (dynamic derivation from the venv — the leaf name is
# version-suffixed upstream, so the gate hardcodes NO leaf); (2) the frozen CLI
# reports its own capability through faster-whisper's OWN loader (--selftest).
$fwAssets = (& python -c "import faster_whisper, os; print(os.path.join(os.path.dirname(faster_whisper.__file__), 'assets'))").Trim()
$distAssets = "dist\VidLift\_internal\faster_whisper\assets"
if (-not (Test-Path $distAssets)) { throw "frozen GUI tree missing faster_whisper assets dir: $distAssets" }
$expected = Get-ChildItem $fwAssets -File | Sort-Object Name
$shipped  = Get-ChildItem $distAssets -File | Sort-Object Name
$missing = Compare-Object ($expected.Name) ($shipped.Name) | Where-Object SideIndicator -eq "<="
if ($missing) { throw "frozen tree missing asset files: $(($missing | ForEach-Object InputObject) -join ', ')" }
foreach ($f in $expected) {
  $twin = Join-Path $distAssets $f.Name
  if ((Get-Item $twin).Length -ne $f.Length) { throw "asset size mismatch: $($f.Name)" }
}
$shippedOnnx = @($shipped | Where-Object Extension -eq ".onnx")
if ($shippedOnnx.Count -lt 1) { throw "frozen tree ships no *.onnx under $distAssets" }
Write-Output "vad assets shipped: $(($shipped.Name) -join ', ')"
& dist\vidlift-cli.exe --selftest | Tee-Object dist\selftest-cli.json
if ($LASTEXITCODE -ne 0) { throw "frozen CLI --selftest failed — see dist\selftest-cli.json" }
```

### 5.2 Insertion B — inside "Validate installed artifact", after the installed-file
`foreach` existence loop (~line 173) and BEFORE the `vidlift-cli.exe --version` run:

```powershell
# v0.4.16.2 (ADR-4): the INSTALLED payload must carry the VAD asset set — the
# same dynamic parity rule as the dist-tree gate (the installer, not merely the
# build, is what users run).
$instAssets = "$install\_internal\faster_whisper\assets"
if (-not (Test-Path $instAssets)) { throw "Installed payload missing faster_whisper assets dir: $instAssets" }
$installed = Get-ChildItem $instAssets -File | Sort-Object Name
$missingInst = Compare-Object ($expected.Name) ($installed.Name) | Where-Object SideIndicator -eq "<="
if ($missingInst) { throw "installed payload missing asset files: $(($missingInst | ForEach-Object InputObject) -join ', ')" }
foreach ($f in $expected) {
  $twin = Join-Path $instAssets $f.Name
  if ((Get-Item $twin).Length -ne $f.Length) { throw "installed asset size mismatch: $($f.Name)" }
}
Write-Output "installed vad assets: $(($installed.Name) -join ', ')"
& "$install\vidlift-cli.exe" --selftest | Tee-Object "$install\selftest-cli.json"
if ($LASTEXITCODE -ne 0) { throw "installed CLI --selftest failed — see $install\selftest-cli.json" }
```

(`$expected`/`$fwAssets` from insertion A are same-job pwsh variables — same job,
earlier step: re-derive them at the top of the Validate step instead to keep each
step self-contained: copy the three `$fwAssets`-derivation lines into the Validate
step header before use. **This is the one deliberate duplication; steps do not
share variables across jobs steps reliably — self-contained steps win.**)

### 5.3 Failure-output contract (what a red build must show)

- Dist parity failure: `throw` text names the missing/mismatched leaf(ler), e.g.
  `frozen tree missing asset files: silero_vad_v6.onnx`.
- Selftest failure: the `vidlift-selftest {…}` JSON (with `ok:false` and `errors[]`)
  is in the step log AND tee'd to a path (dist/install) named in the throw.
- Success prints one `vad assets shipped: …` / `installed vad assets: …` line —
  grep-able in CI logs for audit.

### 5.4 M4 exit gate

New §7.6 workflow-shape tests green; `test_version_stamps.py` and
`test_windows_packaging.py` (b20) still green (both parse the workflow text —
additions must sit INSIDE existing `- name:` blocks and add no version literals;
verify by running those two files explicitly).

---

## 6. Milestone M5 — Release stanza

1. `pyproject.toml` → `version = "0.4.16.2"`; `vidlift/__init__.py` →
   `__version__ = "0.4.16.2"`; `packaging/version_info.txt` → all four stamps
   (the two StringStructs + both tuples `(0, 4, 16, 2)` — 4-part versions map
   as-is per `test_version_stamps.py`'s documented rule).
2. CHANGELOG skeleton entry (Quill polishes): root cause, five moves, gates,
   `api_version` unchanged.
3. Suite green by version-stamp tests.
4. Dispatch `build-windows-installer` workflow; attach the artifact + the two
   selftest JSONs to the release record.

---

## 7. Test strategy — `tests/test_v04162_vad_packaging.py` (THE ONE new test file)

House rules honored: pure-file/monkeypatch assertions (no subprocess PyInstaller
runs), import-free of faster_whisper (N3), conftest guards untouched, no network
(conftest already hard-blocks it). Reuse the core-scaffold idioms from
`tests/test_v0414_transcribe.py` (fake `runner`, stub engine factories, seeded
state) — import nothing private that isn't already imported across test files;
copy tiny fakes in-file when in doubt. Every test docstring names the defect it
pins ("this test would have caught the v0.4.16.1 shipping defect because…").

### 7.1 Spec contracts (M1)

- `test_gui_spec_collects_faster_whisper_data` / `test_cli_spec_collects_faster_whisper_data`:
  each spec's text contains `collect_data_files("faster_whisper")` AND
  `datas=collect_data_files(` AND does NOT contain a hardcoded `silero` leaf
  (the release path must never pin the version-suffixed leaf).
- `test_both_specs_parity`: the two delta shapes identical (extract the
  `datas=` lines; equal modulo whitespace).

### 7.2 Checker unit tests (M2)

- `vad_asset_report` against injected resolvers over `tmp_path`: present (seed
  `assets/x.onnx` + a decoy `.txt` — assert the glob excludes it), missing-empty,
  missing-absent, unknown (resolver returns `(None,"unavailable")`), OSError→unknown
  (inject a resolver whose listing raises — e.g. point at a file-not-dir), injected
  source reporting (`resolver_source == "injected"`), key-set/schema assertion
  (exact key list from §3.1).
- `test_default_resolver_shape_dev_box`: on this dev venv, `_default_vad_assets_dir()`
  returns source `"find-spec"` and a real dir (assert `Path(dir).name == "assets"`);
  `pytest.mark.skipif` when `importlib.util.find_spec("faster_whisper") is None`.
  (NO import; find_spec only.)
- `test_selftest_report_skip_shape_under_env_guard`: suite default env →
  `ok is False`, `skipped` names the env var, `checks == {}`, AND
  `"faster_whisper" not in sys.modules` AFTER the call (the N3 seam proof —
  the suite called the real function and imported nothing).

### 7.3 Pinned asset record (ADR-6; import-free)

```python
EXPECTED_VAD_ASSETS = {
    # faster-whisper 1.2.1 (recorded 2026-11-03; the leaf is version-suffixed
    # upstream — when the faster-whisper floor moves and this trips, refresh
    # from the NEW wheel; never edit this record to silence it):
    #   python - <<'EOF'  (in an env with the new faster-whisper)
    #   import importlib.util, hashlib, pathlib
    #   d = pathlib.Path(list(importlib.util.find_spec("faster_whisper").submodule_search_locations)[0]) / "assets"
    #   for p in sorted(d.glob("*.onnx")): print(p.name, p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest())
    #   EOF
    "silero_vad_v6.onnx": (1245151, "4cbf549b8326f60f80f2536d9eefeb450a9abe83365a098031c89719f1be17d2"),
}
```

- `test_vad_asset_record_matches_installed_package`: skipif find_spec is None;
  else assert the installed assets `*.onnx` set EQUALS the record keys and each
  size+sha256 matches. This runs GREEN today on the dev box and is the tripwire
  that names upstream drift first.
- `test_record_has_no_stale_placeholder`: record non-empty (a guard against the
  empty-dict "fix").

### 7.4 Core + wrapper behavior tests (M2/M3)

- `test_core_preflight_missing_raises_before_any_work`: `transcribe_source_to_srt`
  with `vad_report={"state": "missing", "assets_dir": str(tmp_path)}`, a recording
  fake `runner`, and NO env-guard override needed (guard ON skips preflight — so
  use the existing `transcription_engine_on` fixture to clear it): raises
  `VadAssetMissingError`; message contains the dir + "reinstall"; runner recorded
  ZERO calls (preflight precedes extraction); workdir was not created (assert on
  the tmp parent).
- `test_core_preflight_skips_under_env_guard` (default guard, no fixture): with a
  missing-shaped injected `vad_report`, control reaches the OLD behavior
  (`resolve_model_dir`'s guard raises its TranscriptionError "engine disabled by
  env" — pin that exact pre-existing behavior).
- `test_core_preflight_skips_when_vad_off`: guard cleared + `vad_on=False` +
  missing-shaped injected report → no `VadAssetMissingError` (assert control
  reaches the model-resolve step — e.g. it proceeds far enough to call the fake
  runner for extraction; stub engine factory + seeded hermetic root pattern from
  test_v0414_transcribe.py so the run can complete cheaply, asserting cues land).
- `test_core_preflight_unknown_is_lenient`: injected `{"state":"unknown"}` → same
  completion as the vad_off test (proceeds; the engine decides).
- `test_core_preflight_default_resolver_present_dev_box`: guard cleared, stub
  factory, default resolver (no injection) → completes on THIS box (asset present);
  documents the guard-off real-fs behavior (lenient-unknown makes this
  environment-proof on boxes lacking the package: assert only completion, which
  both states satisfy).
- `_is_vad_asset_fault` pure tests: message containing the dir → True; empty dir
  → False; unrelated message → False; case/whitespace sensitivity pinned
  (exact substring, case-sensitive — document).
- `test_fallback_engine_reraises_transcription_error_from_first_decode` and
  `..._from_first_detect`: drive `_FallbackEngine` exactly as
  test_v04152_cuda_fallback.py does (injected ctor/build fakes; first decode raises
  `VadAssetMissingError(...)`): NO cpu ctor call, exception is the original object,
  no fallback note, no stderr WARNING. Companion: same shape with `RuntimeError`
  STILL rebuilds once (carve-out didn't widen into a behavior change — mirrors the
  existing red-first tests).
- `test_vad_missing_error_is_transcription_error` (isinstance pin — the Mode A/B
  disposition contracts key off the base class).

### 7.5 Surface tests (M3)

- `test_cli_selftest_flag_dispatch`: call `cli.main(["--selftest"])` with
  `vidlift.transcribe.selftest_report` monkeypatched to `{ok: True}` → prints
  `vidlift-selftest {…}` (capsys), returns 0; `{ok: False}` → 1. Assert no
  command dispatch side-effects.
- `test_monitor_status_envelope_carries_transcription_engine`: build the existing
  test_monitor.py fake client shape (follow that file's scaffolding); monkeypatch
  `vidlift.transcribe.vad_asset_report` to a fixed present-report; assert the
  envelope key exists, `ready_for_transcription is True`, `api_version == 1`.
  Companions: missing-report → `False`; `vad_asset_report` raising → `vad_asset
  == "unknown"` + `ready_for_transcription is None` + the envelope still serves
  (no exception — the no-500 doubling).
- `test_status_envelope_foreign_client_degrades`: reuse test_monitor.py's
  `_OldShape`-era fake discipline — minimal client; the block must serve with
  honest fields regardless (soft discipline).

### 7.6 Workflow-shape tests (M4)

- `test_workflow_dist_tree_vad_gate` / `test_workflow_installed_vad_gate`: text
  (b20-style block splits) — the Build step contains `faster_whisper\assets`,
  `--selftest`, the Compare-Object parity rule; the Validate step contains
  `$install\_internal\faster_whisper\assets` and `--selftest`. Assert NO
  `silero` literal in the workflow (the release path must stay leaf-name-free;
  the leaf lives only in the suite tripwire §7.3).
- Re-running `tests/test_version_stamps.py` + `tests/test_windows_packaging.py`
  is part of M4's gate (Forge runs them explicitly; they remain unmodified).

### 7.7 What the suite deliberately does NOT pin

Frozen-tree CONTENT from Linux (impossible there — Karen owns that, Validation Spec
V-9/V-10), the real `InferenceSession` load in the suite (N3 — proven instead by
the CI selftest), and upstream's leaf name in any release-path file (ADR-6).

---

## 8. Dependencies & conventions

- No new dependencies (N2). New imports used: `importlib.util`, `importlib.metadata`
  (stdlib); inside `selftest_report` only: the existing `faster_whisper`/`numpy`
  runtime deps, deferred.
- Style: match the module docstring register (every new section carries a
  `v0.4.16.2 (ADR-n)` provenance comment naming the decision it implements).
- Message constants: `_vad_missing_message` is the ONE template; surfaces may
  decorate but never fork wording.
- Error taxonomy: `VadAssetMissingError` subclasses `TranscriptionError` ONLY;
  do not introduce parallel VAD machinery (N6).

## 9. Acceptance criteria (binding)

1. Both specs collect `faster_whisper` data; suite contracts pin it.
2. Suite: 921 pre-existing + N new pass, 2 skipped, on the Linux gate; ZERO
   modifications outside `tests/test_v04162_vad_packaging.py`.
3. On the dev box, unguarded `vidlift --selftest` prints `"ok": true`; under
   `VIDLIFT_TRANSCRIPTION_ENGINE=0` it prints the skip shape and exits 1.
4. CI build: both new gate blocks pass and print their `vad assets …` lines; a
   doctored tree (Karen's negative test, Validation Spec V-5) fails at the dist
   gate with the named leaf in the throw text.
5. Monitor `/status` serves `transcription_engine` with
   `ready_for_transcription` true/false/null per report; `api_version == 1`;
   bridge/MCP shapes byte-unchanged.
6. `transcribe_source_to_srt` with a missing asset refuses before ffmpeg/model
   work with the named message; Mode A fail-open wording and Mode B failure text
   carry it verbatim (existing arms, no caller edits).
7. `_FallbackEngine` never rebuilds on a `TranscriptionError` from a decode arm;
   RuntimeError decode faults keep their existing once-rebuild behavior.
8. `grep -rn silero .github/workflows/` → no hits (release path leaf-free);
   `grep -n "silero_vad_v6" tests/test_v04162_vad_packaging.py` → exactly the
   pinned-record entry.
