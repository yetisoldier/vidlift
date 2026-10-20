"""PyQt6 widgets for the VidLift desktop interface (v0.4.4 design §2).

Behavior-port of the bluray-to-mp4 ``ui/widgets.py`` machinery (the
EtaProgressBar flicker fix and the ActivityDots liveness indicator) —
the contract is behavioral byte-equivalence with the sibling; only the
import line differs (PyQt6, not PySide6).
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QWidget,
)

__all__ = ["ActivityDots", "EtaProgressBar"]


class ActivityDots(QLabel):
    """Three moving dots: a pure 'the app is alive' indicator that needs
    no progress data at all. A GPU probe or first chunk can sit at 0%
    for many minutes; the dots keep moving the whole time so the screen
    never looks frozen (the sibling's 2026-10-02 lesson)."""

    _FRAMES = ("\u2022 \u00b7 \u00b7", "\u00b7 \u2022 \u00b7", "\u00b7 \u00b7 \u2022", "\u00b7 \u2022 \u00b7")

    def __init__(self, parent=None):
        super().__init__("\u00b7 \u00b7 \u00b7", parent)
        self.setObjectName("activityDots")
        f = self.font()
        if f.pointSize() > 0:
            f.setPointSize(max(f.pointSize(), 14))
        else:
            f.setPixelSize(max(f.pixelSize(), 16))
        self.setFont(f)
        self._frame = 0
        self._timer = QTimer(self)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(self._FRAMES)
        self.setText(self._FRAMES[self._frame])

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.setText("\u00b7 \u00b7 \u00b7")


class EtaProgressBar(QWidget):
    """Progress bar plus an ETA line that updates itself.

    ``set_busy(True)`` switches the bar to Qt's indeterminate pulse and
    shows 'estimating ...' — used while a stage runs but no measurable
    progress exists yet (the GPU probe + first-chunk blind window).
    The first ``set_value`` with real data exits busy mode
    automatically, and a duplicate progress line's ``None`` ETA never
    blanks the last shown estimate (the sibling's validation-run-5
    flicker fix — THE rule here)."""

    def __init__(self, parent=None):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.eta_label = QLabel("")
        self.eta_label.setObjectName("muted")
        layout.addWidget(self.bar, stretch=1)
        layout.addWidget(self.eta_label)
        self._busy = False

    def set_busy(self, busy: bool, text: str = "estimating ...") -> None:
        self._busy = busy
        if busy:
            self.bar.setRange(0, 0)  # 0..0 = Qt indeterminate pulse
            self.eta_label.setText(text)
        else:
            self.bar.setRange(0, 1000)
            if self.eta_label.text() in (
                "estimating ...",
                "starting ...",
                "queue active ...",
                "upscaling ...",
            ):
                self.eta_label.setText("")

    def set_value(self, fraction: float, eta_seconds: float | None = None) -> None:
        if self._busy and (eta_seconds is not None or fraction > 0.0):
            self.set_busy(False)  # real data arrived: leave pulse mode
        self.bar.setValue(int(max(0.0, min(fraction, 1.0)) * 1000))
        if eta_seconds is None:
            # Duplicate progress line: keep the last shown estimate — a
            # None between real ticks must never blank the ETA (found
            # in the sibling's validation run 5, 2026-10-02).
            return
        if eta_seconds > 1:
            m, s = divmod(int(eta_seconds), 60)
            self.eta_label.setText(f"ETA {m}:{s:02d}")
        else:
            self.eta_label.setText("")

    def reset(self) -> None:
        self.set_busy(False)
        self.bar.setValue(0)
        self.eta_label.setText("")