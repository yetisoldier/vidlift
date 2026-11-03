# Docs Task — VidLift v0.4.16.1 (Quill)

You are **Quill**, Technical Documentation Writer. Update the docs for the v0.4.16.1 patch
release, which has been validated (Karen PASS, risk Low) and is ready to ship.

## Repo

`/home/yetisoldier/projects/vidlift`

## Context

v0.4.16.1 is a **patch** on top of v0.4.16.0. It carries NO new features, NO new endpoints,
NO new MCP tools, and NO new dependencies. `api_version` stays 1. It is a truthful-reporting
fix only.

## What shipped in v0.4.16.1 (write about these, accurately)

1. **Fail-path propagation in the GUI.** Previously, when a run failed during an early phase
   (e.g. the `probe` / source-metadata step on an `.avi`, or `transcribe` on an `.mkv`), only
   the **batch summary** and the **job table** correctly showed `failed`. The details panel
   body, the LIVE overlay, and BOTH status bars stayed in the running state indefinitely,
   showing a stale `running — probe` verdict with the bars pulsing forever over an already
   failed batch.

   Now a failure lands on every surface: the details panel body shows `Verdict: failed`, the
   LIVE overlay shows `State: failed`, both status bars leave the busy pulse and clear their
   stale phase labels. This was observed live on a Mode-B "subtitles only" run.

2. **Failure reasons are surfaced.** The actual exception text (for example
   `{phase} command failed (exit N)` or the `ffprobe failed on …` message) is now carried
   out to the user instead of being swallowed. The reason reaches the details panel, the LIVE
   overlay, and the history rows.

   The mechanism is an **additive `error` key** on history rows. It round-trips through
   `client_local.py` history rows, the MCP `tool_list` history block, and the HTTP
   `/history` pass-through. The HTTP route itself is an untouched pass-through. Key sets on a
   history row are exactly the previous keys plus `error`. Records persisted before this
   release load with `error: null`.

3. **Version-stamp fix.** `packaging/version_info.txt` carried stale numeric tuples
   (`filevers` / `prodvers` still reading `0, 4, 16, 0`) while the string stamps and
   `pyproject.toml` / `__init__.py` had moved on. All four `version_info.txt` stamps now
   agree with the package version. This is what Explorer, the CLI smoke gate, and the PE
   stamp assertion actually read off the built binary.

## Deliverables

1. **`CHANGELOG.md`** — add a `## [0.4.16.1] - 2026-11-03` section ABOVE the existing
   `## [0.4.16.0]` section, matching the existing style (headings, bullet structure,
   the `All notable changes … Dates are America/Chicago.` preamble already present at the
   top). Cover the two user-visible fixes and the version-stamp fix. Be precise about what
   is additive: emphasize that `api_version` is unchanged and that the `error` key is
   additive, since integrators read this file to decide whether they must change anything.

2. **`README.md`** — review it for any claim that is now stale or wrong as a result of this
   patch. If the README describes failure handling, run status surfaces, history output
   shape, or troubleshooting "a run appears stuck at probe/transcribe", update it to reflect
   that failures now settle correctly on every surface and that the reason is reported. If
   nothing in the README is stale, say so explicitly in your final report rather than
   inventing edits. **Do not add a v0.4.16.1 feature section** — this is a patch, and the
   README should read as current rather than as a diff.

3. **User-facing troubleshooting note (only if warranted).** If there is a troubleshooting
   or FAQ section where a user would have looked for "why does my run stay on running after
   it failed", add or update that entry.

## Accuracy guardrails

- Do NOT claim behaviour that Karen did not validate. Everything above is from
  `.specs/karen-vidlift-v0.4.16.1-revalidation-report.md` and
  `.specs/forge-vidlift-failpath-fix-task.md` — read both before writing.
- Known residual edge, do NOT describe as fixed: if a user **explicitly deselects** the
  running row mid-run, a stale running overlay can persist after the failure (all other
  surfaces are correct, one click repairs it). This is pre-existing, Low severity, and
  tracked as a follow-up. Do not mention it in user docs unless you judge a note useful;
  if you do, describe it honestly and do not oversell the fix.
- No new configuration options, flags, environment variables, or endpoints exist. Do not
  document any.
- Keep the tone and structure of the existing changelog and README. Match, do not redesign.

## Done when

- `CHANGELOG.md` has the `[0.4.16.1]` section with correct date `2026-11-03`.
- `README.md` is either correctly updated or explicitly confirmed as not stale.
- Your final output is a concise report: what you changed (file by file), what you verified,
  and anything you deliberately did not touch.
