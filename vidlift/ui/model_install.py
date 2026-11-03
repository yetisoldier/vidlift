"""GUI-owned transcription-model install worker (v0.4.16, ADR-4).

A :class:`QThreadPool`-scheduled :class:`QRunnable` calling
``resolve_model_dir`` — the GPU-meter pattern, the GUI's ONLY
background-work idiom (gpu_meter.py) — never the serial JobExecutor
(a 1.5 GB fetch must not starve the job queue) and never any remote
surface (Hermes stays structurally read-only; install is a GUI action
only).

``resolve_model_dir`` is the whole implementation: sha256-pinned,
lock-serialized per tag (ADR-5), ``.part``-resuming. The runnable body
touches NO widgets — signals carry data across threads; the window's
slots own every render (G-12). The ``fetch`` constructor seam exists
for tests to inject a fake (no network in the suite); production
passes ``fetch=None`` (the default fetcher).
"""

from __future__ import annotations

import time

from PyQt6.QtCore import QObject, QRunnable, pyqtSignal

from ..transcribe import resolve_model_dir

__all__ = [
    "ModelInstallSignals",
    "ModelInstallRunnable",
]

# The runnable's progress-emit throttle (≤4 Hz — the same publisher
# cadence the jobs/run record publishers use; the board keeps every
# beat, the pulse renders ≥4 Hz).
_PROGRESS_EMIT_SECONDS = 0.25


class ModelInstallSignals(QObject):
    """The runnable's Qt signal shim (QRunnable cannot emit)."""

    progress = pyqtSignal(dict)  # {"bytes_done": int, "bytes_total": int | None}
    finished = pyqtSignal(bool, str)  # ok, message


class ModelInstallRunnable(QRunnable):
    """One background transcription-model install (download + sha256
    verify + marker) off the UI thread, fully signal-reported."""

    def __init__(
        self,
        tag: str,
        *,
        state_dir,
        board,
        signals: ModelInstallSignals,
        fetch=None,
    ) -> None:
        super().__init__()
        self.tag = tag
        self.state_dir = state_dir
        self.board = board
        self.signals = signals
        self.fetch = fetch  # None in production -> the default fetcher

    def run(self) -> None:  # pragma: no cover - exercised via pool/run
        last = [0.0]

        def on_progress(done: int, total: int | None) -> None:
            now = time.time()
            if (
                now - last[0] < _PROGRESS_EMIT_SECONDS
                and total is not None
                and done < total
            ):
                return
            last[0] = now
            try:
                self.board.update(
                    bytes_done=done, bytes_total=total, file="model.bin"
                )
            except Exception:  # board writes never kill the install
                pass
            self.signals.progress.emit(
                {"bytes_done": done, "bytes_total": total}
            )

        def on_wait(pid, waited) -> None:
            # A second downloader holds the tag lock; the surface (the
            # pill's slot) shows the wait, never a hang.
            self.signals.progress.emit(
                {"waiting": True, "holder": pid, "seconds": waited}
            )

        try:
            resolve_model_dir(
                self.tag,
                state_root=self.state_dir,
                fetch=self.fetch,
                on_download_progress=on_progress,
                on_lock_wait=on_wait,
            )
        except Exception as exc:  # TranscriptionError + OSError family
            try:
                self.board.fail(self.tag, str(exc)[:300])
            except Exception:  # noqa: BLE001 - reporting never crashes
                pass
            self.signals.finished.emit(False, str(exc))
            return
        try:
            self.board.succeed(self.tag)
        except Exception:  # noqa: BLE001 - reporting never crashes
            pass
        self.signals.finished.emit(True, f"installed {self.tag}")