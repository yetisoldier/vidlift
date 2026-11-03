# Validation Specification — VidLift Remote Visibility for Hermes

**For:** Karen (expand freely — this is the baseline, not the ceiling)
**Author:** Bob
**Companions:** `bob-vidlift-visibility-architecture-spec.md`,
`bob-vidlift-visibility-implementation-spec.md`

---

## 1. What Must Be Proven

1. **P1 — Visibility.** Hermes's one-shot read returns the FULL running state: device
   facts (upscale simulated/real; RESOLVED transcription device), per-job stage,
   progress, ETA (honest-null), queue, for both `run` and `subs` jobs.
2. **P2 — Read-only.** The new surface changed nothing about mutability: no write verb
   exists, no enqueue/cancel path is reachable over HTTP, and the Hermes artifact
   contains no mutation code.
3. **P3 — No unauthenticated full-state exposure.** `/status`/`/history` token gates
   are exactly as before the change; the additive key did not leak into `/health`;
   the discovery card did not create a new unauthenticated read path (it is a file in
   the user-profile state dir, not a served resource).
4. **P4 — Contract additivity.** `api_version` remains 1; every pre-existing
   `/status` and per-job payload field is unchanged in name, type, and semantics; the
   ONLY delta is the additive `transcription` block.
5. **P5 — Provisioning honesty.** The discovery card reflects the EFFECTIVE port/token
   after start, toggle, port fallback, and stop; Hermes detects desync deterministically
   (401) with an actionable re-pair message.

## 2. Functional Test Matrix (VidLift side)

| # | Scenario | Expected |
|---|----------|----------|
| T-1 | Live `run` job mid-upscale | `/status` per-job: `state=running`, `current_phase=upscale`, frames progress non-null, `transcription.device == null` (not yet recorded — honest), `device_requested == null` |
| T-2 | Live `run` job whose subtitles phase completed (stub or real) | `transcription.device` equals the recorded value; a CUDA-fallback run shows exactly `"cpu (cuda unavailable)"` (v0.4.15.3 R1 wording — pin the string) |
| T-3 | `subs` job, terminal success | `transcription.device` = `record.result.device`; `device_requested` = options pin (e.g. `"auto"`) |
| T-4 | `subs` job, terminal, foreign engine factory (`result.device == ""`) | `transcription.device == null` (never `""`) |
| T-5 | Finished/failed `run` job with manifest | manifest echo: `manifest.subtitles.transcription.device` surfaced with no live state |
| T-6 | Run job with no transcription (subtitles status `none`) | both keys `null`; all pre-existing keys byte-comparable to pre-change payload |
| T-7 | Server configured with EMPTY token (open mode) | `/status` answers without auth (unchanged semantics); Hermes script's canary fires loudly |
| T-8 | Wrong/absent token, non-empty config | 401 `{"error": "missing or invalid token"}` — unchanged |
| T-9 | Non-GET verbs (POST/PUT/DELETE to `/status`) | 501 — unchanged; no 404-body changes beyond the additive key |
| T-10 | `/health` | still token-free, still ONLY `{ok, version, busy}` — the new payload key must NOT appear here |

## 3. Discovery Card Tests

| # | Scenario | Expected |
|---|----------|----------|
| D-1 | GUI start, monitor serves (default token) | `state_dir/monitor.json` exists, `schema: 1`, `enabled: true`, `port` == actual bound port, `lan_base_url` ends in that port, `token` == `MONITOR_TOKEN_DEFAULT`, `token_source == "default"`, atomic (no `.json.tmp` residue) |
| D-2 | Port fallback (pre-bind 8765 occupied) | card `port` reflects the ACTUAL port (e.g. 8766), matching `monitor/port` QSettings persistence |
| D-3 | Monitor toggled off | card rewritten: `enabled: false`, `port: null`, `lan_base_url: null` |
| D-4 | QSettings token override present | card carries the override token; `token_source == "qsettings"` |
| D-5 | QSettings token stored EMPTY | card carries `""`; `token_source == "empty-open"`; behavior unchanged (T-7) |
| D-6 | Card write failure (read-only state dir) | monitor startup/toggle still succeeds; failure swallowed — never fatal |

## 4. Hermes-Side Tests

| # | Scenario | Expected |
|---|----------|----------|
| H-1 | Paired, idle VidLift | exit 0; renders version, `queue IDLE`, zero jobs, no fabricated device lines |
| H-2 | Paired, busy (live 192.168.1.53:8766 run + queued subs) | stage label from `progress["step"]`, percent from frames, ETA minutes or honest "unknown", device line correct incl. fallback wording, queue order right |
| H-3 | Monitor down / wrong port | exit 3 + reachability guidance |
| H-4 | Stale token (health OK, status 401) | exit 4 + re-pair instruction naming `monitor.json`; NO query-string fallback attempted (assert no `?token=` on the wire) |
| H-5 | Open-mode server | loud open-mode canary + normal render |
| H-6 | `--history` | last-5 render; `manifest_path` never fetched; remote-path caveat noted |
| H-7 | Env var missing | exit 2 + setup hint |
| H-8 | Token secrecy | output never contains the token on ANY exit path |

## 5. Security Expectations (assert all)

- S-1: unauthenticated `/status` and `/history` remain 401 under a non-empty token.
- S-2: no new endpoints; 404 body still lists exactly `/health /status /history`.
- S-3: token compare remains `hmac.compare_digest`.
- S-4: the discovery card is never served by the monitor (it is not an HTTP resource).
- S-5: the card is not logged, not in window chrome, not committed (grep the diff).
- S-6: `test_status_view_cannot_write` and every read-only-construction pin still pass unmodified.

## 6. Regression Gate (must pass unmodified)

- Full existing suite green; explicitly re-run the monitor, MCP bridge status payload,
  GUI monitor widget (start/toggle/pill), and transcription/fallback test blocks
  (v0.4.15.2/v0.4.15.3 pins, incl. the R1 `"cpu (cuda unavailable)"` wording).

## 7. Performance Expectations

- `/status` response unchanged in scale (additive constants only); no new I/O per
  request — the manifest manifest-echo path only triggers for jobs already reading a
  manifest in the existing code path.
- Hermes ask: ≤ ~2 s end-to-end on the LAN (two GETs, 3 s/5 s timeouts are ceilings).

## 8. Acceptance Criteria (ship when ALL hold)

1. P1–P5 proven (sections 2–5 all pass).
2. Regression gate green with ZERO modifications to pre-existing tests or sources
   other than the spec'd additive hunks.
3. End-to-end: Eric's spoken ask → Hermes answers device/stage/progress/ETA/queue in
   one round trip, without any manual relay at ask time.
4. A forced 401 (token desync) yields the re-pair path end-to-end, and re-pairing from
   the card restores service with one file copy.
5. Karen's independent expansion of this spec finds no unauthenticated full-state
   exposure and no mutation path.

## 9. Known Honest Gaps (NOT failures — report expectations)

- Live `subs` job mid-transcription reports `transcription.device: null` until
  completion (the v0.4.15.3 D4 settlement defines the device at completion; the
  in-flight seam is future work — ADR-3 revisit criteria).
- `eta_seconds` remains honest-null outside upscale/encode phases (pre-existing rule).
- The card is only as fresh as the last serving-state change (write-on-change, by
  design — it is provisioning, not telemetry).
