"""Shared test fixtures.

One autouse guard: auto engine selection must resolve to the STUB in
every test on every box. Without this, a machine with a bundled
tools/realesrgan/ layout (Eric's Windows checkout after the packaging
milestone) would make ``run_file(engine_request="auto")`` launch REAL
engine probe frames inside unit tests. The guard pins
VIDLIFT_REALESRGAN_NCNN to a nonexistent path so ``select_engine``
deterministically falls to the stub with its named reason; tests that
exercise real resolution set their own env/binary explicitly and
override this per-test.
"""

from __future__ import annotations

import pytest

_GUARD_PATH = "/nonexistent/vidlift-test-guard/realesrgan-ncnn-vulkan"


@pytest.fixture(autouse=True)
def _auto_engine_guard(monkeypatch):
    monkeypatch.setenv("VIDLIFT_REALESRGAN_NCNN", _GUARD_PATH)


@pytest.fixture(autouse=True)
def _no_monitor_autostart(monkeypatch):
    # v0.4.6: the GUI's LAN monitor ships ON by default; tests isolate
    # QSettings but a construction-time listener must never open in a
    # suite run (nor write the port fallback back to real user
    # QSettings). Monitor-specific tests re-enable per-test.
    monkeypatch.setenv("VIDLIFT_MONITOR_AUTOSTART", "0")

# v0.4.14: transcription engine hard-off in the suite (the design's test
# rule: the suite NEVER downloads the model or needs a GPU). Tests that
# exercise engine resolution set their own seam/env explicitly and
# override per-test.
@pytest.fixture(autouse=True)
def _no_transcription_engine(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("VIDLIFT_TRANSCRIPTION_ENGINE", "0")


@pytest.fixture
def transcription_engine_on(monkeypatch):
    """The v0.4.14 tests build plans under the env guard; tests that
    exercise Mode A's default-ON plan seam need it OFF."""
    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)

# v0.4.15 (release-gate hardening): the suite is LOCAL-ONLY by design
# — no test legitimately needs the network. The v0.4.14 release gate
# failed inside systemd transient units on a REAL model download a
# test triggered through unseeded model resolution (interactive shells
# silently downloaded ~1.5 GB; the units' minimal environment lacked
# the CA bundle, so the same touch failed the job). This guard turns
# every such leak into a named, immediate failure instead of a
# timing/CI-environment lottery:
#   - urllib.request.urlopen raises RuntimeError("test touched the
#     network") — the one prod network path (transcribe.py's download
#     seam) and any accidental direct URL fetch die loudly.
#   - TCP (SOCK_STREAM) connects to loopback and AF_UNIX sockets stay
#     OPEN: the monitor suite serves its in-process HTTP server on
#     127.0.0.1 and drives it with real sockets (loopback is not the
#     network; a loopback-only listener never leaves the host).
#     Everything else — any external socket.connect — raises the same
#     named error.
#   - connection-style helpers route through the patched socket, and
#     SOCK_DGRAM connects send no packets regardless (the GUI's
#     lan_ip() route hint rides untouched).
# A test that legitimately needs a resource must seed/mock it (the
# model-download discipline): fix the test, never the guard.
import socket as _socket
import urllib.parse as _urllib_parse
import urllib.request as _urllib_request

_real_urlopen = _urllib_request.urlopen


class _TestTouchedNetwork(RuntimeError):
    """The named error every network-ward raises."""


def _deny_urlopen(url, *args, **kwargs):
    # Loopback hosts are not the network (the monitor suite drives its
    # in-process HTTP server with real urlopen calls); anything else
    # dies with the named error.
    try:
        if isinstance(url, _urllib_request.Request):
            raw_host = url.host
            if raw_host and ":" in raw_host and raw_host.count(":") == 1:
                raw_host = raw_host.rsplit(":", 1)[0]  # host:port
        else:
            raw_host = _urllib_parse.urlsplit(str(url)).hostname
    except ValueError:
        raw_host = None
    host = raw_host
    if not (
        host
        and (
            host in ("127.0.0.1", "::1", "localhost")
            or (isinstance(host, str) and host.startswith("127."))
        )
    ):
        raise _TestTouchedNetwork(
            f"test touched the network: urllib.request.urlopen({url!r}) — "
            "the suite is local-only; seed or mock the resource instead"
        )
    return _real_urlopen(url, *args, **kwargs)


def _guard_socket_connect(original_connect):
    def connect(self, address):
        if self.type == _socket.SOCK_STREAM and isinstance(
            address, tuple
        ) and len(address) == 2:
            host, _port = address
            if not (
                host in ("127.0.0.1", "::1", "localhost")
                or (isinstance(host, str) and host.startswith("127."))
            ):
                raise _TestTouchedNetwork(
                    f"test touched the network: socket.connect({address!r}) "
                    "— the suite is local-only; seed or mock the resource "
                    "instead"
                )
        return original_connect(self, address)

    return connect


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(_urllib_request, "urlopen", _deny_urlopen)
    monkeypatch.setattr(
        _socket.socket, "connect",
        _guard_socket_connect(_socket.socket.connect),
    )

