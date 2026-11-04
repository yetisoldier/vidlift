# Quill Task — VidLift v0.4.16.2 documentation pass

You are **Quill**. Document the v0.4.16.2 patch release. This is a **packaging hotfix**, not a
feature release. Accuracy over volume; do not invent capabilities.

Repo: `/home/yetisoldier/projects/vidlift` at commit `17b2b78`, version bump already applied
(0.4.16.2). Forge already put a skeleton entry in `CHANGELOG.md`; you own the polish.

## What actually changed (verified — do not embellish)

1. **The Windows installer now bundles faster-whisper's Silero VAD model.** v0.4.16.1 shipped
   without it, so every transcription on a fresh install failed with
   `NO_SUCHFILE ... _internal\faster_whisper\assets\silero_vad_v6.onnx`. Both PyInstaller specs
   now collect the package's data files.
2. **A VAD preflight now refuses to start a job when the asset is genuinely missing**, with a
   clear message naming the file and the remedy, instead of failing deep inside transcription.
3. **`vidlift-cli --selftest`** (new flag) prints a JSON capability report and exits 0 when the
   install can transcribe, non-zero when it cannot. This is what CI and users can use to check
   an install before running a job.
4. **The monitor `GET /status` gained an additive `transcription_engine` key** reporting readiness
   (asset present, count, resolver source, package version). `api_version` is still `1`; the key
   is additive and older clients are unaffected.
5. **CI now fails the build if the bundled VAD asset set is missing or misnamed.** This closes
   the release-gate gap that let v0.4.16.1 ship.

Carried over from v0.4.16.1 (already documented, do not re-describe as new): truthful failure
reporting, failure reasons surfaced in the details panel, overlay and history.

## Deliverables

- `CHANGELOG.md` — polish the 0.4.16.2 entry: user-facing, plain, accurate. Lead with what a
  user notices (transcription works on a fresh install). Mention `--selftest` as something they
  *can* run, not something they must.
- `README.md` — if the troubleshooting table needs a row for "transcription fails immediately on
  a fresh install", add it, linked to a matching section that **you also add**.
- `docs/usage.md` — add a short troubleshooting section covering the missing-VAD failure and how
  to confirm an install is healthy with `--selftest`. Include the exact command.
- `docs/` — check whether any existing page describes the monitor status payload; if so, add
  `transcription_engine` to it. If no such page exists, do not create a new one just for this.

## Hard rules

- **Every anchor you introduce must resolve.** If you link `docs/usage.md#some-section`, that
  section must exist AND describe that exact problem. A previous docs pass shipped a link to a
  section that did not exist; do not repeat it.
- Do not document the `cublas64_12.dll` issue or the stale-overlay edge; both are out of scope.
- Do not add version numbers inside `.github/workflows/` (a suite test forbids them there).
- Do not claim Windows-tested behaviour you cannot verify. Phrase operational claims as what the
  build now guarantees, not as a bench result.
- No new dependencies, endpoints, tools. `api_version` stays 1.
- Do not touch code or tests. Docs and changelog only.

## Verify before you report

- Grep every anchor you added and confirm the target heading exists (show the grep).
- Run the doc-related tests that exist, and the version/CHANGELOG consistency tests:
  `/home/yetisoldier/.venvs/vidlift/bin/python -m pytest -q tests/test_version_stamps.py`
  and any changelog test. Report the real counts.
- Report the files you changed with line counts, and the anchors you verified.
