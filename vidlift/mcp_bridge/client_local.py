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
(the executor starts when the client is constructed), calls
``enqueue_run``/``status_snapshot``/``cancel``/``history`` from the Qt
thread, and stops the executor on application quit.
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
MAX_GUI_BATCH = 64


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
        self.services = build_services(state_dir)
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

    # -- history -----------------------------------------------------------

    def history(self, limit: int = 20) -> list[dict]:
        """Read-through to ``Registry.history()`` in the ``tool_list``
        history-block shape (same serialization, no fork)."""
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