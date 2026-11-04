# Forge Implementation Report — v0.4.17 Mode B replace-source

**Implementer:** Forge — **Date:** 2026-11-04
**Spec:** `.specs/bob-vidlift-v0.4.17-implementation-spec.md` (Bob, authoritative)
**Scope:** Mode B opt-in "replace original on success" — items 1-20, milestones M1→M4.

## Verdict

**IMPLEMENTED — all four milestones landed and the full pytest suite is green under the
canonical gate interpreter (`/home/yetisoldier/.venvs/vidlift/bin/python`).**

## What changed

- `vidlift/subs_job.py` — module docstring caveat, `__all__` growth, new pure
  `subs_replace_target()`, new helper `replace_subs_output()` (§6.1 algorithm verbatim),
  `run_subs_job` gains keyword-only `replace_source=False` (inserted after `force`) plus
  the §6.2 tail block (cancel-observe → `_step("replace-source")` → replace → honest paths;
  `(SubsJobError, OSError)` → keep-both warning note).
- `vidlift/mcp_bridge/client_local.py` — `enqueue_subs()` gains keyword-only
  `replace_source: bool = False`, written into registry options.
- `vidlift/mcp_bridge/jobs.py` — `_execute_subs_job` reads
  `bool(options.get("replace_source", False))` (absent-defaults) and forwards it.
- `vidlift/mcp_bridge/schemas.py` — `transcribe` schema gains `"replace_source"` boolean;
  tool description amended (`.mp4` naming rule named); `api_version` untouched.
- `vidlift/mcp_bridge/tools.py` — `tool_transcribe` maps the flag into job options.
- `vidlift/cli.py` — `subs` parser gains `--replace-source` (store_true); `_cmd_subs`
  threads it into `run_subs_job`; dispatch passes it; exit codes unchanged.
- `vidlift/gui.py` — new `self.subs_replace_source` QCheckBox at grid (2, 8) beside
  "Add Subtitles…" (§6.3 tooltip/label), QSettings key `"subs_replace_source"` with
  `_subs_replace_source_changed` persistence, add_subs_button tooltip amended,
  `_subs_file_rows` column 3 tracks the submitted state, `choose_subs_files` passes the
  checkbox state to enqueue.
- `vidlift/__init__.py`, `pyproject.toml`, `packaging/version_info.txt` — `0.4.17.0`
  (all four version_info values).
- `CHANGELOG.md` — new `[0.4.17.0]` section.
- New tests: `tests/test_v0417_subs_replace_source.py` (§8 file A, groups 1-6);
  `tests/test_v0414_mode_b_gui.py` extended (§8 file B, items 25-29).
- `vidlift/run.py` — **zero lines changed** (verified `git diff vidlift/run.py` empty;
  `_sha256_file` is lazily imported, never moved).

Milestone results and the deviations section are appended below.
---

## Milestone M1 — core (items 1-6) ✅

**Changes (vidlift/subs_job.py only):**
- Item 1: module docstring gained the opt-in caveat paragraph — source-blind **by default**; the `replace_source` post-success path named.
- Item 2: `__all__` grew `"subs_replace_target"` and `"replace_subs_output"`.
- Item 3: new pure `subs_replace_target()` after `subs_output_path` — byte-identical stem-strip regex (`re.sub(r"\.[^.]+$", "", base)`), D6 always-`.mp4` / never-carry-the-old-extension docstring.
- Item 4: new `replace_subs_output()` at end of module — the §6.1 algorithm followed in the spec's exact order: guards (missing-output / missing-source / stranger-`{stem}.mp4` collision refusals, normcase+abspath identity for the same-path case) → same-device `os.replace(output, target)` (OSError → SubsJobError) → defensive cross-device arm (stale-temp sweep, `shutil.copy2` staging at `str(target)+".replace-tmp"`, lazy `from .run import _sha256_file` hash-verify, `os.replace`, `finally` sweep, best-effort `output.unlink()`) → skip source-delete when same_path, else `os.remove(source)` with failure → narrated warning → guarded sidecar rename (D1) → success note `replace-source: output now at {target}` → `(str(target), final_srt)` return.
- Item 5: `run_subs_job` gained keyword-only `replace_source: bool = False` immediately after `force`; docstring gained the never-overwrites-except caveat paragraph.
- Item 6: the §6.2 tail block verbatim — cancel observe (`should_cancel is not None and should_cancel()` → lazy `EngineCancelled("cancelled after remux, before replace")`), `_step("replace-source")`, try `replace_subs_output(... log_line=notes.append ...)`, honest `final_output`/`final_srt` updates, `except (SubsJobError, OSError)` → `"WARNING replace-source failed; keeping both files: {exc}"` note; result built with the final paths + extended notes.

**Tests added (file A groups 1-4):** `tests/test_v0417_subs_replace_source.py`, 18 tests covering spec §8 items 1-17 exactly (pure naming ×2; helper happy paths incl. no-sidecar-→-None, source-is-mp4-no-delete, cross-device staging+verify+sweep + mid-copy-failure keep-both; all refusals/degradations with the §6.4 strings; run_subs_job integration incl. the OFF byte-identical invariant, replace-failure keep-success, cancel-after-remux with zero target renames, and the inert-None CLI posture). FakeEngine/FakeRunner/`engine_on`/`no_lock` seams imported from `test_v0414_subs_job.py` (single source of truth).

**Gate:**
- `pytest tests/test_v0417_subs_replace_source.py tests/test_v0414_subs_job.py -x` → **35 passed**
- `pytest tests/test_run.py` → **104 passed**, unmodified (`git diff vidlift/run.py` empty — Mode A byte-identical).

---

## Milestone M2 — plumbing (items 12-17) ✅

**Changes:**
- Item 12 (`client_local.py`): `enqueue_subs()` gained keyword-only `replace_source: bool = False`; the options dict now carries `"replace_source": bool(replace_source)`; docstring updated (no longer "the GUI picker exposes no overrides" alone — it names the flag and the absent-defaults back-compat). No-kwarg callers stay behavior-identical (options always carry the explicit False, exactly as v0.4.16 wrote no-key-but-read-False).
- Item 13 (`jobs.py`): `_execute_subs_job` forwards `replace_source=bool(options.get("replace_source", False))` with the absent-defaults comment (mirrors jobs.py:247's Mode A v0.4.7 comment style). No other changes — the honest-path flow-through is automatic (`result.output_path` already feeds the JobOutcome).
- Item 14 (`schemas.py`): `TOOL_SCHEMAS["transcribe"]["properties"]` gained `"replace_source": {"type": "boolean"}` with the versioned comment (opt-in; absent-defaults; rename-to-`.mp4` named); `TOOL_DESCRIPTIONS["transcribe"]` gained the spec'd clause "…never overwriting it (unless the optional `replace_source` flag is set — then on success the source is deleted and the output takes the source's name with an `.mp4` extension, with a keep-both guard on collision)". `api_version` untouched.
- Item 15 (`tools.py`): `tool_transcribe` reads `replace_source = bool(args.get("replace_source", False))` and writes it into the options dict; docstring arg list updated.
- Item 16 (`cli.py`): `subs` parser gained `--replace-source` (`store_true`) with the spec's exact help text (Mode B naming rule spelled out, e.g. `…35 Hours.avi → …35 Hours.mp4`).
- Item 17 (`cli.py`): `_cmd_subs` gained kw-only `replace_source: bool = False`, passed into `run_subs_job`; dispatch passes `args.replace_source`. Exit codes unchanged (refused replace ⇒ succeeded job ⇒ exit 0).

**Tests added (file A groups 5-6, §8 items 18-24):** argparse default/flag; `_cmd_subs` kwarg threading + refused-replace-note-exits-0 + OFF-false; schema acceptance/type-refusal (`InvalidInput(field="replace_source")`)/absent-default; `tool_transcribe` option present-True/absent-False; real `_execute_subs_job` drive with legacy-record-absent → False (absent-defaults) and present-True → True; terminal-echo payload keys identical to v0.4.16.2 with the renamed path; real `LocalBridgeClient` registry round-trip for both postures.

**Gate:** `pytest tests/test_mcp_bridge.py tests/test_cli_v049.py tests/test_client_local.py tests/test_v04151_subs_guard.py tests/test_v04153_subs_bitmap_remux_slow.py tests/test_v0414_subs_job.py tests/test_v0417_subs_replace_source.py tests/test_run.py` → **202 passed** (all unmodified suites stayed green).

---

## Milestone M3 — GUI (items 7-11) ✅

**Changes (vidlift/gui.py):**
- Item 7: new `self.subs_replace_source` QCheckBox — label "Replace original after adding subtitles (deletes the source)", `setChecked(self._settings().value("subs_replace_source", False, type=bool))`, the §6.3 tooltip verbatim (worked example `…35 Hours.avi → …35 Hours.mp4`, the sidecar rename-to-match note, the keep-both promise, and the "Applies to Add Subtitles only — the upscale panel has its own replace option" scope disambiguation), `toggled.connect(self._subs_replace_source_changed)`. The `add_subs_button` tooltip's "the source is never modified" sentence now reads "the source is kept (a per-run opt-in can replace it — see the checkbox beside this button)".
- Item 8: `grid.addWidget(self.subs_replace_source, 2, 8)` right of `grid.addWidget(self.add_subs_button, 2, 7)`, with the layout comment.
- Item 9: `_subs_replace_source_changed` persists `"subs_replace_source"` (the verbatim `_replace_source_changed` QSettings pattern; the key-isolation contract named in its docstring).
- Item 10: `_subs_file_rows` column 3 → `"replace original → .mp4" if self.subs_replace_source.isChecked() else "keep original"` (rows render the exact state being enqueued).
- Item 11: `choose_subs_files` calls `self.client.enqueue_subs(paths, replace_source=self.subs_replace_source.isChecked())` per the spec's exact call shape.

**Test doubles (harness plumbing for the new call shape — see deviations):** `_ScriptedClient.enqueue_subs` (test_v0414_mode_b_gui.py, the shared harness test_v04151_subs_guard.py imports) gained the defaulted kwarg and records `last_subs_replace_source`; test_gui.py's client double and the F-13b exploding monkeypatch gained kwarg tolerance; test_v0417_failpath_fix.py's double likewise. Zero assertion semantics changed in any pre-existing test.

**Tests added (file B, §8 items 25-29):** grid position (2,8) beside the button at (2,7) + default OFF + tooltip contents (D6 rule, keep-both, scope, sidecar note) + button-tooltip amendment; persistence/restore both directions with QSettings; key isolation both directions against clean per-direction settings files (Mode B never writes Mode A's key, and vice versa); pick flow threads True when ON / False when OFF; `_subs_file_rows` column 3 text both ways; Mode A `start_upscale` still reads only `self.replace_source` (the Eric-confusion regression fence, inspect-based pin).

**Gate:** `pytest tests/test_v0414_mode_b_gui.py tests/test_v0414_mode_b_gui_executor.py tests/test_gui.py` → **74 passed** (executor suite unmodified and green).

---

## Milestone M4 — release hygiene (items 18-20) ✅

- **Item 18:** `vidlift/__init__.py` `__version__ = "0.4.17.0"`; `pyproject.toml`
  `version = "0.4.17.0"`; `packaging/version_info.txt` all four values —
  `filevers=(0, 4, 17, 0)`, `prodvers=(0, 4, 17, 0)`, `FileVersion='0.4.17.0'`,
  `ProductVersion='0.4.17.0'`. Gate: `tests/test_version_stamps.py` → **7 passed**.
- **Item 19:** `CHANGELOG.md` gained the `[0.4.17.0] - 2026-11-04` section above
  `[0.4.16.2]` — "Added" names all three surfaces (GUI checkbox / CLI flag / MCP
  option), the rename-to-`.mp4` rule, the collision keep-both guard, and the sidecar
  rename-to-match; "Notes" records the `.srt`-toggle follow-up as explicitly out of
  scope.
- **Item 20 (docs sweep):** grep executed across `README.md`, all `docs/*.md`, and
  `docs/designs/*.md` for `.subs.mp4` / `source-blind` / "never touching the
  source" / "keep original" — **zero user-facing passages describe Mode B's
  keep-original default anywhere** (README says nothing about Add Subtitles; the
  design docs are historical and the spec forbids architectural changes there).
  No edits required; recorded here as a verified no-op rather than an invented edit.

**Full-suite gate (canonical interpreter):**
`/home/yetisoldier/.venvs/vidlift/bin/python -m pytest tests/` →
**990 passed, 2 skipped** (both skips are pre-existing Windows-only GPU-meter
proofs, unrelated). ~106 s wall.

---

## Deviations from spec

1. **Test doubles gained a defaulted kwarg (required by item 11's exact call
   shape).** Spec item 11 mandates the GUI's unconditional call
   `self.client.enqueue_subs(paths, replace_source=self.subs_replace_source.isChecked())`.
   Three pre-existing test doubles implement the same `enqueue_subs` client contract
   and would have raised `TypeError` against that shape:
   - `_ScriptedClient.enqueue_subs` in `tests/test_v0414_mode_b_gui.py` (sanctioned
     to extend by §8 file B — and it is what `tests/test_v04151_subs_guard.py`, an
     unmodified-pass suite, imports, so extending it is also what keeps that fence
     green);
   - the client double in `tests/test_gui.py` (M3 gate) and its `exploding_enqueue_subs`
     monkeypatch (gained `**_kwargs`);
   - the client double in `tests/test_v0417_failpath_fix.py` (M4 full-suite green).
   Each change is additive and back-compatible (keyword-with-default), records/passes
   the flag through, and changes zero assertion semantics in any pre-existing test.
   None of these four files are in the spec's unmodified-pass invariant list, and the
   alternative (a conditional call that omits the kwarg when OFF) would have violated
   item 11's literal call shape and weakened §8-27.
2. **`tests/test_v0414_mode_b_gui.py` was split into the planned five new contracts
   across six test functions** (the persistence and key-isolation items of §8-26 run
   as two functions against separate clean QSettings files per isolation direction).
   Reason: `contains()` can only prove absence on a settings file the other direction
   has never written; a single shared file would have made one of the two isolation
   assertions unfalsifiable. The spec's five contract items are all present; only the
   function packaging differs, and the harness's per-test `tmp_path` keeps each
   direction decisive.
3. **Cross-device-arm test 6 uses a byte-copy stand-in for `shutil.copy2` plus a
   divergent-`st_dev` fake `os.stat`.** The fake stat (tuple-built `stat_result`) lacks
   the nanosecond slots stdlib `copystat` reads, so the real `copy2` crashes inside
   the test's seam even though production code is untouched. The test's subject is the
   staging/SHA-256/atomic-rename/temp-sweep/`output.unlink` flow (which the real
   helper still executes, including real `_sha256_file` hashing); metadata fidelity of
   `copy2` is stdlib-tested. The spec's test plan asked for "staged temp-sibling copy,
   SHA-256 verified, os.replace, output unlinked, temp gone, byte-identity" — all
   asserted.
4. **`subs_replace_target` returns the string path via the identical
   `os.path.join/os.path.dirname` derivation as `subs_output_path`** — the spec's §6.1
   outline shows `target = Path(subs_replace_target(source_path))` with the helper
   holding the derivation; implemented exactly as item 3 describes (the pure function
   owns it and both consumers use it). No behavioral difference; stated for
   completeness.
5. **`replace_subs_output` wraps its narration behind an internal `_log` guard** so
   the `log_line=None` default is inert. The §6.1 outline calls `log_line(...)`
   unconditionally at three narration sites; with the spec'd `None` default that would
   crash the bare library call (the tail block always passes `notes.append`, so the
   spec'd caller behavior is unaffected). The function body otherwise follows the
   outline step-for-step.

**Non-deviation confirmations:** `vidlift/run.py` is byte-identical (`git diff
vidlift/run.py` empty; also zero diff on the other four named no-touch modules:
`registry.py`, `transcribe.py`, `subtitles.py`, `monitor.py`); the option-OFF
behavior is asserted byte-identical to v0.4.16.2 (§8-14); all six §6.4 stable
strings are exact; the §6.1 algorithm order and §6.2 tail block are followed
verbatim; the GUI tooltip is §6.3 verbatim; the CLI help text is item 16 verbatim;
`api_version` untouched; no new third-party dependencies.

## Observations for validation (not deviations)

- With the checkbox ON, the subs detail panel will print the column-3 value as
  `Resolution handling: replace original → .mp4` (`_subs_detail_text` renders any
  non-"keep original" column-3 text under that heading). Spec item 10 changes only
  the cell; the heading text was left untouched per the change table. Flagging so
  Karen's UI pass isn't surprised.
- The `subs_output_path` never-overwrite pre-flight and `.subs.mp4` naming are
  unchanged; a second Mode B job for the same source after a replace will collide
  with the pre-existing `{stem}.subs.mp4` only if one still exists — with replace ON,
  the renamed landscape means a re-pick runs against `{stem}.mp4`/`{stem}.srt`, which
  the existing output-guard evaluates coherently (it guards the `.subs.mp4` name only,
  as shipped in v0.4.16).
- `vidlift.egg-info/` carries the previous release's metadata from an earlier local
  build; it is untracked build residue, not a stamp the version gate reads.

## Developer notes

- The duplicated replace discipline (Mode A `replace_source_with_output` vs Mode B
  `replace_subs_output`) is deliberate per spec D3 — one shared ~60-line
  parameterization was rejected by Bob; the SHA-256 primitive is the only shared
  code, lazily imported (`from .run import _sha256_file`), matching
  `_default_runner`'s established posture.
- The Mode B helper deliberately stages its cross-device temp at a SIBLING OF THE
  TARGET (not of the output) — same directory as both, so the final `os.replace` is
  always an atomic rename(2) even if a future Mode B output-dir option ever makes
  output and target genuinely cross-device.
- The tail block's `EngineCancelled` path skips `_step("replace-source")` by design:
  the GUI's step heartbeat only exists after the cancel gate, so a cancelled job
  never shows a replace phase it did not run.
