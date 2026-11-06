"""Real spawn-process tests; never open video, models or a GPU."""
import multiprocessing
import os
import struct
import time

import pytest

from vidlift.transcription_watchdog import (
    WorkerCancelled, WorkerExited, WorkerTimeout,
    supervise_worker, supervise_with_cpu_retry,
)


def _worker(pipe, mode):
    pipe.send(("pid", os.getpid()))
    if mode == "crash":
        os._exit(7)
    if mode != "hang_init":
        pipe.send(("decode", None))
    if mode == "complete":
        pipe.send(("progress", 1))
        pipe.send(("result", ["segment"]))
        return
    while True:
        time.sleep(1)


def _assert_reaped(pid):
    assert all(child.pid != pid for child in multiprocessing.active_children())
    if os.name != "nt":
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_spawn_completion_and_order():
    seen = []
    assert supervise_worker(
        _worker, ("complete",), on_message=lambda k, v: seen.append((k, v)),
        init_timeout=5, decode_timeout=5,
    ) == ["segment"]
    assert [kind for kind, _ in seen] == ["pid", "decode", "progress"]
    _assert_reaped(seen[0][1])


@pytest.mark.parametrize("mode,phase", [("hang_init", "init"), ("hang_decode", "decode")])
def test_hung_process_is_terminated_and_reaped(mode, phase):
    seen = []
    with pytest.raises(WorkerTimeout, match=phase):
        supervise_worker(
            _worker, (mode,), on_message=lambda k, v: seen.append((k, v)),
            init_timeout=1, decode_timeout=0.2,
        )
    _assert_reaped(seen[0][1])


def test_cancel_terminates_and_reaps_without_timeout():
    seen = []
    with pytest.raises(WorkerCancelled):
        supervise_worker(
            _worker, ("hang_decode",),
            should_cancel=lambda: any(k == "decode" for k, _ in seen),
            on_message=lambda k, v: seen.append((k, v)),
            init_timeout=5, decode_timeout=5,
        )
    _assert_reaped(seen[0][1])


def test_crash_is_reported_and_reaped():
    seen = []
    with pytest.raises(WorkerExited):
        supervise_worker(
            _worker, ("crash",), on_message=lambda k, v: seen.append((k, v)),
            init_timeout=5, decode_timeout=5,
        )
    _assert_reaped(seen[0][1])


def _retry_worker(pipe, device, cpu_hangs):
    pipe.send(("attempt", (device, os.getpid())))
    if device == "cuda" or cpu_hangs:
        while True:
            time.sleep(1)
    pipe.send(("decode", None))
    pipe.send(("progress", 1))
    pipe.send(("result", "cpu output"))


def _partial_frame_worker(pipe):
    # Connection framing advertises a longer payload than is sent. The
    # parent reader must never block its deadline in recv().
    os.write(pipe.fileno(), struct.pack("!i", 1000) + b"partial")
    while True:
        time.sleep(1)


@pytest.mark.skipif(os.name == "nt", reason="raw Unix pipe descriptor test")
def test_partial_ipc_frame_does_not_defeat_watchdog():
    started = time.monotonic()
    with pytest.raises(WorkerTimeout, match="init"):
        supervise_worker(_partial_frame_worker, init_timeout=0.5)
    assert time.monotonic() - started < 4


@pytest.mark.parametrize("cpu_hangs", [False, True])
def test_hung_cuda_reaped_before_one_cpu_retry(cpu_hangs):
    seen = []

    def message(kind, payload):
        if kind == "attempt" and payload[0] == "cpu":
            _assert_reaped(seen[0][1][1])
        seen.append((kind, payload))

    if cpu_hangs:
        with pytest.raises(WorkerTimeout, match="init"):
            supervise_with_cpu_retry(_retry_worker, (True,), device="cuda",
                                     on_message=message, init_timeout=0.6,
                                     decode_timeout=1)
    else:
        assert supervise_with_cpu_retry(
            _retry_worker, (False,), device="cuda", on_message=message,
            init_timeout=0.6, decode_timeout=1,
        ) == (
            "cpu output", "cpu", "init_timeout"
        )
    attempts = [value for kind, value in seen if kind == "attempt"]
    assert [device for device, _ in attempts] == ["cuda", "cpu"]
    for _, pid in attempts:
        _assert_reaped(pid)


def test_cancel_never_retries_on_cpu():
    seen = []
    with pytest.raises(WorkerCancelled):
        supervise_with_cpu_retry(
            _retry_worker, (False,), device="cuda",
            should_cancel=lambda: bool(seen),
            on_message=lambda k, v: seen.append((k, v)),
            init_timeout=5, decode_timeout=5,
        )
    assert [value[0] for kind, value in seen if kind == "attempt"] == ["cuda"]
    _assert_reaped(seen[0][1][1])


# ---- F6 (v0.4.20): reader-thread death must be a clean WorkerExited -------
# The old narrow except-tuple let an exotic recv exception kill the daemon
# reader silently; the poll loop then spun at 20 Hz on a dead pipe until the
# phase deadline. These arms drive every reader-death class through a fake
# spawn context (no real child, no GPU) and pin the prompt-exit contract.


class _ExoticFrameError(Exception):
    """A recv failure type outside the old (EOFError, OSError, ValueError)."""


def _fake_spawn_context(monkeypatch, recv_script):
    """Replace the module's spawn context with a scripted fake pipe.

    ``recv_script`` items: ("msg", payload) returns payload; ("raise", exc)
    raises exc. Exhausted scripts raise EOFError (the real child-exit fact).
    The fake process is never alive — the script IS the simulated child, so
    no spawn, IPC or reaping work happens for real."""

    class FakeReceive:
        def __init__(self):
            self._script = list(recv_script)

        def recv(self):
            if not self._script:
                raise EOFError("pipe closed")
            item = self._script.pop(0)
            if item[0] == "msg":
                return item[1]
            raise item[1]

        def close(self):
            pass

    class FakeSend:
        def close(self):
            pass

    class FakeProcess:
        def __init__(self, target=None, args=()):
            self._alive = False

        def start(self):
            pass  # the recv script simulates the child; it never lives

        def is_alive(self):
            return self._alive

        def terminate(self):
            pass

        def kill(self):
            pass

        def join(self, timeout=None):
            pass

        def close(self):
            pass

    class FakeContext:
        @staticmethod
        def Pipe(duplex=False):
            return FakeReceive(), FakeSend()

        @staticmethod
        def Process(target=None, args=()):
            return FakeProcess(target, args)

    monkeypatch.setattr(
        "vidlift.transcription_watchdog.multiprocessing.get_context",
        lambda name: FakeContext(),
    )


def test_reader_exotic_failure_is_prompt_worker_exited(monkeypatch):
    _fake_spawn_context(monkeypatch, [
        ("msg", ("pid", 42)),
        ("raise", _ExoticFrameError("corrupted frame")),
    ])
    started = time.monotonic()
    with pytest.raises(WorkerExited) as excinfo:
        supervise_worker(_worker, ("complete",), init_timeout=30, decode_timeout=30)
    # The sanitized type name rides the detail (ADR-2 sanitization rule).
    assert "reader failure: _ExoticFrameError" in str(excinfo.value)
    # Prompt — well under any phase deadline (the F6 spin class is gone).
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("pipe_error", [EOFError("eof"), OSError("broken")])
def test_reader_expected_pipe_errors_keep_existing_message(monkeypatch, pipe_error):
    _fake_spawn_context(monkeypatch, [("raise", pipe_error)])
    with pytest.raises(WorkerExited) as excinfo:
        supervise_worker(_worker, ("complete",), init_timeout=30, decode_timeout=30)
    # The normal crash/EOF path is byte-identical to pre-F6 behavior.
    assert str(excinfo.value) == "transcription worker exited without result"


def test_reader_failure_after_result_still_returns_result(monkeypatch):
    # A reader death AFTER the result frame must not swallow a completed
    # attempt: queue order delivers the result first.
    _fake_spawn_context(monkeypatch, [
        ("msg", ("result", ["done"])),
        ("raise", _ExoticFrameError("late corruption")),
    ])
    assert supervise_worker(
        _worker, ("complete",), init_timeout=30, decode_timeout=30,
    ) == ["done"]
