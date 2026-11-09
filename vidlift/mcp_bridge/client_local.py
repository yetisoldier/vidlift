"""In-process bridge client for the GUI (v0.4.4 design §5).

The GUI starts its runs through the SAME registry and JobExecutor the
``vidlift mcp-server`` stdio entry uses — never a second worker, never
its own cancel flag, never a private status fork. One process, one
registry (``$VIDLIFT_STATE_DIR/jobs.json``), two reader surfaces (the
GUI's Run panel and any MCP agent).

No sockets: the GUI imports this module in-process. The status
contract is THE bridge's ``_status_payload`` — imported directly from
``tools.py`` (design Appendix: fork it and Slice 3 is wrong).

Lifecycle: the GUI owns one :class:`LocalBridgeClient` at app start
(the sweep runs first, then the executor starts), calls
``enqueue_run``/``status_snapshot``/``cancel``/``history`` from the Qt
thread, and stops the executor on application quit.

v0.4.15.1 (startup sweep): the client's construction is THE GUI's
startup sweep — the registry was just loaded (fresh or after a crash)
and the executor has not started, so every 'queued'/'running' record
in the file is a previous session's orphan. They close as ``failed``
with ``STARTUP_SWEEP_ERROR`` and remain as visible failed history
rows; ``active_job_count()`` is therefore 0 at window construction,
and the guards (Clear/Start/cancel) cannot block a fresh start off
the previous session's leftovers (Eric's 2026-11-02 12:43 CST repro:
fresh start, empty table, GPU idle, Clear popped "Wait for the
current upscale…"). The stdio server keeps §6 queue-resume semantics
(its executor starts with the server; a queued job enqueued across a
restart honestly runs).
"""

from __future__ import annotations

import os
from pathlib import Path

from .server import build_services
from .tools import _status_payload

__all__ = [
    "MAX_GUI_BATCH",
    "LocalBridgeClient",
    "default_state_dir",
]

# The GUI refuses to enqueue more than this many files per Start click
# (design §3): a mis-click on a folder of thousands must not silently
# queue hours of GPU work. The plan view remains the honest gate.
# v0.4.18 (WS1, D4): 64 -> 500, ALIGNED with gui.MAX_FILES — one
# batch-size policy (the v0.4.15.4 F-13a principle generalized). The
# reason: a Mode B recursive folder-add of 65-500 files passes
# gui.MAX_FILES and would then die on this per-call raise — the
# feature's flagship case (5 seasons x 20 episodes) failed at enqueue
# with a "run them in rounds" dialog. The cap stays GUI-local (the MCP
# tool_run/tool_transcribe enqueue loops were never capped); worst
# case is ~500 pending registry records (~0.5 MB) drained by the
# existing terminal-retention rules, and the queue table already
# renders 500 rows for Mode A planning.
MAX_GUI_BATCH = 500


def default_state_dir() -> Path:
    """``$VIDLIFT_STATE_DIR`` else the platform state dir.

    Mirrors ``run_gui.py``'s frozen-app default so a packaged GUI and
    any agent-side MCP server land on the SAME registry by default:
    Windows ``%LOCALAPPDATA%\\YetiWurks\\VidLift``, Linux
    ``~/.config/vidlift``, macOS ``~/Library/Application
    Support/VidLift``; the development default (no frozen marker and
    no env var) stays ``~/.vidlift`` — the benchmark cache's own
    default — so dev sessions keep one root."""
    env = os.environ.get("VIDLIFT_STATE_DIR")
    if env:
        return Path(env)
    import sys

    frozen = getattr(sys, "frozen", False)
    if not frozen:
        from ..benchmark import state_dir as benchmark_state_dir

        return benchmark_state_dir()
    import platform

    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData/Local")
        return Path(base) / "YetiWurks" / "VidLift"
    if system == "Darwin":
        return Path.home() / "Library/Application Support/VidLift"
    return Path.home() / ".config" / "vidlift"


class BatchTooLarge(Exception):
    """The Start click exceeded MAX_GUI_BATCH; the GUI shows this as a
    message box, not a crash."""


class LocalBridgeClient:
    """The GUI's single in-process handle on the shared registry/executor."""

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        start_executor: bool = True,
    ) -> None:
        # build_services runs the SAME Registry construction + restart
        # sweep as the stdio server (design §5's restart discipline:
        # a crashed GUI's mid-run job surfaces as failed with the
        # binding restart error on next launch).
        # v0.4.15.1 (startup sweep): the GUI mode also closes stale
        # 'queued' records as failed BEFORE the executor can start and
        # BEFORE any active_job_count() read in the window — the guard
        # (gui.py active_job_count() > 0) then sees a clean registry on
        # a fresh start, never the previous session's leftovers (Eric's
        # 12:43 CST repro; see sweep_orphans' docstring for why the
        # stdio server must NOT do this — its queued jobs resume).
        self.services = build_services(state_dir, gui_startup=True)
        self._owns_executor = start_executor
        if start_executor:
            self.services.executor.start()

    @property
    def registry(self):
        return self.services.registry

    # -- enqueue ----------------------------------------------------------

    def enqueue_run(
        self,
        paths: list[str],
        options: dict | None = None,
        engine: str = "auto",
    ) -> list[str]:
        """Validate paths, set ``options["engine"]``, enqueue one run
        job per path in submission order, wake the executor, and return
        the job_ids in that order so GUI rows map 1:1.

        Engine resolution NEVER happens here (design §3, binding): the
        choice rides the JobRecord's options into ``run_file`` so the
        honesty rules stay centralized in ``select_engine``."""
        opts = dict(options or {})
        opts["engine"] = engine
        clean: list[str] = []
        for path in paths:
            path_str = str(path)
            if not Path(path_str).is_file():
                raise FileNotFoundError(
                    f"no such file on this machine: {path_str}"
                )
            clean.append(path_str)
        if not clean:
            return []
        if len(clean) > MAX_GUI_BATCH:
            raise BatchTooLarge(
                f"{len(clean)} files exceeds the {MAX_GUI_BATCH}-file "
                "per-batch limit; add fewer files and run them in rounds"
            )
        job_ids: list[str] = []
        for path in clean:
            record = self.registry.enqueue(kind="run", paths=[path], options=opts)
            self.services.executor.notify_new_work()
            job_ids.append(record.job_id)
        return job_ids

    # v0.4.14 (Mode B): the GUI's enqueue surface for standalone subs
    # jobs — the EXACT registry/executor path the MCP transcribe tool
    # (tool_transcribe) uses (same kind, same per-file shape). One
    # enqueue surface, never a fork; the GUI's file-picker validation
    # mirrors enqueue_run's (a missing file raises, the picker never
    # shows fabricated rows).
    def enqueue_subs(
        self,
        paths,
        *,
        replace_source: bool = False,
        suppress_srt: bool = False,
        device: str = "auto",
    ) -> list[str]:
        """Validate paths, enqueue one ``subs`` job per path in
        submission order, wake the executor, and return the job_ids in
        that order so GUI rows map 1:1. Options follow tool_transcribe's
        absent-defaults rule.

        v0.4.17: ``replace_source`` (default False — the source-blind
        v0.4.14 behavior) rides into the registry record's options so
        ``_execute_subs_job``'s absent-defaults read picks it up; the GUI
        checkbox feeds it explicitly, and calls without the kwarg are
        behavior-identical to v0.4.16 (options keep carrying the False
        default, never a fabricated ON).

        v0.4.18 (opt-in sidecar suppression): ``suppress_srt`` (default
        False — the KEEP sidecar discipline) rides the same way; calls
        without the kwarg are behavior-identical to v0.4.17."""
        if device not in {"auto", "cuda", "cpu"}:
            raise ValueError("device must be one of auto|cuda|cpu")
        clean: list[str] = []
        for path in paths:
            path_str = str(path)
            if not Path(path_str).is_file():
                raise FileNotFoundError(
                    f"no such file on this machine: {path_str}"
                )
            clean.append(path_str)
        if not clean:
            return []
        if len(clean) > MAX_GUI_BATCH:
            raise BatchTooLarge(
                f"{len(clean)} files exceeds the {MAX_GUI_BATCH}-file "
                "per-batch limit; add fewer files and run them in rounds"
            )
        job_ids: list[str] = []
        for path in clean:
            record = self.registry.enqueue(
                kind="subs",
                paths=[path],
                options={"language": "", "model": None, "device": device,
                         "force": False,
                         "replace_source": bool(replace_source),
                         "suppress_srt": bool(suppress_srt)},
            )
            self.services.executor.notify_new_work()
            job_ids.append(record.job_id)
        return job_ids

    # -- status -------------------------------------------------------------

    def status_snapshot(self, job_id: str) -> dict | None:
        """Exactly the bridge ``_status_payload`` output, minus the
        stdio envelope. Unknown job: None (the GUI reads this once a
        second; the row simply falls back to the registry-less view)."""
        try:
            record = self.registry.require(job_id)
        except KeyError:
            return None
        return _status_payload(self.services, record)

    def enqueue_snapshots(self, job_ids: list[str]) -> list[dict | None]:
        """One tick's read for several jobs, submission order."""
        return [self.status_snapshot(job_id) for job_id in job_ids]

    # -- queue views (v0.4.6: the status API's active-job read) ----------

    def queue_views(self) -> tuple[list, list]:
        """The registry's queued-then-running job records, for readers
        that enumerate active work (the v0.4.6 HTTP monitor's
        ``active_jobs[]``).

        A pure READ over the shared registry — the same
        lock-snapshotted views ``status_snapshot()``/``history()``
        traverse (registry N1 discipline), queued sorted by submission
        order exactly like ``queue_position``. No lock crossing, no
        write surface: the tuples are plain record views.
        """
        registry = self.registry
        running = registry.running()
        queued = sorted(registry.queued(), key=lambda r: r.submitted_at)
        return running, queued

    # -- cancel ---------------------------------------------------------------

    def cancel(self, job_id: str) -> str:
        """Same string values as ``tool_cancel``'s second return:
        ``cancelling | cancelled | not-found | already-finished`` (the
        ``conflict`` case raises BridgeError tool-side; here a double
        cancel degrades to ``cancelling`` since one is already in
        flight). One path: MCP-side cancels land on the same flag."""
        result = self.registry.cancel(job_id)
        if result == "conflict":
            return "cancelling"
        return result

    def cancel_batch(self, job_ids) -> dict[str, str]:
        """``Registry.cancel_batch`` per-id, with the SAME per-job
        conflict-degrade rule as ``cancel()``: a ``conflict`` verdict
        (one flag already in flight) reads as ``cancelling`` to the
        caller — one cancel is already doing the job."""
        return {
            job_id: ("cancelling" if result == "conflict" else result)
            for job_id, result in self.registry.cancel_batch(job_ids).items()
        }

    # -- history -----------------------------------------------------------

    def history(self, limit: int = 20) -> list[dict]:
        """Read-through to ``Registry.history()`` in the ``tool_list``
        history-block shape (same serialization, no fork).

        v0.4.17 (fail-path, the failure-reason surfacing): the record's
        own ``error`` rides every row (additive key, ``api_version``
        untouched — the additive-keys rule in monitor.py). A failed
        row's ``engine_note: null`` never named WHY; the reason already
        persisted on the record (the executor stamps the exception
        text) just wasn't serialized. The HTTP /history pass-through
        and the GUI's recent-rows read this shape verbatim."""
        limit = max(1, min(200, int(limit)))
        history = self.registry.history()
        rows: list[dict] = []
        for record in history[:limit]:
            rows.append(
                {
                    "job_id": record.job_id,
                    "file_path": record.paths[0] if record.paths else None,
                    "state": record.state,
                    "finished_at": record.finished_at,
                    "ok": bool(record.ok),
                    "engine_simulated": bool(record.engine_simulated)
                    if record.engine_simulated is not None
                    else False,
                    "engine_note": record.engine_note,
                    "error": record.error,
                    "output_path": record.output_path,
                    "manifest_path": record.manifest_path,
                }
            )
        return rows

    # -- terminal-state view ------------------------------------------------

    def active_job_count(self) -> int:
        """Jobs not yet in a terminal state (queued + running): the
        Start button re-enables only when this is 0 (design §5's
        concurrency contract)."""
        registry = self.registry
        return len(registry.queued()) + len(registry.running())

    # -- lifecycle --------------------------------------------------------

    def stop(self) -> None:
        """Stop the executor on application quit (running jobs join)."""
        if self._owns_executor:
            self.services.executor.stop()
            self._owns_executor = False