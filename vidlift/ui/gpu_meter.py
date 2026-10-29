"""Task-Manager-style GPU utilization meter (v0.4.10, R1).

A compact fixed-size widget for the GUI's top bar: a dark panel with a
faint horizontal grid, a rolling filled area chart of recent NVIDIA GPU
utilization (0..100%), the word "GPU" top-left, and the current
percentage in large text top-right — Eric's Windows Task Manager
reference, small.

Contract discipline (the gui.py honesty rules apply here too):

- ``paintEvent`` is the ONLY painter; layout never changes after
  construction (a ``setFixedSize`` widget), so a sample can never
  shift or resize the top bar. ``update()`` alone repaints.
- Sampling runs OFF the UI thread through a
  :class:`QThreadPool`/:class:`QRunnable` worker that shells
  ``nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,
  nounits`` (the same external tool the ncnn engine path already
  relies on); a hard TIMEOUT_SECONDS child timeout terminates the
  process and reads as "n/a". The worker signals a Qt signal back; the
  widget's slot only appends a bounded sample and repaints.
- Nothing an exception can escape: the runnable's run() wraps its body
  in ``try/except Exception``; the widget treats the sampler purely as
  data. A missing/defective nvidia-smi, no NVIDIA GPU, or a timeout
  degenerates to "GPU n/a" — never a crash, never a log spam (failed
  probes retry at the meter's cadence with no logging at all).
- Multiple GPUs: the sample is the MAX utilization across devices
  (the busy card, not the average) with a small "x2"/"x3" hint;
  a single device shows no hint.
- No third-party dependency: PyQt6/Qt and stdlib only (this repo
  standardizes on PyQt6 — sibling bluray-to-mp4 uses PySide6; the
  gpu_meter follows VidLift's import line, same as ui/widgets.py).

The widget owns no sampling thread between ticks — one pooled task
every ~0.5 s (METER_INTERVAL_MS) that finishes in the same tick unless
nvidia-smi hangs; the pool keeps its single thread alive either way.
A still-running previous probe is skipped, never queued (the meter
slows down to one sample per resolved probe; it cannot pile up).
"""

from __future__ import annotations

import shutil
import subprocess

from PyQt6.QtCore import (
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

__all__ = [
    "GPU_METER_HEIGHT",
    "GPU_METER_WIDTH",
    "METER_INTERVAL_MS",
    "SAMPLE_CAPACITY",
    "TIMEOUT_SECONDS",
    "GpuMeterWidget",
    "GpuSample",
    "parse_nvidia_smi_output",
]


# Geometry (logical px). FIXED — the top bar never reflows for data.
GPU_METER_WIDTH = 180
GPU_METER_HEIGHT = 54
# Sample cadence ~2 Hz: fine-grained enough to read as the Task
# Manager feel, cheap enough that a hung nvidia-smi never stacks work.
METER_INTERVAL_MS = 500
# Rolling buffer: 90 samples ≈ 45 s of history on screen.
SAMPLE_CAPACITY = 90
# Hard child timeout (s): nvidia-smi is a tiny local CLI; even a busy
# machine answers far inside this. On expiry the child is terminated.
TIMEOUT_SECONDS = 2.0

# The nvidia-smi query line (Windows-preferred source; present on the
# Linux checkout as well when a driver ships it). Kept byte-identical
# to the sibling tools' invocation so a support question matches both.
NVIDIA_SMI_QUERY_ARGS = [
    "--query-gpu=utilization.gpu",
    "--format=csv,noheader,nounits",
]


class GpuSample:
    """One resolved sample: utilization percent (or None when
    unknown), the device count it came from, and whether the probe
    spoke to a real nvidia-smi at all (False → the no-tool "n/a"
    state)."""

    __slots__ = ("utilization", "device_count", "tool_available")

    def __init__(
        self,
        utilization: float | None,
        device_count: int = 1,
        tool_available: bool = True,
    ) -> None:
        self.utilization = utilization
        self.device_count = device_count
        self.tool_available = tool_available


def parse_nvidia_smi_output(text: str | None) -> GpuSample:
    """Parse ``--format=csv,noheader,nounits`` output into a sample.

    Pure function (R4's GPU-sampler parse layer): one line per GPU.
    A missing/None or empty tool answer, unparsable percentages, or
    per-device "N/A" cells degrade to the n/a sample — never an
    exception. Multi-GPU: the returned utilization is the MAX across
    devices (the busy card), with the device count riding along.
    """
    if text is None:
        return GpuSample(None, 0, False)
    values: list[float] = []
    for line in str(text).strip().splitlines():
        line = line.strip()
        if not line:
            continue
        cell = line.split(",")[0].strip()
        # Windows locale quirks occasionally surface here; accept only
        # an exact float-looking cell ("N/A", empty, junk → skip).
        try:
            value = float(cell)
        except ValueError:
            continue
        if 0.0 <= value <= 100.0:
            values.append(value)
    if not values:
        # Either truly nothing (no driver / no NVIDIA GPU) or a
        # broken nvidia-smi — the widget's n/a state either way.
        return GpuSample(None, 0, text.strip() != "")
    return GpuSample(max(values), len(values), True)


class _GpuProbeSignals(QObject):
    """The runnable's Qt signal shim (QRunnable cannot emit)."""

    sampled = pyqtSignal(object)


class GpuProbeRunnable(QRunnable):
    """One nvidia-smi probe off the UI thread, hard-timeout guarded.

    Body is exception-proof by design: any failure resolves to the
    n/a sample through the same signal — never into the event loop.
    On timeout the child is terminated, then reaped.
    """

    def __init__(self, binary: str) -> None:
        super().__init__()
        self.signals = _GpuProbeSignals()
        self._binary = binary

    def run(self) -> None:  # pragma: no cover - exercised via pool/run
        try:
            self.signals.sampled.emit(self._probe())
        except Exception:  # pragma: no cover - absolute last resort
            try:
                self.signals.sampled.emit(GpuSample(None, 0, False))
            except Exception:
                pass

    def _probe(self) -> GpuSample:
        try:
            proc = subprocess.Popen(
                [self._binary, *NVIDIA_SMI_QUERY_ARGS],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            # Missing binary / not launched at all → the honest n/a.
            return GpuSample(None, 0, False)
        try:
            stdout, _stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.terminate()
                try:
                    proc.communicate(timeout=1.0)
                except Exception:
                    pass
            return GpuSample(None, 0, False)
        if proc.returncode != 0:
            return GpuSample(None, 0, False)
        return parse_nvidia_smi_output(stdout)


class GpuMeterWidget(QWidget):
    """The fixed-size top-bar meter (see module docstring)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(QSize(GPU_METER_WIDTH, GPU_METER_HEIGHT))
        self.setToolTip(
            "NVIDIA GPU utilization (max across devices, sampled every "
            "0.5 s via nvidia-smi).\nShows n/a when nvidia-smi is not "
            "available."
        )
        self._samples: list[GpuSample] = []
        self._probe_in_flight = False
        self._binary = shutil.which("nvidia-smi")
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        self._timer = QTimer(self)
        self._timer.setInterval(METER_INTERVAL_MS)
        self._timer.timeout.connect(self._request_sample)

    # -- sampling --------------------------------------------------------

    def start(self) -> None:
        """Begin sampling at the meter cadence. Sampling is entirely
        passive — it never touches the upscale pipeline, writes no
        state, and holds no lock; with nvidia-smi absent it emits the
        n/a sample every tick without touching disk or log."""
        if not self._timer.isActive():
            self._timer.start()
        self._request_sample()

    def stop(self) -> None:
        self._timer.stop()

    def _request_sample(self) -> None:
        # One in-flight probe max: a hung nvidia-smi cannot stack
        # queued runnables (the meter slows to the probe's pace).
        if self._probe_in_flight:
            return
        if not self._binary:
            # Graceful degradation without any subprocess churn.
            self._accept_sample(GpuSample(None, 0, False))
            return
        self._probe_in_flight = True
        runnable = GpuProbeRunnable(self._binary)
        runnable.signals.sampled.connect(self._on_sampled)
        self._pool.start(runnable)

    def _on_sampled(self, sample: object) -> None:
        self._probe_in_flight = False
        if isinstance(sample, GpuSample):
            self._accept_sample(sample)
        else:  # defensive: a misbehaving payload still lands in n/a
            self._accept_sample(GpuSample(None, 0, False))

    def _accept_sample(self, sample: GpuSample) -> None:
        self._samples.append(sample)
        del self._samples[:-SAMPLE_CAPACITY]
        self.update()  # repaint ONLY; layout is fixed forever

    # -- pure rendering inputs (asserted by unit tests) -------------------

    def _rendered_values(self) -> "tuple[float | None, str]":
        """(percent_label, device_hint) the painter will draw — the
        pure projection of the rolling buffer for tests (no Qt paint
        needed). percent_label is None in the n/a state ("n/a"
        renders; no percent ever invents itself)."""
        if not self._samples or not self._samples[-1].tool_available:
            return (None, "")
        last = self._samples[-1]
        return (float(last.utilization or 0.0), _hint(last.device_count))

    # -- painting ---------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter: QPainter) -> None:
        rect = self.rect()

        # Panel: the Task-Manager dark card.
        painter.fillRect(rect, QColor("#1b1e22"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Faint horizontal grid lines (25% steps).
        grid_pen = QPen(QColor(255, 255, 255, 18))
        grid_pen.setWidthF(1.0)
        painter.setPen(grid_pen)
        chart_top = 18.0
        chart_bottom = rect.height() - 4.0
        span = chart_bottom - chart_top
        for step in (0.25, 0.5, 0.75):
            y = chart_bottom - step * span
            painter.drawLine(
                QPointF(rect.left() + 2, y),
                QPointF(rect.right() - 2, y),
            )

        # The area chart of recent samples (newest at the right edge).
        self._paint_chart(
            painter,
            QRectF(
                rect.left() + 2.0,
                chart_top,
                rect.width() - 4.0,
                span,
            ),
        )

        # Label: "GPU" top-left.
        label_font = painter.font()
        label_font.setPixelSize(9)
        label_font.setBold(False)
        painter.setFont(label_font)
        painter.setPen(QColor("#9aa5a2"))
        painter.drawText(6, 11, "GPU")

        # Percentage top-right in large text; "n/a" whenever the last
        # resolved probe had no usable value (tool missing/failed, or
        # parse-degenerate). No percent ever invents itself.
        value_font = painter.font()
        value_font.setPixelSize(17)
        value_font.setBold(True)
        painter.setFont(value_font)
        if not self._samples or not self._samples[-1].tool_available:
            painter.setPen(QColor("#88919a"))
            text = "n/a"
        else:
            painter.setPen(QColor("#c9b8f2"))
            last = self._samples[-1]
            text = f"{int(round(last.utilization or 0.0))}%"
            hint = _hint(last.device_count)
            if hint:
                painter.setPen(QColor("#9aa5a2"))
                hint_font = painter.font()
                hint_font.setPixelSize(9)
                hint_font.setBold(False)
                painter.setFont(hint_font)
                hint_width = painter.fontMetrics().horizontalAdvance(hint)
                painter.drawText(
                    rect.right() - 5 - hint_width,
                    11,
                    hint,
                )
                painter.setFont(value_font)
                painter.setPen(QColor("#c9b8f2"))
        painter.drawText(rect.adjusted(0, 0, -5, 0), int(Qt.AlignmentFlag.AlignRight) | int(Qt.AlignmentFlag.AlignTop), text)

    def _paint_chart(self, painter: QPainter, area: QRectF) -> None:
        """The filled lavender area curve: one x-step per buffered
        sample, right-aligned so the newest sits at the right edge; y
        is the fixed 0..100 scale — the shape NEVER rescales."""
        usable = [s for s in self._samples if s.tool_available and s.utilization is not None]
        if len(usable) < 2:
            # One sample: a flat baseline (the honest chart of a fresh
            # window) — still a filled band, never a jump-cut.
            usable = usable or []
            if not usable:
                return
        width = max(1.0, float(area.width()))
        n = len(usable)
        step = width / max(1, SAMPLE_CAPACITY - 1)
        # Right-align: the newest usable sample maps x = right edge.
        y_for = lambda u: area.bottom() - (max(0.0, min(u, 100.0)) / 100.0) * area.height() + 0.0

        points = []
        for i, sample in enumerate(usable):
            x = (area.right() + 1.0) - (n - 1 - i) * step
            points.append(QPointF(x, y_for(sample.utilization)))
        path = QPainterPath()
        path.moveTo(points[0].x(), area.bottom() + 1.0)
        for p in points:
            path.lineTo(p)
        path.lineTo(points[-1].x(), area.bottom() + 1.0)
        path.closeSubpath()

        fill = QColor("#9f7fe8")
        fill.setAlpha(190)
        painter.fillPath(path, fill)

    # -- explicit teardown (the pool is a child QObject; not needed) ------

    def sample_count(self) -> int:
        """Rolling-buffer length (test seam)."""
        return len(self._samples)


def _hint(device_count: int) -> str:
    """The small multi-GPU hint: 'x2'/'x3'/... for >1 device, '' for
    0/1 (pure helper, unit-tested)."""
    if device_count > 1:
        return f"x{device_count}"
    return ""