# Forge Task — VidLift v0.4.16.2: bundle the faster-whisper VAD asset

You are **Forge**. Implement Bob's design for the VAD packaging hotfix.

## Read first (all in-repo)

1. `.specs/bob-vidlift-v0.4.16.2-implementation-spec.md` — your binding work order.
2. `.specs/bob-vidlift-v0.4.16.2-architecture-spec.md` — ADRs, rationale, the non-determinism determination.
3. `.specs/bob-vidlift-v0.4.16.2-validation-spec.md` — what Karen will hold you to.
4. `.specs/hermes-brief-vidlift-v0.4.16.2-silero-vad.md` — the live defect evidence.

## The defect in one line

The v0.4.16.1 Windows installer cannot transcribe any file:
`NO_SUCHFILE ... _internal\faster_whisper\assets\silero_vad_v6.onnx`. Both PyInstaller
specs carry `datas=[]`, so faster-whisper's package data is never bundled.

## What to deliver (per Bob's spec)

- **M1** `collect_data_files("faster_whisper")` (whole package) in BOTH `vidlift_gui.spec`
  and `vidlift_cli.spec`.
- **M2** the checker family in `vidlift/transcribe.py` (`vad_asset_report`,
  `selftest_report`, `VadAssetMissingError(TranscriptionError)`) — names are binding.
- **M3** VAD preflight at the top of `transcribe_source_to_srt`, the TOCTOU backstop in
  `WhisperEngine.transcribe_segments`, and the `_FallbackEngine` decode-arm carve-out
  (ADR-7) so an honest `TranscriptionError` is not mislabeled as a CUDA device fault.
- **M4** `--selftest` on the frozen CLI (JSON, exit 0/1) plus the CI gate steps in
  `.github/workflows/build-windows-installer.yml`.
- **M5** additive `transcription_engine` envelope key on the monitor's `GET /status`.
- **M0** new test file `tests/test_v04162_vad_packaging.py` only.

## Constraints (non-negotiable)

- `api_version` stays 1. Additive keys only. No new endpoints, no new MCP tools, no new deps.
- **The deferred-import rule in `transcribe.py` must survive**: the test suite must never
  import `faster_whisper`. Your new checks must be import-free / `find_spec`-based.
- **No pre-existing test may be modified.** Currently 921 passed, 2 skipped.
- Gate env: `/home/yetisoldier/.venvs/vidlift/bin/python` (3.12.3, pytest 9.1.1).
- Bump the version to **0.4.16.2** in `pyproject.toml`, `vidlift/__init__.py`, AND all four
  stamps in `packaging/version_info.txt` (both strings AND both numeric tuples
  `filevers`/`prodvers` — a prior bump missed the tuples and left the suite red).
- Do not touch: the separate `cublas64_12.dll` concern, Karen's K-1 stale-overlay edge.
- Windows-only behaviours must be tested cross-platform: never round-trip a hardcoded
  `/`-rooted path, and never assert on a raw captured `os.path.exists` call stream.

## Done when

- Full suite green (expect 921 + your new tests), run it yourself and report the real number.
- Report: changed files with line counts, what you verified and how, and anything you
  deliberately did not do.
