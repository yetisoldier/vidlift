# Follow-up (next build): raise QGroupBox panel titles

**Author:** Hermes (orchestrator) — **Date:** 2026-11-04
**Source:** Eric, live install of v0.4.17.0 on Windows.
**Severity:** cosmetic — NOT blocking; fold into the next build/release.

## Observation

On the real Windows build (Segoe UI), the two panel titles — **"Upscale"** and
**"Add subtitles (no upscale)"** — render **sitting directly on top of the panel's
top border line** (~1-2 px clearance above the border). They read as resting on the
line rather than floating above it as a proper fieldset caption. Eric wants them
"slightly higher."

Layout is otherwise correct (no overlap with the first control row; the window fits).

## Location

`vidlift/gui.py` §6.5 QSS (workstream-2):

```
QGroupBox#Controls { background: #191d1f; border: 1px solid #2b3134; border-radius: 8px; margin-top: 14px; padding-top: 4px; font-weight: 600; }
QGroupBox#Controls::title { subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 4px; color: #98a196; }
```

## Fix direction (for Bob/Forge)

Raise the title by one of (tune on a real Windows run — offscreen Linux does not
reproduce the Segoe UI metric):

- add a negative `top` to `::title` (e.g. `top: -8px;`), and/or
- increase `margin-top` on `QGroupBox#Controls` so the title clears the border.

Keep the fix QSS-only; no structural change. Verify the new offsets against both
the two-row Upscale group and the single-row Mode B group, and confirm the title
still clears the first control row.

## Related tracked follow-ups (same backlog)

- Sidecar-vs-burned `.srt` output-choice toggle (Eric) — out of scope for v0.4.17.
- Karen F-1 (workstream-2): no single test asserts the Mode B group contains EXACTLY
  its three controls — suggest a `subs_grid.count() == 3` exhaustiveness assertion.
