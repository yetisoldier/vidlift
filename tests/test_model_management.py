"""v0.4.16 transcription-model management (impl spec §3, tests 1-7).

Everything runs against injected fakes or a LOOPBACK-ONLY http.server
(conftest's ``_no_network`` guard permits 127.0.0.1) — never the real
model, never the network beyond the loopback, never ``~/.vidlift``.
The GUI block lives in ``tests/test_model_management_gui.py``.

Sections (mirroring the validation spec's matrices):
  - the fetch seam (M-1..M-7): conditional-arity discipline, per-chunk
    beats, truncation/sha honesty, ``.part`` short-circuit;
  - the per-tag download lock (L-1..L-5) + heartbeat (L-6);
  - ``model_install_state`` derivation (D-1..D-9);
  - the Mode B publisher (M-8 / H-1 / H-3 unit level: populate, touch,
    pop, thread through run_subs_job);
  - the per-job payload + /status envelope (S-matrix, api_version
    stability, additivity guard, soft-read tolerance).
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import vidlift.transcribe as transcribe_mod
from vidlift.mcp_bridge.client_local import LocalBridgeClient
from vidlift.mcp_bridge.jobs import _execute_subs_job, _model_download_publisher, CancelFlag, JobOutcome
from vidlift.mcp_bridge.registry import Registry
from vidlift.mcp_bridge.tools import BridgeServices, _status_payload
from vidlift.monitor import (
    API_VERSION,
    MONITOR_TOKEN_DEFAULT,
    start_server,
    status_payload,
    stop_server,
)
from vidlift.transcribe import (
    DEFAULT_MODEL,
    MODEL_LOCK_STALE_SECONDS,
    MODEL_SHA256,
    ModelInstallBoard,
    TranscriptionError,
    _default_fetcher,
    _download_file,
    _lock_heartbeat,
    _model_download_lock,
    model_install_state,
    resolve_model_dir,
)


# ---- shared fakes -------------------------------------------------------------


_STUB_BIN = (b"model-bytes-stub-header" + bytes(range(256)) * 6100)
_STUB_SHA = hashlib.sha256(_STUB_BIN).hexdigest()  # > 1 MiB: ≥ MIN_DOWNLOAD_SIZE
_SUPPORT_SIZES = {
    "config.json": 2257,
    "tokenizer.json": 2203239,
    "vocabulary.txt": 459861,
}


@pytest.fixture
def _engine_on(monkeypatch) -> None:
    """v0.4.14's transcription engine env OFF guard, inverted: these
    tests exercise the resolve/download seam, which refuses honestly
    under the suite-wide off-switch (conftest)."""
    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)


def _pin_medium_sha(monkeypatch, sha: str) -> None:
    monkeypatch.setitem(MODEL_SHA256, "medium", sha)


def _arity_fetch(calls):
    """A variadic fake that records the CALL ARITY it received (M-1's
    "assert every call len==2" shape). Writes the file each call
    targets so the resolve completes: support files at their pinned
    sizes, model.bin at the stub bytes whose sha the test pins."""

    def fetch(url, *args, **kwargs):
        calls.append(1 + len(args) + len(kwargs))
        path = args[0] if not isinstance(args[0], Path) else args[0]
        name = Path(path).name
        if name.endswith(".part"):
            name = name[: -len(".part")]
        if name == "model.bin":
            Path(path).write_bytes(_STUB_BIN)
        else:
            Path(path).write_bytes(b"x" * _SUPPORT_SIZES[name])

    return fetch


class _LoopbackFileServer:
    """A loopback-only HTTP server for the default-fetcher tests.

    HTTP/1.0 (BaseHTTPRequestHandler's default): close-delimited
    bodies. The with-Content-Length case still carries the header; the
    without case exercises the ``total is None`` arm honestly.
    """

    def __init__(
        self,
        content: bytes,
        *,
        send_content_length: bool = True,
        claimed_length: int | None = None,
    ) -> None:
        outer = self
        outer.send_content_length = send_content_length
        outer.claimed_length = claimed_length

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                if outer.send_content_length:
                    # claimed_length overrides the real size: the M-5
                    # truncation arm (claims 9000, delivers fewer).
                    self.send_header(
                        "Content-Length",
                        str(
                            outer.claimed_length
                            if outer.claimed_length is not None
                            else len(outer.content)
                        ),
                    )
                self.end_headers()
                self.wfile.write(outer.content)

            def log_message(self, *args):  # keep the console quiet
                pass

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._httpd.server_address[1]
        self.content = content

    def url(self, name: str = "f.bin") -> str:
        return f"http://127.0.0.1:{self.port}/{name}"

    def __enter__(self):
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()
        # readiness: poll a loopback connect so the first request never
        # races serve_forever's poll loop
        deadline = time.time() + 5.0
        while True:
            try:
                with socket.create_connection(
                    ("127.0.0.1", self.port), timeout=1
                ):
                    break
            except OSError:
                if time.time() >= deadline:
                    raise
                time.sleep(0.02)
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
        return False


def _seed_installed(state_root, tag: str = "medium") -> Path:
    """Fabricate the warm cache (the resolve fast-path contract —
    marker + model.bin). Content is irrelevant for state derivation:
    the fast path performs NO digest reads; it is the shipped
    definition of "installed" (ADR-1)."""
    target = Path(state_root) / "whisper" / "models" / tag
    target.mkdir(parents=True, exist_ok=True)
    (target / "model.bin").write_bytes(_STUB_BIN)
    (target / ".complete").write_text(f"{tag}\n{'0' * 64}\n", encoding="utf-8")
    return target


# ==========================================================================
# 1. The seam: conditional arity + per-chunk beats (impl spec §3 tests 1-2)
# ==========================================================================


def test_m1_two_arg_fake_with_no_consumer_keeps_legacy_arity(
    tmp_path, monkeypatch, _engine_on
):
    """M-1: a legacy 2-arg fake through resolve_model_dir with NO
    progress consumer — every fetch call is exactly 2 positional args
    (the conditional-arity discipline; the F-11 suite's shape keeps
    working untouched) and the download still completes + installs."""
    calls: list[int] = []
    _pin_medium_sha(monkeypatch, _STUB_SHA)
    state_root = tmp_path / "s"
    resolved = resolve_model_dir(
        "medium", state_root=state_root, fetch=_arity_fetch(calls)
    )
    target = state_root / "whisper" / "models" / "medium"
    assert resolved == str(target)
    assert calls, "the fake fetch must have run"
    assert set(calls) == {2}, (
        f"a no-progress 2-arg fake received a non-2 call: {calls}"
    )
    assert (target / ".complete").is_file()
    assert (target / "model.bin").is_file()
    assert model_install_state("medium", state_root=state_root)[
        "state"
    ] == "installed"


def test_m2_three_arg_fake_receives_monotonic_beats_with_consumer(
    tmp_path, monkeypatch, _engine_on
):
    """M-2 (impl spec §3 test 2): a 3-arg fake + a progress consumer —
    the callback observed with monotonic ``done``, final ``done ==
    size``, ``total`` forwarded as given, and the marker lands only
    AFTER the sha pass."""
    chunked = [b"a" * (512 * 1024), b"b" * (512 * 1024), b"c" * 1024]
    total_size = sum(len(c) for c in chunked)
    _pin_medium_sha(
        monkeypatch, hashlib.sha256(b"".join(chunked)).hexdigest()
    )
    state_root = tmp_path / "s"
    target = state_root / "whisper" / "models" / "medium"
    target.mkdir(parents=True)
    # the support files sit at their pinned sizes: only model.bin fetches
    (target / "config.json").write_bytes(b"x" * _SUPPORT_SIZES["config.json"])
    (target / "tokenizer.json").write_bytes(b"x" * _SUPPORT_SIZES["tokenizer.json"])
    (target / "vocabulary.txt").write_bytes(b"x" * _SUPPORT_SIZES["vocabulary.txt"])

    fetched_names: list[str] = []

    def fetch_exact(url, target_path, on_progress=None):
        path = Path(target_path)
        fetched_names.append(path.name)
        assert path.name == "model.bin.part"
        done = 0
        with open(path, "wb") as fh:
            for chunk in chunked:
                fh.write(chunk)
                done += len(chunk)
                if on_progress is not None:
                    on_progress(done, total_size)

    beats: list[tuple[int, int]] = []
    resolve_model_dir(
        "medium",
        state_root=state_root,
        fetch=fetch_exact,
        on_download_progress=lambda done, total: beats.append((done, total)),
    )
    assert fetched_names == ["model.bin.part"]
    # three chunk beats with the fetch's Content-Length semantics... plus
    # _download_file's pinned post-completion 100%-beat:
    # on_progress(size, expected_size) — expected_size None for model.bin.
    assert beats == [
        (len(chunked[0]), total_size),
        (len(chunked[0]) + len(chunked[1]), total_size),
        (total_size, total_size),
        (total_size, None),
    ]
    done_values = [b for b, _t in beats]
    assert done_values == sorted(done_values)
    assert done_values[-1] == total_size
    # the marker lands only after the sha pass completed
    assert (target / ".complete").is_file()
    assert (target / "model.bin").read_bytes() == b"".join(chunked)


def test_m2b_progress_consumer_riding_a_warm_cache_never_fetches(
    tmp_path, monkeypatch, _engine_on
):
    """M-1's complement: a caller that REQUESTS progress never breaks
    the fast path — over a warm cache the resolve returns with ZERO
    fetch calls (the consumer rides only a real download; ADR-3's
    conditional-arity discipline is a download-time contract: the 3rd
    arg is passed exactly when a consumer exists AND a fetch runs)."""

    def two_arg_only(url, target_path):
        raise AssertionError("never reached cleanly")

    state_root = tmp_path / "s"
    target = state_root / "whisper" / "models" / "medium"
    target.mkdir(parents=True)
    # everything is already installed: the no-fetch fast path — a caller
    # consumer riding a warm cache never touches the fetch at all.
    (target / "model.bin").write_bytes(_STUB_BIN)
    (target / ".complete").write_text("medium\n0\n", encoding="utf-8")
    beats: list[tuple] = []
    resolved = resolve_model_dir(
        "medium",
        state_root=state_root,
        fetch=two_arg_only,
        on_download_progress=lambda d, t: beats.append((d, t)),
    )
    assert resolved == str(target)


# ==========================================================================
# 2. The default fetcher over loopback (M-3..M-5)
# ==========================================================================


def test_m3_default_fetcher_content_length_beats(tmp_path):
    """M-3: the real default fetcher against a 127.0.0.1 http.server
    WITH Content-Length — beats sum to exactly N, total == N on every
    beat, the final file is byte-identical, and no .tmp survives."""
    content = bytes(range(256)) * (2 * 1024 * 1024 // 256 + 4)  # > 2 MiB
    with _LoopbackFileServer(content) as server:
        beats: list[tuple[int, int | None]] = []
        target = tmp_path / "f.bin"
        _default_fetcher()(
            server.url(), target, lambda done, total: beats.append((done, total))
        )
    assert target.read_bytes() == content
    assert beats, "the loopback download must produce beats"
    assert beats[-1] == (len(content), len(content))
    for _done, total in beats:
        assert total == len(content)
    done_values = [d for d, _t in beats]
    assert done_values == sorted(done_values)
    assert [p.name for p in tmp_path.iterdir()] == ["f.bin"]


def test_m4_default_fetcher_no_content_length_honest_none(tmp_path):
    """M-4: a close-delimited response (no Content-Length) — beats
    carry ``total is None`` (never a fabricated percent); the final
    size check is skipped (existing semantics); the file is correct."""
    content = b"z" * (1024 * 1024 + 777)
    with _LoopbackFileServer(content, send_content_length=False) as server:
        beats: list[tuple[int, int | None]] = []
        target = tmp_path / "f.bin"
        _default_fetcher()(
            server.url(), target, lambda done, total: beats.append((done, total))
        )
    assert target.read_bytes() == content
    assert beats and all(total is None for _d, total in beats)
    assert beats[-1][0] == len(content)
    assert [p.name for p in tmp_path.iterdir()] == ["f.bin"]


def test_m5_truncated_download_keeps_wording_and_state(tmp_path):
    """M-5: a Content-Length N response delivering <N raises the
    preserved truncation wording; the .tmp is cleaned, no marker
    lands, and model_install_state honestly resolves not-installed."""
    real_content = b"short"
    with _LoopbackFileServer(
        real_content, send_content_length=True, claimed_length=9000
    ) as server:
        target_dir = tmp_path / "whisper" / "models" / "medium"
        target_dir.mkdir(parents=True)
        for name, size in _SUPPORT_SIZES.items():
            (target_dir / name).write_bytes(b"x" * size)

        with pytest.raises(TranscriptionError) as excinfo:
            _download_file(
                target_dir,
                "model.bin",
                server.url(),
                expected_size=9000,
                fetch=_default_fetcher(),
                on_progress=None,
            )
        assert "was truncated (9000 bytes expected)" in str(excinfo.value)
        assert sorted(p.name for p in target_dir.iterdir()) == sorted(
            _SUPPORT_SIZES
        )
        assert model_install_state("medium", state_root=tmp_path)[
            "state"
        ] == "not-installed"


def test_m6_wrong_bytes_sha_mismatch_wording_retry(tmp_path, monkeypatch, _engine_on):
    """M-6: a fake fetch writing wrong bytes → sha256 mismatch —
    delete + the preserved "retry to refetch" raise; the state
    resolves not-installed; no marker was written."""
    _pin_medium_sha(monkeypatch, _STUB_SHA)
    state_root = tmp_path / "s"

    def fetch(url, *args, **kwargs):
        path = Path(args[0])
        name = path.name
        if name.endswith(".part"):
            name = name[: -len(".part")]
        if name == "model.bin":
            # big enough to pass MIN_DOWNLOAD_SIZE, wrong enough to
            # fail the sha256 pin
            path.write_bytes(b"HOSTILE-BYTES" * 300_000)
        else:
            path.write_bytes(b"x" * _SUPPORT_SIZES[name])

    with pytest.raises(TranscriptionError) as excinfo:
        resolve_model_dir("medium", state_root=state_root, fetch=fetch)
    assert "failed its sha256 pin" in str(excinfo.value)
    assert "retry to refetch" in str(excinfo.value)
    assert not (state_root / "whisper" / "models" / "medium" / ".complete").exists()
    assert model_install_state("medium", state_root=state_root)[
        "state"
    ] == "not-installed"


def test_m7_part_short_circuit_survives_the_new_path(
    tmp_path, monkeypatch, _engine_on
):
    """M-7: a size-matching staged ``.part`` short-circuits WITHOUT a
    fetch (the pre-existing contract, re-verified down the new
    conditional-arity path)."""
    calls: list[str] = []
    _pin_medium_sha(monkeypatch, _STUB_SHA)
    state_root = tmp_path / "s"
    target = state_root / "whisper" / "models" / "medium"
    target.mkdir(parents=True)
    (target / "config.json.part").write_bytes(b"x" * _SUPPORT_SIZES["config.json"])

    def fetch(url, *args, **kwargs):
        calls.append(Path(args[0]).name)
        # a completing fetch (name-aware: support files at their pinned
        # sizes, model.bin the sha-pinned stub)
        path = Path(args[0])
        name = Path(path).name
        if name.endswith(".part"):
            name = name[: -len(".part")]
        if name == "model.bin":
            path.write_bytes(_STUB_BIN)
        else:
            path.write_bytes(b"x" * _SUPPORT_SIZES[name])

    resolve_model_dir("medium", state_root=state_root, fetch=fetch)
    assert (target / "config.json").read_bytes() == b"x" * _SUPPORT_SIZES["config.json"]
    assert "config.json.part" not in calls, (
        "a size-matching .part must short-circuit without a fetch"
    )
    assert "model.bin.part" in calls  # the bin still downloads


# ==========================================================================
# 3. The per-tag download lock (L-1..L-5) + heartbeat (L-6)
# ==========================================================================


def test_l1_lock_created_pid_stamped_and_released(tmp_path):
    """L-1: cold acquire creates ``.download.lock`` (pid + timestamp
    content); the context exit unlinks it."""
    target = tmp_path / "medium"
    lock_path = target / ".download.lock"
    with _model_download_lock(target) as held:
        assert held is True
        assert lock_path.is_file()
        lines = lock_path.read_text(encoding="utf-8").split("\n")
        assert lines[0] == str(os.getpid())  # content is pid/timestamp only
        assert 0.0 < float(lines[1]) <= time.time() + 1
    assert not lock_path.exists()


@pytest.mark.usefixtures("_engine_on")
def test_l2_held_lock_waits_then_fast_paths_without_downloading(
    tmp_path, monkeypatch
):
    """L-2: a second acquirer over a FRESH held lock waits (the
    on_lock_wait label fires with the holder pid) and fast-paths the
    moment the winner's marker + bin appear — the fetch runs 0 times."""
    state_root = tmp_path / "s"
    target = state_root / "whisper" / "models" / "medium"
    target.mkdir(parents=True)
    lock_path = target / ".download.lock"
    lock_path.write_text(f"{os.getpid() + 12345}\n{time.time()}\n")

    waits: list[tuple[int | None, float]] = []

    def on_wait(pid, waited):
        waits.append((pid, waited))
        # the winner finishes while we wait, then frees the lock
        (target / "model.bin").write_bytes(_STUB_BIN)
        (target / ".complete").write_text("medium\n0\n", encoding="utf-8")
        try:
            lock_path.unlink()
        except OSError:
            pass

    monkeypatch.setattr(time, "sleep", lambda _s: None)  # wait at 1 Hz, fast
    fetch_calls: list[str] = []

    def fetch(url, *args, **kwargs):
        fetch_calls.append(url)

    resolved = resolve_model_dir(
        "medium", state_root=state_root, fetch=fetch, on_lock_wait=on_wait
    )
    assert resolved == str(target)
    assert waits, "the waiter must surface its wait"
    assert waits[0][0] == os.getpid() + 12345
    assert len(fetch_calls) == 0, "the winner completed: no second download"


@pytest.mark.usefixtures("_engine_on")
def test_l3_stale_lock_reclaimed_unilaterally(
    tmp_path, monkeypatch, _engine_on
):
    """L-3: a lock older than MODEL_LOCK_STALE_SECONDS (a killed
    writer) is reclaimed; the acquirer proceeds and releases."""
    state_root = tmp_path / "s"
    target = state_root / "whisper" / "models" / "medium"
    target.mkdir(parents=True)
    lock_path = target / ".download.lock"
    lock_path.write_text("123\n1.0\n", encoding="utf-8")
    backdated = time.time() - MODEL_LOCK_STALE_SECONDS - 10
    os.utime(lock_path, (backdated, backdated))

    calls: list[int] = []
    _pin_medium_sha(monkeypatch, _STUB_SHA)
    resolved = resolve_model_dir(
        "medium", state_root=state_root, fetch=_arity_fetch(calls)
    )
    assert resolved == str(target)
    assert calls, "the reclaim let THIS acquirer download"
    assert not lock_path.exists(), "the winner's release cleaned the lock"


def test_l4_unwritable_lock_degrades_open(
    tmp_path, monkeypatch, _engine_on
):
    """L-4: an OSError on the FIRST create attempt (permissions) is
    degrade-OPEN — the lock yields False, never raises, and the
    download proceeds (sha256 remains the integrity backstop)."""
    real_open = transcribe_mod.os.open

    def refusing_open(path, flags, *args, **kwargs):
        if str(path).endswith(".download.lock"):
            raise PermissionError(13, "read-only test fs")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(transcribe_mod.os, "open", refusing_open)
    target = tmp_path / "medium"
    with _model_download_lock(target) as held:
        assert held is False  # not held — the caller proceeds regardless
        assert not (target / ".download.lock").exists()

    _pin_medium_sha(monkeypatch, _STUB_SHA)
    calls: list[int] = []
    resolved = resolve_model_dir(
        "medium", state_root=tmp_path, fetch=_arity_fetch(calls)
    )
    assert resolved
    assert calls, "the download proceeded without the lock"
    assert not (tmp_path / "medium" / ".download.lock").exists()


@pytest.mark.usefixtures("_engine_on")
def test_l5_exception_mid_download_releases_lock(
    tmp_path, monkeypatch, _engine_on
):
    """L-5: an exception inside the critical section releases the lock
    in the finally (no stranded fresh lock) — and the resolve-level
    failure leaves NO .download.lock behind (with the F-11 no-staging
    debris contract intact)."""
    target = tmp_path / "medium"
    lock_path = target / ".download.lock"
    with pytest.raises(RuntimeError):
        with _model_download_lock(target) as held:
            assert held is True
            raise RuntimeError("boom")
    assert not lock_path.exists()

    def exploding_fetch(url, *args, **kwargs):
        raise TranscriptionError("forced mid-download failure")

    state_root = tmp_path / "s"
    with pytest.raises(TranscriptionError):
        resolve_model_dir("medium", state_root=state_root, fetch=exploding_fetch)
    model_dir = state_root / "whisper" / "models" / "medium"
    assert sorted(p.name for p in model_dir.iterdir()) == [], (
        "no lock, no staging debris left behind (F-11 holds on the new path)"
    )


def test_l6_heartbeat_throttles_at_one_hz_and_delegates(tmp_path, monkeypatch):
    """L-6: the heartbeat wrapper refreshes the lock's mtime at most
    once per second, delegates EVERY beat to the callback, and (when
    installed with a None callback — the default-fetcher no-progress
    path) still heartbeats with no delegate side effects."""
    lock_path = tmp_path / ".download.lock"
    lock_path.write_bytes(b"")
    utimes: list[Path] = []
    real_utime = os.utime

    def recording_utime(path, *args, **kwargs):
        utimes.append(path)
        return real_utime(path, *args, **kwargs)

    monkeypatch.setattr(transcribe_mod.os, "utime", recording_utime)

    beats: list[tuple[int, int | None]] = []
    wrapped = _lock_heartbeat(
        lambda done, total: beats.append((done, total)), lock_path
    )
    wrapped(1024, 4096)  # beat 1: fires utime #1
    wrapped(2048, 4096)  # beat 2 inside the 1 s window: NO second utime
    assert len(utimes) == 1
    time.sleep(1.05)
    wrapped(4096, 4096)  # past the window: utime #2
    assert len(utimes) == 2
    assert beats == [(1024, 4096), (2048, 4096), (4096, 4096)]

    # the no-op form (default fetcher, no progress consumer): heartbeats
    # exactly as before, delegates nothing (no observable side effects)
    noop_utimes: list[Path] = []
    monkeypatch.setattr(
        transcribe_mod.os,
        "utime",
        lambda path, *a, **k: (noop_utimes.append(path), real_utime(path, *a, **k))[1],
    )
    noop = _lock_heartbeat(None, lock_path)
    noop(1, None)
    noop(2, None)
    assert len(noop_utimes) == 1


@pytest.mark.usefixtures("_engine_on")
def test_l6b_default_fetcher_download_heartbeats_lock_mtime(
    tmp_path, monkeypatch, _engine_on
):
    """L-6 (integration arm): through the REAL resolve_model_dir (the
    default fetcher, NO caller progress), the internal no-op heartbeat
    refreshes the lock during the download (an os.utime on the lock is
    observed) and the content still lands sha-verified + marker.
    The resolve is driven over the loopback server via a patched
    ``model_download_url_for`` (the seam's URL derivation)."""
    content = bytes(range(256)) * (3 * 1024 * 1024 // 256)
    monkeypatch.setitem(
        transcribe_mod.MODEL_SHA256, "medium", hashlib.sha256(content).hexdigest()
    )
    utime_calls: list[str] = []
    real_utime = os.utime

    def recording_utime(path, *args, **kwargs):
        utime_calls.append(str(path))
        return real_utime(path, *args, **kwargs)

    monkeypatch.setattr(transcribe_mod.os, "utime", recording_utime)

    with _LoopbackFileServer(content) as server:
        monkeypatch.setattr(
            transcribe_mod,
            "model_download_url_for",
            lambda tag, filename: server.url(filename),
        )
        state_root = tmp_path / "s"
        target = state_root / "whisper" / "models" / "medium"
        target.mkdir(parents=True)
        for name, size in _SUPPORT_SIZES.items():
            # support files sit at their pinned sizes: only model.bin fetches
            (target / name).write_bytes(b"x" * size)
        resolved = resolve_model_dir("medium", state_root=state_root)
        assert resolved == str(target)
    lock_touches = [p for p in utime_calls if p.endswith(".download.lock")]
    assert lock_touches, (
        "the no-progress default-fetcher path still heartbeats the lock"
    )
    assert (target / ".complete").is_file()
    assert (target / "model.bin").read_bytes() == content
    assert not (target / ".download.lock").exists()


# ---- L-8: the D-4 mid-wait degradation bound (Karen probe 3) -----------------


def _lock_open_script(monkeypatch, target, script) -> list[str]:
    """Drive ``_model_download_lock``'s create-attempt sequence through a
    scripted ``os.open`` (deterministic on every platform, unlike real
    chmod races): script entries are 'FE' (FileExistsError — round-0/live
    contention) and 'OE'/'PE' (a generic OSError / PermissionError raised
    forever once the script runs out — the cache dir degraded mid-wait).
    The contender's lock file pre-exists so the FE arm's stale-stat sees
    a live writer. Returns the observed attempt kinds."""
    target.mkdir(parents=True, exist_ok=True)
    (target / ".download.lock").write_text("99999\n0\n", encoding="utf-8")
    real_open = transcribe_mod.os.open
    attempts: list[str] = []

    def scripted_open(path, flags, *args, **kwargs):
        if not str(path).endswith(".download.lock"):
            return real_open(path, flags, *args, **kwargs)
        index = len(attempts)
        kind = script[index] if index < len(script) else "OE"
        attempts.append(kind)
        if kind == "FE":
            raise FileExistsError(17, "contended (test script)")
        if kind == "PE":
            raise PermissionError(13, "read-only test fs")
        raise OSError(30, "degraded cache dir (test script)")

    monkeypatch.setattr(transcribe_mod.os, "open", scripted_open)
    return attempts


def test_l8_midwait_permission_flip_degrades_open_bounded(
    tmp_path, monkeypatch, _engine_on
):
    """L-8 (Karen's D-4 probe regression): round-0 contention followed
    by a MID-WAIT permission flip (the cache dir made read-only while a
    live contender holds the lock) must degrade-OPEN — the old loop
    retried OSError unconditionally on every round after round 0 and
    spun at 1 Hz forever. A PermissionError on ANY round breaks out
    (bounded calls, bounded wall time, lock never acquired, no residue);
    an intervening FileExistsError resets the streak (contention is not
    degradation) without disturbing the any-round arm."""
    target = tmp_path / "medium"
    attempts = _lock_open_script(
        monkeypatch, target, ["FE", "OE", "FE", "PE"]
    )
    started = time.monotonic()
    with _model_download_lock(target) as held:
        assert held is False  # degrade-OPEN: the caller proceeds regardless
    elapsed = time.monotonic() - started
    # FE(r0, contention) -> OE(r1, streak 1) -> FE(r2, streak reset) ->
    # PE(r3) breaks the wait-out immediately: bounded, not a 1 Hz spin.
    assert attempts == ["FE", "OE", "FE", "PE"]
    assert elapsed < 10.0, "the wait-out must stay bounded (never a hang)"
    # the contender's lock is untouched (we never held it, never removed it)
    assert (target / ".download.lock").read_text().startswith("99999\n")


def test_l8b_bounded_consecutive_oserror_streak_degrades_open(
    tmp_path, monkeypatch, _engine_on
):
    """L-8b: a degradation that is NOT a PermissionError (e.g. an
    EROFS-shaped OSError) also cannot spin forever — after
    MODEL_LOCK_OERROR_DEGRADE_ROUNDS (3) CONSECUTIVE non-FileExistsError
    OSErrors the lock degrades-OPEN exactly like the permission arm."""
    target = tmp_path / "medium"
    attempts = _lock_open_script(monkeypatch, target, ["FE"])
    started = time.monotonic()
    with _model_download_lock(target) as held:
        assert held is False
    elapsed = time.monotonic() - started
    # FE(r0) + exactly DEGRADE_ROUNDS consecutive OSErrors: one 1 Hz wait
    # per pre-bound round, then the bound fires — bounded calls and time,
    # never the old unbounded retry loop.
    assert len(attempts) == 1 + transcribe_mod.MODEL_LOCK_OERROR_DEGRADE_ROUNDS
    assert attempts == ["FE", "OE", "OE", "OE"]
    assert elapsed < 10.0
    assert (target / ".download.lock").read_text().startswith("99999\n")


# ==========================================================================
# 4. model_install_state (D-1..D-9)
# ==========================================================================


_EXPECTED_KEYS = {
    "tag",
    "state",
    "cache_dir",
    "bytes_done",
    "bytes_total",
    "file",
    "error",
}


@pytest.mark.parametrize(
    "seed,expected",
    [
        ("nothing", "not-installed"),  # D-1 cold
        ("warm", "installed"),  # D-2 warm cache
        ("marker-only", "not-installed"),  # D-3 debris
        ("bin-only", "not-installed"),  # D-4 debris inverted
    ],
)
def test_d1_d4_disk_predicate_matches_the_resolver(tmp_path, seed, expected):
    if seed == "warm":
        _seed_installed(tmp_path)
    if seed == "marker-only":
        target = _seed_installed(tmp_path)
        (target / "model.bin").unlink()
    if seed == "bin-only":
        target = Path(tmp_path) / "whisper" / "models" / "medium"
        target.mkdir(parents=True, exist_ok=True)
        (target / "model.bin").write_bytes(b"x")
    block = model_install_state("medium", state_root=tmp_path)
    assert set(block) == _EXPECTED_KEYS
    assert block["state"] == expected
    assert block["bytes_done"] is None and block["bytes_total"] is None
    assert block["file"] is None and block["error"] is None
    assert block["tag"] == "medium"
    assert block["cache_dir"] == str(tmp_path / "whisper" / "models" / "medium")


def test_d5_unreadable_cache_is_unknown_never_missing(tmp_path, monkeypatch):
    """D-5: a permissions failure on the disk probe is ``unknown`` —
    never a fabricated ``not-installed``."""
    real_is_file = Path.is_file

    def exploding_is_file(self):
        if "whisper" in str(self):
            raise OSError(13, "permission denied")
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", exploding_is_file)
    block = model_install_state("medium", state_root=tmp_path)
    assert block["state"] == "unknown"


def test_d6_downloading_board_beats_cold_disk(tmp_path):
    """D-6: a board downloading snapshot wins over the disk predicate —
    the bytes have not landed the marker yet."""
    board = ModelInstallBoard()
    board.begin("medium")
    board.update(bytes_done=734003200, bytes_total=1531000000, file="model.bin")
    block = model_install_state("medium", state_root=tmp_path, board=board)
    assert block["state"] == "downloading"
    assert block["bytes_done"] == 734003200
    assert block["bytes_total"] == 1531000000
    assert block["file"] == "model.bin"
    assert board.snapshot()["updated_at"] > 0


def test_d7_board_fail_rides_disk_truth(tmp_path):
    """D-7: a failed install's error rides the disk-derived
    not-installed state ("not-installed + why")."""
    board = ModelInstallBoard()
    board.begin("medium")
    board.fail("medium", "transcription model download failed (model.bin): boom")
    block = model_install_state("medium", state_root=tmp_path, board=board)
    assert block["state"] == "not-installed"
    assert block["error"] == "transcription model download failed (model.bin): boom"


def test_d8_lying_succeed_board_never_overrides_disk(tmp_path):
    """D-8: a succeed snapshot with a COLD disk contributes nothing —
    the file system is the terminal state's truth."""
    board = ModelInstallBoard()
    board.succeed("medium")
    block = model_install_state("medium", state_root=tmp_path, board=board)
    assert block["state"] == "not-installed"
    assert block["error"] is None


def test_d9_board_slot_for_another_tag_ignored(tmp_path):
    """D-9: a board snapshot for a DIFFERENT tag is ignored entirely."""
    board = ModelInstallBoard()
    board.begin("small")
    board.update(bytes_done=5, bytes_total=10, file="model.bin")
    block = model_install_state("medium", state_root=tmp_path, board=board)
    assert block["state"] == "not-installed"
    assert block["tag"] == "medium"


def test_board_snapshot_none_when_never_used():
    """A fresh process's board reads as None (never a fabricated
    state); begin/update/fail/succeed mutate exactly one slot under the
    lock."""
    board = ModelInstallBoard()
    assert board.snapshot() is None
    board.begin("medium")
    snap = board.snapshot()
    assert snap["state"] == "downloading" and snap["tag"] == "medium"
    assert snap["bytes_done"] == 0 and snap["bytes_total"] is None


# ==========================================================================
# 5. The Mode B publisher (M-8 / H-1 / H-3 unit level)
# ==========================================================================


class _Touching:
    """A last_activity_at + touch pair (the record/state contract the
    publisher drives)."""

    def __init__(self) -> None:
        self.last_activity_at = 0.0
        self.touches = 0

    def touch(self) -> None:
        self.last_activity_at = time.time()
        self.touches += 1


def test_m8_publisher_populates_bytes_and_touches():
    """M-8/H-1 (unit): beats populate ``progress["model_download"]``,
    name the step, and touch BOTH the state and the record so
    ``last_activity_at`` (activity_age_seconds's source) keeps
    advancing across a simulated slow download; a faster-than-4 Hz
    mid-download flood collapses (while the final beat always lands)."""
    record = _Touching()
    state_touches: list[float] = []
    state = SimpleNamespace(
        progress=None, touch=lambda: state_touches.append(time.time())
    )
    publisher = _model_download_publisher(record, state)

    publisher(500, 1000)
    assert state.progress["model_download"] == {
        "bytes_done": 500,
        "bytes_total": 1000,
        "file": "model.bin",
    }
    assert state.progress["step"] == "downloading transcription model (model.bin)"
    assert record.last_activity_at > 0 and state_touches
    record_age_0 = record.last_activity_at

    # a mid-download beat <0.25 s later is throttled (no dict churn,
    # no touch)...
    publisher(501, 1000)
    assert state.progress["model_download"]["bytes_done"] == 500
    assert record.last_activity_at == record_age_0
    # ...but the FINAL beat always lands regardless of spacing
    publisher(1000, 1000)
    assert state.progress["model_download"]["bytes_done"] == 1000
    assert record.last_activity_at >= record_age_0
    # the total-None shape publishes honestly too (no fabricated total)
    publisher(300, None)
    assert state.progress["model_download"] == {
        "bytes_done": 300,
        "bytes_total": None,
        "file": "model.bin",
    }
    assert record.touches == 3  # beats 1, 3, 4 (beat 2 was throttled)


@pytest.mark.usefixtures("_engine_on")
def test_h1_h3_mode_b_download_beats_then_pop(
    tmp_path, monkeypatch, _engine_on
):
    """H-1/H-3 through the REAL _execute_subs_job (the subs_job seam
    monkeypatched): the publisher rides the run_subs_job kwargs
    (the threading pin), a download beat lands
    ``progress["model_download"]`` with record/state touches, and the
    first post-resolve step pops the fact."""
    from vidlift import subs_job as subs_job_mod
    from vidlift.mcp_bridge.registry import Registry

    driven: dict = {}

    def fake_run_subs_job(path, **kwargs):
        driven.update(kwargs)
        kwargs["on_download_progress"](500, 10_000)
        kwargs["on_download_progress"](10_000, 10_000)
        kwargs["on_step"]("transcribe-resolve")
        kwargs["on_step"]("transcribe-language")
        return SimpleNamespace(
            output_path=str(tmp_path / "out.subs.mp4"),
            srt_path=str(tmp_path / "out.srt"),
            language="en",
            cue_count=2,
            model="/stub",
            device="cpu",
            notes=[],
        )

    monkeypatch.setattr(subs_job_mod, "run_subs_job", fake_run_subs_job)

    registry = Registry(tmp_path / "state")
    record = registry.enqueue(
        kind="subs",
        paths=[str(tmp_path / "src.avi")],
        options={"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
    )
    registry.transition(record, "running")
    services = SimpleNamespace(
        registry=registry, state_dir=tmp_path / "state"
    )
    outcome = _execute_subs_job(record, services, CancelFlag())
    assert isinstance(outcome, JobOutcome) and outcome.ok

    assert "on_download_progress" in driven, "the publisher must ride the kwargs"
    assert driven["on_download_progress"] is not None
    live_state = record.get_live("state_box")["state"]
    # H-3: the download fact popped at the first post-resolve step
    assert "model_download" not in live_state.progress
    assert live_state.progress["step"] == "transcribe-language"


@pytest.mark.usefixtures("_engine_on")
def test_h1b_publisher_heartbeat_keeps_record_activity_young(
    tmp_path, monkeypatch, _engine_on
):
    """H-1's activity-age half through the real executor path: during a
    SIMULATED SLOW download (beats spaced beyond the 4 Hz throttle),
    the record's ``last_activity_at`` keeps advancing — the reader's
    ``activity_age_seconds`` would stay small across the whole window
    (Eric's 17-minute silent-stall symptom inverted)."""
    from vidlift import subs_job as subs_job_mod
    from vidlift.mcp_bridge.registry import Registry

    def fake_run_subs_job(path, **kwargs):
        publisher = kwargs["on_download_progress"]
        # simulate: 5 chunks, ~0.3 s apart (past the 0.25 s throttle);
        # each beat must re-touch the record so a reader's
        # activity_age_seconds stays small across the whole window
        touches: list[float] = []
        for done in (200, 400, 600, 800, 1000):
            time.sleep(0.3)
            publisher(done, 1000)
            touches.append(record.last_activity_at)
        assert len(set(touches)) >= 4, (
            "at least 4 distinct touches across 5 throttled beats — "
            "a silent download never leaves the heartbeat stale"
        )
        return SimpleNamespace(
            output_path=str(tmp_path / "o.subs.mp4"),
            srt_path=str(tmp_path / "o.srt"),
            language="en",
            cue_count=1,
            model="/s",
            device="cpu",
            notes=[],
        )

    monkeypatch.setattr(subs_job_mod, "run_subs_job", fake_run_subs_job)
    registry = Registry(tmp_path / "state")
    record = registry.enqueue(
        kind="subs",
        paths=[str(tmp_path / "src.avi")],
        options={"ffmpeg": "ffmpeg", "ffprobe": "ffprobe"},
    )
    registry.transition(record, "running")
    services = SimpleNamespace(
        registry=registry, state_dir=tmp_path / "state"
    )
    before = record.last_activity_at
    outcome = _execute_subs_job(record, services, CancelFlag())
    assert outcome.ok
    after = record.last_activity_at
    assert after > before, "the record's activity advanced past the download"
    assert after - before >= 1.0, "multiple throttled beats touched the record"


# ==========================================================================
# 6. The per-job payload + /status envelope (S-matrix)
# ==========================================================================


_PAYLOAD_LEGACY_KEYS = {
    "job_id",
    "state",
    "queue_position",
    "file_path",
    "current_phase",
    "started_at",
    "finished_at",
    "last_activity_at",
    "activity_age_seconds",
    "log_path",
    "log_tail",
    "commands",
    "phases",
    "engine_simulated",
    "engine_note",
    "verification",
    "manifest_path",
    "error",
    "eta_seconds",
    "transcription",
}


def _services_for(tmp_path, *, registry=None):
    registry = registry or Registry(tmp_path / "state")
    return BridgeServices(registry=registry, state_dir=tmp_path / "state")


def test_s8_payload_additivity_guard(tmp_path):
    """S-8: the pre-existing payload key set gains EXACTLY
    ``transcription_model`` — no renames, no removals; the visibility
    ``transcription`` block keeps its exact shape (idle/queued shape:
    no live state, no mirror)."""
    registry = Registry(tmp_path / "state")
    record = registry.enqueue(
        kind="subs", paths=["/media/movie.avi"], options={"model": "small"}
    )
    services = _services_for(tmp_path, registry=registry)
    payload = _status_payload(services, record)
    assert payload["transcription"] == {
        "device": None,
        "device_requested": None,
    }, "the visibility device echo keeps its exact shape"
    assert set(payload) == _PAYLOAD_LEGACY_KEYS | {"transcription_model"}


def test_s4_live_downloading_via_mirror(tmp_path):
    """S-4/D-11: a RUNNING subs record whose persisted mirror carries
    ``model_download`` derives ``downloading`` with the byte truth
    (cross-process trusted-while-running rule); freshness rides the
    existing ``activity_age_seconds`` field."""
    registry = Registry(tmp_path / "state")
    record = registry.enqueue(kind="subs", paths=["/media/movie.avi"], options={})
    registry.transition(record, "running")
    record.progress_snapshot = {
        "progress": {
            "model_download": {
                "bytes_done": 734003200,
                "bytes_total": 1531000000,
                "file": "model.bin",
            }
        },
        "current_phase": "transcribe",
        "last_activity_at": time.time(),
    }
    registry.save()
    payload = _status_payload(_services_for(tmp_path, registry=registry), record)
    block = payload["transcription_model"]
    assert block["state"] == "downloading"
    assert block["bytes_done"] == 734003200
    assert block["bytes_total"] == 1531000000
    assert payload["activity_age_seconds"] is not None


def test_d12_terminal_records_do_not_trust_mirror(tmp_path):
    """D-12: a TERMINAL record's mirror carrying ``model_download`` is
    NOT trusted — the (dead writer's last beat) mirror is ignored and
    the disk derivation stands."""
    registry = Registry(tmp_path / "state")
    record = registry.enqueue(kind="subs", paths=["/media/movie.avi"], options={})
    record.state = "failed"
    record.progress_snapshot = {
        "progress": {
            "model_download": {
                "bytes_done": 1,
                "bytes_total": 2,
                "file": "model.bin",
            }
        },
    }
    payload = _status_payload(_services_for(tmp_path, registry=registry), record)
    assert payload["transcription_model"]["state"] == "not-installed"
    assert payload["transcription_model"]["bytes_done"] is None


def test_d13_d15_tag_resolution_rules(tmp_path):
    """D-13/D-14/D-15: a subs record's pin names the tag when it is a
    KNOWN model; an unknown pin (a user PATH) reports the DEFAULT
    tag's machine state; run/benchmark kinds report DEFAULT."""
    _seed_installed(tmp_path / "state", "small")
    registry = Registry(tmp_path / "state")
    services = _services_for(tmp_path, registry=registry)
    subs_small = registry.enqueue(
        kind="subs", paths=["/media/a.avi"], options={"model": "small"}
    )
    payload = _status_payload(services, subs_small)
    assert payload["transcription_model"]["tag"] == "small"
    assert payload["transcription_model"]["state"] == "installed"

    subs_pathpin = registry.enqueue(
        kind="subs", paths=["/media/b.avi"], options={"model": "/models/wisper"}
    )
    payload = _status_payload(services, subs_pathpin)
    assert payload["transcription_model"]["tag"] == DEFAULT_MODEL
    assert payload["transcription_model"]["state"] == "not-installed"

    run_record = registry.enqueue(kind="run", paths=["/media/c.avi"], options={})
    payload = _status_payload(services, run_record)
    assert payload["transcription_model"]["tag"] == DEFAULT_MODEL


def test_s5_terminal_subs_job_warm_cache_installed(tmp_path):
    """S-5: a terminal (succeeded) subs job against a warm cache derives
    ``installed`` from the disk predicate."""
    _seed_installed(tmp_path / "state")
    registry = Registry(tmp_path / "state")
    record = registry.enqueue(kind="subs", paths=["/media/a.avi"], options={})
    record.state = "succeeded"
    payload = _status_payload(_services_for(tmp_path, registry=registry), record)
    assert payload["transcription_model"]["state"] == "installed"
    assert payload["transcription_model"]["cache_dir"] == str(
        tmp_path / "state" / "whisper" / "models" / DEFAULT_MODEL
    )


def test_s10_services_without_board_degrade_to_disk(tmp_path):
    """S-10: a services object WITHOUT ``model_install_board`` still
    yields a well-formed block (soft getattr; N5's foreign fakes keep
    working)."""
    registry = Registry(tmp_path / "state")
    bare = SimpleNamespace(registry=registry, state_dir=tmp_path / "state")
    record = registry.enqueue(kind="subs", paths=["/media/a.avi"], options={})
    payload = _status_payload(bare, record)
    assert payload["transcription_model"]["state"] == "not-installed"
    assert set(payload["transcription_model"]) == _EXPECTED_KEYS


def test_s11_foreign_state_shapes_still_derive_block(tmp_path):
    """S-11: a fake live RunState lacking progress/subtitles shapes
    still yields a well-formed ``transcription_model`` block (the
    defensive-getattr discipline the visibility echo established)."""
    registry = Registry(tmp_path / "state")
    record = registry.enqueue(kind="run", paths=["/media/a.avi"], options={})

    class _BareState:
        # lacks .progress/.subtitles/.plan (None) and .log — every
        # _status_payload read must tolerate it and still derive the
        # model block
        log = None
        plan = None
        progress = None
        subtitles = None
        current_phase = "probe"
        last_activity_at = None

        def to_dict(self):
            return {
                "current_phase": "probe",
                "progress": None,
                "started_at": time.time(),
                "finished_at": None,
                "last_activity_at": time.time(),
                "log_path": None,
                "commands": [],
                "phases": [],
                "engine_simulated": False,
                "engine_note": None,
                "verification": None,
                "manifest_path": None,
                "error": None,
                "subtitles": None,
            }

    record.set_live("state_box", {"state": _BareState()})
    payload = _status_payload(_services_for(tmp_path, registry=registry), record)
    assert payload["transcription_model"]["state"] == "not-installed"
    assert set(payload["transcription_model"]) == _EXPECTED_KEYS


# ---- the /status envelope -----------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _no_leaked_listener():
    yield
    stop_server()


def _client(tmp_path):
    return LocalBridgeClient(
        state_dir=tmp_path / "bridge-state", start_executor=False
    )


def test_s1_envelope_idle_cold_cache(tmp_path):
    """S-1: idle queue + cold cache → the envelope carries
    ``transcription_model`` with state ``not-installed`` (machine truth
    with ZERO jobs) and ``api_version`` stays 1."""
    client = _client(tmp_path)
    try:
        server, _base = start_server(
            client=client,
            host="127.0.0.1",
            port=_free_port(),
            token=MONITOR_TOKEN_DEFAULT,
        )
        envelope = status_payload(server.view)
        assert envelope["api_version"] == API_VERSION == 1
        block = envelope["transcription_model"]
        assert block["state"] == "not-installed"
        assert block["tag"] == DEFAULT_MODEL
        assert set(block) == _EXPECTED_KEYS
        # the machine block's cache dir is the CLIENT's state dir (the
        # same root Mode B downloads into), never the default root
        assert block["cache_dir"] == str(
            Path(client.services.state_dir) / "whisper" / "models" / DEFAULT_MODEL
        )
    finally:
        client.stop()


def test_s2_envelope_sees_idle_gui_install(tmp_path):
    """S-2: a GUI install in flight (a fake board downloading) makes the
    ENVELOPE ``downloading`` with bytes — visible with zero jobs in the
    system."""
    client = _client(tmp_path)
    try:
        board = client.services.model_install_board
        board.begin(DEFAULT_MODEL)
        board.update(bytes_done=734003200, bytes_total=1531000000, file="model.bin")
        server, _base = start_server(
            client=client,
            host="127.0.0.1",
            port=_free_port(),
            token=MONITOR_TOKEN_DEFAULT,
        )
        envelope = status_payload(server.view)
        block = envelope["transcription_model"]
        assert block["state"] == "downloading"
        assert block["bytes_done"] == 734003200
        assert block["bytes_total"] == 1531000000
    finally:
        client.stop()


def test_s3_envelope_job_download_beats_idle_disk(tmp_path):
    """S-3: an active mid-download job's block wins the envelope over
    idle disk truth."""
    client = _client(tmp_path)
    try:
        record = client.registry.enqueue(
            kind="subs", paths=["/media/x.avi"], options={}
        )
        client.registry.transition(record, "running")
        record.progress_snapshot = {
            "progress": {
                "model_download": {
                    "bytes_done": 123,
                    "bytes_total": None,
                    "file": "model.bin",
                }
            },
            "current_phase": "transcribe",
            "last_activity_at": time.time(),
        }
        client.registry.save()
        server, _base = start_server(
            client=client,
            host="127.0.0.1",
            port=_free_port(),
            token=MONITOR_TOKEN_DEFAULT,
        )
        envelope = status_payload(server.view)
        block = envelope["transcription_model"]
        assert block["state"] == "downloading"
        assert block["bytes_done"] == 123
        assert block["bytes_total"] is None
    finally:
        client.stop()


def test_s6_s7_health_unchanged_api_stability(tmp_path):
    """S-7/S-6: /health stays token-free and EXACTLY {ok, version,
    busy}; the additive envelope key never bumps ``api_version``."""
    client = _client(tmp_path)
    try:
        import urllib.request

        server, base = start_server(
            client=client,
            host="127.0.0.1",
            port=_free_port(),
            token=MONITOR_TOKEN_DEFAULT,
        )
        with urllib.request.urlopen(f"{base}/health", timeout=5) as response:
            health = json.loads(response.read().decode("utf-8"))
        assert set(health) == {"ok", "version", "busy"}
        assert health["version"] == __import__("vidlift").__version__
    finally:
        client.stop()


def test_s10b_envelope_degrades_on_foreign_view(tmp_path):
    """S-10 (envelope arm): a view whose client carries no
    ``services`` at all degrades to the disk-only derivation without a
    500 — a well-formed block is served. StatusView's own
    duck-typing gate (status_snapshot/history required) is satisfied:
    the read surface is the tolerance target, not the write surface."""
    from vidlift.monitor import StatusView

    read_only_client = SimpleNamespace(
        queue_views=lambda: ([], []),
        enqueue_snapshots=lambda ids: [],
        status_snapshot=lambda job_id: None,
        history=lambda limit=200: [],
    )
    envelope = status_payload(StatusView(read_only_client))
    assert set(envelope["transcription_model"]) == _EXPECTED_KEYS
    assert envelope["transcription_model"]["state"] in (
        "installed",
        "not-installed",
        "downloading",
        "unknown",
    )


# ==========================================================================
# 7. Read-only / structural security expectations (P5/R-matrix)
# ==========================================================================


def test_r1_bridge_tool_registry_unchanged():
    """R-1: the tool set stays exactly the pre-change 9 — no install /
    model tool exists to trigger an install from any MCP client."""
    from vidlift.mcp_bridge.tools import TOOL_HANDLERS

    assert set(TOOL_HANDLERS) == {
        "probe",
        "plan",
        "benchmark",
        "run",
        "transcribe",
        "status",
        "cancel",
        "list",
        "manifest",
    }


def test_r2_monitor_routes_unchanged():
    """R-2: the routing table stays /health + /status + /history (the
    404 body's list untouched); the envelope is additive-only inside
    the EXISTING GET /status."""
    from vidlift.monitor import ENDPOINTS

    assert ENDPOINTS == ["/health", "/status", "/history"]


def test_r4_install_callers_are_gui_and_publishers_only():
    """R-4 (grep-provable structural rule): only gui.py calls the
    install board's WRITERS (begin) — the bridge's tool handlers and
    monitor routes build no install affordance, and no other module
    can start a model download outside the spec'd pass-through call
    sites (jobs/run/thread the publisher into the core's resolve)."""
    import vidlift

    pkg_root = Path(vidlift.__file__).parent
    hits: list[str] = []
    for path in pkg_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "model_install_board" in text and (".begin(" in text):
            hits.append(str(path.relative_to(pkg_root)))
    assert hits == ["gui.py"], (
        f"only gui.py may begin an install; found: {hits}"
    )