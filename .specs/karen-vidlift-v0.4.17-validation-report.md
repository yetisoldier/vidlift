# Karen Validation Report — v0.4.17 Mode B replace-source (checkpoint 0269cb0)

**Validator:** Karen (Testing & Validation) — **Date:** 2026-11-04
**Work under test:** checkpoint commit `0269cb09561dfa13cd7de26d3ee3d11a8a27889a` ("wip(v0.4.17): Mode B replace-source + tests + specs (checkpoint for Karen validation; NOT a release)")
**Baseline:** `ac90825` (release v0.4.16.2)
**Specs:** Bob `bob-vidlift-v0.4.17-implementation-spec.md` (authoritative, §10 baseline) · Forge `forge-vidlift-v0.4.17-fix-report.md` · Hermes brief + release plan
**Gate interpreter:** `/home/yetisoldier/.venvs/vidlift/bin/python` (canonical, as mandated)

---

## Executive Summary

**Overall Status: PASS (code cleared at the checkpoint)**

**Code is cleared: YES.** Every §10 acceptance criterion holds, proven live — not
asserted from source. The gate fires at three independent tiers (fake-harness,
real-ffmpeg, real-CLI-with-real-whisper); option-OFF is byte-identical to v0.4.16.2
(result fields, full command argv, and complete on-disk state, sha-verified); option-ON
deletes the source and lands the verified output at `{stem}.mp4` with byte-identical
content (a pure rename, sha-proven). All keep-both guards (collision / cancel / failure /
read-only directory / sidecar collision) hold under live adversarial probing. Mode A is
byte-identical (`git diff ac90825..0269cb0 -- vidlift/run.py` is empty) and its replace
still fires live. Full suite: **990 passed, 2 skipped** (both skips pre-existing
Windows-only GPU-meter proofs, verified unrelated).

**Release is cleared: NO — by design, not by defect.** Per Eric's 2026-11-04 directive
(`hermes-release-plan-v0.4.17-combined.md`), no release is cut from this checkpoint; the
combined release waits on workstream 2 (UI restructure), the Quill docs pass, the version
decision, and the release mechanics. Remaining release-gate items are listed in the
final section. Nothing in this validation blocks proceeding to workstream 2.

**Overall Risk: Low** (no Critical or High defects; findings are Medium/Low/Informational)
**Confidence: High** (every conclusion below carries reproducible evidence)

---

## Findings table

| ID | Severity | Finding | Impact | Recommendation |
|---|---|---|---|---|
| K-1 | **Medium (UI)** | Main-window minimum width inflated **1290px → 1532px (+242px)** by the new checkbox label at grid (2,8) (352px wide at default font). Hiding the checkbox restores exactly 1290px — it is the sole cause. | On displays ≤1532 logical px (e.g. 1366×768 laptops) the window cannot fit; content extends past the right screen edge; user cannot resize below 1532. | Spec-conformant (§6.3 mandates this exact label at this exact cell — the implementation matches; the spec under-anticipated the min-width cost). Feed into Bob's workstream-2 UI restructure: shorter label + tooltip, or move the control into the planned Mode B panel. |
| K-2 | **Low (UI wording)** | With the checkbox ON, the subs detail panel renders "**Resolution handling:** replace original → .mp4" — an incoherent heading for a replace-source note (`_subs_detail_text` gui.py:2063 appends any non-"keep original" column-3 text under that heading). | Cosmetic confusion in a secondary surface; no functional impact. | Spec item 10 scoped the change to the cell only; Forge disclosed the consequence. Fix the heading (or special-case the replace text) in workstream 2. |
| K-3 | **Informational** | Dangling-symlink edge: the collision guard uses `Path.exists()` (follows symlinks), so a **dangling** symlink at `{stem}.mp4` passes the guard and is silently consumed by the `os.replace`. A symlink to a real file is correctly refused (exists→True→refusal). | Minimal practical risk (Windows-primary user base; a dangling symlink is not data). Mode A is not comparable (its target IS the source, which must be a real file). | If hardening is ever wanted: `os.path.lexists(target)` in the guard. Not required for this release. |
| K-4 | **Informational (pre-existing)** | `vidlift subs <missing-file>` exits with a raw `ProbeError` traceback (no clean CLI error). **Byte-identical at v0.4.16.2** — pre-existing — but the replace workflow makes it newly *reachable*: re-running the same `subs … --replace-source` command after a successful replace (source now deleted) hits it. | Ugly CLI UX in a workflow the feature makes more common. GUI path is safe (enqueue validates existence → dialog). | Out of scope for this change (behavior-identical to baseline). Suggest a `ProbeError` catch in `_cmd_subs` in a future polish pass. |
| K-5 | **Informational (pre-existing)** | The `.subs.mp4.subs-work` scratch directory is left beside the output after every subs job — both versions, both modes, unchanged by this work. | Harmless residue; unchanged behavior. | Pre-existing; out of scope. |
| K-6 | **Informational (docs)** | `vidlift subs` (and hence `--replace-source`) is undocumented in README/docs — a **pre-existing** gap. Forge's item-20 "docs sweep = verified no-op" claim was independently re-verified and is **correct**: no user-facing passage describes Mode B's keep-original default, so there was nothing to amend. | No stale claims shipped; but the new flag has no user-facing documentation. | Quill's combined-release docs pass must document `vidlift subs` incl. `--replace-source` (already mandated by the release plan). |
| K-7 | **Informational (report nit)** | Forge's report under-counts its own tests: "18 tests" / "M1 gate 35 passed" — actual: **25 test functions** in file A, M1 gate **42 passed**. | None — the error is in the safe direction (more coverage than claimed); all pass. | None. Recorded for accuracy. |

No Critical findings. No High findings. K-1 is the only Medium and is a spec-level
layout consequence, not an implementation defect.

---

## Key gate evidence (the four mandated gates)

### Gate (a) — `vidlift/run.py` byte-identical to ac90825 (Mode A untouched)

**PASS.**

```
$ git diff ac90825..0269cb0 -- vidlift/run.py          → 0 lines (empty)
$ git diff ac90825..0269cb0 -- vidlift/mcp_bridge/registry.py \
      vidlift/transcribe.py vidlift/subtitles.py vidlift/monitor.py → 0 lines (empty)
$ git diff ac90825..0269cb0 -- tests/test_run.py tests/test_v0414_subs_job.py \
      tests/test_v0414_mode_b_gui_executor.py tests/test_v04151_subs_guard.py \
      tests/test_v04153_subs_bitmap_remux_slow.py tests/test_cli_v049.py \
      tests/test_mcp_bridge.py tests/test_client_local.py \
      tests/test_version_stamps.py                     → 0 lines (empty)
```

All five spec-named no-touch modules and all nine unmodified-pass invariant test
files have **zero diff**. `tests/test_run.py` (incl. the Mode A replace block
:1073-1143) passes green and unmodified. `API_VERSION = 1` lives in the untouched
`monitor.py:52`. Live Mode A probe: `replace_source_with_output()` still fires —
staged `35 Hours.avi` + `35 Hours.avi.720p.mp4` → the output moved onto the
source's **exact** path (extension included), `.720p.mp4` name consumed, success
note logged. A full manual Mode A upscale was not feasible in this environment
(no realesrgan-ncnn binary); run.py byte-identity + the green replace block + the
live helper probe are the evidence, and byte-identity is the strongest possible
form of it.

### Gate (b) — the gate FIRES (original defect reproduced, both directions)

**PASS — proven live at three independent tiers, not asserted from code presence.**

The original defect (Hermes brief): at v0.4.16.2, Mode B is source-blind — the
Mode A preference is simply not consumed; output stays `{stem}.subs.mp4`, source
untouched. Reproduced live at the baseline worktree (git worktree at `ac90825`):
`run_subs_job` on `The Adventures of Pete & Pete (1993) - S03E01 - 35 Hours.avi`
→ output `…35 Hours.subs.mp4`, source intact, no replace note. That is the defect.

**Tier 1 — fake-harness byte-identity (deterministic).** The real `run_subs_job`
(seams: FakeEngine/FakeRunner from the v0.4.14 suite, NoLock, seeded model state)
was run four times against Eric's exact filename; each run dumped the full result
fields, every command argv, and the complete on-disk state (name/size/sha256 per
file):

| Run | output_path basename | source | verdict |
|---|---|---|---|
| ac90825, kwarg **absent** (pre-0.4.17 caller) | `…35 Hours.subs.mp4` | kept | baseline |
| HEAD, kwarg **absent** | `…35 Hours.subs.mp4` | kept | **byte-identical** to baseline |
| HEAD, `replace_source=False` explicit | `…35 Hours.subs.mp4` | kept | **byte-identical** to baseline |
| HEAD, `replace_source=True` | `…35 Hours.mp4` | **deleted** | gate fires |

"Byte-identical" = the canonicalized dumps (result fields + all 3 command argvs +
disk state) hash equal (`8d734138f9cb23e5759a…` for all three OFF runs). The ON
run's `…35 Hours.mp4` holds sha `24715de73047…` — the **same sha as the baseline's
verified `.subs.mp4`** — and `…35 Hours.srt` holds the baseline sidecar's sha
`3bb82f820f7b…`: the replace is a pure rename of verified bytes, never a re-encode,
never the old extension.

**Tier 2 — real ffmpeg/ffprobe.** Same scenario with real binaries (only the
whisper engine faked): OFF at HEAD is byte-identical to OFF at baseline (remux
output sha `4338e9b408a8…` equal across versions — the real remux is deterministic);
ON deletes the source and lands a **real mp4** (ffprobe: mpeg4 video + mp3 audio +
mov_text subtitle tagged `eng`) at `{stem}.mp4`.

**Tier 3 — the real CLI with real whisper.** `vidlift subs "The Adventures of Pete
& Pete (1993) - S03E01 - 35 Hours.avi" --replace-source` with the production model
cache (`~/.vidlift/whisper/models/medium`, `.complete` present, zero network):
real faster-whisper transcription of real speech (language `en`, confidence 0.966,
1 cue), exit 0, honest note printed, and on disk: **`.avi` gone, `…35 Hours.mp4`
(106,875 bytes) + `…35 Hours.srt` present**. The OFF posture via the real CLI at
HEAD and at baseline produces identical outputs (same names, sizes, sha, exit 0,
source untouched, same pre-existing workdir residue).

**Extension policy (D6) probed live:** `.avi`/`.mkv`/`.ts`/`.MP4`/multi-dot
(`a.b.c.avi`→`a.b.c.mp4`)/no-extension (`movie`→`movie.mp4`)/dotfile
(`.avi`→`.mp4`) — the old extension is never carried; a no-extension source ran
the full real job end-to-end (`movie` → `movie.mp4` + `movie.srt`, source gone).

### Gate (c) — whole-tree review (not just the diff)

**PASS.** Beyond the diff: the final `subs_job.py` was read in full (520 lines) —
the §6.1 algorithm order is followed exactly (guards before any mutation →
same-device atomic rename / defensive cross-device arm with stale-temp sweep,
copy2, lazy `_sha256_file` verify, finally-sweep → source delete skipped on
same-path → guarded sidecar rename → honest return). The §6.2 tail block matches
the spec verbatim (cancel observe → `_step("replace-source")` → try/except
`(SubsJobError, OSError)` → keep-both warning). Lazy imports resolve
(`run.EngineCancelled` at run.py:173, `run._sha256_file` at run.py:1154 — both
verified present). The executor's cancel mapping verified in source
(jobs.py:485-486: `EngineCancelled` → `JobOutcome(cancelled=True)` — exact-type
catch before the RunError mapping, per the v0.4.16 comment). GUI grid verified:
(2,8) was genuinely the last free cell (row 2: monitor span 0-4 / resume 1-2
overlap is pre-existing v0.4.9 design, profile 5-6, button 7, checkbox 8); no
new overlap introduced (programmatic intersection check). All 20 change-table
items verified present. Forge's five declared deviations were each cross-checked
and are justified (the `_log` None-guard is a strict improvement over the §6.1
outline, which would have crashed the bare library call; the test-double kwarg
tolerance is additive and required by item 11's literal call shape; the file-B
split makes the key-isolation assertions falsifiable; the copy2 stand-in is
test-seam-only; the pure-function derivation matches item 3's text). The two
production callers of `run_subs_job` (jobs.py:459, cli.py:397) both pass keywords
— the kw-only insertion is source-compatible. The checkpoint contains only the
20 expected files; the working tree is clean at `0269cb0`.

### Gate (d) — collision / cancel / failure keep-both guards hold

**PASS — every guard probed live (real files, real syscalls).**

| Guard (spec §9 row) | Live probe | Result |
|---|---|---|
| Stranger `{stem}.mp4` present | pre-existing stranger beside staged source+output | **Refused pre-mutation** with the exact §6.4 string; source, verified output, stranger, sidecar all byte-identical after. Inside a real job: job **succeeds**, output stays `.subs.mp4`, `WARNING replace-source failed; keeping both files: …` in notes |
| Cancel post-remux, pre-replace | flag armed by the real ffmpeg remux command, real `should_cancel` | `EngineCancelled("cancelled after remux, before replace")`; source + `.subs.mp4` + `.subs.srt` all kept; `{stem}.mp4` never created |
| Rename fails (read-only directory) | scenario dir chmod 555 | `SubsJobError` refusal; both files kept (Bob's §10 read-only edge case holds) |
| Source-delete fails after shipped rename | unit test 11 (monkeypatched OSError) + code review | success stands, both files kept, warning narrated |
| Sidecar collision (stranger `{stem}.srt`) | stranger sidecar beside staged pair | mp4 replace **still ships** (source gone, target holds output bytes); stranger `.srt` untouched; sidecar keeps `.subs.srt`; honest `final_srt` + warning |
| Source-is-`.mp4` (target == source) | staged `.mp4` source | one atomic move; `.subs.mp4` name consumed; **no other files remain**; unit test 5 additionally proves `os.remove` is never called |
| Cross-device arm | unit tests 6/6b (faked `st_dev` — unreachable by construction in production since output and target share the source's directory) | staged temp-sibling, SHA-256 verify, atomic rename, temp swept, output unlinked; mid-copy failure strands only the temp, keeps both originals |
| Missing output / missing source | live probes 9/10 | refusal; the other file kept |
| Second job for the same source after a replace | live re-run | source is gone → coherent `no such file` refusal via the pre-existing channel (see K-4) |

---

## Per-criterion verdicts (Bob §10 acceptance criteria)

### Criterion 1 — option OFF (all three surfaces) is byte-identical to v0.4.16.2 — **PASS**

- Library: absent-kwarg and explicit-False runs are byte-identical to the
  baseline across result fields, all command argvs, and complete disk state
  (Gate (b), Tier 1). Real-ffmpeg tier: byte-identical remux (Tier 2).
- CLI: `vidlift subs` without the flag at HEAD ≡ baseline (same output name,
  size, sha, exit code, source untouched).
- MCP: `_execute_subs_job` absent-defaults verified live (legacy record without
  the key forwards False; present-True forwards True) and by unit tests 22;
  `tool_transcribe` absent→False (test 21); `client_local.enqueue_subs` no-kwarg
  call writes False (test 23, live registry round-trip).
- GUI: checkbox default OFF on fresh settings (live + test 25); the OFF posture
  enqueues False (test 27) and renders "keep original" (live + test 28).

### Criterion 2 — option ON, Eric's repro end-to-end — **PASS**

Live at all three tiers (Gate (b)): source deleted; `35 Hours.mp4` (not
`.subs.mp4`, not `.avi`); `35 Hours.srt` beside it; the pair's bytes equal the
pre-replace output/sidecar bytes (sha-proven); job state `succeeded`; history
`output_path` shows the renamed path — verified through the **real registry
round-trip** (LocalBridgeClient → executor → real ffmpeg job → terminal record:
`state=succeeded`, `output_path=…/35 Hours.mp4`, result payload keys identical to
the v0.4.16.2 shape with the renamed path and the replace note).

### Criterion 3 — source-is-`.mp4`: one atomic move, no residue — **PASS**

Live probe: single file remains (`35 Hours.mp4` holding the output bytes), the
`.subs.mp4` name is consumed, no delete residue; unit test 5 proves `os.remove`
is never invoked for this case (spy). The same-path identity uses the
normcase+abspath discipline (case-insensitive volumes handled).

### Criterion 4 — collision keeps all three files, job succeeds, §6.4 string — **PASS**

Live at helper level and inside a real job (Gate (d), row 1): all three files
byte-identical after; job `succeeded`; the exact §6.4 refusal string
(`replace-source refused: 35 Hours.mp4 already exists as a different file; keeping
both files`) rides the notes channel that the CLI prints and the bridge ships.

### Criterion 5 — replace failure never fails a job; cancellation never replaces — **PASS**

Live cancel probe (real remux-armed flag): `EngineCancelled`, both files kept,
target never created; the executor maps it to `cancelled` (jobs.py:485-486,
source-verified). Failure path: live collision-in-job (a real SubsJobError inside
the tail block) → job still `succeeded` with the keep-both warning; unit test 15
(monkeypatched raise) confirms the original `.subs.mp4` path rides and both files
stay on disk.

### Criterion 6 — Mode A byte-identical — **PASS**

`git diff` empty (Gate (a)); `tests/test_run.py` green unmodified; live Mode A
helper probe fires (output takes the source's exact path, extension included).
Full manual upscale not feasible here (no engine binary) — recorded as an
environment limitation, not a defect; byte-identity is the strongest evidence
available and it is conclusive.

### Criterion 7 — persistence, absent-means-OFF, api_version 1, additive schema — **PASS**

- QSettings persistence proven live across processes (see Environment note: a
  fresh `MainWindow` in a second process restored the checkbox state the first
  process wrote — the accidental live proof of §10-7), plus the isolated
  restore-both-directions and key-isolation tests (file B items 26).
- Absent MCP field and absent CLI flag both mean OFF (criterion 1 evidence).
- `API_VERSION = 1` in the zero-diff `monitor.py`.
- `tools/list` surface: `TOOL_SCHEMAS["transcribe"]["properties"]` carries
  `"replace_source": {"type": "boolean"}` (live-verified); `required` stays
  `["paths"]`; the tool description carries the spec'd replace clause.
- Schema boundary security: `replace_source: "yes"` → `InvalidInput(field="replace_source")`;
  unknown fields still rejected (`InvalidInput(field=bogus_option)`) — live.

### Criterion 8 — UI: checkbox at the row cell, honest label/tooltip, column 3 tracks — **PASS (with findings K-1, K-2)**

- Placement verified programmatically: button at (2,7), checkbox at (2,8), no
  other widget intersecting it; fully contained in the controls widget; grab
  rendered non-blank (screenshots captured; this validator cannot render
  images in-session — geometry/metrics are the visual evidence, disclosed).
- Label is §6.3-verbatim; tooltip is §6.3-verbatim (worked example, sidecar
  note, keep-both promise, scope disambiguation); the Add-Subtitles button
  tooltip amendment present.
- Column 3 tracks the submitted state live: ON → `"replace original → .mp4"`,
  OFF → `"keep original"`; enqueue threads the same read (test 27/29, the
  Eric-confusion regression fence: `start_upscale` still reads only Mode A's
  checkbox — inspect-pinned).
- Findings: K-1 (min-width +242px) and K-2 (detail heading) — recorded above;
  neither is an implementation deviation from the spec.

### Criterion 9 — stamps, CHANGELOG, docs sweep — **PASS**

`tests/test_version_stamps.py` → **7 passed** (run explicitly); all four
`packaging/version_info.txt` values + `__init__.py` + `pyproject.toml` at
`0.4.17.0` (diff-verified); CHANGELOG `[0.4.17.0] - 2026-11-04` section present
with all three surfaces, the naming rule, the keep-both guard, the sidecar
rename, and the out-of-scope note. Docs sweep: Forge's "verified no-op" claim
independently re-verified — no user-facing passage describes Mode B's
keep-original default (see K-6 for the pre-existing `vidlift subs` docs gap).

---

## Test plan and results

**Executed (canonical interpreter, `/home/yetisoldier/.venvs/vidlift/bin/python`):**

| Run | Result |
|---|---|
| Full suite `python -m pytest tests/ -q` | **990 passed, 2 skipped** (103.9s) — matches Forge's claim exactly; both skips verified pre-existing Windows-only GPU-meter proofs |
| `tests/test_version_stamps.py` (explicit) | 7 passed |
| M1 gate `test_v0417_subs_replace_source.py test_v0414_subs_job.py` | 42 passed (Forge reported 35 — see K-7) |
| M3 gate `test_v0414_mode_b_gui.py test_v0414_mode_b_gui_executor.py test_gui.py` | 74 passed (matches Forge) |

**Test-quality review (beyond pass/fail):** the new file A (25 test functions)
covers all 24 spec §8 items plus a bonus mid-copy-failure test (6b). Assertions
are meaningful, not vacuous: byte-level `read_bytes()` comparisons; a spy proving
`os.remove` is **never called** in the same-path case; a rename spy that
discriminates the replace target from the `.subs.mp4` remux finalize and the
`.muxing.mp4` temp (cancel test asserts **zero** replace renames); §6.4 stable
strings asserted exactly; the OFF-invariant test asserts no replace note and
untouched source. File B covers all five contracts incl. the key-isolation fence
in both directions against clean per-direction settings files. The four
test-double modifications (declared deviation 1) are additive kwarg tolerance
with zero assertion-semantics changes — verified by reading each diff.

**Live adversarial probes added by Karen (beyond the suite):** the three-tier
gate repro; cross-version byte-identity dumps; real-whisper CLI run; real
registry/executor round-trip; collision/cancel/read-only/sidecar/source-is-mp4/
no-extension/dotfile/multi-dot/uppercase-extension probes; second-job-after-replace;
schema boundary (non-bool, unknown field); GUI layout metrics at HEAD vs baseline
with hidden-checkbox differential; QSettings cross-process persistence.

**Skipped/limited:** full manual Mode A upscale (no engine binary in this
environment — evidence substituted per criterion 6); cross-device arm live
(unreachable by construction in production — validated via the suite's faked
`st_dev` tests, adequate for a defensive-only arm); pixel-level screenshot
review (no image rendering available to this validator — geometry metrics +
non-blank grab substituted, disclosed).

---

## Code cleared vs release cleared

**Code is cleared: YES** — checkpoint `0269cb0` is validated against Bob's §10
baseline with no Critical or High defects. The tree may proceed to workstream 2
as-is.

**Release is cleared: NO** — per Eric's 2026-11-04 directive this checkpoint is
explicitly **not** a release, and the following release-gate items remain
(tracking `hermes-release-plan-v0.4.17-combined.md`; none are code defects from
this workstream):

1. **Workstream 2 — UI restructure** (Bob design → Forge → Karen): separate the
   upscale and Mode B surfaces, render the invisible transcription checkbox
   (Hermes Finding 1), stop showing Mode A's replace where it is ignored.
   **Feed K-1 and K-2 into Bob's design** — the new checkbox's 242px min-width
   cost and the detail-heading wording belong in that restructure.
2. **Quill docs pass** (mandatory) covering BOTH workstreams — including
   documenting `vidlift subs` and `--replace-source` (K-6: currently
   undocumented, pre-existing gap).
3. **Version decision**: 0.4.17.0 (already staged in the tree) vs 0.5.0 for the
   combined release — Bob's call during the UI design; if changed, restamp all
   four `packaging/version_info.txt` values plus `__init__.py`/`pyproject.toml`.
   Do **not** double-bump.
4. **Release mechanics** (after workstream 2's Karen pass + Quill): commit,
   push to GitHub AND Gitea, cut the single combined release, hash-verify the
   published artifact.
5. Both workstreams keep `vidlift/run.py` byte-identical unless the UI
   restructure deliberately touches Mode A surface code — flag any such change
   to Eric (release-plan invariant).

## Environment note (validator disclosure)

One validation-environment incident, disclosed for the record: Karen's first
GUI probe failed to isolate QSettings and wrote `subs_replace_source=true` into
the real user config (`~/.config/YetiWurks/VidLift.conf`). It was removed
immediately (config restored to its pre-probe state; the pre-existing
`denoise/mode=bogus-legacy` entry was already present and untouched). Two
silver linings, both entered as evidence: (a) a fresh `MainWindow` in a second
process restored the state the first process wrote — a live cross-process proof
of the §10-7 persistence contract; (b) the incident confirms the toggle handler
writes only its own key. All subsequent GUI probes used per-run isolated
settings files (the test-harness house pattern).

## Release recommendation

**Ready with Minor Fixes — for the checkpoint; NOT for release (which is
correct: no release is due from this checkpoint).**

Reasoning, from evidence: the feature is implemented faithfully to Bob's spec
(all 20 items, §6.1/§6.2 verbatim, all six §6.4 strings exact); the safety gates
are proven live at three tiers including a real-whisper CLI run; the option-OFF
posture is byte-identical to the shipped v0.4.16.2; Mode A is byte-identical and
still fires; the full suite is green with zero modifications to any
unmodified-pass invariant file. The findings (K-1, K-2) are spec-level UI
consequences best absorbed by the already-queued workstream-2 restructure, and
the remaining release-gate items are process steps Eric has already sequenced.
No recycle loop is required: nothing in this workstream needs Forge rework or a
Bob redesign *of this feature*.

— Karen
