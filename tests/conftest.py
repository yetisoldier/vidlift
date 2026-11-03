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

import os
from pathlib import Path

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


# v0.4.16 (hermeticity, forge task B): the suite's DEFAULT state-root
# resolution never lands on the real user profile ``~/.vidlift``.
#
# Every production default resolution (``benchmark.state_dir()``) is
# lazy-imported at CALL time by its consumers (the machine GPU-phase
# lock, the model cache, the bridge's registry/monitor-card roots), so
# one autouse patch makes each test's "default root" an isolated tmp
# directory instead of real user state: no test run may ever churn
# (create/unlink) ``~/.vidlift/.gpu-phase.lock``, warm-read
# ``~/.vidlift/whisper/models``, or (were a GUI path under test) write
# ``monitor.json`` into the user profile.
#
# Precedence is preserved exactly: an explicit argument wins, an
# explicit ``$VIDLIFT_STATE_DIR`` wins second (env-pinned tests keep
# passing), and ONLY the silent fallback (which used to be
# ``Path.home() / ".vidlift"``) is redirected. The redirected root
# carries the seeded transcription-model state the resolve fast-path
# contract promises — the Mode A/seam tests resolve the model through
# the default root and, on this dev box, previously read the dev
# session's warm real cache; seeding the isolated root keeps their
# resolution behavior identical (fast path, zero network) now that the
# default root is hermetic.
@pytest.fixture(autouse=True)
def _hermetic_state_root(tmp_path_factory, monkeypatch):
    from vidlift import benchmark as benchmark_mod

    # A fixture-PRIVATE tmp dir (tmp_path_factory), NOT the test's own
    # tmp_path: some existing tests assert their tmp_path holds exactly
    # N entries after a run — a sibling seeded root inside it would
    # break the pin.
    root = tmp_path_factory.mktemp("vidlift-state")
    seed_model_state(root)

    def _isolated_state_dir(explicit=None):
        if explicit:
            return Path(explicit)
        env = os.environ.get("VIDLIFT_STATE_DIR")
        if env:
            return Path(env)
        return root

    monkeypatch.setattr(benchmark_mod, "state_dir", _isolated_state_dir)


def seed_model_state(state_root, tag: str = "medium") -> dict:
    """The machine model-cache state the resolve fast-path contract
    promises: ``.complete`` marker + ``model.bin`` (the contract checks
    ONLY the marker's presence + model.bin's existence — resolve skips
    all digest/size reads on this path, sha256 stays a download-time
    gate). The support files ride along so the seeded cache matches the
    real repos' file set. Returns the seeded model dir; tests that
    assert cold-cache shapes pass their own explicit empty ``state_dir``
    instead."""

    import hashlib

    model_dir = Path(state_root) / "whisper" / "models" / tag
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    (model_dir / "model.bin").write_bytes(b"stub-model-bin")
    (model_dir / "vocabulary.txt").write_text("", encoding="utf-8")
    (model_dir / ".complete").write_text(
        f"{tag}\n{hashlib.sha256(b'stub-model-bin').hexdigest()}\n",
        encoding="utf-8",
    )
    return model_dir

