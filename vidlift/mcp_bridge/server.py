"""The stdio MCP server loop (mcp-bridge.md Sections 3, 5, 7).

Transport: MCP over stdio — JSON-RPC 2.0 messages, ONE JSON object per
line (newline framing), UTF-8. stdout carries ONLY protocol frames;
all bridge diagnostics go to stderr and the bridge's small rotating
log. No network listener ever opens (Section 2 binding non-goal).

Process model: one server per MCP client session. On stdin EOF the
server stops accepting new tool calls but does NOT kill running jobs
— jobs belong to the registry, and a later server instance re-attaches
through the persisted registry (Section 6's restart sweep covers the
crash case).

Dispatch (Section 5.2): JSON-RPC decode -> schemas.validate -> tools
adapter -> vidlift.* -> serialize the Section-4 payload. Errors carry
``isError: true`` with the human message plus the machine-readable
``code`` (Section 7 table).

v0.4.4 (GUI/status/MCP unification design §5): :func:`build_services`
extracts the registry/executor construction so BOTH the stdio server
and the GUI's in-process client (``client_local.py``) share one
construction path — same objects in both worlds, no fork of behavior.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from .jobs import JobExecutor
from .registry import Registry
from .schemas import InvalidInput, TOOL_DESCRIPTIONS, TOOL_SCHEMAS, validate_tool_args
from .tools import BridgeError, BridgeServices, TOOL_HANDLERS
from ..run import MissingToolError

__all__ = [
    "JSONRPCError",
    "McpServer",
    "PROTOCOL_VERSION",
    "SERVER_INFO",
    "TOOLS_LIST",
    "build_services",
    "serve_stdio",
]

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "vidlift", "version": "0.2.0"}


class BridgeLog:
    """The bridge's small rotating server-level log (Section 5.3).

    Plain text, capped (default 5 MB), rotating to a single ``.1``
    sibling: startup, client connect/disconnect, and job lifecycle
    transitions. Run logs stay where the runner already puts them —
    this log is bridge events ONLY, never a duplicate of run output.
    """

    def __init__(self, path: Path, *, max_bytes: int = 5 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + text + "\n"
        try:
            with self._lock:
                try:
                    size = self.path.stat().st_size
                except OSError:
                    size = 0
                if size + len(line) > self.max_bytes:
                    rotated = self.path.with_suffix(self.path.suffix + ".1")
                    try:
                        rotated.unlink()
                    except OSError:
                        pass
                    try:
                        self.path.replace(rotated)
                    except OSError:
                        pass
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        except OSError:
            pass  # diagnostics never break the server

# The registered surface tools/list reports: the pinned schemas ARE the
# validated schemas (one source of truth; the v0.2.32 lesson).
TOOLS_LIST = [
    {
        "name": name,
        "description": TOOL_DESCRIPTIONS[name],
        "inputSchema": schema,
    }
    for name, schema in TOOL_SCHEMAS.items()
]


def _default_state_dir(explicit: str | Path | None) -> Path:
    """``$VIDLIFT_STATE_DIR`` else ``~/.vidlift`` — the same resolution
    the benchmark cache uses, so the benchmark truth and the job
    registry always share one root."""
    if explicit is None:
        from ..benchmark import state_dir as default_state_dir

        return default_state_dir()
    return Path(explicit)


def _scratch_roots_for_gc(registry) -> list[Path]:
    """The distinct log roots referenced by the registry's job records
    (v0.4.9 §1.6 startup GC): options["log_dir"] when a job set one,
    else the recorded source's sibling .vidlift-runs. Best-effort
    reads; missing paths are skipped by the GC itself."""
    roots: list[Path] = []
    seen: set[str] = set()
    try:
        records = list(registry.history())
    except Exception:
        return roots
    for record in records:
        try:
            explicit = (record.options or {}).get("log_dir")
            if explicit:
                root = Path(str(explicit))
            elif record.paths:
                root = Path(record.paths[0]).parent / ".vidlift-runs"
            else:
                continue
        except Exception:
            continue
        key = str(root)
        if key not in seen:
            seen.add(key)
            roots.append(root)
    return roots


def build_services(state_dir: str | Path | None = None) -> "BridgeServices":
    """The v0.4.4 unification factory (design §5): one construction
    path for the bridge's services object, used by BOTH the stdio MCP
    server's entry and the GUI's in-process client.

    Builds the registry at ``state_dir`` (or ``$VIDLIFT_STATE_DIR`` or
    the platform default), runs the restart sweep exactly as the MCP
    server always has, and wires a :class:`JobExecutor` around it. The
    executor is constructed but NOT started: the server starts it in
    ``McpServer.__init__`` (worker threads owned there); the GUI client
    starts it after the Qt app is up and stops it on quit. Test seams
    on the returned services object (probe/command runners) stay None
    — production wiring, the real subprocesses."""
    from .tools import BridgeServices

    resolved = _default_state_dir(state_dir)
    registry = Registry(resolved)
    # Restart sweep FIRST (Section 6): the previous process's running
    # jobs are corpses; they become failed with the binding restart
    # error, never silently succeeded. Identical for both consumers.
    registry.sweep_orphans()
    # v0.4.9 (design §1.6): the scratch GC runs beside the restart
    # sweep — retained scratch becomes durable state with resume
    # shipped, and startup is where stale sets are judged and removed
    # (best-effort, logged; never fatal to startup). The GC walks the
    # log roots the REGISTRY's records reference (each job's
    # options["log_dir"], else the source's .vidlift-runs).
    from ..run import gc_stale_scratch

    try:
        for root in _scratch_roots_for_gc(registry):
            gc_stale_scratch(root)
    except Exception as exc:  # startup robustness: log and continue
        print(
            f"vidlift-registry: scratch GC skipped ({exc})", flush=True
        )
    services = BridgeServices(registry=registry, state_dir=resolved)
    services.executor = JobExecutor(services)
    return services


class JSONRPCError(Exception):
    """A protocol-level JSON-RPC error (bad framing/parse), carrying
    the JSON-RPC error code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _bridge_error_payload(exc: BridgeError) -> dict:
    """isError content block with the machine-readable code (Section 7)."""
    text = {"code": exc.code, "message": str(exc)}
    if exc.payload:
        text["payload"] = exc.payload
    return {
        "isError": True,
        "content": [
            {"type": "text", "text": json.dumps(text, ensure_ascii=False)}
        ],
    }


def _result_payload(data: dict) -> dict:
    """A successful tool result: one text content block, UTF-8 JSON."""
    return {
        "content": [
            {"type": "text", "text": json.dumps(data, ensure_ascii=False)}
        ],
    }


class McpServer:
    """The MCP session object: owns the registry, executor, services,
    and the message loop driver. ``serve()`` runs until stdin EOF.

    ``read``/``write`` default to real stdin/stdout (line-framed);
    tests drive the same object over a pipe pair — the v0.2.32 rule
    that the boundary test exercises the real server, not a mock of it.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        services: BridgeServices | None = None,
        registry: Registry | None = None,
        executor: JobExecutor | None = None,
        stderr=sys.stderr,
        start_workers: bool = True,
    ) -> None:
        if state_dir is None:
            from ..benchmark import state_dir as default_state_dir

            state_dir = default_state_dir()
        self.state_dir = Path(state_dir)
        self.registry = registry or Registry(self.state_dir)
        # Restart sweep FIRST (Section 6): the previous server's
        # running jobs are corpses; they become failed with the
        # binding restart error, never silently succeeded.
        self.registry.sweep_orphans()
        # Wire services.registry BEFORE the executor reads it: the
        # executor's worker threads claim jobs through it.
        if services is None:
            services = BridgeServices(
                registry=self.registry, state_dir=self.state_dir
            )
        else:
            services.registry = self.registry
        self.executor = executor or JobExecutor(services)
        services.executor = self.executor
        self.services = services
        self.stderr = stderr
        self.bridge_log = BridgeLog(self.state_dir / "mcp-server.log")
        self.services.log_event = self._log_event
        # v0.4.5 (D2): the registry's soft-failure warnings (reload
        # parse errors, defensively-kept in-flight rows) ride the same
        # rotating bridge log.
        self.registry.log_event = self._log_event
        self._log_lock = threading.Lock()
        self._requests_handled = 0
        if start_workers:
            self.executor.start()

    # -- diagnostics (stderr only; stdout is protocol-only) -----------

    def _diag(self, text: str) -> None:
        line = f"[vidlift-mcp {time.strftime('%H:%M:%S')}] {text}"
        try:
            with self._log_lock:
                print(line, file=self.stderr, flush=True)
        except OSError:
            pass

    def _log_event(self, text: str) -> None:
        """Bridge-level event into the rotating log (Section 5.3): the
        executor calls this for job lifecycle transitions."""
        self.bridge_log.write(text)

    # -- protocol framing ----------------------------------------------

    def _write_frame(self, write, message: dict) -> None:
        write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))

    @staticmethod
    def _parse_frame(raw: bytes) -> dict:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            raise JSONRPCError(-32600, "empty request frame")
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JSONRPCError(-32700, f"parse error: {exc}") from exc
        if not isinstance(message, dict):
            raise JSONRPCError(-32600, "request must be a JSON object")
        return message

    # -- request handling ------------------------------------------------

    def _error_response(self, request_id, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def _result_response(self, request_id, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _handle_request(self, message: dict) -> dict | None:
        """One decoded JSON-RPC request -> its response (None for
        notifications)."""
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(method, str):
            return self._error_response(
                request_id, -32600, "request missing 'method'"
            )
        if method.startswith("notifications/"):
            return None
        if method == "initialize":
            return self._result_response(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            )
        if method == "ping":
            return self._result_response(request_id, {})
        if method == "tools/list":
            return self._result_response(
                request_id, {"tools": TOOLS_LIST}
            )
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                return self._error_response(
                    request_id, -32602, "tools/call params missing tool name"
                )
            arguments = params.get("arguments") or {}
            handler = TOOL_HANDLERS.get(name)
            if handler is None:
                return self._result_response(
                    request_id,
                    _bridge_error_payload(
                        BridgeError("invalid_input", f"unknown tool: {name}")
                    ),
                )
            try:
                validate_tool_args(name, arguments)
            except InvalidInput as exc:
                return self._result_response(
                    request_id,
                    _bridge_error_payload(
                        BridgeError("invalid_input", str(exc))
                    ),
                )
            try:
                data = handler(self.services, arguments)
            except BridgeError as exc:
                self._diag(f"tools/call {name}: {exc.code}: {exc}")
                return self._result_response(
                    request_id, _bridge_error_payload(exc)
                )
            except MissingToolError as exc:
                return self._result_response(
                    request_id,
                    _bridge_error_payload(
                        BridgeError("missing_tool", str(exc))
                    ),
                )
            except Exception as exc:  # internal: message + stderr pointer
                self._diag(f"tools/call {name} internal error: {exc}")
                return self._result_response(
                    request_id,
                    _bridge_error_payload(
                        BridgeError(
                            "internal",
                            f"{type(exc).__name__}: {exc} (see the bridge "
                            "stderr log)",
                        )
                    ),
                )
            self._requests_handled += 1
            return self._result_response(request_id, _result_payload(data))
        return self._error_response(
            request_id, -32601, f"method not found: {method}"
        )

    # -- the loop -----------------------------------------------------------

    def serve(self, read=None, write=None) -> None:
        """Run the message loop until EOF (Section 3's process
        lifetime). Running jobs are never killed on disconnect; the
        worker threads are daemonized so process exit drains them per
        the registry persistence contract."""
        if read is None:
            read = sys.stdin.buffer.readline
        if write is None:
            def write(frame: bytes) -> None:
                sys.stdout.buffer.write(frame)
                sys.stdout.buffer.flush()

        self._diag(f"server up; state dir {self.state_dir}")
        try:
            while True:
                raw = read()
                if not raw:  # EOF: the client disconnected
                    self._diag("client disconnected (stdin EOF)")
                    break
                if isinstance(raw, str):
                    raw = raw.encode("utf-8")
                try:
                    message = self._parse_frame(raw)
                except JSONRPCError as exc:
                    # a framing-level error never kills the server (a
                    # chatty client must not wedge the loop); the id is
                    # unknowable at this point, so it is null in the
                    # error response
                    self._diag(f"frame error: {exc}")
                    self._write_frame(
                        write,
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": exc.code, "message": str(exc)},
                        },
                    )
                    continue
                try:
                    response = self._handle_request(message)
                except Exception as exc:  # never let one request die the loop
                    self._diag(f"handler crashed: {exc}")
                    response = self._error_response(
                        message.get("id"),
                        -32603,
                        f"internal error: {exc}",
                    )
                if response is not None:
                    self._write_frame(write, response)
        finally:
            # stop accepting new work; running jobs continue on the
            # daemon workers until process exit drains them
            self._diag("server stopping; registry persisted")
            try:
                self.registry.save()
            except Exception as exc:
                self._diag(f"registry save on exit failed: {exc}")

    # -- in-process drive (tests + the entry point's single-shot path) ----

    def handle_line(self, raw: str | bytes) -> str | None:
        """Serve exactly one line and return the response line (or
        None for notifications). The t-series tests drive the real
        dispatch through this without a subprocess."""
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        message = self._parse_frame(raw)
        response = self._handle_request(message)
        if response is None:
            return None
        return json.dumps(response, ensure_ascii=False)


def serve_stdio(
    *,
    state_dir: str | Path | None = None,
) -> None:
    """The ``vidlift mcp-server`` entry: construct and serve on the
    real stdio streams. Diagnostics to stderr; stdout only frames.

    v0.4.4: the services object comes from :func:`build_services` —
    the same factory the GUI's in-process client uses (design §5), so
    both consumers share one registry/executor construction path."""
    services = build_services(state_dir)
    server = McpServer(
        state_dir=services.state_dir,
        services=services,
        registry=services.registry,
        executor=services.executor,
        start_workers=True,
    )
    server.serve()
