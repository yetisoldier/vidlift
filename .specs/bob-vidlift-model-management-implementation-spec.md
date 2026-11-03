# Implementation Specification — VidLift Transcription-Model Management

**For:** Forge
**Author:** Bob
**Companion docs:** `bob-vidlift-model-management-architecture-spec.md` (ADRs,
rationale), `bob-vidlift-model-management-validation-spec.md` (Karen's baseline)
**Scope discipline:** additive-only to the status contract; `api_version` stays 1; no
new endpoints, no new MCP tools, no new dependencies; **the existing suite must pass
unmodified** — Forge adds new tests and never edits existing ones (the one deliberate
exception is documented in §2.1: NOTHING in the existing suite needs changing — the
conditional-arity discipline exists precisely so every 2-arg fake fetch keeps working).

---

## 1. Change Summary

| Module | Change | Size (est.) |
|---|---|---|
| `vidlift/transcribe.py` | Fetch-seam progress channel (3rd positional, conditional-arity), `resolve_model_dir(on_download_progress=, on_lock_wait=)`, `_model_download_lock`, `ModelInstallBoard`, exports | ~120 LoC |
| `vidlift/mcp_bridge/jobs.py` | `_execute_subs_job`: download-progress publisher (heartbeat + `progress["model_download"]`), pass through to `run_subs_job` | ~30 LoC |
| `vidlift/run.py` | Mode A: pass `on_download_progress`/`on_lock_wait` into the direct `resolve_model_dir` call; clear the fact after resolve | ~20 LoC |
| `vidlift/mcp_bridge/tools.py` | `_transcription_model_echo(record, state, manifest, services)` helper + additive per-job key | ~55 LoC |
| `vidlift/monitor.py` | Additive envelope key `transcription_model` in `status_payload` | ~20 LoC |
| `vidlift/mcp_bridge/server.py` | `ModelInstallBoard` instance on `BridgeServices` at `build_services` | ~5 LoC |
| `vidlift/gui.py` | Title-stack pill + Install button + QThreadPool install worker + run-panel label/stage-bar render | ~130 LoC |
| Hermes `vidlift-status` | One render line for the new block (follows the visibility impl spec §3.2) | ~10 LoC |
| Tests (new files only) | `tests/test_model_management.py`, GUI block, monitor/payload block | new |

Version stamp: **v0.4.16.0** — bump `pyproject.toml` + `vidlift/__init__.py` per the
repo's release discipline (the visibility work's `M …` files in git status ship in the
same release; coordinate the version string with that merge if it lands first).

---

## 2. Module-by-Module Work

### 2.1 `vidlift/transcribe.py` — the seam, the lock, the board

Add to `__all__`: `"MODEL_LOCK_STALE_SECONDS"`, `"ModelInstallBoard"`,
`"model_install_state"`. (Names below are binding.)

**a) The fetch contract (ADR-3).** The seam's documented contract becomes:

```python
# Callable[[str, Path], None]  ->  Callable[[str, Path, Callable[[int, int | None], None] | None], None]
# ``fetch(url, target_path, on_progress=None)``: on_progress(done_bytes,
# total_bytes_or_None) fires per copied chunk when supplied. Call sites pass
# the third argument ONLY when a progress consumer exists (conditional arity —
# the shape that keeps every legacy 2-arg fake valid on no-progress paths).
```

**b) `_default_fetcher` — chunked copy with live progress.** Replace the
`shutil.copyfileobj(response, fh, 1024 * 1024)` body with an explicit loop. New
signature `def fetch(url: str, target_path: Path, on_progress=None) -> None:` —
behavior:

```python
def fetch(url: str, target_path: Path, on_progress=None) -> None:
    tmp = target_path.with_name(target_path.name + ".tmp")
    size_hint: int | None = None
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length:
                size_hint = int(length)
            tmp.parent.mkdir(parents=True, exist_ok=True)
            done = 0
            with open(tmp, "wb") as fh:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, size_hint)
        if size_hint is not None and tmp.stat().st_size != size_hint:
            raise TranscriptionError(
                f"download of {target_path.name} was truncated "
                f"({size_hint} bytes expected)"
            )
        os.replace(tmp, target_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
```

Every existing guard (`.tmp` staging, post-hoc size check, BaseException cleanup —
F-11) is preserved verbatim; the only delta is the loop + callback.

**c) `_download_file` — conditional-arity forwarding.** Current signature
(`target_dir, filename, url, *, expected_size, fetch, on_progress`) keeps its shape;
inside, replace `fetch(url, part)` with:

```python
if on_progress is not None:
    fetch(url, part, on_progress)
else:
    fetch(url, part)
```

The existing post-completion `on_progress(size, expected_size)` call stays (it is the
final 100%-of-file beat). Do not rename anything; do not touch error wording — Karen's
F-11/G-series pins live on those messages.

**d) `resolve_model_dir` — new keyword-only parameters + the lock (ADR-5).**

```python
def resolve_model_dir(
    pin: str | None = None,
    *,
    state_root: str | os.PathLike | None = None,
    fetch=None,
    on_download_progress: Callable[[int, int | None], None] | None = None,
    on_lock_wait: Callable[[int | None, float], None] | None = None,
) -> str:
```

- Both default `None` → existing call sites behave byte-identically.
- Wrap the download critical section — everything from just after the fast-path return
  through `marker.write_text(...)` — in `with _model_download_lock(target, on_wait=on_lock_wait):`.
- After acquiring the lock, **re-run the fast-path check** (`marker.is_file() and
  (target/"model.bin").is_file() → return str(target)`) before any download: the
  winning racer may have completed while we waited.
- Forward progress: both `_download_file` calls get `on_progress=on_download_progress`.
- Update the docstring's progress promise: it is now true — name the callback shape
  `(done_bytes, total_bytes_or_None)` and note `.part` short-circuit semantics.

**e) `_model_download_lock(target_dir, on_wait=None)` — context manager.** Stdlib only,
cross-platform:

```python
MODEL_LOCK_STALE_SECONDS = 1800  # 30 min: heartbeats keep an active lock young

@contextlib.contextmanager  # (import contextlib at module top)
def _model_download_lock(target_dir: Path, on_wait=None):
    """Per-tag download critical section (v0.4.16, model-management ADR-5).

    O_CREAT|O_EXCL lockfile ``<tag>/.download.lock`` — create-close, never
    held open (no Windows sharing-violation trap). The download's own
    progress callback heartbeats its mtime (≤1 Hz), so a live writer never
    looks stale; a lock older than MODEL_LOCK_STALE_SECONDS is dead weight
    (killed process) and is reclaimed once. Degrade-OPEN on an unwritable
    cache dir: proceed WITHOUT the lock — the sha256 pin remains the
    integrity backstop; never a hang, never a refusal (the GPU-phase-lock
    honesty rule, mirrored). The caller re-runs its fast-path check after
    acquire: the winner likely finished while we waited."""
```

Implementation notes (binding):

- lock path: `target_dir / ".download.lock"`.
- Acquire loop: try `os.open(lock, O_CREAT | O_EXCL | O_WRONLY)`; write
  `f"{os.getpid()}\n{time.time()}\n"`, close. On `FileExistsError`: read the file
  (best-effort holder pid for the wait label), `on_wait(pid_or_None, waited_seconds)`
  if provided, sleep 1.0, retry.
- Before sleeping, stale-check: `time.time() - os.stat(lock).st_mtime >
  MODEL_LOCK_STALE_SECONDS` → `os.unlink(lock)` guarded by `except OSError: pass`,
  then retry the O_EXCL create (once per loop iteration is fine).
- Degrade-open: `OSError` on the FIRST create attempt (permissions, read-only fs) →
  yield the token `False` (lock NOT held) and skip all further lock I/O; the caller
  proceeds regardless (the note/logging is the caller's surface — see jobs/run
  publishers below). Every later OSError inside the loop (stat/unlink races with the
  other process) is swallowed and retried — acquisition loops are crash-prone by
  nature; the retry IS the correctness.
- Release: `try: os.unlink(lock) except OSError: pass` in a `finally`, only when held.
- **Heartbeat wiring lives in `resolve_model_dir`**, not here: wrap the caller's
  `on_download_progress` so that before delegating it does
  `os.utime(lock_path, None)` at most once per second (keep `last = [0.0]` inside the
  wrapper; compare `time.time()`), guarded by `except OSError: pass`. When the caller
  passed no progress callback, heartbeat from a trivial internal counter instead —
  `_download_file`'s post-completion beat is enough for support files; `model.bin`
  under a no-progress caller heartbeats never… therefore: when
  `on_download_progress is None`, `resolve_model_dir` installs a *no-op* progress
  callback purely for the heartbeat (`def _hb(done, total): <utime ≤1 Hz>`) and passes
  it. This keeps the lock young on the legacy no-progress path without changing any
  observable behavior.

**f) `ModelInstallBoard` — the idle-install visibility slot (ADR-4).** A tiny class in
`transcribe.py` (model-delivery state belongs with model delivery):

```python
class ModelInstallBoard:
    """In-memory, process-local install/install-attempt truth (v0.4.16,
    ADR-4). The GUI's install worker (and only it, today) writes; the
    status surfaces read. No file, no mirror — the board dies with the
    process, which is honest: a dead install is not downloading. One
    threading.Lock guards a single-slot snapshot dict; readers copy under
    the lock and never hold it."""

    def __init__(self) -> None: ...
    def begin(self, tag: str) -> None: ...          # state=downloading, bytes 0/None
    def update(self, *, bytes_done: int, bytes_total: int | None, file: str) -> None: ...
    def succeed(self, tag: str) -> None: ...        # state=installed (disk agrees; UI re-checks anyway)
    def fail(self, tag: str, error: str) -> None: ...  # state=not-installed, error=message
    def snapshot(self) -> dict | None: ...          # {"tag","state","bytes_done","bytes_total","file","error","updated_at"} or None when never used
```

- `snapshot()` returns `None` when nothing was ever recorded (fresh process). Keys are
  exactly the `transcription_model` payload keys plus `updated_at` (epoch float;
  readers use it for freshness display only — never to fabricate state).
- Threading: install worker writes from the QThreadPool thread; monitor/tools read
  from the HTTP/Qt threads — the internal `threading.Lock` is mandatory, not
  decorative.

**g) `model_install_state(tag, *, state_root=..., board=None, progress_download=None)` —
the one derivation helper everyone reads (ADR-1/ADR-2).** Put it in `transcribe.py`
beside `model_cache_dir` so the detector, the GUI, tools.py, and monitor.py share ONE
implementation of "what is the model's state right now":

```python
def model_install_state(
    tag: str,
    *,
    state_root: str | os.PathLike | None = None,
    board: ModelInstallBoard | None = None,
    progress_download: dict | None = None,
) -> dict:
    """The additive ``transcription_model`` block (v0.4.16).

    Derive-at-read, never persist a state string. Order (architecture §6):
    1. ``progress_download`` (a live job's ``progress["model_download"]``):
       the in-flight truth — ``downloading`` with its byte fields.
    2. a fresh board snapshot whose ``state == "downloading"`` (a GUI
       install in flight while no job rides this record).
    3. the disk predicate — ADR-1, the resolve fast-path contract verbatim:
       ``.complete`` marker AND ``model.bin`` present → ``installed``;
       else ``not-installed``.
    4. OSError on the disk probe → ``state="unknown"`` (honest: a
       permissions failure is not "missing").
    ``bytes_*``/``file`` are null unless downloading; ``error`` rides only a
    board ``fail`` snapshot (until the next begin/succeed clears it)."""
```

Return shape (exactly — payload stability):

```python
{
    "tag": tag,
    "state": "installed" | "not-installed" | "downloading" | "unknown",
    "cache_dir": str(model_cache_dir(state_root) / tag),
    "bytes_done": int | None,
    "bytes_total": int | None,
    "file": str | None,
    "error": str | None,
}
```

Board priority rule (binding): a board snapshot participates ONLY when
`snap["tag"] == tag`; a `downloading` board beats disk (the bytes haven't landed the
marker yet); a `fail` snapshot contributes only its `error` string with state derived
from disk (post-failure truth is "not-installed + why"); a `succeed` snapshot
contributes nothing (disk is the truth — if disk disagrees the pill re-checks next
tick and honestly says so).

### 2.2 `vidlift/mcp_bridge/server.py` — construct the board

In `build_services`, after the existing service assembly (read the file and place it
with the other attribute assignments; `BridgeServices` accepts the attribute per its
current construction style — if it is a plain object/dataclass, add a field or assign
in the constructor path the module already uses):

```python
from ..transcribe import ModelInstallBoard
services.model_install_board = ModelInstallBoard()
```

Soft-read rule for ALL consumers below: `getattr(services, "model_install_board", None)`
— foreign `BridgeServices` fakes in the existing suite must not need it (N5).

### 2.3 `vidlift/mcp_bridge/jobs.py` — the Mode B publisher (Eric's exact symptom)

In `_execute_subs_job`, beside the existing `on_segments`/`on_step` closures (`jobs.py:349-374`),
add:

```python
_publish_state = {"t": 0.0}  # ≤4 Hz publisher-side throttle (mirror self-throttles at 10 s)

def on_download_progress(done: int, total: int | None) -> None:
    """v0.4.16 (model management): model fetch bytes ride the SAME
    progress dict the GUI tick / /status / mirror already consume — and
    every beat TOUCHES the record/state so a 17-minute download never
    again reads as climbing activity_age_seconds (Eric's silent-download
    repro). The fact is popped by on_step at the first post-resolve step
    (below) so it never outlives the download."""
    now = time.time()
    if now - _publish_state["t"] < 0.25 and total is not None and done < total:
        return
    _publish_state["t"] = now
    progress = dict(state.progress or {})
    progress["model_download"] = {
        "bytes_done": int(done),
        "bytes_total": int(total) if total is not None else None,
        "file": "model.bin",  # support files are KB-scale; the byte-bearing fetch is model.bin
    }
    progress["step"] = "downloading transcription model (model.bin)"
    state.progress = progress
    state.touch()
    record.touch()
```

Amend `on_step`: at the top, `progress = dict(state.progress or {})` already happens;
add immediately after — `progress.pop("model_download", None)` — so every later step
(`transcribe-language`, `transcribe-run`, …) clears the download fact. (Note the
ordering subtlety: `transcribe-resolve` fires BEFORE resolve; the first progress beat
lands after; popping in on_step is safe because the pop only affects steps *after*
resolve starts, and by then the download is done. Keep the pop unconditional — any
post-resolve step means resolve returned.)

Thread `on_download_progress=on_download_progress` into the `run_subs_job(...)` call
(`jobs.py:384-400`) — the parameter already exists (`subs_job.py:113`) and already
forwards to the core; no `subs_job.py` change.

`time` is already imported in jobs.py — verify and reuse.

### 2.4 `vidlift/run.py` — the Mode A publisher

At the Mode A branch (run.py:2505-2544), change the direct resolve call
(`run.py:2516`) to publish through `record_step`-style merging (mirror jobs.py):

```python
_dl_state = {"t": 0.0}

def _on_model_download(done: int, total: int | None) -> None:
    now = time.time()
    if now - _dl_state["t"] < 0.25 and total is not None and done < total:
        return
    _dl_state["t"] = now
    progress = dict(state.progress or {})
    progress["model_download"] = {
        "bytes_done": int(done),
        "bytes_total": int(total) if total is not None else None,
        "file": "model.bin",
    }
    progress["step"] = "downloading transcription model (model.bin)"
    state.progress = progress
    state.touch()

model_dir = resolve_model_dir(
    knobs.get("model"),
    on_download_progress=_on_model_download,
    on_lock_wait=_transcribe_wait_label,   # reuse the GPU-lock wait-label shape
)
progress = dict(state.progress or {})     # clear the fact the moment resolve returns
progress.pop("model_download", None)
state.progress = progress
```

Notes:

- `_transcribe_wait_label` (run.py:2461) is the GPU lock's waiter; passing it to
  `on_lock_wait` gives the download-lock wait the same visible labeling for free.
  Its signature `(holder_pid, held_seconds)` matches the lock's callback contract.
- The `transcribe_source_to_srt(...)` call inside Mode A re-resolves through the warm
  cache (fast path — no download, no progress) and needs **no** new kwargs.
- `import time` position: check the module's imports; reuse if present.

### 2.5 `vidlift/transcribe.py` core — wire the (formerly decorative) parameter

`transcribe_source_to_srt`: change

```python
model_dir = resolve_model_dir(
    model_pin,
    state_root=state_root,
)
```

to also pass `on_download_progress=on_download_progress`. Leave the existing
`download_log` line (1685-1688) alone — it is honest post-hoc logging.

### 2.6 `vidlift/mcp_bridge/tools.py` — the additive per-job key (ADR-2)

New helper beside `_transcription_echo` (429-472), called from `_status_payload`
immediately after `payload["transcription"] = _transcription_echo(...)` (line 516):

```python
payload["transcription_model"] = _transcription_model_echo(record, state, services)


def _transcription_model_echo(record, state, services) -> dict:
    """Additive (model-management ADR-2): transcription-model presence /
    download state, derived at read — never projected, never persisted as
    a state string. In-flight byte truth comes from the live
    ``progress["model_download"]`` (same-process) or the persisted mirror
    (cross-process, trusted only while the record is running — a terminal
    record's mirror can be a dead writer's last beat); the GUI install
    board covers idle-queue installs; the disk predicate is the ground
    truth everywhere else."""
    if record.kind == "subs":
        pin = (record.options or {}).get("model") or None
    else:
        pin = None
    from ..transcribe import DEFAULT_MODEL, KNOWN_MODELS, model_install_state  # lazy: tools.py import weight
    # resolve_model_dir's own rule: an unknown pin is a user PATH, not a
    # tag — the block then honestly reports the DEFAULT tag's machine state.
    tag = pin if pin in KNOWN_MODELS else DEFAULT_MODEL
    live_dl = None
    if state is not None:
        live_dl = (getattr(state, "progress", None) or {}).get("model_download")
    elif record.state == "running" and record.progress_snapshot:
        live_dl = (record.progress_snapshot.get("progress") or {}).get("model_download")
    board = getattr(services, "model_install_board", None)
    return model_install_state(
        tag,
        state_root=services.state_dir,
        board=board,
        progress_download=live_dl if isinstance(live_dl, dict) else None,
    )
```

Binding rules:

- Import `KNOWN_MODELS` too for the pin check; an unknown `options["model"]` is a
  user-path pin (resolve_model_dir's own semantics) — the block reports the DEFAULT
  tag's machine state, which is the honest machine-level answer (the pinned path is
  the user's own directory and needs no installer).
- `services.state_dir` is the attribute `_execute_subs_job` already passes
  (`jobs.py:396`) — the same root Mode B downloads into.
- Docstring must name the derive-at-read discipline and the `api_version`-stays-1
  convention (mirror the `_transcription_echo` docstring's style).

### 2.7 `vidlift/monitor.py` — the additive envelope key

In `status_payload(view)` (123-150), add after `queued_count`:

```python
from .transcribe import model_install_state, DEFAULT_MODEL  # module-top import is fine here — monitor already imports vidlift modules

board = None
client = getattr(view, "_client", None)
services = getattr(client, "services", None)
board = getattr(services, "model_install_board", None)
# In-flight job download beats idle disk truth for the machine block:
in_flight = None
for job in active:                       # `active` is the already-built per-job list
    dl = (job.get("transcription_model") or {})
    if dl.get("state") == "downloading":
        in_flight = dl
        break
payload["transcription_model"] = in_flight or model_install_state(
    DEFAULT_MODEL,
    state_root=getattr(services, "state_dir", None),
    board=board,
)
```

Rules: soft `getattr` chain throughout (foreign clients/fakes must not 500 the
envelope — degrade to the disk-only call with `state_root=None`, which is exactly
`model_cache_dir`'s default); `/health` gains NOTHING; `API_VERSION` stays 1; the
`ENDPOINTS` 404 list is untouched.

### 2.8 `vidlift/gui.py` — pill, Install button, worker, run-panel render (ADR-4)

**a) Widgets (in `__init__`, beside `self.monitor_pill` creation, gui.py:~498):**

```python
self.subs_model_pill = QLabel()           # objectName "VersionBadge" (same chrome)
self.subs_model_button = QPushButton("Install")
self.subs_model_button.setObjectName("ModelInstallButton")  # style hook; small
self.subs_model_button.setToolTip(
    "Download the transcription model (~1.5 GB, one time, sha256-verified) "
    "used by Add subtitles. Downloads in the background; jobs keep running."
)
self.subs_model_button.clicked.connect(self._install_subs_model)
```

**b) Layout:** in `_build_shell` (gui.py:1026-1031), add a horizontal row under the
monitor pill: `title_stack.addWidget(self.subs_model_pill, …)` then a `QHBoxLayout`
row is NOT needed — put the button inline via a small `QWidget`+`QHBoxLayout`
(spacing 6, no margins) containing `subs_model_pill` + `subs_model_button`, added to
`title_stack`. Keep the title-stack pattern (align left).

**c) Refresh — `_refresh_subs_model_pill()`:**

```python
def _refresh_subs_model_pill(self) -> None:
    """ADR-1/4: the disk predicate, re-read on every call (1 Hz while a
    slate lives; on construction; after an install settles). The pill
    NAMES its tag — a config-pinned different model is the job's own pin,
    never this indicator's lie."""
    board = getattr(self.client.services, "model_install_board", None)
    live_dl: dict | None = None
    # an in-flight IN-RUN download (this process's executor) wins over a
    # bare disk read so the pill counts bytes during jobs too; the 1 Hz
    # tick's snapshot is the source — read it from the last polled payload
    # cache if one is kept, else let the tick path call this refresh.
    block = model_install_state(
        TRANSCRIPTION_MODEL_DEFAULT,
        state_root=default_state_dir(),
        board=board,
        progress_download=live_dl,
    )
    # render per the state table below (binding strings)
```

Render contract (binding text — Karen pins these strings):

| state | pill | button |
|---|---|---|
| `installed` | `Subs model ({tag}): installed` | hidden (`setVisible(False)`) |
| `not-installed` | `Subs model ({tag}): not installed — first subtitles use downloads ~1.5 GB` | visible, enabled, text `Install` |
| `downloading` (total known) | `Subs model ({tag}): downloading — {fmt(done)} / {fmt(total)} ({pct}%)` (`format_bytes`, existing gui.py:184) | visible, disabled, text `Downloading…` |
| `downloading` (total None) | `Subs model ({tag}): downloading — {fmt(done)} (total size unknown)` | visible, disabled |
| `unknown` | `Subs model ({tag}): state unknown (cache unreadable)` | visible, enabled `Install` |

Call sites: end of `__init__`; inside `_consume_running_snapshot`'s tick path — pass
the polled snapshot's `progress.get("model_download")` as `live_dl` so the pill counts
bytes during in-run downloads too (piggyback the existing 1 Hz `_poll_jobs`; do NOT
add a second timer — two stat calls plus a dict read are cheap); and after every
install settle (`_on_subs_model_finished`).

**d) The install worker — `_install_subs_model()`:**

```python
def _install_subs_model(self) -> None:
    """ADR-4: GUI-owned install on a QThreadPool runnable (the GPU-meter
    pattern — the GUI's only background idiom), NEVER the serial
    JobExecutor (a 1.5 GB fetch must not starve the queue) and NEVER any
    remote surface (N4). resolve_model_dir is the whole implementation:
    sha256-pinned, lock-serialized, .part-resuming. In-flight click guard
    via self._subs_model_installing."""
    if getattr(self, "_subs_model_installing", False):
        return
    self._subs_model_installing = True
    board = self.client.services.model_install_board
    board.begin(TRANSCRIPTION_MODEL_DEFAULT)
    signals = _ModelInstallSignals()
    signals.progress.connect(self._on_subs_model_progress)
    signals.finished.connect(self._on_subs_model_finished)
    runnable = _ModelInstallRunnable(
        TRANSCRIPTION_MODEL_DEFAULT,
        state_dir=default_state_dir(),
        board=board,
        signals=signals,
        fetch=None,   # tests inject a fake here via the constructor — see §2.9
    )
    QThreadPool.globalInstance().start(runnable)
    self._refresh_subs_model_pill()
```

Module-level (gui.py bottom, beside other helpers) or a new `vidlift/ui/model_install.py`
— **Forge's choice, recommend `vidlift/ui/model_install.py`** for file size; binding
contents:

```python
class _ModelInstallSignals(QObject):
    progress = pyqtSignal(dict)      # {"bytes_done", "bytes_total"}
    finished = pyqtSignal(bool, str) # ok, message

class _ModelInstallRunnable(QRunnable):
    def __init__(self, tag, *, state_dir, board, signals, fetch=None): ...
    def run(self) -> None:
        last = [0.0]
        def on_progress(done, total):
            now = time.time()
            if now - last[0] < 0.25 and total is not None and done < total:
                return
            last[0] = now
            board.update(bytes_done=done, bytes_total=total, file="model.bin")
            signals.progress.emit({"bytes_done": done, "bytes_total": total})
        def on_wait(pid, waited):
            signals.progress.emit({"waiting": True, "holder": pid, "seconds": waited})
        try:
            resolve_model_dir(self.tag, state_root=self.state_dir,
                              fetch=self.fetch,           # None in production → default fetcher
                              on_download_progress=on_progress,
                              on_lock_wait=on_wait)
        except Exception as exc:   # TranscriptionError + OSError family land here
            board.fail(self.tag, str(exc)[:300])
            signals.finished.emit(False, str(exc))
            return
        board.succeed(self.tag)
        signals.finished.emit(True, f"installed {self.tag}")
```

**e) Slots:**

```python
def _on_subs_model_progress(self, info: dict) -> None:
    self._refresh_subs_model_pill()   # the board now carries the truth; one render path

def _on_subs_model_finished(self, ok: bool, message: str) -> None:
    self._subs_model_installing = False
    self._refresh_subs_model_pill()
    if not ok:
        QMessageBox.warning(
            self, "Transcription model install failed",
            f"{message}\n\nThe model was NOT installed. Check the network "
            "connection and try again — the button stays available."
        )
```

**f) Run-panel render (F2's in-run face):** in `run_stage_label` (gui.py:304+), insert
BEFORE the phase dispatch (and note the `transcribe` branch stays as the fallback):

```python
dl = progress.get("model_download")
if isinstance(dl, dict) and phase in ("transcribe", "subtitles"):
    done = dl.get("bytes_done")
    total = dl.get("bytes_total")
    if isinstance(done, (int, float)) and done >= 0:
        if isinstance(total, (int, float)) and total > 0:
            pct = min(100.0, done * 100.0 / total)
            return (f"downloading transcription model — "
                    f"{format_bytes(done)} / {format_bytes(total)} ({pct:.0f}%)")
        return (f"downloading transcription model — "
                f"{format_bytes(done)} (total size unknown)")
```

And in `_consume_running_snapshot` (2122-2270), where the stage bar is set busy for
the transcribe phase: when `progress["model_download"]` has a positive
`bytes_total`, call `self.stage_bar.set_value(int(done * 100 / total))` instead of
`set_busy` for that tick (the value path already exits busy per EtaProgressBar:96).
When total is None, keep `set_busy` — honest indeterminate.

**Do not touch** `_PHASE_BUSY_LABELS`: no new phase is introduced (the download rides
the existing `transcribe`/`subtitles` phases), so the N-2 parity pins stay intact.

### 2.9 Hermes artifact delta (visibility impl spec §3.2's script)

Add one render in `vidlift-status` (per-job lines and the header when idle):

```
subs model: not installed (medium) — open VidLift and click Install
```

from `job["transcription_model"]` (fall back to the envelope key when no active
jobs). Bytes render `done/total (pct%)` when downloading. Never emit an install
command — there is none (N4).

---

## 3. New Tests (Forge creates; nothing existing is touched)

`tests/test_model_management.py` (suggested structure — Karen expands):

1. **Seam conditional arity:** a 2-arg fake fetch still passes `resolve_model_dir`
   with no progress consumer (the F-11 suite's shape; prove untouched-compat); a 3-arg
   fake receives per-chunk callbacks with monotonic `done`, final `done == size`, and
   `total == Content-Length`/`None` cases (spin a `http.server` on 127.0.0.1 —
   conftest's `_no_network` only denies non-loopback).
2. **`resolve_model_dir` progress forwarding:** monkeypatched 3-arg fetch → callback
   observed; `marker` written only after sha pass (existing behavior re-verified down
   the new path).
3. **Lock:** first acquisition creates `.download.lock` and releases on exit; second
   acquisition with a live fresh lock waits then fast-paths when the marker appears
   (fabricate marker+bin inside the wait); stale lock (mtime backdated
   >`MODEL_LOCK_STALE_SECONDS`) is reclaimed; unwritable dir → degrade-open yields
   without raising.
4. **`model_install_state`:** installed/not-installed/unknown shapes (unreadable dir
   via monkeypatched `Path.is_file` raising) with exact key set
   `{"tag","state","cache_dir","bytes_done","bytes_total","file","error"}`; board
   `downloading` beats disk; board `fail` error rides a disk-derived `not-installed`.
5. **jobs.py publisher:** drive `on_download_progress` directly against a fake
   state/record: `progress["model_download"]` populated, `record.touch()` advances
   `last_activity_at`, `on_step("transcribe-language")` pops the fact.
6. **tools.py payload:** synthetic subs record + fake live state with
   `model_download` → `payload["transcription_model"]["state"] == "downloading"`
   with bytes; terminal record + warm cache → `installed`; cold cache →
   `not-installed`; **`api_version`-additivity guard**: pre-existing payload key set
   gains exactly `{"transcription_model"}` (mirror the visibility spec's §2.4 test 5).
7. **monitor envelope:** `_start` a test server: envelope carries
   `transcription_model`; `/health` unchanged; idle + cold cache → `not-installed`;
   a fake board downloading → envelope `downloading` with bytes.
8. **GUI:** construct MainWindow (existing fixtures), fake board states → pill text
   and button visibility per the §2.8c table; `_install_subs_model` with an injected
   fake fetch that feeds `on_progress` then writes the marker+bin → pill ends
   `installed`, button hidden; fake fetch raising `TranscriptionError` → warning box
   path (qtbot), pill back to `not-installed`, `_subs_model_installing` cleared.
9. **run_stage_label:** snapshot with `progress["model_download"]` in both
   `transcribe` and `subtitles` phases renders the download label (with and without
   total).

Then run the FULL suite — it must pass with zero edits to existing tests.

---

## 4. Implementation Order (milestones — each independently testable)

- **M1 — Core seam + lock + board (transcribe.py + §2.2).** Tests 1-4. Acceptance:
  chunked progress provable over loopback HTTP; lock serialize/reclaim/degrade-open
  proven; full suite green unmodified.
- **M2 — Publishers + status surfaces (jobs.py, run.py, tools.py, monitor.py; §2.3-2.7).**
  Tests 5-7. Acceptance: `/status` per-job and envelope carry `transcription_model`;
  a synthetic in-flight download shows `downloading` with bytes and
  `activity_age_seconds` staying small; `api_version == 1`; `/health` untouched.
- **M3 — GUI (§2.8).** Tests 8-9. Acceptance: pill truth at construction and at 1 Hz;
  install end-to-end with a fake fetch (no network); failure path honest; run-panel
  download label in both phases; stage-bar determinate when total known.
- **M4 — Hermes render (§2.9).** Acceptance: script prints the model line for idle
  (envelope) and busy (per-job) servers; no install affordance exists to trigger.

M1 before M2 before M3: surfaces depend on the seam. M4 can join M3's landing.

## 5. Coding Conventions

- Match the module docstring/version-reference style (`v0.4.16 (model management,
  ADR-n): …`) — the repo's living-history voice.
- Honest-null everywhere; never derive a percent when `bytes_total is None`.
- No f-string of secrets into logs (no tokens involved here, but the discovery-card
  rule is adjacent — keep it).
- All new public names into the owning module's `__all__`.
- Cross-platform: `pathlib`/`os` only in the lock; no `fcntl`, no signals, no POSIX
  sleep assumptions; `os.utime` guarded; tests must pass Linux + windows-latest.
- Threading rules: board's lock is mandatory; publishers run on the worker thread and
  only mutate `state.progress`/touch (existing discipline); Qt widgets are touched
  ONLY from slots on the Qt thread (signals carry data, never widget calls).

## 6. Acceptance Criteria (this spec is done when)

1. All four milestones' tests pass; full suite green **with no existing test edited**.
2. Manual: fresh state dir → GUI shows *not installed* + Install; click → pill counts
   bytes to completion → *installed*; a Mode B job against a warm cache shows no
   download label; against a cold cache shows the download label with advancing bytes
   and non-climbing `activity_age_seconds`.
3. `curl -H "Authorization: Bearer …" :8766/status` shows `transcription_model` in the
   envelope and per job; `api_version` still 1; `/health` unchanged.
4. Hermes script renders the model line; no code path exists anywhere to trigger an
   install outside the GUI button (grep: no new tool in `_TOOLS`/registry, no new
   route in monitor, no enqueue kind).
