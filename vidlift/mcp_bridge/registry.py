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
    "VALID_TRANSITIONS",
]

JOBS_FILENAME = "jobs.json"

RESTART_ERROR = (
    "server restarted during run; outcome unknown, see log"
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


class RegistryError(RuntimeError):
    """The registry is unusable or a transition is illegal."""


class JobRecord:
    """One job: run or benchmark (mcp-bridge.md Section 6)."""

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
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobRecord":
        return cls(
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
        )


class Registry:
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
        lock, re-stats INSIDE the lock (another copy may have reloaded
        us already), re-parses, and merges by job_id:
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
        """
        mtime = self._stat_mtime_or_none()
        if mtime == self._loaded_mtime:
            return
        with self._lock:
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
        """
        with self._lock:
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
            self._jobs[record.job_id] = record
        self.save()
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
        """
        with self._lock:
            record.transition(new_state)
            if new_state == "running":
                from .jobs import CancelFlag  # deferred: jobs imports registry
                self._cancel_flags[record.job_id] = CancelFlag()
            elif new_state in TERMINAL_STATES:
                self._cancel_flags.pop(record.job_id, None)
        self.save()

    def get_cancel_flag(self, job_id: str):
        """The claim-time registered CancelFlag for a running job
        (None before claim or after the job closes)."""
        return self._cancel_flags.get(job_id)

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

    def sweep_orphans(self) -> list[JobRecord]:
        """The Section-6 restart rule: jobs in ``running`` state with no
        live thread owner are the previous server's corpses. They are
        marked ``failed`` with the binding restart error — never
        silently "succeeded", never silently dropped."""
        swept: list[JobRecord] = []
        for record in list(self._jobs.values()):
            if record.state == "running":
                record.error = RESTART_ERROR
                record.ok = False
                self.transition(record, "failed")  # running -> failed is legal
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
        """
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

    def set_cancel_flag(self, job_id: str, flag) -> None:
        if flag is None:
            self._cancel_flags.pop(job_id, None)
        else:
            self._cancel_flags[job_id] = flag