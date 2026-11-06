"""Cooperating-process JSON transactions and durable submission identities.

Lock order: executor lease (optional), local RLock, persistent mutation inode.
Only a committed image is public. Local filesystems and v2 writers required;
old/downgrade writers that discard receipts are not supported.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from ..file_mutex import FileMutex
from .registry import (JobRecord, RegistryError, RESTART_ERROR, STARTUP_SWEEP_ERROR,
                       TERMINAL_STATES, BATCH_CANCELLED_ERROR)

_SESSION = uuid.uuid4().hex


def owner_fact():
    from ..engine_ncnn import _default_process_creation_time
    creation = _default_process_creation_time(os.getpid())
    if os.name == "posix":
        try:
            creation = {"kind": "linux_start_ticks", "value": Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()[19]}
        except (OSError, IndexError):
            pass
    return {"pid": os.getpid(), "creation_time": creation, "session": _SESSION}


def owner_alive(fact):
    from ..engine_ncnn import _default_liveness_probe, _default_process_creation_time
    if not isinstance(fact, dict) or not isinstance(fact.get("pid"), int):
        return None
    pid = fact["pid"]
    alive = _default_liveness_probe(pid)
    if alive is not True:
        return alive
    creation = fact.get("creation_time") or {}
    if creation.get("kind") == "linux_start_ticks":
        try:
            actual = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            return actual == creation.get("value")
        except (OSError, IndexError):
            return None
    if creation.get("kind") == "windows_filetime":
        actual = _default_process_creation_time(pid)
        if actual.get("kind") == creation["kind"]:
            return actual == creation
    return True  # alive but creation identity unavailable: never reclaim


def canonical_payload(kind, path, options):
    if kind not in {"run", "subs"}:
        raise ValueError("GUI submission kind must be run or subs")
    payload = {"payload_version": 1, "kind": kind,
               "source": os.path.normcase(str(Path(path).resolve())),
               "options": copy.deepcopy(options)}
    # Round-trip both validates and detaches all nested input values.
    return json.loads(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))


def payload_hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate registry key")
        result[key] = value
    return result


class TransactionalRegistry:
    """Registry public API backed by one checked interprocess transaction."""
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "jobs.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs = {}
        self._cancel_flags = {}
        self.log_event = None
        self._image = {"schema_version": 2, "generation": 0, "jobs": [], "submissions": {}}
        self._baseline = {}
        self._observed_registry_file = False
        self.load()

    def _stat_mtime_or_none(self):
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def _read_image(self):
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            if self._observed_registry_file:
                raise RegistryError(
                    "Registry disappeared after it was observed; preserve state and "
                    "reconcile exact submission IDs; do not recreate it to retry"
                )
            return {"schema_version": 2, "generation": 0, "jobs": [], "submissions": {}}
        self._observed_registry_file = True
        try:
            image = json.loads(text, object_pairs_hook=_unique_object,
                               parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite value")))
            if not isinstance(image, dict) or not isinstance(image.get("jobs"), list):
                raise ValueError("no jobs list")
            if image.get("schema_version", 2) != 2:
                raise ValueError("unsupported registry schema; old writers/downgrade unsupported")
            image.setdefault("schema_version", 2)
            image.setdefault("generation", 0)
            image.setdefault("submissions", {})
            if not isinstance(image["generation"], int) or not isinstance(image["submissions"], dict):
                raise ValueError("invalid receipt ledger")
            ids = [row["job_id"] for row in image["jobs"]]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate job IDs")
            for key, receipt in image["submissions"].items():
                self._validate_id(key)
                if receipt["submission_id"] != key or receipt["payload_sha256"] != payload_hash(receipt["payload"]):
                    raise ValueError("receipt identity/hash mismatch")
                if receipt["state"] not in {"prepared", "accepted", "rejected", "abandoned"}:
                    raise ValueError("invalid receipt state")
                if receipt["state"] == "accepted" and (len(receipt["job_ids"]) != 1 or receipt["job_ids"][0] not in ids):
                    raise ValueError("accepted receipt references missing job")
            return image
        except (ValueError, TypeError, KeyError) as exc:
            raise RegistryError(f"Registry corrupt or unsupported ({exc}); preserve the registry and reconcile exact submission IDs; do not delete it to retry") from exc

    def _publish(self, image):
        jobs = {}
        for row in image["jobs"]:
            parsed = JobRecord.from_dict(row)
            existing = self._jobs.get(parsed.job_id)
            if existing is not None:
                live = getattr(existing, "_live", None)
                existing.__dict__.update(parsed.__dict__)
                if live:
                    existing._live = live
                parsed = existing
            jobs[parsed.job_id] = parsed
        self._jobs = jobs
        self._image = copy.deepcopy(image)
        self._baseline = {r["job_id"]: copy.deepcopy(r) for r in image["jobs"]}
        self._loaded_mtime = self._stat_mtime_or_none()
        self._observe_cancel_requests()

    def load(self):
        with self._lock:
            self._publish(self._read_image())

    def _parse_rows(self):
        return self._read_image()["jobs"]

    def _maybe_reload(self):
        # Always read authoritative truth, including identical-mtime writes.
        with self._lock:
            self._publish(self._read_image())

    def _reload_if_changed_unlocked(self):
        self._publish(self._read_image())

    def _log_soft(self, text):
        if self.log_event:
            self.log_event(text)

    def _save_unlocked(self):
        image = self._candidate
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".jobs-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(image, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            self._observed_registry_file = True
            if os.name == "posix":
                parent = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    @contextmanager
    def _transaction(self):
        with self._lock:
            mutex = FileMutex(self.path.with_name(self.path.name + ".mutation"))
            try:
                if not mutex.acquire(5.0):
                    raise RegistryError("registry transaction lock unavailable; no enqueue attempted")
                original = self._read_image()
                candidate = copy.deepcopy(original)
                self._candidate = candidate
                try:
                    yield candidate
                    if candidate != original:
                        candidate["generation"] = original["generation"] + 1
                        try:
                            self._save_unlocked()
                        except Exception:
                            # Replace may have committed before a directory flush or
                            # injected post-replace failure. Only exact disk truth
                            # resolves this ambiguity, never active-queue absence.
                            observed = self._read_image()
                            self._publish(observed)
                            if observed != candidate:
                                raise
                        self._publish(candidate)
                    else:
                        self._publish(original)
                except BaseException:
                    self._publish(self._read_image())
                    raise
                finally:
                    self._candidate = None
            finally:
                mutex.release()

    def save(self):
        # Compatibility: merge explicit field deltas, never stale full rows.
        with self._lock:
            deltas = {}
            for key, record in self._jobs.items():
                baseline = self._baseline.get(key)
                if baseline is not None:
                    deltas[key] = {field: copy.deepcopy(value) for field, value in record.to_dict().items()
                                   if value != baseline.get(field)}
            with self._transaction() as image:
                for row in image["jobs"]:
                    delta = deltas.get(row["job_id"], {})
                    baseline = self._baseline.get(row["job_id"], {})
                    # A stale live writer cannot resurrect terminal truth.
                    if row["state"] != baseline.get("state"):
                        continue
                    for field, value in delta.items():
                        if row.get(field) == baseline.get(field):
                            row[field] = value

    def new_job_id(self):
        return f"vl-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"

    def enqueue(self, *, kind, paths, options):
        record = JobRecord(job_id=self.new_job_id(), kind=kind, paths=paths, options=options)
        with self._transaction() as image:
            row = record.to_dict()
            row["origin"] = owner_fact()
            image["jobs"].append(row)
        return self._jobs[record.job_id]

    def get(self, job_id):
        self._maybe_reload()
        return self._jobs.get(job_id)

    def require(self, job_id):
        record = self.get(job_id)
        if record is None:
            raise KeyError(job_id)
        return record

    def update_job(self, job_id, *, expected_state=None, owner_token=None, **fields):
        with self._transaction() as image:
            row = next((r for r in image["jobs"] if r["job_id"] == job_id), None)
            if row is None:
                raise RegistryError(f"job not found: {job_id}")
            if row["state"] == "running" and (not owner_token or row.get("owner", {}).get("claim_token") != owner_token):
                raise RegistryError("stale or missing owner token")
            if expected_state is not None and row["state"] != expected_state:
                raise RegistryError("stale job update refused")
            if "state" in fields:
                raise RegistryError("use transition/finish_job for state changes")
            row.update(copy.deepcopy(fields))
        return self._jobs[job_id]

    def transition(self, record, new_state):
        # Capture result deltas before authoritative reload; do not lose outcome
        # fields written by a legacy library caller on the current record.
        baseline = self._baseline.get(record.job_id, {})
        deltas = {k: v for k, v in record.to_dict().items() if v != baseline.get(k) and k != "state"}
        with self._transaction() as image:
            row = next(r for r in image["jobs"] if r["job_id"] == record.job_id)
            current = JobRecord.from_dict(row)
            current.transition(new_state)
            row.update(deltas)
            row.update(state=current.state, started_at=current.started_at,
                       finished_at=current.finished_at, last_activity_at=current.last_activity_at)
            if new_state == "running":
                row["owner"] = {**owner_fact(), "claim_token": uuid.uuid4().hex}
        if new_state == "running":
            from .jobs import CancelFlag
            self._cancel_flags[record.job_id] = CancelFlag()
        elif new_state in TERMINAL_STATES:
            self._cancel_flags.pop(record.job_id, None)

    def finish_job(self, job_id, new_state, *, owner_token=None, **fields):
        with self._transaction() as image:
            row = next((r for r in image["jobs"] if r["job_id"] == job_id), None)
            if row is None:
                raise RegistryError(f"job not found: {job_id}")
            if row["state"] == "running" and (not owner_token or row.get("owner", {}).get("claim_token") != owner_token):
                raise RegistryError("stale or missing owner token")
            current = JobRecord.from_dict(row)
            current.transition(new_state)
            row.update(copy.deepcopy(fields))
            row.update(state=new_state, finished_at=current.finished_at, last_activity_at=current.last_activity_at)
        self._cancel_flags.pop(job_id, None)

    def claim_next(self):
        claimed = None
        with self._transaction() as image:
            # A live/unknown running owner prevents a parallel executor even
            # when a direct library claimant did not take the executor lease.
            if not any(r["state"] == "running" for r in image["jobs"]):
                queued = sorted((r for r in image["jobs"] if r["state"] == "queued"), key=lambda r: r["submitted_at"])
                if queued:
                    row = queued[0]
                    current = JobRecord.from_dict(row)
                    current.transition("running")
                    row.update(state="running", started_at=current.started_at,
                               last_activity_at=current.last_activity_at,
                               owner={**owner_fact(), "claim_token": uuid.uuid4().hex})
                    claimed = row["job_id"]
        if claimed is not None:
            from .jobs import CancelFlag
            self._cancel_flags[claimed] = CancelFlag()
            self._observe_cancel_requests()
            return self._jobs[claimed]
        return None

    def _observe_cancel_requests(self):
        for row in self._image["jobs"]:
            flag = self._cancel_flags.get(row["job_id"])
            if flag is not None and row.get("cancel_requested"):
                flag.set()

    def poll_cancellation(self):
        self._maybe_reload()

    def get_cancel_flag(self, job_id):
        self._maybe_reload()
        return self._cancel_flags.get(job_id)

    def set_cancel_flag(self, job_id, flag):
        if flag is None:
            self._cancel_flags.pop(job_id, None)
        else:
            self._cancel_flags[job_id] = flag

    def _view(self, states):
        self._maybe_reload()
        with self._lock:
            return [r for r in self._jobs.values() if r.state in states]

    def queued(self):
        return self._view({"queued"})

    def running(self):
        return self._view({"running"})

    def history(self):
        return sorted(self._view(TERMINAL_STATES), key=lambda r: r.finished_at or 0, reverse=True)

    def queue_position(self, job_id):
        queued = sorted(self.queued(), key=lambda r: r.submitted_at)
        return next((i for i, row in enumerate(queued) if row.job_id == job_id), None)

    def sweep_orphans(self, *, error=RESTART_ERROR, include_queued=False, lease_held=False):
        lease = FileMutex(self.path.with_name(self.path.name + ".executor"))
        acquired = False
        swept = []
        try:
            if not lease_held:
                acquired = lease.acquire(0)
                if not acquired:
                    return []
            with self._transaction() as image:
                for row in image["jobs"]:
                    if row["state"] not in ({"running", "queued"} if include_queued else {"running"}):
                        continue
                    fact = row.get("owner" if row["state"] == "running" else "origin")
                    # Legacy records are migrated only with exclusive lease and
                    # no owner fact. Live or unverifiable modern facts stay.
                    if fact is not None and owner_alive(fact) is not False:
                        continue
                    row.update(state="failed", ok=False, error=(RESTART_ERROR if row["state"] == "running" else error),
                               finished_at=time.time(), last_activity_at=time.time())
                    swept.append(row["job_id"])
            return [self._jobs[key] for key in swept]
        finally:
            if acquired:
                lease.release()

    def cancel(self, job_id):
        return self.cancel_batch([job_id]).get(job_id)

    def cancel_batch(self, job_ids, *, include_running=True):
        results = {}
        with self._transaction() as image:
            rows = {r["job_id"]: r for r in image["jobs"]}
            for key in job_ids:
                row = rows.get(key)
                if row is None:
                    results[key] = "not-found"
                elif row["state"] in TERMINAL_STATES:
                    results[key] = "already-finished"
                elif row["state"] == "queued":
                    row.update(state="cancelled", error=BATCH_CANCELLED_ERROR, ok=False,
                               finished_at=time.time(), last_activity_at=time.time())
                    results[key] = "cancelled"
                elif include_running:
                    results[key] = "conflict" if row.get("cancel_requested") else "cancelling"
                    row["cancel_requested"] = True
                else:
                    results[key] = "conflict"
        return results

    @staticmethod
    def _validate_id(key):
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{32}", key) is None:
            raise ValueError("submission_id must be UUID hex")

    def prepare_submission(self, submission_id, payload):
        self._validate_id(submission_id)
        digest = payload_hash(payload)
        with self._transaction() as image:
            receipt = image["submissions"].get(submission_id)
            if receipt is not None:
                if receipt["payload_sha256"] != digest:
                    raise RegistryError("idempotency_conflict")
            else:
                now = time.time()
                receipt = {"submission_id": submission_id, "payload_version": 1,
                           "payload": copy.deepcopy(payload), "payload_sha256": digest,
                           "state": "prepared", "job_ids": [], "created_at": now,
                           "updated_at": now, "origin": owner_fact()}
                image["submissions"][submission_id] = receipt
        return copy.deepcopy(receipt)

    def enqueue_idempotent(self, submission_id, payload):
        self._validate_id(submission_id)
        digest = payload_hash(payload)
        with self._transaction() as image:
            receipt = image["submissions"].get(submission_id)
            if receipt is not None and receipt["payload_sha256"] != digest:
                raise RegistryError("idempotency_conflict")
            if receipt is None:
                raise RegistryError("submission must be prepared before commit")
            if receipt["state"] == "prepared":
                record = JobRecord(job_id=self.new_job_id(), kind=payload["kind"],
                                   paths=[payload["source"]], options=payload["options"])
                row = record.to_dict()
                row["origin"] = owner_fact()
                row["submission_id"] = submission_id
                image["jobs"].append(row)
                receipt.update(state="accepted", job_ids=[record.job_id], updated_at=time.time())
        return copy.deepcopy(receipt)

    def lookup_submission(self, submission_id):
        self._validate_id(submission_id)
        self._maybe_reload()
        return copy.deepcopy(self._image["submissions"].get(submission_id))

    def list_submissions(self):
        self._maybe_reload()
        return copy.deepcopy(list(self._image["submissions"].values()))

    def abandon_submission(self, submission_id):
        self._validate_id(submission_id)
        with self._transaction() as image:
            receipt = image["submissions"][submission_id]
            if receipt["state"] == "prepared":
                receipt.update(state="abandoned", updated_at=time.time())
