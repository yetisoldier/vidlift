"""Cancellable GUI preflight, separate from the serial media executor.

Only the ffprobe child is forcibly bounded. Filesystem enumeration or a
foreign injected probe may still block; no native-thread termination is used.
"""
from __future__ import annotations

import subprocess
import threading
import time

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from .. import _proc
from ..probe import ProbeError


class PreflightSignals(QObject):
    progress = pyqtSignal(int, int, int)
    finished = pyqtSignal(int, object, object)


class PreparationRunnable(QRunnable):
    """Read-only callable with cancellable, GUI-thread-only result delivery."""

    def __init__(self, generation, operation, cancel, signals):
        super().__init__()
        self.generation = generation
        self.operation = operation
        self.cancel = cancel
        self.signals = signals

    def run(self):
        result, failures = None, []
        try:
            result = self.operation(self.cancel, lambda done, total:
                                    self.signals.progress.emit(self.generation, done, total))
        except Exception as exc:
            failures.append(str(exc))
        finally:
            self.signals.finished.emit(self.generation, result, failures)


def probe_runner(cancel: threading.Event, timeout: float = 30.0):
    def run(binary: str, args: list[str]):
        if cancel.is_set():
            raise ProbeError("Checking cancelled")
        proc = subprocess.Popen([binary, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", **_proc.popen_kwargs())
        deadline = time.monotonic() + timeout
        try:
            while True:
                if cancel.is_set() or time.monotonic() >= deadline:
                    raise ProbeError("Checking cancelled" if cancel.is_set() else "File check timed out")
                try:
                    stdout, stderr = proc.communicate(timeout=0.1)
                    return proc.returncode, stdout, stderr
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=2.0)
    return run


class PreflightRunnable(QRunnable):
    def __init__(self, generation: int, paths: tuple[str, ...], probe, cancel, signals):
        super().__init__()
        self.generation = generation
        self.paths = paths
        self.probe = probe
        self.cancel = cancel
        self.signals = signals

    def run(self):
        reports, failures = [], []
        try:
            for index, path in enumerate(self.paths):
                if self.cancel.is_set():
                    break
                try:
                    reports.append(self.probe(path))
                except Exception as exc:
                    failures.append(f"{path}: {exc}")
                if not self.cancel.is_set():
                    self.signals.progress.emit(self.generation, index + 1, len(self.paths))
        finally:
            self.signals.finished.emit(self.generation, reports, failures)
