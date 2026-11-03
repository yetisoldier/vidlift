# Implementation Specification — VidLift Remote Visibility for Hermes

**For:** Forge
**Author:** Bob
**Companion docs:** `bob-vidlift-visibility-architecture-spec.md` (ADRs, rationale),
`bob-vidlift-visibility-validation-spec.md` (Karen's baseline)
**Scope discipline:** additive-only to the status contract; `api_version` stays 1; no new
network surface; no change to existing endpoint behavior; no test modifications —
Forge adds new tests, existing suite must stay green untouched.

---

## 1. Change Summary

| Side | Change | Size |
|------|--------|------|
| VidLift | Additive `transcription` block in `_status_payload` (`mcp_bridge/tools.py`) | ~25 LoC |
| VidLift | Monitor discovery card writer (`gui.py` + one constant in `config.py`) | ~35 LoC |
| VidLift | New tests (additive) for both | 2 test files/blocks |
| Hermes | `~/.hermes/bin/vidlift-status` stdlib script + `~/.hermes/.env` keys | ~150 LoC |
| Hermes | `vidlift-status` skill wrapper | ~30 LoC |
| Ops | One-time pairing copy of `monitor.json` values into `~/.hermes/.env` | minutes |

---

## 2. VidLift-Side Changes

### 2.1 `vidlift/mcp_bridge/tools.py` — additive `transcription` key in `_status_payload`

Add to the payload dict built in `_status_payload`, a single nested key (place it
logically beside `"subtitles"`, and document it as additive in the module's pass-through
convention):

```python
payload["transcription"] = _transcription_echo(record, state, manifest)
```

New private helper in the same module (no other module touched):

```python
def _transcription_echo(record, state, manifest) -> dict:
    """Additive (ADR-3): the RESOLVED transcription device, honest-null.

    device: where the transcription RAN — "cuda" | "cpu" |
    "cpu (cuda unavailable)" (the v0.4.15.3 R1 fallback wording) | null.
    device_requested: the caller's pin ("auto"|"cuda"|"cpu") when the job
    carried one (subs options; run jobs: null — the pin lives in the plan,
    not the record options), else null. null everywhere = not recorded,
    NEVER inferred.
    """
    echo: dict = {"device": None, "device_requested": None}
    if record.kind == "subs":
        requested = (record.options or {}).get("device")
        echo["device_requested"] = requested if requested else None
        result = record.result or {}
        device = result.get("device") or None  # "" = D4 honest unknown
        echo["device"] = device
        return echo
    # run-kind jobs (Mode A)
    block = None
    if state is not None and isinstance(state.subtitles, dict):
        block = state.subtitles
    elif manifest is not None and isinstance(manifest.get("subtitles"), dict):
        block = manifest["subtitles"]
    if block:
        tr = block.get("transcription") or {}
        device = tr.get("device") or None
        echo["device"] = device
    return echo
```

Rules that keep the repo's honesty culture:

- Never project, never infer from `engine_note`/log lines; only recorded facts above.
- `state.subtitles` is the live Mode A truth (populated when the subtitles phase
  records; `null` during transcription is the CORRECT in-flight answer).
- Manifest echo covers finished/failed Mode A jobs (manifest written with
  `"subtitles": state.subtitles`, run.py:2840).
- Do NOT extend `record.result` surfacing generally; only the `transcription.device`
  read above crosses over for subs jobs.
- `api_version` in `monitor.py` is NOT bumped (additive; the header documents why).

### 2.2 `vidlift/config.py` — discovery card constant

```python
MONITOR_DISCOVERY_FILENAME = "monitor.json"
MONITOR_DISCOVERY_SCHEMA = 1
```

Add both to `__all__`.

### 2.3 `vidlift/gui.py` — write the discovery card

In `MainWindow`, add one private method and call it after every monitor start/toggle
path where the serving state resolves (the exact places `monitor_port_reported` is set
or cleared today: `_start_monitor` success, `_start_monitor` failure/disabled,
`_toggle_monitor` on/off):

```python
def _write_monitor_discovery(
    self, *, enabled: bool, port: int | None, token: str, token_source: str
) -> None:
    """ADR-2 discovery card: state_dir/monitor.json (atomic temp+replace).
    Carries the EFFECTIVE port/token the monitor serves so a paired watcher
    (Hermes) learns the CURRENT pair without reading QSettings. The token
    is a secret: never logged, never in window chrome, never committed —
    the card lives in the user-profile state dir (same trust zone as the
    QSettings store)."""
```

Behavior:

- Path: `client_local.default_state_dir() / MONITOR_DISCOVERY_FILENAME` — the GUI's
  own resolution (import `from .mcp_bridge.client_local import default_state_dir`;
  NOTE: gui.py already constructs its client without an explicit state_dir, so this
  helper's default matches the live registry root).
- Payload keys: `schema`, `enabled`, `bind_host`, `port` (actual, `null` when off),
  `lan_base_url` (`http://{lan_ip()}:{port}` when serving, else `null`), `token`,
  `token_source` (`"default"` if the QSettings value is absent, `"qsettings"` when an
  override exists, `"empty-open"` when stored empty — see note), `version`
  (`vidlift.__version__`), `written_at` (`time.time()`).
- Determine `token_source` where `_monitor_settings()` reads QSettings:
  absence → `"default"`; present-and-nonempty → `"qsettings"`; present-empty →
  `"empty-open"` (Karen validates the canary wording; do not change empty-token
  serving semantics in this milestone).
- Atomic write: `path.with_suffix(".json.tmp")` then `os.replace` — the repo's
  manifest/registry convention. Best-effort: wrap in try/except OSError, never fatal
  to monitor startup (mirror the BridgeLog swallow discipline).
- Call sites:
  - `_start_monitor`: after `self.monitor_port_reported = actual_port` (success);
    in the disabled / autostart-opt-out / OSError branches, write with
    `enabled=False, port=None` and the resolved token values.
  - `_toggle_monitor`: same — on after success; off after `stop_server()`.
- Do NOT write from `monitor.py`: the card is a provisioning artifact owned by the
  process that knows QSettings (the GUI), keeping `monitor.py` free of config concerns.

### 2.4 New tests (additive — Forge creates; nothing existing is touched)

Create `tests/test_visibility_hermes.py`:

1. `_status_payload` on a synthetic run job whose `state.subtitles` carries
   `{"transcription": {"device": "cpu (cuda unavailable)"}}` → payload
   `transcription.device == "cpu (cuda unavailable)"`, `device_requested is None`.
2. `_status_payload` on a synthetic subs job with `record.result =
   {"device": "cuda"}` and `options={"device": "auto"}` →
   `{"device": "cuda", "device_requested": "auto"}`.
3. Subs job with `record.result = {"device": ""}` → `device is None` (D4 honest
   unknown, not empty string).
4. Finished run job (no live state, fake manifest dict with
   `subtitles.transcription.device = "cuda"`) → echoed.
5. Run job with no subtitles facts → both keys `None`; payload still contains all
   pre-existing keys unchanged (additivity guard: compare against the pre-change key
   set + `{"transcription"}`).
6. Discovery card helper: fake QSettings/absence → `token_source == "default"`,
   atomic file exists, parses, `schema == 1`, `lan_base_url` ends with the actual port;
   served-off path writes `enabled: False, port: None`. Use `tmp_path` via a
   monkeypatched `default_state_dir` seam — do not touch real state.

Run the FULL existing suite afterwards; it must pass unmodified (especially
`test_monitor*.py`, `test_mcp*.py` / t-series status payload pins).

---

## 3. Hermes-Side Artifacts (outside this repo — deliver as files + instructions)

### 3.1 `~/.hermes/.env` additions

```dotenv
# paired from <vidlift-state-dir>/monitor.json (ADR-2); re-copy on 401
VIDLIFT_MONITOR_URL=http://192.168.1.53:8766
VIDLIFT_MONITOR_TOKEN=vidlift-mon-<current-value>
```

File mode 0600; never commit.

### 3.2 `~/.hermes/bin/vidlift-status` (python3, stdlib only)

Contract:

- Reads the two env keys (missing → exit 2 with setup hint).
- `GET {URL}/health` (timeout 3 s): connect error → print "VidLift monitor not
  reachable at {URL} (GUI down, monitor off, or port re-fell-back — re-check
  monitor.json)" and exit 3.
- `GET {URL}/status` with `Authorization: Bearer {TOKEN}` (timeout 5 s):
  - **401** → print "VidLift token rejected — re-pair: copy `token`/`lan_base_url`
    from `<vidlift-state>/monitor.json` into ~/.hermes/.env" and exit 4. Never retry
    with `?token=`.
  - **200 without auth having been required** (i.e., token set but server answered
    with an empty/missing gate — detectable by sending an intentionally bad bearer
    once and still getting 200): print a loud canary: "VidLift monitor is in OPEN
    mode (empty token) — full state is unauthenticated on the LAN" then render
    normally. (Karen T-7 covers the semantics; the canary wording is the
    acceptance point.)
- Renders (no LLM):

  ```
  VidLift 0.4.15.x — queue ACTIVE (1 running, 2 queued) — up 3h 12m
  ▶ movie.mkv [run] upscale 62% (frame 81234/131072) — ETA 47m
      engine: real (GPU), transcription: cpu (cuda unavailable)   ← when known
      stage: upscaling chunk 7
  ⏸ clip2.mkv [subs] queued (#1) — transcription requested: auto
  ```

  Rules: ETA shows `eta_seconds` rounded to minutes, else the honest word
  "unknown"; device line shows upscale truth (`engine_simulated` → "stub (simulated)")
  and `transcription.device` when non-null, else "transcription device: unknown (not
  yet recorded)"; `progress["step"]` is the stage label source; subs jobs use
  `segments_done`; queue order from `queue_position`/`active_jobs` order +
  `queued_count`; timestamps via `server_time - started_at`.
- `--history` flag: one extra GET `/history`, render last 5 rows
  (`state`, `ok`, `file_path` basename, `engine_simulated`); note that
  `manifest_path` is remote-only and not fetched.
- Output is deterministic text the skill relays verbatim.

### 3.3 Skill `~/.hermes/skills/vidlift-status/`

SKILL.md essence: trigger on Eric asking about VidLift status/progress/queue/"what's
going on"; run `~/.hermes/bin/vidlift-status` (with `--history` only when history is
asked for); relay stdout; on exit 3/4 relay stderr guidance. One round-trip, no LLM in
the loop beyond the trigger match.

---

## 4. Provisioning (Ops)

1. Start VidLift GUI (monitor on) → `<state_dir>/monitor.json` exists (e.g. dev:
   `~/.vidlift/monitor.json`; packaged Windows: `%LOCALAPPDATA%\YetiWurks\VidLift\`).
2. Copy `lan_base_url` → `VIDLIFT_MONITOR_URL`, `token` → `VIDLIFT_MONITOR_TOKEN` into
   `~/.hermes/.env` on the minipc (any one-time channel; a USB-stick/hand copy is
   fine — the card removes the need to transcribe a uuid by hand).
3. Verify: `vidlift-status` prints the summary; deliberate wrong-token run exits 4.
4. Re-pair only on: QSettings token override, rotation-by-release (401 makes both
   unmissable), or port re-fallback (card rewritten every start).

---

## 5. Implementation Order (milestones — each ships working, independently testable)

- **M1 — VidLift visibility delta.** § 2.1–2.4. Acceptance: new tests green; full
  suite green unmodified; manual curl of a dev GUI shows the `transcription` key in
  `/status` and `monitor.json` on disk; `api_version` still 1.
- **M2 — Hermes script.** § 3.1–3.2. Acceptance: against the live 192.168.1.53:8766,
  busy and idle renders correct; exit codes 3/4 verified by induced failures;
  open-mode canary verified against a dev empty-token server.
- **M3 — Skill + pairing.** § 3.3 + § 4. Acceptance: Eric's spoken ask resolves to the
  script output in one round trip; a forced 401 produces the re-pair instruction
  through Hermes.

## 6. Coding Conventions

- VidLift side: match existing module docstring style (version-stamped, design
  references, honesty rationale); no new dependencies; additive keys only;
  `hmac`/auth logic untouched; monitor.py routing untouched.
- Hermes side: stdlib only (`urllib.request`, `json`, `os`, `sys`); no f-string
  leakage of the token into output on any path; timeouts on every request.
- Nothing in this spec modifies an existing source behavior or test; all changes are
  strictly additive.
