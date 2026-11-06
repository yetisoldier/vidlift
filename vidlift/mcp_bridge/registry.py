"""The bridge's persisted job registry (mcp-bridge.md Section 6).

One JSON file, ``$VIDLIFT_STATE_DIR/jobs.json``, holding every job the
bridge has enqueued plus the benchmark cache table reference. JSON,
not SQLite, deliberately: the registry is small (hundreds of records),
human-inspectable, and its corrupt-file recovery story ("delete the
file, history is gone, media outputs are unaffected") beats a database
at this scale.

Atomic writes: write-to-temp + ``os.replace`` under one lock — the
registry is the bridge's only mutable shared state. State transitions
are enforced HERE (Section 5.4), not by caller discipline:
``queued -> running -> {succeeded, failed, cancelled}`` and
``queued -> cancelled``; anything else is refused.

Restart semantics: on startup the registry sweeps jobs found in
``running`` state with no live thread owning them — the previous
server died mid-run — and marks them ``failed`` with the binding
restart error. Honest, never silently "succeeded", never silently
dropped. Queued jobs across a restart resume automatically (the
executor picks them up).

v0.4.15.1 (GUI startup sweep): the GUI's entry (``LocalBridgeClient``
-> ``build_services(gui_startup=True)``) runs the sweep in startup
mode, which ALSO fails stale 'queued' records. Rationale: the design's
"queued jobs resume across a restart" is true when another PROCESS
re-attaches (the stdio server's executor is about to run); at GUI
startup the executor thread has not started, so queued records are
every bit as orphaned as running ones — yet ``active_job_count()``
(counts queued+running) then blocks every guarded click (Eric's
2026-11-02 12:43 CST repro: fresh start, empty table, GPU idle, and
Clear popped "Wait for the current upscale…" because stale records
from a crashed session still read as active). Swept records become
visible failed history rows (honest, never dropped); the stdio MCP
server keeps the §6 semantics (queued resumes) unchanged.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path

__all__ = [
    "JOBS_FILENAME",
    "JobRecord",
    "Registry",
    "RESTART_ERROR",
    "STARTUP_SWEEP_ERROR",
    "VALID_TRANSITIONS",
    "BATCH_CANCELLED_ERROR",
]

JOBS_FILENAME = "jobs.json"

RESTART_ERROR = (
    "server restarted during run; outcome unknown, see log"
)

# v0.4.15.1: the error string stamped on records the GUI's STARTUP
# sweep closes (registry's own vocabulary, self-explanatory history
# rows: "stale job from previous session").
STARTUP_SWEEP_ERROR = (
    "stale job from previous session (startup sweep)"
)

# Allowed state machine (mcp-bridge.md Section 5.4), enforced in the
# registry, not by caller discipline.
VALID_TRANSITIONS: dict[str, set[str]] = {
    "queued": {"running", "cancelled"},
    "running": {"succeeded", "failed", "cancelled"},
    # terminal states are terminal
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
}

TERMINAL_STATES = {"succeeded", "failed", "cancelled"}

# v0.4.13 (design §3.4): the per-record error string a queue-cancelled
# job carries — naming the batch action makes history rows
# self-explanatory ("cancelled, never failed" reporting).
BATCH_CANCELLED_ERROR = "batch cancelled by user"


class RegistryError(RuntimeError):
    """The registry is unusable or a transition is illegal."""


class JobRecord:
    """One job: run or benchmark (mcp-bridge.md Section 6)."""

    _extra: dict

    def __init__(
        self,
        *,
        job_id: str,
        kind: str,
        paths: list[str],
        options: dict,
        state: str = "queued",
        submitted_at: float | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
        last_activity_at: float | None = None,
        output_path: str | None = None,
        manifest_path: str | None = None,
        log_path: str | None = None,
        error: str | None = None,
        ok: bool | None = None,
        engine_simulated: bool | None = None,
        engine_note: str | None = None,
        result: dict | None = None,
        progress_snapshot: dict | None = None,
    ) -> None:
        self.job_id = job_id
        self.kind = kind  # "run" | "benchmark"
        self.paths = list(paths)
        self.options = dict(options)
        self.state = state
        self.submitted_at = (
            submitted_at if submitted_at is not None else time.time()
        )
        self.started_at = started_at
        self.finished_at = finished_at
        self.last_activity_at = last_activity_at or self.submitted_at
        self.output_path = output_path
        self.manifest_path = manifest_path
        self.log_path = log_path
        self.error = error
        self.ok = ok
        self.engine_simulated = engine_simulated
        self.engine_note = engine_note
        self.result = result
        # v0.4.9 (progress mirror, design §2.1): throttled mid-run
        # progress detail; None until a worker mirrors it (or an old
        # jobs.json carried one). Never written by anything else.
        self.progress_snapshot = (
            dict(progress_snapshot) if progress_snapshot else None
        )

    # -- state machine ---------------------------------------------------

    def transition(self, new_state: str) -> None:
        allowed = VALID_TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            raise RegistryError(
                f"illegal transition {self.state} -> {new_state} "
                f"for job {self.job_id}"
            )
        self.state = new_state
        self.last_activity_at = time.time()
        if new_state == "running":
            self.started_at = self.last_activity_at
        if new_state in TERMINAL_STATES:
            self.finished_at = self.last_activity_at

    # -- live (in-memory, never persisted) ---------------------------------

    def touch(self) -> None:
        """Bump last-activity: called by the executor's tracked runner
        seam on every external command a live job executes."""
        self.last_activity_at = time.time()
        # v0.4.9 (progress mirror, design §2.1): a touch is the natural
        # liveness tick of a running job — the mirror piggybacks here
        # (throttled inside the executor's _mirror_progress, which owns
        # the 10 s cadence and the state_box read).

    def set_live(self, key: str, value) -> None:
        if not hasattr(self, "_live"):
            self._live = {}
        self._live[key] = value

    def get_live(self, key: str):
        return getattr(self, "_live", {}).get(key)

    def clear_live(self) -> None:
        self._live = {}

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            **getattr(self, "_extra", {}),
            "job_id": self.job_id,
            "kind": self.kind,
            "paths": list(self.paths),
            "options": dict(self.options),
            "state": self.state,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_activity_at": self.last_activity_at,
            "output_path": self.output_path,
            "manifest_path": self.manifest_path,
            "log_path": self.log_path,
            "error": self.error,
            "ok": self.ok,
            "engine_simulated": self.engine_simulated,
            "engine_note": self.engine_note,
            "result": self.result,
            # v0.4.9 (progress mirror, design §2.1): the throttled
            # mid-run progress mirror (chunk i/N, frames done/total,
            # step, phase_note, last_activity). Absent in old files ⇒
            # None ⇒ readers treat as "no live detail" — additive both
            # directions (old readers drop the key on their next save;
            # documented, matches every prior additive field).
            "progress_snapshot": dict(self.progress_snapshot)
            if self.progress_snapshot
            else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobRecord":
        record = cls(
            job_id=str(data["job_id"]),
            kind=str(data.get("kind", "run")),
            paths=list(data.get("paths", [])),
            options=dict(data.get("options", {})),
            state=str(data.get("state", "queued")),
            submitted_at=data.get("submitted_at"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            last_activity_at=data.get("last_activity_at"),
            output_path=data.get("output_path"),
            manifest_path=data.get("manifest_path"),
            log_path=data.get("log_path"),
            error=data.get("error"),
            ok=data.get("ok"),
            engine_simulated=data.get("engine_simulated"),
            engine_note=data.get("engine_note"),
            result=data.get("result"),
            progress_snapshot=data.get("progress_snapshot"),
        )
        record._extra = {key: value for key, value in data.items() if key not in record.to_dict()}
        return record


class _LegacyRegistry:
    """The persisted registry: load, enqueue, mutate, atomic save.

    ``sweep_orphans()`` runs on startup (Section 6's restart rule).
    The in-memory dict is the truth; ``save()`` writes it atomically
    under one lock. A corrupt file raises on load (recovery: delete the
    file; media outputs are unaffected — stated in the error).
    """

    def __init__(self, state_dir: str | Path) -> None:
        self.path = Path(state_dir) / JOBS_FILENAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}
        # Claim-time CancelFlag registration (the D1 fix): the running
        # transition creates the flag under the same lock as the state
        # itself, so a running job ALWAYS has a live cancel flag.
        self._cancel_flags: dict = {}
        self.load()
        # v0.4.5 (D2, progress-honesty design §4): the mtime this
        # process loaded — the baseline every reader's _maybe_reload
        # compares against. A registered ``log_event`` hook (server
        # wiring; optional) receives soft-failure warnings.
        self._loaded_mtime = self._stat_mtime_or_none()
        self.log_event = None

    # -- mtime reload (v0.4.5 §4, D2) ------------------------------------

    def _stat_mtime_or_none(self) -> float | None:
        """The registry file's current mtime, None when absent/unstatable
        (a missing file is the pre-enqueue state, not an error)."""
        try:
            return os.stat(self.path).st_mtime
        except OSError:
            return None

    def _maybe_reload(self) -> None:
        """D2 (§4): converge on other writers' truth before any read.

        One ``stat`` in the common case; a different mtime takes the
        lock and converges via ``_reload_if_changed_unlocked`` (which
        re-stats INSIDE the lock — another copy may have reloaded us
        already — then re-parses and merges by job_id):
        - file rows win over in-memory rows for every persisted field;
        - in-memory ``_live`` boxes and ``_cancel_flags`` survive (they
          are process-local by design);
        - a job in memory but absent from the file is LEFT ALONE (this
          server's own in-flight record, not yet flushed — absence
          means another writer deleted it, which the writer set never
          does; a warning is logged defensively).

        Failure semantics: a failed stat or parse is SOFT — logged,
        in-memory view kept. The hard raise stays at startup ``load()``.
        Test seam: monkeypatch ``_stat_mtime_or_none`` or ``_parse_rows``
        to simulate mtime drift without the filesystem.

        v0.4.15.3 (F-3): the SAME converge body also guards the WRITE
        boundary (``save``/``enqueue``/``transition`` call the unlocked
        body directly under their own lock hold) — a stale-view save
        must never revert another process's newer rows.
        """
        mtime = self._stat_mtime_or_none()
        if mtime == self._loaded_mtime:
            return
        with self._lock:
            self._reload_if_changed_unlocked()

    def _reload_if_changed_unlocked(self) -> None:
        """The D2 converge body with ``_lock`` already held (F-3): the
        re-stat doubles as the double-check — another thread may have
        reloaded us while we waited for the lock, so a stale parse
        never runs twice."""
        mtime = self._stat_mtime_or_none()
        if mtime == self._loaded_mtime:
            return
        try:
            rows = self._parse_rows()
        except Exception as exc:
            self._log_soft(
                f"registry reload failed; keeping in-memory view: {exc}"
            )
            return
        file_ids = set()
        for row in rows:
            record = JobRecord.from_dict(row)
            file_ids.add(record.job_id)
            existing = self._jobs.get(record.job_id)
            if existing is not None and getattr(existing, "_live", None):
                live = existing._live  # process-local, survives reload
            else:
                live = None
            self._jobs[record.job_id] = record
            if live:
                self._jobs[record.job_id]._live = live
        for job_id in self._jobs:
            if job_id not in file_ids:
                self._log_soft(
                    f"registry reload: job {job_id} is in memory but "
                    "absent from the file; leaving it alone (this "
                    "server's in-flight record)"
                )
        self._loaded_mtime = mtime

    def _parse_rows(self) -> list[dict]:
        """The file's job rows parsed strictly; raises for the soft-fail
        path to catch."""
        data = json.loads(self.path.read_text(encoding="utf-8"))
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise ValueError("registry file has no jobs list")
        return jobs

    def _log_soft(self, text: str) -> None:
        """Best-effort observability for soft failures: a ``log_event``
        hook attribute when attached (server wiring sets it), stderr
        otherwise. Never raises (a reload problem must not kill a
        reader)."""
        hook = getattr(self, "log_event", None)
        if hook is not None:
            try:
                hook(f"registry: {text}")
                return
            except Exception:
                pass
        print(f"vidlift-registry: {text}", flush=True)

    # -- lifecycle ---------------------------------------------------------

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(
                f"job registry at {self.path} is unreadable ({exc}); "
                "delete the file to reset (job history only, media "
                "outputs are unaffected)"
            ) from exc
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise RegistryError(
                f"job registry at {self.path} has no jobs list; delete "
                "the file to reset"
            )
        for row in jobs:
            record = JobRecord.from_dict(row)
            self._jobs[record.job_id] = record

    def save(self) -> None:
        """Atomic write-to-temp + os.replace under the lock.

        v0.4.5 (§4): the post-write mtime becomes ``_loaded_mtime``, so
        a save→read sequence does not trigger an unnecessary reload of
        our own write.

        v0.4.15.3 (F-3): converge on other writers' truth BEFORE
        writing; a stale-view save must never revert another process's
        newer rows (the v0.4.5 D2 rule, now applied at the write
        boundary too — Karen's 2026-11-02 validation: a second
        process's wholesale save resurrected ``succeeded → running``
        and dropped freshly enqueued rows).
        """
        with self._lock:
            self._reload_if_changed_unlocked()
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        """The atomic write body, called by ``save()`` under its own
        ``_lock`` hold. ``claim_next``/``cancel_batch`` (v0.4.13) call
        this directly while ALREADY holding ``_lock`` — the lock is a
        plain non-reentrant ``threading.Lock``, so those paths must not
        go through ``save()``; the post-write mtime update keeps their
        save→read sequences from triggering a spurious reload."""
        payload = json.dumps(
            {
                "jobs": [record.to_dict() for record in self._jobs.values()],
            },
            indent=2,
        ) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".jobs-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, self.path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self._loaded_mtime = self._stat_mtime_or_none()

    # -- job operations -------------------------------------------------------

    def new_job_id(self) -> str:
        return f"vl-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

    def enqueue(
        self,
        *,
        kind: str,
        paths: list[str],
        options: dict,
    ) -> JobRecord:
        record = JobRecord(
            job_id=self.new_job_id(),
            kind=kind,
            paths=paths,
            options=options,
        )
        with self._lock:
            # v0.4.15.3 (F-3): converge under the SAME hold as the
            # insert + save — a fresh insert rides converged truth and
            # cannot trigger (or race) the "in memory but absent from
            # file" defensive warning on a stale view.
            self._reload_if_changed_unlocked()
            self._jobs[record.job_id] = record
            self._save_unlocked()
        return record

    def get(self, job_id: str) -> JobRecord | None:
        self._maybe_reload()
        return self._jobs.get(job_id)

    def require(self, job_id: str) -> JobRecord:
        self._maybe_reload()
        record = self._jobs.get(job_id)
        if record is None:
            raise KeyError(job_id)
        return record

    def transition(self, record: JobRecord, new_state: str) -> None:
        """Enforced state machine + atomic persist.

        The D1 fix (layer a): the ``running`` transition creates and
        registers the job's CancelFlag under the same lock that
        enforces the state — the flag exists the instant the job is
        running, so a cancel can never land in the old claim window
        against "no live owner". Terminal transitions pop the flag
        (the close is what makes it dead).

        v0.4.15.3 (F-3): converge → mutate → persist under ONE hold
        (mirrors ``claim_next``'s inlined pattern): the caller's
        pending transition is applied ON TOP of converged truth instead
        of racing it, so a foreign writer's insert/update cannot be
        reverted by this save.
        """
        with self._lock:
            self._reload_if_changed_unlocked()
            # mutate the CANONICAL record — a converge may have
            # re-parsed this job's row from another writer's save, and
            # that re-parsed row is the object the file now knows.
            record = self._jobs.get(record.job_id, record)
            record.transition(new_state)
            if new_state == "running":
                from .jobs import CancelFlag  # deferred: jobs imports registry
                self._cancel_flags[record.job_id] = CancelFlag()
            elif new_state in TERMINAL_STATES:
                self._cancel_flags.pop(record.job_id, None)
            self._save_unlocked()

    def get_cancel_flag(self, job_id: str):
        """The claim-time registered CancelFlag for a running job
        (None before claim or after the job closes)."""
        return self._cancel_flags.get(job_id)

    def claim_next(self) -> JobRecord | None:
        """Claim the oldest queued job (submission order), atomically.

        The v0.4.13 race gate (design §3.3): the view→transition pair
        the executor used to run as two separately-locked steps —
        ``queued()`` snapshotted under the lock and released before the
        ``running`` transition re-acquired it — gave no ordering
        primitive against another writer's marks. Moving BOTH halves
        under ONE ``_lock`` hold serializes the claim against
        ``Registry.cancel_batch`` on the same lock: either the claim
        lands first (the job is legitimately ``running`` and a batch
        cancel flips its flag) or the cancel marks land first (this
        finds nothing claimable from that set). Behavior-preserving
        otherwise: same sorted view, same skip-on-cancelled rule, and
        the claim transition creates/registers the CancelFlag exactly
        the way ``transition("running")`` always has — inlined here
        because ``transition()``/``save()`` acquire ``_lock``
        themselves (a plain, non-reentrant ``threading.Lock``).
        """
        self._maybe_reload()
        with self._lock:
            queued = [
                r for r in self._jobs.values() if r.state == "queued"
            ]
            queued.sort(key=lambda r: r.submitted_at)
            claimed: JobRecord | None = None
            for job in queued:
                try:
                    job.transition("running")
                except RegistryError:
                    continue  # cancelled between view and claim
                from .jobs import CancelFlag  # deferred: jobs imports registry
                self._cancel_flags[job.job_id] = CancelFlag()
                claimed = job
                break
        if claimed is not None:
            with self._lock:
                self._save_unlocked()
        return claimed

    # -- queue views -----------------------------------------------------------

    def queued(self) -> list[JobRecord]:
        # The N1 fix: snapshot under the lock. The worker thread calls
        # this from _claim_next while the MCP message loop enqueues;
        # an unlocked iteration raised RuntimeError mid-enqueue and
        # killed the single worker (D1's consequence class).
        self._maybe_reload()
        with self._lock:
            return [
                r for r in self._jobs.values()
                if r.state == "queued"
            ]

    def queue_position(self, job_id: str) -> int | None:
        """0-based position among queued jobs (submission order), None
        once the job has started."""
        self._maybe_reload()
        record = self._jobs.get(job_id)
        if record is None or record.state != "queued":
            return None
        queued = self.queued()
        queued.sort(key=lambda r: r.submitted_at)
        for position, item in enumerate(queued):
            if item.job_id == job_id:
                return position
        return None

    def running(self) -> list[JobRecord]:
        # Same N1 class: snapshot under the lock (status tool calls
        # this from the message-loop thread while enqueues land).
        self._maybe_reload()
        with self._lock:
            return [
                r for r in self._jobs.values()
                if r.state == "running"
            ]

    def history(self) -> list[JobRecord]:
        # Same N1 class: snapshot under the lock, sort the snapshot.
        self._maybe_reload()
        with self._lock:
            records = [
                r for r in self._jobs.values()
                if r.state in TERMINAL_STATES
            ]
        records.sort(key=lambda r: r.finished_at or 0, reverse=True)
        return records

    # -- restart semantics ---------------------------------------------------

    def sweep_orphans(
        self,
        *,
        error: str = RESTART_ERROR,
        include_queued: bool = False,
    ) -> list[JobRecord]:
        """The Section-6 restart rule: jobs in ``running`` state with no
        live thread owner are the previous server's corpses. They are
        marked ``failed`` with ``error`` (default: the binding restart
        error) — never silently "succeeded", never silently dropped.

        v0.4.15.1 (startup mode, ``include_queued=True``): 'queued'
        records are swept too, with ``error=STARTUP_SWEEP_ERROR`` as
        the GUI passes. Justification: at GUI startup the executor
        thread has not started, so a queued record is exactly as
        orphaned as a running one — and unlike the stdio server (whose
        executor starts seconds later and honestly RESUMES §6's queued
        jobs), the GUI counts queued+running as active and blocks
        every guarded interaction. Swept records stay in the registry
        as failed history rows (honest, never dropped).

        The queued close is a documented state-machine write (a
        privileged bookkeeping close, not a normal transition):
        'queued -> failed' is refused by ``VALID_TRANSITIONS`` (only a
        live owner may move a queued job to running, and the queued
        close paths go through cancel), so the sweep stamps
        ``state/ok/error/finished_at/last_activity_at`` directly and
        deliberately does NOT fabricate ``started_at`` — the job never
        started (same honesty shape as a §3.4 batch-cancelled record,
        with "failed" naming the restart's outcome-unknown instead of
        the user's cancel). Idempotent: terminal records are never
        re-swept, so a second run over a swept registry finds nothing.
        """
        swept: list[JobRecord] = []
        now = time.time()
        for record in list(self._jobs.values()):
            if record.state == "running":
                record.error = error
                record.ok = False
                self.transition(record, "failed")  # running -> failed is legal
                swept.append(record)
            elif include_queued and record.state == "queued":
                # Privileged startup close (see docstring): stamp and
                # persist; no fabricated started_at.
                record.state = "failed"
                record.ok = False
                record.error = error
                record.finished_at = now
                record.last_activity_at = now
                swept.append(record)
        if swept:
            self.save()
        return swept

    def cancel(self, job_id: str) -> str:
        """Cooperative cancel semantics (Section 4.6). Returns one of
        ``cancelling|cancelled|not-found|already-finished``.

        - queued: removed without starting -> ``cancelled``.
        - running: the executor's cancel flag flips; the job finishes
          as cancelled on its own -> ``cancelling``.
        - terminal or unknown: a reported non-event, never an exception.

        v0.4.15.4 (F-7): converge on the file's truth at entry — a job
        another PROCESS enqueued/claimed is file-visible but invisible
        to a stale in-memory view, so the bare read returned a lying
        ``not-found`` (Karen's 2026-11-03 evidence). Downstream arms
        need no further change: the queued arm's ``transition()`` and
        ``save()`` already converge + canonicalize under one lock
        (v0.4.15.3, F-3); the running arm's flag flip now rides a
        converged view.
        """
        # v0.4.15.4 (F-7): converge FIRST (fast-path mtime stat,
        # self-locking), THEN re-read the record — the re-fetch ordering
        # matters because a converge may have re-parsed and replaced the
        # row objects. One extra ``stat`` per cancel: the established
        # cost model (save/get/enqueue all pay it).
        self._maybe_reload()
        record = self._jobs.get(job_id)
        if record is None:
            return "not-found"
        if record.state in TERMINAL_STATES:
            return "already-finished"
        if record.state == "queued":
            self.transition(record, "cancelled")
            return "cancelled"
        # running: the flag flip happens through the claim-time
        # CancelFlag the registry holds a reference to (created at the
        # running transition, popped at the terminal close). A second
        # cancel while one is already in flight is the state_conflict
        # (Section 7): reported, not silent.
        cancel_flag = self._cancel_flags.get(job_id)
        if cancel_flag is not None:
            if cancel_flag.is_set():
                return "conflict"
            cancel_flag.set()
            record.last_activity_at = time.time()
            self.save()
            return "cancelling"
        # Unreachable since claim-time registration (a running job
        # always has a live flag); kept as last-resort defense with the
        # same honest sweep semantics.
        self.transition(record, "cancelled")
        return "cancelled"

    def cancel_batch(self, job_ids, *, include_running: bool = True) -> dict:
        """Cancel an entire batch atomically w.r.t. the claim loop
        (v0.4.13 design §3.2/§3.3).

        Under ONE ``_lock`` hold:
          a) running jobs in the id set -> their claim-time CancelFlags
             flip (collected then set INSIDE the lock; the worker's
             terminal transition happens later, jobs.py's D1 layer b);
          b) queued jobs in the id set -> ``cancelled`` immediately
             (legal per VALID_TRANSITIONS) with
             ``error=BATCH_CANCELLED_ERROR`` and ``ok=False`` (design
             §3.4 — "cancelled, never failed");
          c) terminal/unknown ids -> counted, untouched.

        Returns ``{job_id: "cancelling"|"cancelled"|"already-finished"|"not-found"}``
        — the same vocabulary as ``cancel()``, one entry per requested
        id. ONE save under the same lock (``_save_unlocked``): the file
        reflects batch-cancelled truth before the call returns, and the
        claim loop's next ``claim_next()`` — gated on the same lock —
        finds nothing from the set (the §3.3 total order).

        ``include_running=False`` is the §4.6 variant seam
        (cancel-queued-but-keep-current): only queued jobs are marked
        and the running job keeps running. NOT surfaced by the GUI in
        v0.4.13.

        v0.4.15.4 (F-7 parity): converge on other writers' truth
        BEFORE the batch reads (inside the lock hold) — a stale
        in-memory view marked every file-visible batch row
        ``not-found`` (the identical hole ``cancel()`` had: same
        defect class, same one-line fix).
        """
        id_set = list(job_ids)
        results: dict[str, str] = {}
        flags_to_set = []
        with self._lock:
            # v0.4.15.4 (F-7 parity): the unlocked converge body as the
            # FIRST statement — the re-stat doubles as the
            # double-check, exactly the save/enqueue/transition
            # write-boundary rule (F-3).
            self._reload_if_changed_unlocked()
            for job_id in id_set:
                record = self._jobs.get(job_id)
                if record is None:
                    results[job_id] = "not-found"
                elif record.state in TERMINAL_STATES:
                    results[job_id] = "already-finished"
                elif record.state == "queued":
                    record.transition("cancelled")
                    record.error = BATCH_CANCELLED_ERROR
                    record.ok = False
                    results[job_id] = "cancelled"
                else:  # running (the only non-terminal state left)
                    if include_running:
                        flag = self._cancel_flags.get(job_id)
                        if flag is not None and not flag.is_set():
                            flags_to_set.append(flag)
                            results[job_id] = "cancelling"
                        else:
                            # Double cancel while one is already in
                            # flight — the same conflict semantics
                            # per-job cancel reports.
                            results[job_id] = "conflict"
                    else:
                        results[job_id] = "conflict"
            for flag in flags_to_set:
                flag.set()
            if any(result != "not-found" for result in results.values()):
                self._save_unlocked()
        return results

    def set_cancel_flag(self, job_id: str, flag) -> None:
        if flag is None:
            self._cancel_flags.pop(job_id, None)
        else:
            self._cancel_flags[job_id] = flag


# The legacy implementation above is retained only as a migration reference.
# All production imports use the transaction protocol below. Keep this facade
# in the historical module so callers do not need an import-path migration.
from .registry_transactions import TransactionalRegistry as _TransactionalRegistry


class Registry(_TransactionalRegistry):
    """Compatibility facade enforcing the process-local owner on closes.

    A stale executor may not finish a job after another process has recovered
    or otherwise replaced its claim. The owner identity is captured durably by
    claim_next/transition(running); terminal writers must match it.
    """

    def _assert_owner_for_close(self, job_id: str, new_state: str) -> None:
        if new_state not in TERMINAL_STATES:
            return
        from .registry_transactions import owner_fact
        current = owner_fact()
        with self._transaction() as image:
            row = next((item for item in image["jobs"] if item["job_id"] == job_id), None)
            if row is None:
                raise RegistryError(f"job not found: {job_id}")
            owner = row.get("owner") or {}
            current = owner_fact()
            if row.get("state") == "running" and any(owner.get(key) != value for key, value in current.items()):
                raise RegistryError(f"stale owner cannot close job {job_id}")

    def transition(self, record, new_state):
        if new_state not in TERMINAL_STATES:
            return super().transition(record, new_state)
        from .registry_transactions import owner_fact
        baseline = self._baseline.get(record.job_id, {})
        claim_token = (getattr(record, "_extra", {}).get("owner") or {}).get("claim_token")
        deltas = {k: v for k, v in record.to_dict().items()
                  if v != baseline.get(k) and k != "state"}
        with self._transaction() as image:
            row = next((item for item in image["jobs"] if item["job_id"] == record.job_id), None)
            if row is None:
                raise RegistryError(f"job not found: {record.job_id}")
            owner = row.get("owner") or {}
            if row.get("state") == "running" and (
                not claim_token or owner.get("claim_token") != claim_token
                or any(owner.get(key) != value for key, value in owner_fact().items())
            ):
                raise RegistryError(f"stale owner cannot close job {record.job_id}")
            current = JobRecord.from_dict(row)
            current.transition(new_state)
            row.update(deltas)
            row.update(state=current.state, started_at=current.started_at,
                       finished_at=current.finished_at, last_activity_at=current.last_activity_at)
        self._cancel_flags.pop(record.job_id, None)

    def finish_job(self, job_id, new_state, *, owner_token=None, **fields):
        from .registry_transactions import owner_fact
        with self._transaction() as image:
            row = next((item for item in image["jobs"] if item["job_id"] == job_id), None)
            if row is None:
                raise RegistryError(f"job not found: {job_id}")
            owner = row.get("owner") or {}
            current = owner_fact()
            if row.get("state") == "running" and (not owner_token or owner.get("claim_token") != owner_token or any(owner.get(key) != value for key, value in current.items())):
                raise RegistryError(f"stale owner or missing claim token cannot close job {job_id}")
            current = JobRecord.from_dict(row)
            current.transition(new_state)
            row.update(fields)
            row.update(state=new_state, finished_at=current.finished_at,
                       last_activity_at=current.last_activity_at)
        self._cancel_flags.pop(job_id, None)
