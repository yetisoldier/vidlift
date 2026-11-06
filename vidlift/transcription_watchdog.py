"""Spawn-based supervision primitives for isolated transcription workers.

This module deliberately does not start a worker at import time.  Worker
entry points must be importable module-level callables for Windows spawn.
The caller owns the transcription policy, including any CPU retry.
"""
from __future__ import annotations

import multiprocessing
import queue
import threading
import time
from collections.abc import Callable


INIT_TIMEOUT_SECONDS = 90.0
DECODE_TIMEOUT_SECONDS = 120.0
_POLL_SECONDS = 0.05
_REAP_SECONDS = 2.0


class WorkerTimeout(RuntimeError):
    """A worker failed to make progress within its phase budget."""

    def __init__(self, phase: str):
        self.phase = phase
        super().__init__(f"engine_timeout: {phase}")


class WorkerExited(RuntimeError):
    """A worker exited without delivering a result."""


class WorkerCancelled(RuntimeError):
    """The caller explicitly cancelled the worker."""


def _stop_and_reap(process) -> None:
    if process.is_alive():
        process.terminate()
        process.join(_REAP_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(_REAP_SECONDS)
    if process.is_alive():
        raise RuntimeError("transcription worker could not be reaped")
    process.join()


def supervise_worker(
    target: Callable,
    args: tuple = (),
    *,
    should_cancel: Callable[[], bool] | None = None,
    on_message: Callable[[str, object], None] | None = None,
    init_timeout: float = INIT_TIMEOUT_SECONDS,
    decode_timeout: float = DECODE_TIMEOUT_SECONDS,
    absolute_timeout: float | None = None,
):
    """Run an importable target in a spawned process and return its result.

    The target receives a send-only Connection as its first argument and
    sends (kind, payload) pairs.  "decode" switches to the decode budget,
    "progress" renews that budget, and "result" completes the attempt.
    All other messages are forwarded unchanged.  Cancellation and every
    exceptional exit terminate and reap before returning to the caller.
    An absolute timeout bounds the attempt even if progress keeps arriving.
    """
    if (init_timeout <= 0 or decode_timeout <= 0
            or (absolute_timeout is not None and absolute_timeout <= 0)):
        raise ValueError("worker timeouts must be positive")
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(send, *args))
    started = False
    reader = None
    messages = queue.Queue()
    phase = "init"
    started_at = time.monotonic()
    deadline = started_at + init_timeout
    absolute_deadline = (started_at + absolute_timeout
                         if absolute_timeout is not None else None)
    try:
        if should_cancel is not None and should_cancel():
            raise WorkerCancelled("transcription cancelled")
        process.start()
        started = True
        send.close()

        def read_messages():
            # recv can block after a partial IPC frame. Keep it off the
            # watchdog thread so cancellation and deadlines remain live.
            try:
                while True:
                    messages.put((True, receive.recv()))
            except SystemExit:
                raise  # interpreter shutdown must not be swallowed here
            except BaseException as exc:  # noqa: BLE001 - F6 (v0.4.20): ANY reader-thread death is a clean WorkerExited; the old narrow tuple let an exotic recv exception kill the thread silently and spin the poll loop at 20 Hz until the phase deadline
                messages.put((False, exc))

        reader = threading.Thread(target=read_messages, daemon=True)
        reader.start()
        while True:
            if should_cancel is not None and should_cancel():
                raise WorkerCancelled("transcription cancelled")
            now = time.monotonic()
            if absolute_deadline is not None and now >= absolute_deadline:
                raise WorkerTimeout("absolute")
            remaining = deadline - now
            if absolute_deadline is not None:
                remaining = min(remaining, absolute_deadline - now)
            if remaining <= 0:
                raise WorkerTimeout(phase)
            try:
                valid, message = messages.get(timeout=min(_POLL_SECONDS, remaining))
            except queue.Empty:
                if not process.is_alive():
                    # The reader can still be draining a final message.
                    continue
                continue
            if not valid:
                detail = "transcription worker exited without result"
                if isinstance(message, BaseException) and not isinstance(
                        message, (EOFError, OSError, ValueError)):
                    # F6 (v0.4.20): an unexpected reader-thread death names
                    # its type — a sanitized class name only (no payload),
                    # so the failure is diagnosable without leaking data.
                    detail += f"; reader failure: {type(message).__name__}"
                raise WorkerExited(detail)
            else:
                kind, payload = message
                if kind == "result":
                    return payload
                if kind == "decode":
                    phase = "decode"
                    deadline = time.monotonic() + decode_timeout
                elif kind == "progress" and phase == "decode":
                    deadline = time.monotonic() + decode_timeout
                if on_message is not None:
                    on_message(kind, payload)
    finally:
        send.close()
        if started:
            _stop_and_reap(process)
            process.close()
            if reader is not None:
                reader.join(_REAP_SECONDS)
        receive.close()


def supervise_with_cpu_retry(
    target: Callable,
    args: tuple = (),
    *,
    device: str,
    should_cancel: Callable[[], bool] | None = None,
    on_message: Callable[[str, object], None] | None = None,
    init_timeout: float = INIT_TIMEOUT_SECONDS,
    decode_timeout: float = DECODE_TIMEOUT_SECONDS,
    absolute_timeout: float | None = None,
):
    """Run one attempt and, on a pre-progress CUDA failure, retry on CPU.

    Target receives (connection, device, *args). Return (result, device,
    fallback_reason). Cancellation never retries; post-progress failures do
    not restart a partially observed transcription.
    """
    if device not in ("cpu", "cuda"):
        raise ValueError("device must be cpu or cuda")
    progressed = False

    def forward(kind, payload):
        nonlocal progressed
        if kind == "progress":
            progressed = True
        if on_message is not None:
            on_message(kind, payload)

    def attempt(selected):
        return supervise_worker(
            target, (selected, *args), should_cancel=should_cancel,
            on_message=forward, init_timeout=init_timeout,
            decode_timeout=decode_timeout, absolute_timeout=absolute_timeout,
        )

    try:
        return attempt(device), device, None
    except (WorkerTimeout, WorkerExited) as exc:
        if device != "cuda" or progressed:
            raise
        reason = (f"{exc.phase}_timeout" if isinstance(exc, WorkerTimeout)
                  else "worker_exited")
    return attempt("cpu"), "cpu", reason
