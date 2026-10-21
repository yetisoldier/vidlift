"""Read-only HTTP status API (v0.4.6 design: docs/designs/v0.4.6-status-api.md).

D1 lane (d): cross-machine VISIBILITY over one HTTP GET surface served
from the process that owns the registry — the GUI. No parallel store,
no mirror, no stale window: every response reads the SAME
LocalBridgeClient the GUI's 1 Hz tick reads (one truth, two readers).

Routes (stdlib ``ThreadingHTTPServer``; the sibling bluray-to-mp4
monitor.py's handler/start-stop pattern, VidLift route names):

    GET /health   token-free liveness: version + busy
    GET /status   token-gated live snapshot — ``api_version: 1``, the
                  bridge's own ``_status_payload`` per active job
                  (progress dict, honest-null eta_seconds, log_tail)
    GET /history  token-gated pass-through to the bridge's history()

Anything else is 404 (the body lists the endpoints); any non-GET verb
is 501 — read-only BY CONSTRUCTION: the handler defines only
``do_GET``, and the server holds a :class:`StatusView` facade whose
entire surface is three bridge READ methods. Remote enqueue/cancel
stays out of scope (design §7): that remains minipc-dev-only through
the stdio MCP bridge.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from . import __version__

if TYPE_CHECKING:  # duck-typed at runtime; tests pass fakes
    from .mcp_bridge.client_local import LocalBridgeClient

__all__ = [
    "API_VERSION",
    "ENDPOINTS",
    "MONITOR_TOKEN_DEFAULT",
    "MonitorServer",
    "StatusView",
    "lan_ip",
    "start_server",
    "start_server_with_fallback",
    "status_payload",
    "stop_server",
]

API_VERSION = 1
"""Bumped only on a breaking payload-shape change; additive keys never
bump it (the repo's schema-v2 convention, same as v0.4.1's subtitles
block)."""

ENDPOINTS = ["/health", "/status", "/history"]
"""The 404 body lists these so a curl-ing agent can self-serve."""

# Baked default token (design §3.3): a FRESH uuid4 — deliberately NOT
# the sibling's b2m-mon token (separate blast radii). Override and
# persist via the GUI's QSettings monitor/token key (vidlift/config.py
# holds the default set); rotate if this repo's visibility ever changes.
MONITOR_TOKEN_DEFAULT = "vidlift-mon-0e3f142b-cdee-4fdb-819a-7f65e81c92cd"

_VERSION_NOTE = (
    "payload mirrors the bridge's _status_payload shape; additive keys "
    "land here automatically"
)

_HISTORY_LIMIT = 200  # the client's own clamp (client_local.history)

_VIEW_METHODS = ("status_snapshot", "enqueue_snapshots", "history", "queue_views")
# The read surface a client MUST expose before the monitor will serve
# it (see StatusView.__init__).

_STARTED_AT = time.time()  # process start; /status's uptime_seconds


class StatusView:
    """The monitor's ONLY window on the bridge (design §3.2).

    Exactly three methods — every one a read through the client's
    documented read surface. ``enqueue_run``/``cancel`` and every
    registry WRITE method are structurally unreachable from the server
    path: the facade holds ``_client`` privately and exposes nothing
    else. ``test_status_view_cannot_write`` pins this shape.

    The one level-deep read (active-job enumeration in
    :func:`status_payload`) goes through ``client.queue_views()`` — the
    client's own pure-read views over the shared registry, the same
    lock-snapshotted reads ``status_snapshot()``/``history()`` traverse.

    Construction REFUSES an object that lacks that read surface
    (duck-typing as a guard): wiring a client without the v0.4.6 view
    methods would start a server that lies about what it can serve.
    """

    def __init__(self, client: "LocalBridgeClient") -> None:
        missing = [name for name in _VIEW_METHODS if not callable(getattr(client, name, None))]
        if missing:
            raise TypeError(
                "client lacks the read surface the status API serves "
                f"(missing: {', '.join(missing)}); refusing to start a "
                "monitor over a foreign shape"
            )
        self._client = client

    def status_snapshot(self, job_id: str) -> dict | None:
        """Exactly the bridge ``_status_payload`` output, minus the
        stdio envelope; None for an unknown job."""
        return self._client.status_snapshot(job_id)

    def enqueue_snapshots(self, job_ids: list[str]) -> list[dict | None]:
        """One read pass over several jobs, submission order."""
        return self._client.enqueue_snapshots(job_ids)

    def history(self, limit: int = _HISTORY_LIMIT) -> list[dict]:
        """Pass-through to the client's history rows (newest first)."""
        return self._client.history(limit)


def status_payload(view: "StatusView") -> dict:
    """The GET /status envelope: the bridge's live per-job payloads
    verbatim under an ``api_version`` identity block (design §3.1).

    ``active_jobs[]`` enumerates the registry's running-then-queued
    views through the client's ``queue_views()`` pure-read surface and
    serves ONE ``_status_payload`` per job, unchanged — the same shape
    the GUI's 1 Hz tick consumes, so additive runner keys land here
    automatically. Honesty rides the bridge: ``eta_seconds`` stays
    honest-null per v0.4.5's anti-lie rule, never projected from a stub
    benchmark. ``elapsed_seconds`` is left to the reader
    (``server_time - started_at``); nothing is computed the GUI does
    not compute.
    """
    running, queued = view._client.queue_views()
    active_ids = [record.job_id for record in running]
    active_ids += [record.job_id for record in queued]
    active = [s for s in view.enqueue_snapshots(active_ids) if s is not None]
    return {
        "api_version": API_VERSION,
        "version": __version__,
        "vidlift_version_note": _VERSION_NOTE,
        "queue_active": bool(running),
        "uptime_seconds": round(time.time() - _STARTED_AT, 3),
        "active_jobs": active,
        "queued_count": len(queued),
        "server_time": time.time(),
    }


def _is_busy(view: "StatusView") -> bool:
    """Any running job — the /health ``busy`` bit (a pure-read view)."""
    running, _queued = view._client.queue_views()
    return bool(running)


class _Handler(BaseHTTPRequestHandler):
    """GET-only JSON responder; the sibling _Handler's port (§2.1)."""

    server_version = "vidlift-mon/1.0"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the watcher hung up; nothing to serve anymore

    def _serve(self, produce) -> None:
        """Run ``produce()`` and send its payload with 200 — a
        reader-side exception must never take the connection down
        without a response (the watcher gets an honest 500 with the
        error name instead of a socket reset; the wedged-run diagnosis
        this API exists for must not itself look wedged)."""
        try:
            self._send(200, produce())
        except Exception as exc:  # the honest last resort
            self._send(500, {"error": "status handler failed", "detail": repr(exc)})

    def _authorized(self) -> bool:
        """``Authorization: Bearer <token>`` OR ``?token=***`` query
        (sibling monitor.py:330-334); an empty configured token means
        local-only open."""
        token = getattr(self.server, "token", "")
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {token}":
            return True
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(self.path).query)
        return q.get("token", [""])[0] == token

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("", "/health"):
            # token-free liveness (§3): nothing beyond version/busy.
            self._serve(
                lambda: {
                    "ok": True,
                    "version": __version__,
                    "busy": _is_busy(self.server.view),  # type: ignore[attr-defined]
                }
            )
            return
        if not self._authorized():
            self._send(401, {"error": "missing or invalid token"})
            return
        if path == "/status":
            self._serve(lambda: status_payload(self.server.view))  # type: ignore[attr-defined]
        elif path == "/history":
            self._serve(lambda: {"jobs": self.server.view.history()})  # type: ignore[attr-defined]
        else:
            self._send(404, {"error": "unknown endpoint", "endpoints": ENDPOINTS})

    def log_message(self, *args) -> None:  # keep the console quiet
        pass


class MonitorServer(ThreadingHTTPServer):
    """The ThreadingHTTPServer carrying the StatusView + token (§2.1)."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, addr: tuple[str, int], view: "StatusView", token: str = ""
    ) -> None:
        super().__init__(addr, _Handler)
        self.view = view
        self.token = token


def lan_ip() -> str:
    """Best-effort LAN IPv4 address (no packet is actually sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))  # external route hint; nothing sent
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


def _base_url(host: str, port: int) -> str:
    return f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"


active_server: MonitorServer | None = None
"""The running monitor instance, if any (owned by start/stop_server)."""


def start_server(
    *,
    client: "LocalBridgeClient",
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = MONITOR_TOKEN_DEFAULT,
) -> tuple[MonitorServer, str]:
    """Start the monitor in a background thread; returns (server, base_url).

    The client (the GUI's LocalBridgeClient) is required — the server
    has NO state of its own to serve (one-truth rule, design §2.2).
    Read-only by construction: it can never enqueue, cancel, or change
    configuration. Bind host 0.0.0.0 with a token for LAN monitoring.
    Idempotent: if a server is already running FOR THIS CLIENT
    (started through this helper), it is returned as-is so UI toggles
    can't stack duplicate listeners — a second caller's host/port/
    token are ignored, and the caller's client must be the same
    object (a different client means a new registry epoch: the old
    listener is stopped and restarted on the new truth, see below).
    """
    global active_server
    if active_server is not None:
        if active_server.view._client is client:
            h, p = active_server.server_address
            return active_server, _base_url(h, p)
        # A DIFFERENT client is a new registry epoch (the GUI's client
        # was rebuilt in-process). The running listener's truth source
        # is the OLD client — serving it now would be a stale mirror
        # of a registry nobody owns (the one thing this module must
        # never become). Stop it and serve the NEW truth: same
        # idempotence guarantee, correct reader.
        stop_server()
    server = MonitorServer((host, port), StatusView(client), token)
    thread = threading.Thread(
        target=server.serve_forever, daemon=True, name="vidlift-monitor"
    )
    thread.start()
    active_server = server
    return server, _base_url(*server.server_address)


def start_server_with_fallback(
    *,
    client: "LocalBridgeClient",
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = MONITOR_TOKEN_DEFAULT,
    fallback_attempts: int = 10,
) -> tuple[MonitorServer, str, int]:
    """Start the monitor, moving to a nearby port if the preferred one fails.

    Windows can reserve individual ports for Hyper-V, WSL, VPN clients,
    or system services. When that happens, binding even 127.0.0.1 can
    raise WinError 10013. Keep the monitor available by trying the next
    few ports; callers persist the returned port so the next launch is
    clean (the GUI does exactly that).
    """
    if active_server is not None:
        if active_server.view._client is client:
            server, base = start_server(
                client=client, host=host, port=port, token=token
            )
            return server, base, server.server_address[1]
        # New registry epoch (different client): rebind the reader on
        # the new truth before honoring the call (same guarantee as
        # start_server — the old listener's view must never outlive
        # its client's registry).
        stop_server()

    first_error: OSError | None = None
    attempts = max(0, int(fallback_attempts))
    # Sibling v0.2.29 (Karen's validation): clamp the walk to 65535 —
    # binding above that raises OverflowError, which is NOT an OSError,
    # so it would escape both this handler and the UI's except OSError
    # and crash startup for a configured port near the top of the range.
    for candidate in range(port, min(port + attempts + 1, 65536)):
        try:
            server, base = start_server(
                client=client,
                host=host,
                port=candidate,
                token=token,
            )
            return server, base, server.server_address[1]
        except OSError as exc:
            if first_error is None:
                first_error = exc
    assert first_error is not None
    raise first_error


def stop_server() -> bool:
    """Stop the running monitor (the UI toggle's 'off' path)."""
    global active_server
    if active_server is None:
        return False
    server, active_server = active_server, None
    server.shutdown()
    server.server_close()
    return True