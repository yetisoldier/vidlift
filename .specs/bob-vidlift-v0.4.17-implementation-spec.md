# VidLift v0.4.17 Implementation Spec — Mode B opt-in "replace original on success"

**Author:** Bob (Principal Software Architect)
**Date:** 2026-11-04
**Source brief:** `.specs/hermes-brief-v0.4.17-modeb-replace-source.md` (Hermes)
**Audience:** Forge (implementation), Karen (validation baseline in §10)
**Scope:** Mode B (Add Subtitles) replace-source only. The unrelated sidecar-vs-burned `.srt` toggle Eric raised earlier is a tracked follow-up and is **not** part of this release.

---

## 0. Evidence verification

Every fact in the brief's "Root cause" section was re-verified against the working
tree before this spec was written:

- `vidlift/run.py:1891-1921` — `run_file(..., replace_source: bool = False, ...)` (Mode A only; at :1910).
- `vidlift/run.py:1683-1796` — `replace_source_with_output()` (output moves onto the source's **exact** path).
- `vidlift/run.py:2964-2974` — sole caller, post-`verify_output`, `RunError`/`OSError` → logged warning, run still succeeds.
- `vidlift/run.py:1154-1159` — `_sha256_file()` (chunked SHA-256 used by the v0.4.15.3 F-2 cross-device staging discipline).
- `vidlift/subs_job.py:97-117` — `run_subs_job()` signature, no `replace_source`.
- `vidlift/subs_job.py:70-77` — `subs_output_path()` → `{stem}.subs.mp4` beside the source.
- `vidlift/subs_job.py:8` — "never touching the source (source-blind safety)" (docstring must gain the opt-in caveat).
- `vidlift/gui.py:605-618` — Mode A checkbox, label/tooltip upscale-scoped; `gui.py:1181-1185` — `_replace_source_changed` persists QSettings key `"replace_source"`.
- `vidlift/gui.py:661` — denoise comment confirming Mode B has no prepare chain (evidence line as cited).
- `vidlift/gui.py:1525-1541` — `_subs_file_rows()` hard-codes the `"keep original"` column (column index 3).
- `vidlift/gui.py:743-753` — `self.add_subs_button` ("Add Subtitles…"); grid cell **(2, 8)** is free (`grid.addWidget(self.add_subs_button, 2, 7)` at gui.py:1328).
- `vidlift/gui.py:1636` — `self.client.enqueue_subs(paths)` (no options surface today).
- `vidlift/mcp_bridge/jobs.py:459-479` — `_execute_subs_job()` calls `run_subs_job(...)`; `jobs.py:247` shows the Mode A `replace_source=bool(options.get("replace_source", False))` pattern to mirror. Subs options arrive via `record.options` (`jobs.py:353`).
- `vidlift/mcp_bridge/schemas.py:120-134` — the `transcribe` tool schema (paths/lang/model/device/force); `:51` carries Mode A's `replace_source`.
- `vidlift/mcp_bridge/tools.py:736-767` — `tool_transcribe()` builds the subs `options` dict.
- `vidlift/mcp_bridge/client_local.py:162-193` — `enqueue_subs()` builds options `{"language": "", "model": None, "device": "auto", "force": False}`.
- `vidlift/cli.py:370-418` — `_cmd_subs()`; `cli.py:662-687` — the `subs` parser (no replace flag); `cli.py:802-810` — dispatch; `cli.py:594-600` — Mode A's `--replace-source` help text to mirror.
- Cancel seam: `transcribe.py:2429-2432` — `if should_cancel is not None and should_cancel(): raise EngineCancelled(...)`; `jobs.py:53-63` — `CancelFlag` is callable (`cancel() -> bool`). `EngineCancelled` is a `RunError` subclass (`jobs.py:514-515` comment).
- Version stamps: `vidlift/__init__.py:9` `__version__ = "0.4.16.2"`, `pyproject.toml:3`, `packaging/version_info.txt` (both strings + both tuples), gated by `tests/test_version_stamps.py`.
- Mode A replace tests to keep green **unmodified**: `tests/test_run.py:1073-1143`.
- Mode B suites to keep green **unmodified**: `tests/test_v0414_subs_job.py`, `tests/test_v0414_mode_b_gui.py`, `tests/test_v0414_mode_b_gui_executor.py`, `tests/test_v04151_subs_guard.py`, `tests/test_v04153_subs_bitmap_remux_slow.py`, `tests/test_cli_v049.py`.

---

## 1. Decisions — every "Decisions Bob must make" item, answered

### D1 (brief item 1): Sidecar `.srt` — rename-to-match, guarded

**Decision:** On a successful replace, rename `{stem}.subs.srt` → `{stem}.srt` so the
delivered pair is `{stem}.mp4` + `{stem}.srt` (the player auto-load convention: a
sidecar is picked up when its name equals the media file's stem + `.srt`; leaving
`{stem}.subs.srt` would silently orphan it under most players' sidecar rules).

**Guard:** the rename is **best-effort and never destructive** — if `{stem}.srt`
already exists as a different file (normcase-and-identity check), or the `os.replace`
raises, the sidecar **keeps its `{stem}.subs.srt` name**, a warning is logged into the
result notes, and the mp4 replace **still ships**. The sidecar can never block,
downgrade, or destroy the mp4 replace; an unrelated pre-existing file is never
overwritten.

**Rationale:** the brief recommends rename-to-match (I agree — the pair stays in sync
and Eric's end-state reads "the exact filename" for the media; the sidecar should
follow its media). The refusal branch preserves the "never destroy data you didn't
create" invariant that the whole replace feature is built on.

**Consequence:** `SubsJobResult.srt_path` must report the **honest** post-replace
location; the helper returns it (§5, §6).

### D2 (brief item 2): Target-path collision guard — refuse, keep both

**Decision:** target = `{stem}.mp4` beside the source, always.

- **Same path as the source** (source is itself `.mp4`; compare with the Mode A
  discipline — `os.path.normcase(os.path.abspath(...))` on both sides, so
  case-insensitive Windows volumes are handled): **allowed, and it is safe**. The
  final step for this case is a single `os.replace(output, target)` — one atomic
  rename(2) that *is* the delete-and-move; when target == source the rename consumes
  the source's name. There is no "delete, then hope the move lands" window. Confirming
  the brief's question explicitly: **safe and intended**, and it is the strongest
  case of the whole feature (no cleanup step at all).
- **Target exists and is a *different* file** (e.g. `35 Hours.avi` beside an unrelated
  pre-existing `35 Hours.mp4`): **refuse before touching anything** — raise
  `SubsJobError` inside the helper, which the job converts to a logged/note warning.
  Nothing is deleted, nothing is moved: the source, the verified `.subs.mp4`, and the
  stranger `.mp4` all survive. The job still reports success. Overwriting an unrelated
  file is categorically off the table.

**Rationale:** mirrors Mode A's "never destroy the only good data" guardrail and
extends it to a file Mode A never has to worry about (Mode A's output targets the
source's own path, so the only possible victim is the source itself — consented).
Mode B renames onto a *new* name, so a third-party file can sit there; consent for
the source does not transfer to it.

### D3 (brief item 3): Dedicated helper in `subs_job.py` — Mode A stays byte-identical

**Decision:** option (a). Add `replace_subs_output()` to `vidlift/subs_job.py`
carrying the **same atomicity discipline** (temp-sibling staging + SHA-256
verification + atomic `os.replace`) but with rename-to-stem-`.mp4` semantics.
Do **not** generalise `vidlift/run.py:1683 replace_source_with_output()`. Do not touch
a single byte of the Mode A code path — no refactor, no shared-parameter plumbing, no
renaming.

**Alternatives considered:** (b) generalising the Mode A helper with a `target_name=`
parameter was rejected: it edits the most safety-critical function in the upscale
pipeline for a feature Mode A does not need, and every regression risk lands on the
path Eric already trusts. Shared code here buys ~60 lines and risks the flagship
safety invariant; the discipline is duplicated deliberately and documented as such.

**Reuse:** the SHA-256 primitive is *not* duplicated — the cross-device staging branch
lazy-imports `from .run import _sha256_file` (the established lazy-import pattern in
`subs_job.py:224-227 _default_runner`, identical circular-import posture).

### D4 (brief item 4): Surface — GUI checkbox beside "Add Subtitles…", CLI flag, MCP option

**Decision:** three opt-in surfaces, one semantics, absent-defaults everywhere:

1. **GUI:** a new `QCheckBox` bound as `self.subs_replace_source`, placed at grid cell
   **(2, 8)** — immediately right of `self.add_subs_button` at (2, 7) — the only free
   cell on the Mode B action row (gui.py:1322-1328). Spatially binding the control to
   the Mode B button is deliberate: the root cause of Eric's report was a Mode A
   control being *assumed* to govern Mode B; co-location makes the scope visually
   unambiguous and keeps the upscale settings panel untouched. QSettings-persisted
   under key **`"subs_replace_source"`** (no collision with Mode A's
   `"replace_source"`), default **OFF**, persisted from its `toggled` signal exactly
   like `_replace_source_changed` (gui.py:1181-1185).
2. **CLI:** `--replace-source` (`action="store_true"`, default False) on the `subs`
   subparser, help text mirroring cli.py:594-600 with the Mode B naming rule spelled
   out (D6). Threaded through `_cmd_subs` → `run_subs_job(replace_source=...)`.
3. **MCP:** the `transcribe` tool schema gains optional `"replace_source": {"type":
   "boolean"}` (schemas.py:120-134). `tool_transcribe` maps it into the job's options
   dict; `_execute_subs_job` reads it with the absent-defaults rule
   (`bool(options.get("replace_source", False))`) — pre-0.4.17 registry records and
   pre-0.4.17 callers are behavior-identical. `api_version` stays 1; no new endpoints,
   no new tools, one additive optional field (the v0.4.15 `denoise` precedent).

The GUI's enqueue path goes through the same registry/executor surface
(`client_local.enqueue_subs` gains the flag; `choose_subs_files` passes the checkbox
state) — the one-enqueue-surface rule is preserved.

### D5 (brief item 5): Failure discipline — identical to Mode A, plus a cancel observe

**Decision:**

- A replace **failure never fails the job**: the helper raises `SubsJobError`; the
  call site in `run_subs_job` catches `(SubsJobError, OSError)`, appends a
  `"WARNING ..."` entry to the result notes (the honest channel the CLI already prints
  and the bridge already ships — `_cmd_subs` cli.py:416-417; `_execute_subs_job`
  jobs.py:501), and returns the success intact. Both files are kept.
- **Cancellation never replaces**: a `should_cancel` observe is inserted *after* the
  remux, *before* the replace block, mirroring `transcribe.py:2429-2432` verbatim
  (`if should_cancel is not None and should_cancel(): raise EngineCancelled(...)`,
  lazy import). When `should_cancel` is `None` (CLI) the check is inert and the CLI
  path is byte-identical in behavior. `EngineCancelled` propagates through
  `_execute_subs_job`'s existing catch (jobs.py:480-481) → job closes `cancelled`,
  both files on disk.
- The source is deleted **only** after the remux has succeeded *and* the output is
  already living at its final verified name on disk (see the ordering ADR, §2).

### D6 (brief item 6): Extension policy — always `.mp4`, declared in user-facing text

**Decision:** confirmed — the rename target is **always** `{stem}.mp4` regardless of
the source container (`.avi`, `.mkv`, `.ts`, `.mp4`, …). mp4 is where mov_text lives
(the `subs_output_path` docstring already says this for the output; the replace target
inherits it). Never carry over the old extension. This rule is surfaced in:

- the GUI checkbox tooltip (explicit worked example: `…35 Hours.avi` → `…35 Hours.mp4`),
- the CLI `--help` text,
- the MCP `transcribe` tool description.

---

## 2. Explicit deviations from the brief (two, both intentional)

1. **Rename-before-delete ordering.** The brief's normative steps read "1. Delete the
   original source file. 2. Rename the output." This spec reverses the on-disk order:
   the output is atomically moved onto `{stem}.mp4` **first**; the source file is
   deleted **second** (skipped entirely when the source is `.mp4` — the rename
   consumes its name). **Why:** the brief's own failure invariant is "on
   failure/cancel keep BOTH files" and "never destroy the only good data". Under
   delete-then-rename, a failed `os.replace` after the delete leaves exactly one file
   (the output) and the invariant is already broken. Under rename-then-delete, *every*
   failure mode keeps both files: a failed rename leaves source + `.subs.mp4`
   untouched; a failed source-delete leaves `…35 Hours.mp4` **and** `…35 Hours.avi`
   (the user gets the rename they asked for plus an undeleted original to remove
   manually — a strictly safer failure residue). The end state on success is
   bit-identical to the brief's spec: source gone, output at `{stem}.mp4`. This is a
   semantics-preserving reordering chosen for failure-mode dominance; it is recorded
   here so nobody "fixes" the order later.
2. **Collision refusal is not just a delete-skip.** The brief's guard language covers
   refusing the *delete*; this spec refuses the **entire replace** (no move either) on
   a third-party target collision. Deleting the source while leaving the output at
   `.subs.mp4` would give Eric the worst of both worlds (original gone, name not
   delivered). Refusing everything keeps both files and surfaces a named warning.

No other deviations. Mode A's replace path, the `api_version`, the endpoint set, and
the Mode B default (option OFF) are unchanged as constrained.

---

## 3. The atomicity discipline (temp-sibling + SHA-256 + `os.replace`)

Mode B's geometry makes this simpler than Mode A's and the spec keeps both arms:

- Output and target **share a directory by construction** — `subs_output_path()`
  and `subs_replace_target()` both derive `os.path.dirname(source_path)`. The final
  `os.replace(output, target)` is therefore a rename(2) on one filesystem: atomic —
  either the old name state or the new, never a torn file. Windows nuance:
  `os.replace` fails if the destination is held open (e.g. an indexer/player peeking
  at a stranger `{stem}.mp4` — but the collision guard has already refused that case)
  or if the source-of-rename is open; failures are caught and become the keep-both
  warning.
- **Defensive cross-device arm** (unreachable by construction today, specced anyway so
  the helper is a self-contained library function that never silently relies on
  caller geometry — e.g. a future output-dir option on Mode B): stage the copy at
  `str(target) + ".replace-tmp"` (a temp **sibling of the target** → same directory →
  same filesystem → the final `os.replace` is atomic rename(2)), `shutil.copy2`, then
  **SHA-256-verify the staged copy against the verified output** via
  `_sha256_file` (lazy-imported from `.run`; reused, not duplicated); only on a hash
  match, `os.replace(tmp, target)` and `output.unlink()`. The temp never outlives the
  call (`finally` sweep, the run.py:1774-1783 pattern). A mid-copy failure strands
  only the temp; the verified output and the source both survive, and the source is
  never deleted until proven bytes sit at the target.
- **Sidecar** moves by plain `os.replace(srt, target_srt)` — sibling rename, atomic,
  collision-guarded (D1), failure-isolated from the mp4 outcome.
- **Stale-temp hygiene:** a pre-existing `{target}.replace-tmp` from a killed process
  is removed before staging (best-effort), mirroring run.py:1752-1757.

Ordering (full happy-path sequence, same-device arm):

1. Guard: verified output must `is_file()`; source must `is_file()`; collision guard
   (D2). All refusals happen here, before any mutation.
2. `_step("replace-source")` — the GUI's step heartbeat shows the phase (the executor's
   `tracked()` infers phases from commands and the replace runs no commands, so the
   explicit step is the only observability surface; zero contract change).
3. `os.replace(output, target)` — output now lives at `{stem}.mp4`. Rename(2) atomic.
4. If target != source path: `os.remove(source)`. `OSError` → logged warning, success
   stands, both files left (source undeleted).
5. Sidecar: `{stem}.subs.srt` → `{stem}.srt` (guarded, D1).
6. Notes appended; result paths updated honestly.

---

## 4. Architecture deltas (one screen)

```
GUI checkbox (2,8) ──toggled──▶ QSettings["subs_replace_source"]
        │ choose_subs_files (gui.py:1636)
        ▼
client_local.enqueue_subs(paths, replace_source=…) ──▶ registry options {"replace_source": bool, …}
CLI: vidlift subs --replace-source ──▶ _cmd_subs ──────────────┐
MCP: transcribe{replace_source} ──▶ tool_transcribe ──▶ options ┤
        ▼                                                        ▼
jobs._execute_subs_job  (absent-defaults: options.get(…, False))  run_subs_job(..., replace_source=False)
        │                                                        │
        └──────────────────▶ after mplex success: cancel observe → replace_subs_output()
                                          │ SubsJobError/OSError → notes "WARNING …", success kept
                                          ▼
                          source deleted; output at {stem}.mp4; sidecar {stem}.srt
```

`SubsJobResult.output_path` / `.srt_path` report the **final** post-replace paths, so
`_execute_subs_job`'s existing `JobOutcome`/result block (jobs.py:488-503) and the CLI
print block (cli.py:410-417) surface the renamed location with **no payload changes**.

---

## 5. Per-module change table

All line numbers are pre-change anchors verified in this tree. LoC estimates include
docstrings/comments in house style; they are ±20%.

| # | Module : anchor | Binding / exact signature | Change | LoC |
|---|---|---|---|---|
| 1 | `vidlift/subs_job.py` : module docstring (1-21) | — | Amend the "never touching the source" sentence: source-blind **by default**; the opt-in `replace_source` post-success path is named. | +3 |
| 2 | `vidlift/subs_job.py` : `__all__` (38) | `__all__` | Add `"subs_replace_target"`, `"replace_subs_output"`. | +1 |
| 3 | `vidlift/subs_job.py` : after `subs_output_path` (77) | `def subs_replace_target(source_path: str) -> str:` | **New pure function.** Returns `os.path.join(os.path.dirname(source_path), f"{stem}.mp4")` using the byte-identical stem-strip regex as `subs_output_path` (`re.sub(r"\.[^.]+$", "", base)`). Docstring carries the D6 always-`.mp4` rule and the never-carry-the-old-extension rule. One naming derivation for display, pre-flight, and the run guard — the v0.4.15.3 F-5 lesson. | +12 |
| 4 | `vidlift/subs_job.py` : after `mplex` (end of module, ~302) | `def replace_subs_output(source_path: str, output_path: str, *, srt_path: str \| None = None, log_line=None) -> tuple[str, str \| None]:` | **New function** — the D3 dedicated helper. Full algorithm in §6. Returns `(final_output_path, honest_final_srt_path_or_None)`. Raises `SubsJobError` on refusal/failure (caller converts to keep-both warning). | +95 |
| 5 | `vidlift/subs_job.py` : `run_subs_job` signature (97-117) | `def run_subs_job(source_path, *, ffmpeg, ffprobe="ffprobe", probe_runner=None, runner=None, engine_factory=None, model_pin=None, device_pin="auto", language_pin="", force=False, replace_source: bool = False, workdir=None, state_root=None, on_step=None, on_progress=None, on_download_progress=None, should_cancel=None, download_log=None, gpu_lock_factory=None) -> SubsJobResult:` | Insert `replace_source: bool = False` **immediately after `force`** (keyword-only block; ordering inside a kw-only block is source-compatible for every caller — all seven existing call sites pass keywords). Docstring gains the "Never overwrites the source (unless the opt-in replace_source post-success path ships — then the source is deleted and the output lands at `{stem}.mp4`)" paragraph. | +10 |
| 6 | `vidlift/subs_job.py` : `run_subs_job` body, after `mplex(...)` (200-207), before `return` (209) | — | **New block** (§6.2): cancel observe → `_step("replace-source")` → try `replace_subs_output(...)` with `log_line=notes.append`-style collector → honest path updates → except `(SubsJobError, OSError)` → `"WARNING replace-source failed; keeping both files: {exc}"` note, original paths kept. Result constructed with final paths + extended notes. | +30 |
| 7 | `vidlift/gui.py` : after `add_subs_button` block (743-753) | `self.subs_replace_source = QCheckBox("Replace original after adding subtitles (deletes the source)")` | **New checkbox** (D4-1). `setChecked(self._settings().value("subs_replace_source", False, type=bool))`; tooltip per §6.3 (D6 worked example + D2 guard honesty + D1 sidecar note); `toggled.connect(self._subs_replace_source_changed)`. Also: amend `add_subs_button` tooltip — "the source is never modified" → "…is kept (a per-run opt-in can replace it — see the checkbox beside this button)". | +22 |
| 8 | `vidlift/gui.py` : shell grid, after `grid.addWidget(self.add_subs_button, 2, 7)` (1328) | — | `grid.addWidget(self.subs_replace_source, 2, 8)` with a layout comment (sits in the Mode B action cell; (2,8) verified free). | +3 |
| 9 | `vidlift/gui.py` : after `_replace_source_changed` (1181-1185) | `def _subs_replace_source_changed(self, checked: bool) -> None:` | `self._settings().setValue("subs_replace_source", bool(checked))` — the QSettings house rule, verbatim pattern. | +5 |
| 10 | `vidlift/gui.py` : `_subs_file_rows` (1525-1541) | — | Column-3 cell becomes `"replace original → .mp4" if self.subs_replace_source.isChecked() else "keep original"`. (Rows render at enqueue time from the exact state being enqueued — the display cannot diverge from the submitted option.) | +4 |
| 11 | `vidlift/gui.py` : `choose_subs_files` enqueue call (1636) | — | `self.client.enqueue_subs(paths, replace_source=self.subs_replace_source.isChecked())` | +1 |
| 12 | `vidlift/mcp_bridge/client_local.py` : `enqueue_subs` (162-193) | `def enqueue_subs(self, paths: list[str], *, replace_source: bool = False) -> list[str]:` | New keyword-only param; options dict gains `"replace_source": bool(replace_source)`. Docstring updated (no longer "no overrides"). Absent-defaults preserved: GUI is the only caller and passes it explicitly; tests calling without get the False default. | +6 |
| 13 | `vidlift/mcp_bridge/jobs.py` : `_execute_subs_job` call site (459-479) | — | Add `replace_source=bool(options.get("replace_source", False)),` with the absent-defaults comment (mirrors jobs.py:247-255 comment style). No other changes — the honest-path flow-through is automatic. | +4 |
| 14 | `vidlift/mcp_bridge/schemas.py` : `transcribe` schema (120-134) | `TOOL_SCHEMAS["transcribe"]["properties"]` | Add `"replace_source": {"type": "boolean"}` with a versioned comment (opt-in; absent-defaults; rename-to-`.mp4` named). Also amend `TOOL_DESCRIPTIONS["transcribe"]` (180-186): "…never overwriting it" → "…never overwriting it (unless the optional `replace_source` flag is set — then on success the source is deleted and the output takes the source's name with an `.mp4` extension, with a keep-both guard on collision)". `api_version` untouched. | +6 |
| 15 | `vidlift/mcp_bridge/tools.py` : `tool_transcribe` (736-767) | — | `replace_source = bool(args.get("replace_source", False))` and one new options entry `"replace_source": replace_source`. Docstring arg list updated. | +4 |
| 16 | `vidlift/cli.py` : `subs` parser (682-687) | — | `subs_parser.add_argument("--replace-source", action="store_true", help="after a verified subtitles pass, delete the source video and rename the output to the source's name with an .mp4 extension (e.g. …35 Hours.avi → …35 Hours.mp4); if a file of that name already exists, both files are kept (default: off)")` | +7 |
| 17 | `vidlift/cli.py` : `_cmd_subs` (370-418) + dispatch (802-810) | `def _cmd_subs(files, *, lang, model, device, force, ffmpeg, ffprobe, replace_source: bool) -> int:` | New kw-only param; pass `replace_source=replace_source` into `run_subs_job`; dispatch passes `args.replace_source`. Print path unchanged — `result.output_path` and the notes loop already surface the rename/refusal honestly (cli.py:410-417). Exit codes unchanged (a refused replace is still a succeeded job, exit 0). | +4 |
| 18 | `vidlift/__init__.py` (9); `pyproject.toml` (3); `packaging/version_info.txt` (both StringStructs + both tuples) | `__version__ = "0.4.17.0"` / `version = "0.4.17.0"` / `(0, 4, 17, 0)` | Version bump — all four version_info values (the `tests/test_version_stamps.py` gate asserts parity). | ~8 |
| 19 | `CHANGELOG.md` : new `[0.4.17.0]` section above line 5 | — | "Added": Mode B opt-in replace-original (three surfaces named); the rename-to-`.mp4` rule; the collision keep-both guard; the sidecar rename-to-match. "Notes": `.srt`-toggle follow-up explicitly out of scope. | +14 |
| 20 | Docs sweep: `README.md`, `docs/` | — | grep for `.subs.mp4` / "source-blind" / "never touching the source"; wherever Mode B's keep-original default is described, add the one-sentence opt-in caveat. **No architectural passages change** (the default is untouched). | ~6 |

**Explicit non-changes (regression fences):** `vidlift/run.py` — zero lines changed
(`replace_source_with_output` at 1683-1796 and its call site at 2964-2974 stay
byte-identical; `_sha256_file` is *imported lazily*, not moved). `vidlift/mcp_bridge/registry.py`,
`vidlift/transcribe.py`, `vidlift/subtitles.py`, `vidlift/monitor.py` — no changes.

**Estimated totals:** source +CHANGELOG/docs ≈ **240 LoC**; tests ≈ **430 LoC** (§8).

---

## 6. Code-level detail

### 6.1 `replace_subs_output` — exact algorithm (spec item 4 above)

```python
def replace_subs_output(
    source_path: str,
    output_path: str,
    *,
    srt_path: str | None = None,
    log_line=None,
) -> tuple[str, str | None]:
    """Mode B replace (v0.4.17, opt-in): delete the source and land the
    verified subs output at the source's stem with an .mp4 extension.

    Discipline (mirrors run.replace_source_with_output's invariants, with
    rename-to-stem-.mp4 semantics):
    - rename-before-delete: the output moves onto {stem}.mp4 FIRST
      (atomic os.replace), the source is deleted SECOND; every failure
      mode keeps BOTH files (spec §2 deviation 1);
    - collision guard: a pre-existing {stem}.mp4 that is NOT the source
      (normcase/abspath identity) refuses the whole replace — an
      unrelated file is never overwritten;
    - source-is-.mp4 (target == source path): safe and intended — the
      single os.replace IS the delete-and-move;
    - cross-device (unreachable by construction — output and target share
      the source's directory — but defended): stage at a temp SIBLING of
      the target, shutil.copy2, SHA-256-verify against the verified
      output (from .run import _sha256_file — lazy, the _default_runner
      pattern), then os.replace; the temp never outlives the call;
    - sidecar: {stem}.subs.srt is renamed to {stem}.srt when (and only
      when) no unrelated {stem}.srt exists; on refusal or OSError the
      sidecar keeps its .subs.srt name — best-effort, never blocks the
      mp4 outcome;
    - raises SubsJobError on refusal/failure; the job-level caller keeps
      the verified success either way.

    Returns (final_output_path, final_srt_path_or_None): the second
    element is the sidecar's HONEST post-attempt location — the renamed
    path, the original path when the rename was refused/failed, or None
    when no sidecar exists. result.srt_path must never lie.
    """
```

Body outline (Forge follows this order exactly):

```python
    source = Path(source_path)
    output = Path(output_path)
    target = Path(subs_replace_target(source_path))
    target_srt = target.with_suffix(".srt")
    # 1. guards (no mutation before this line)
    if not output.is_file():  raise SubsJobError("replace-source refused: verified output is missing: …")
    if not source.is_file():  raise SubsJobError("replace-source refused: source is gone: …")
    same_path = normcase(abspath(source)) == normcase(abspath(target))
    if not same_path and target.exists():
        raise SubsJobError(
            f"replace-source refused: {target.name} already exists as a "
            "different file; keeping both files"
        )
    # 2. land the output at the target (atomic)
    if os.stat(source).st_dev == os.stat(output).st_dev:
        try:
            os.replace(str(output), str(target))
        except OSError as exc:
            raise SubsJobError(f"replace-source failed to move the output to {target}: {exc}") from exc
    else:
        tmp = str(target) + ".replace-tmp"
        # stale-temp sweep (best-effort), copy2, sha256 both sides
        # (lazy: from .run import _sha256_file), mismatch → SubsJobError
        # keeping everything; os.replace(tmp, target); finally-sweep tmp;
        # then output.unlink() best-effort (warning on failure, the
        # verified copy at the target is already the good data)
    # 3. delete the source copy (skipped when same_path — the rename
    #    consumed the name)
    if not same_path:
        try:
            os.remove(str(source))
        except OSError as exc:
            log_line(f"WARNING replace-source: output shipped to {target} "
                     f"but the source copy could not be removed: {exc}")
    # 4. sidecar (best-effort, guarded)
    final_srt: str | None = None
    if srt_path is not None and os.path.isfile(srt_path):
        if normcase(abspath(srt_path)) == normcase(abspath(str(target_srt))):
            final_srt = srt_path  # already matches (defensive)
        elif target_srt.exists():
            log_line(f"WARNING replace-source: {target_srt.name} already "
                     "exists; sidecar kept as " + os.path.basename(srt_path))
            final_srt = srt_path
        else:
            try:
                os.replace(srt_path, str(target_srt))
                final_srt = str(target_srt)
            except OSError as exc:
                log_line(f"WARNING replace-source: sidecar kept at {srt_path}: {exc}")
                final_srt = srt_path
    log_line(f"replace-source: output now at {target}")
    return str(target), final_srt
```

### 6.2 `run_subs_job` tail block (spec item 6)

```python
    final_output = output_path
    final_srt = transcribed.srt_path
    notes = list(transcribed.notes)
    if replace_source:
        # v0.4.17: cancellation never replaces — after the remux lands,
        # before any mutation (transcribe.py's should_cancel contract).
        if should_cancel is not None and should_cancel():
            from .run import EngineCancelled  # lazy: circular import
            raise EngineCancelled("cancelled after remux, before replace")
        _step("replace-source")
        try:
            final_output, maybe_srt = replace_subs_output(
                source_path, output_path,
                srt_path=transcribed.srt_path,
                log_line=notes.append,   # helper narration -> result notes
            )
            if maybe_srt is not None:
                final_srt = maybe_srt
        except (SubsJobError, OSError) as exc:
            # Mode A discipline: a replace failure is a warning that keeps
            # the verified success; both files stay on disk.
            notes.append(
                f"WARNING replace-source failed; keeping both files: {exc}"
            )

    return SubsJobResult(
        source_path=source_path,
        output_path=final_output,
        srt_path=final_srt,
        …  # all other fields unchanged
    )
```

Note: the `os.path.exists(output_path)` never-overwrite pre-flight at
subs_job.py:152-156 and the `{stem}.subs.mp4` naming are **unchanged** — replace only
ever runs *after* a job that passed those gates.

### 6.3 GUI tooltip (checkbox, item 7) — exact text

> "After a successful subtitles pass, delete the original file and rename the output
> to the original name with an .mp4 extension
> (e.g. …35 Hours.avi → …35 Hours.mp4).\n\n
> The .srt sidecar is renamed to match. If a file of that name already exists, or the
> replace cannot be done, both files are kept and the job still succeeds.
> Applies to Add Subtitles only — the upscale panel has its own replace option."

Label text (item 7): `"Replace original after adding subtitles (deletes the source)"`
— names the mode, names the irrecoverability, fits one grid cell beside the button.

### 6.4 Error-text vocabulary (stable strings Karen can assert)

- helper refusal (collision): `"replace-source refused: {name} already exists as a different file; keeping both files"`
- helper refusal (missing output): `"replace-source refused: verified output is missing: {path}"`
- helper refusal (missing source): `"replace-source refused: source is gone: {path}"`
- job-level warning note: `"WARNING replace-source failed; keeping both files: {exc}"` (mirrors run.py:2974's wording)
- success note: `"replace-source: output now at {target}"` (mirrors run.py:1795)
- cancel: `"cancelled after remux, before replace"`

---

## 7. Implementation milestones (Forge)

| Milestone | Content | Exit gate |
|---|---|---|
| **M1 — core** | items 1-6 (subs_job.py only) + new pure/helper/integration tests (§8, file A groups 1-4) | `pytest tests/test_v0417_subs_replace_source.py tests/test_v0414_subs_job.py -x` green; `tests/test_run.py` green unmodified |
| **M2 — plumbing** | items 12-17 (client_local, jobs, schemas, tools, cli) + file A groups 5-7 | M1 gate + bridge/cli tests green; `tests/test_mcp_bridge.py tests/test_cli_v049.py tests/test_client_local.py` green unmodified |
| **M3 — GUI** | items 7-11 + file B tests | `pytest tests/test_v0414_mode_b_gui.py tests/test_v0414_mode_b_gui_executor.py tests/test_gui.py` green |
| **M4 — release hygiene** | items 18-20 | full suite green incl. `tests/test_version_stamps.py` |

Coding conventions: house style (versioned comments `v0.4.17`, lazy imports for
run-bound symbols in subs_job, keyword-only params, absent-defaults rule on all
options surfaces, `os.path.normcase(os.path.abspath(...))` for path identity,
UTF-8/`from __future__ import annotations`). No new third-party dependencies.

---

## 8. Test plan — which files to add / extend

### File A (new): `tests/test_v0417_subs_replace_source.py`  (~430 LoC)

Conventions and harnesses are copied from `tests/test_v0414_subs_job.py` (fake engine
factory, fake `runner` recording argv + manufacturing the remux temp/output, tmp_path
media fixtures). No real ffmpeg, no real whisper, no network.

**Group 1 — pure naming (`subs_replace_target`):**
1. `.avi` / `.mkv` / `.ts` / `.MP4` sources → `{stem}.mp4` beside the source, old extension never carried.
2. Multi-dot stem (`show - S03E01 - 35 Hours.avi`) → `… - 35 Hours.mp4` (stem-strip regex parity with `subs_output_path`).

**Group 2 — helper, happy paths (`replace_subs_output`):**
3. avi source + output + sidecar → source gone; `{stem}.mp4` holds the output's exact bytes; `{stem}.srt` holds the sidecar's exact bytes; returns `(target, target_srt)`; log_line narrates.
4. No sidecar on disk → returns `(target, None)`; no failure.
5. Source-is-`.mp4` → single atomic move: source path now holds output bytes; no delete step observable (patch `os.remove` to assert it is never called for this case).
6. Cross-device arm (monkeypatch `os.stat` st_dev to differ): staged temp-sibling copy, SHA-256 verified, `os.replace` into place, output unlinked, temp gone; byte-identity asserted.

**Group 3 — helper, refusals and graceful degradation:**
7. Collision: pre-existing stranger `{stem}.mp4` (different bytes) → `SubsJobError`; source, output, and the stranger file all byte-identical after; refusal message matches §6.4.
8. Sidecar collision: pre-existing stranger `{stem}.srt` → mp4 replace still ships; stranger `.srt` byte-identical; sidecar still at `{stem}.subs.srt`; warning narrated.
9. Missing output → refusal; source kept. 10. Missing source → refusal; output kept.
11. `os.remove(source)` raises (monkeypatched `OSError`) → success stands, both target and source exist, warning narrated.
12. `os.replace(output, target)` raises → `SubsJobError`; source and output both intact (nothing deleted first).

**Group 4 — `run_subs_job` integration (fake engine + fake runner):**
13. `replace_source=True` end-to-end: `…35 Hours.avi` → returns `output_path=…35 Hours.mp4`, `srt_path=…35 Hours.srt` — both real on disk; source gone; success notes narrated.
14. Default (`replace_source` absent) → **byte-identical to v0.4.16.2 behavior** (output stays `.subs.mp4`, source untouched, no replace note) — this is the option-OFF invariant test.
15. Replace failure inside the job (monkeypatch `replace_subs_output` to raise `SubsJobError`) → `SubsJobResult` still returned with the original `.subs.mp4` path, both files on disk, `"WARNING replace-source failed; keeping both files"` in notes.
16. `should_cancel` returning True (invoked after the fake remux) → `EngineCancelled("cancelled after remux, before replace")`; source and `.subs.mp4` both on disk; replace never attempted (monkeypatch-spy `os.replace` call count on the target sees zero).
17. `should_cancel=None` (CLI posture) + `replace_source=True` → replace ships normally (the cancel check is inert by contract).

**Group 5 — CLI:**
18. `vidlift subs` argparse: default `args.replace_source is False`; `--replace-source` → True (the `tests/test_cli_v049.py:276-293` pattern).
19. `_cmd_subs` threads the flag into `run_subs_job` (patch the import; assert kwarg) and exits 0 when a mocked job succeeds with a refused-replace note in `notes` (refusal ≠ failure).

**Group 6 — MCP/registry plumbing:**
20. Schema: `validate_tool_args("transcribe", {"paths": ["x"], "replace_source": True})` accepts; `replace_source: "yes"` raises `InvalidInput(field="replace_source")`; absent validates (absent-defaults).
21. `tool_transcribe` enqueues options with `"replace_source": True` when set, `False` when absent (registry fake per test_v0414 harness).
22. `_execute_subs_job` forwards `bool(options.get("replace_source", False))` into `run_subs_job` (patch-spy, both the present-True and the legacy-record-absent cases).
23. `client_local.enqueue_subs(paths, replace_source=True)` writes the option; the no-kwarg call keeps writing `False` (back-compat for existing scripted callers).
24. Terminal echo: after a successful replace the job's result dict carries the renamed `output_path` (through the real `_execute_subs_job` block, no payload-shape change asserted — keys identical to v0.4.16.2).

### File B (extend): `tests/test_v0414_mode_b_gui.py`  (+~90 LoC, existing offscreen/QSettings-isolated harness)

25. Checkbox exists beside the Add Subtitles button (grid (2,8)), default **OFF**, tooltip carries the `.mp4` naming rule (D6) and the keep-both promise.
26. Toggling persists QSettings key `"subs_replace_source"`; a fresh window restores it; Mode A's `"replace_source"` key is untouched by it (key-isolation assertion).
27. Pick flow with the checkbox ON → `enqueue_subs` receives `replace_source=True`; OFF/absent → False (scripted-client harness).
28. `_subs_file_rows` column 3 reads `"replace original → .mp4"` when ON, `"keep original"` when OFF.
29. Mode A checkbox still scopes itself: the upscale options dict at gui.py:2159-2179 still reads only `self.replace_source` (regression fence against cross-wiring — the Eric-confusion class).

### Unmodified-pass invariants (must hold with zero edits)

`tests/test_run.py` (incl. the replace block at :1073-1143), `tests/test_v0414_subs_job.py`,
`tests/test_v0414_mode_b_gui_executor.py`, `tests/test_v04151_subs_guard.py`,
`tests/test_v04153_subs_bitmap_remux_slow.py`, `tests/test_cli_v049.py`,
`tests/test_mcp_bridge.py`, `tests/test_client_local.py`, `tests/test_version_stamps.py`
(after item 18's stamps). Any required edit to these is a spec violation — escalate.

---

## 9. Failure analysis

| Failure | Behavior |
|---|---|
| Stranger `{stem}.mp4` present | Whole replace refused pre-mutation; both files kept; warning note; job `succeeded` |
| `os.replace(output, target)` fails (Windows file-lock, permissions) | Both files kept (nothing deleted yet); warning note; job `succeeded` |
| `os.remove(source)` fails after a shipped rename | Target + source both exist; warning note; job `succeeded` (user deletes the original manually — documented worst residue) |
| Source deleted externally between remux and replace | Refusal; output kept at `.subs.mp4`; job `succeeded` |
| Sidecar rename fails / stranger `{stem}.srt` | mp4 replace unaffected; sidecar keeps `.subs.srt` name; warning note |
| Cancel arrives post-remux, pre-replace | `EngineCancelled`; job closes `cancelled`; both files kept; no mutation |
| Crash between shipped rename and source delete | On disk: target (good data) + source. Restart: `subs_output_path` guard is unaffected; nothing is corrupt |
| Crash mid-cross-device copy | Temp sibling stranded (cleaned on next attempt); source + output both intact |
| Registry record from ≤0.4.16.2 (no `replace_source` option) | absent-defaults → False → source-blind behavior, byte-identical |
| Disk full during cross-device copy | `shutil.copy2` raises → refusal; temp swept; both originals intact |

No single points of failure are introduced; the feature's worst-case residue is
"extra files kept," which is the safe direction by design.

---

## 10. Validation baseline for Karen (expand freely)

**Acceptance criteria:**

1. With the option OFF (all three surfaces), Mode B is byte-identical to v0.4.16.2:
   `{stem}.subs.mp4` + `{stem}.subs.srt`; source untouched. (The §8-14 invariant test.)
2. With the option ON and a verified success on `35 Hours.avi` (Eric's repro): source
   deleted; `35 Hours.mp4` (not `.subs.mp4`, not `.avi`) beside where the source was;
   `35 Hours.srt` beside it; the pair's bytes equal the pre-replace output/sidecar
   bytes; job state `succeeded`; history `output_path` shows the renamed path.
3. Source-is-`.mp4`: `35 Hours.mp4` (subtitled) replaces `35 Hours.mp4` in one atomic
   move; no `.subs.mp4` residue.
4. Collision: pre-existing unrelated `35 Hours.mp4` → all three files survive
   byte-identical; job `succeeded`; the §6.4 refusal string appears (CLI output /
   result notes / GUI-reachable payload).
5. Replace failure never fails a job and cancellation never replaces (§8-15/-16).
6. Mode A upscale replace: byte-identical behavior — run the `tests/test_run.py`
   replace block; also one manual upscale with replace ON (output takes the source's
   exact path, extension included). `git diff vidlift/run.py` must be empty.
7. Persistence: GUI checkbox survives restart (QSettings); absent MCP field and
   absent CLI flag both mean OFF; `api_version` still 1; `tools/list` shows the one
   additive `transcribe.replace_source` boolean.
8. UI: checkbox at the Add Subtitles row cell, honest label/tooltip (§6.3), row
   column 3 text tracks the option at submission time.
9. Stamps: `tests/test_version_stamps.py` green on 0.4.17.0; CHANGELOG entry present;
   docs sweep landed.

**Edge cases to probe beyond §8:** UNC/network-drive source (st_dev arm, Windows);
source open in a player during replace (Windows sharing violation path → refusal);
read-only source directory (rename fails → keep-both); dotfile-ish stems and
multi-dot names (group 1); a second Mode B job queued for the same source immediately
after a replace (the `subs_output_path` pre-flight now evaluates against the renamed
landscape — confirm the guard message is coherent); QSettings key isolation
(§8-26/-29).

**Security expectations:** no path leaves the source's directory (target and
temp-sibling both derive from `os.path.dirname(source_path)`); no overwrite of any
file that is neither the source nor a path this job created; no privilege changes;
options validated at the schema boundary (unknown fields still rejected).

**Performance expectations:** replace adds one directory-local rename (+ one file
delete + one optional sidecar rename) — sub-second on the same volume; the
cross-device arm adds one full copy + two SHA-256 reads and is unreachable by
construction today; no steady-state cost when the option is OFF.

---

## 11. Final review checklist

✓ Requirements satisfied (brief §Desired behaviour; six decisions answered: §1 D1-D6)
✓ Evidence verified against the tree (§0)
✓ Alternatives evaluated (D3 reuse-vs-generalise; ordering analysis §2)
✓ Tradeoffs documented (deliberate duplication of the replace discipline; tuple return)
✓ Security reviewed (no new surfaces; directory-confined paths; §10)
✓ Performance reviewed (§10)
✓ Observability designed (`_step("replace-source")` + honest notes + truthful result paths)
✓ Testability reviewed (fake-runner/fake-engine harness reuse; named §6.4 strings)
✓ Extensibility reviewed (helper is self-contained; a future Mode B output-dir keeps the discipline valid)
✓ Technical debt minimized (no refactor of Mode A; one borrowed private import, lazy)
✓ Risks documented (§9)
✓ Deviations stated explicitly (§2)
✓ Forge guidance complete (§5-§7) ✓ Karen baseline complete (§10)

— Bob
