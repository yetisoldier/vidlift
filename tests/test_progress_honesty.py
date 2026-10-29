# v0.4.5 Slice 2 (D-count + D-eta) — count-pass honesty + stage labels.

# Design docs/designs/v0.4.5-progress-honesty.md §3/§8 (tests 7-13):
# the decode-count heartbeat LOG line, the three stage-label renders,
# the measuring busy label, ETA busy-drop on the first chunk's
# counters, and the phase_note suffix. Every visible number stays
# measured-or-labeled; no fabricated percent.

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
)
from vidlift.run import ToolBox, UPSCALE_STEP_PREPARED_COUNT
from vidlift.gui import MainWindow, _stage_label_text, run_stage_label


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _report_payload():
    return """
    {
      "streams": [
        {
          "index": 0,
          "codec_type": "video",
          "codec_name": "mpeg4",
          "width": 640,
          "height": 360,
          "sample_aspect_ratio": "1:1",
          "field_order": "progressive",
          "r_frame_rate": "25/1",
          "avg_frame_rate": "25/1"
        },
        {"index": 1, "codec_type": "audio", "codec_name": "mp3", "channels": 2}
      ],
      "format": {"format_name": "avi", "duration": "3600.0"}
    }
    """


def _make_media(tmp_path, name="movie.avi"):
    path = tmp_path / name
    path.write_bytes(b"fake-avi-bytes")
    return str(path)


class _FakeRunState:
    """The slice of run.RunState the count-pass pump touches."""

    def __init__(self) -> None:
        self.log_lines: list[str] = []
        self.progress: dict | None = None

    def touch(self) -> None:
        pass

    def record_step(self, label: str) -> None:
        progress = dict(self.progress or {})
        progress["step"] = label
        self.progress = progress


def _engine_with_probe(probe_seconds: float, result=(3600, False)):
    """A NcnnVulkanEngine-shaped object driving the real
    _probe_frame_count_alive pump against a blocking fake runner; the
    engine's heavy __init__ (binary resolution) is bypassed — the pump
    only touches _seam_runner/_step/_log."""
    from vidlift.engine_ncnn import NcnnVulkanEngine

    engine = NcnnVulkanEngine.__new__(NcnnVulkanEngine)
    engine.profile = "live-action"
    engine.spec = SimpleNamespace(name="realesrgan-x2plus", native_scale=2)
    engine.runner = _BlockingProbeRunner(probe_seconds, result)
    engine.engine_runner = None
    engine.on_progress = None
    engine.on_step = None
    engine.invocations = []
    engine.log_lines = []
    engine.tile_floor = None
    return engine


class _BlockingProbeRunner:
    """The ffprobe seam: the -count_frames pass sleeps (a controllable
    'minutes-long' decode at test scale) then returns the count."""

    def __init__(self, probe_seconds: float, result) -> None:
        self.probe_seconds = probe_seconds
        self.result = result

    def __call__(self, binary, args):
        joined = " ".join(args)
        if "-count_frames" in args:
            time.sleep(self.probe_seconds)
            code = 0
            stdout = str(self.result[0])
            return code, stdout, ""
        if "nb_frames" in joined and "stream=" in joined:
            return 0, '{"streams": []}', ""
        if "-show_format" in args:
            return 0, '{"format": {"duration": "3600.0"}}', ""
        if "-show_streams" in args:
            return 0, '{"streams": []}', ""
        return 0, "", ""


def test_count_probe_writes_heartbeat_log_line(monkeypatch):
    """Test 7: the count pass writes honest liveness LOG lines at the
    throttled cadence — phase named, elapsed named, no fabricated
    percent; RunState.log gains them (the record_step seam feeds the
    runner's log via the on_step callback the pump fires)."""
    import vidlift.engine_ncnn as engine_module

    monkeypatch.setattr(engine_module, "PROBE_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(engine_module, "PROBE_HEARTBEAT_LOG_SECONDS", 0.1)
    engine = _engine_with_probe(0.35)
    steps: list[str] = []
    engine.on_step = steps.append
    tools = ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe")
    engine._probe_frame_count_alive(
        "/x/movie.prepared.mkv", tools=tools, label=UPSCALE_STEP_PREPARED_COUNT
    )
    heartbeats = [
        line
        for line in engine.log_lines
        if "heartbeat t=" in line and "decode-count pass" in line
    ]
    assert heartbeats, f"no heartbeat log lines: {engine.log_lines}"
    for line in heartbeats:
        # the honest shape: <label>: heartbeat t=<n>s (decode-count
        # pass; no per-frame signal until ffprobe summary)
        assert line.startswith("prepared-count-probe: heartbeat t=")
        assert "%" not in line
    # step heartbeats also fired (state touch cadence, B.6, unchanged)
    assert steps.count(UPSCALE_STEP_PREPARED_COUNT) >= 2


# ---- stage labels (§3b) ------------------------------------------------------


def test_stage_label_names_count_pass():
    """Test 8: the count pass renders the honest counting-frames label
    and never a made-up percentage."""
    from vidlift.gui import run_stage_label

    snapshot = {
        "state": "running",
        "current_phase": "upscale",
        "progress": {"step": UPSCALE_STEP_PREPARED_COUNT},
    }
    label = run_stage_label(snapshot)
    assert "counting frames" in label
    assert "no per-frame ETA exists yet" in label
    assert "%" not in label


def test_stage_label_names_chunk_progress():
    """Test 9: the promoted chunk counters render 'upscaling chunk 3/12'."""
    from vidlift.gui import run_stage_label

    label = run_stage_label(
        {
            "state": "running",
            "current_phase": "upscale",
            "progress": {"step": "chunk", "chunk_index": 3, "chunk_total": 12},
        }
    )
    assert label == "upscaling chunk 3/12"


def test_stage_label_encode_heartbeat():
    """Test 10: encode phase with encode_seconds renders the heartbeat."""
    from vidlift.gui import run_stage_label

    label = run_stage_label(
        {
            "state": "running",
            "current_phase": "encode",
            "progress": {"step": "encode", "encode_seconds": 42.4},
        }
    )
    assert label == "encoding (heartbeat t=42s)"


# ---- the measuring busy label + ETA busy-drop (§3c) --------------------------


class _BarHarness(QMainWindow):
    """The minimal widget set _consume_running_snapshot touches (bound
    to the real MainWindow methods; only the bridge bits are absent —
    the snapshot is handed in directly)."""

    def __init__(self, qapp: QApplication, snapshot: dict) -> None:
        super().__init__()
        from vidlift.ui.widgets import EtaProgressBar

        self.stage_label = QLabel("")
        self.stage_bar = EtaProgressBar()
        self.overall_bar = EtaProgressBar()
        self.run_log_tail = QPlainTextEdit()
        self._stage_now = "upscale"
        self._stage_eta = None
        self._stage_fraction = None
        self.run_total = 1
        self.run_counts = [None]
        self._progress_chunk_color = None
        # one job, one snapshot; the log tail is empty (separate tests)
        self.snapshot = snapshot
        self.job_ids = ["vl-test-1"]

        self.file_table = QTableWidget(1, 1)
        self.file_table.setItem(0, 0, QTableWidgetItem(""))

        self._consume_running_snapshot = (
            MainWindow._consume_running_snapshot.__get__(self)
        )
        self._set_text = MainWindow._set_text.__get__(self)
        self._render_log_tail = MainWindow._render_log_tail.__get__(self)
        self._consume_running_snapshot(0, snapshot, run_stage_label(snapshot))


def test_busy_label_measures_not_estimates(qapp):
    """Test 11: while frames_total is 0 (count pass still running) the
    busy pulse reads 'measuring — counting frames…', never a percent."""
    from vidlift.gui import _BUSY_MEASURING_LABEL

    snapshot = {
        "state": "running",
        "current_phase": "upscale",
        "progress": {"step": UPSCALE_STEP_PREPARED_COUNT},
    }
    win = _BarHarness(qapp, snapshot)
    assert win.stage_bar.eta_label.text() == _BUSY_MEASURING_LABEL
    assert "estimating ..." not in win.stage_bar.eta_label.text()


def test_eta_drops_busy_on_first_chunk(qapp):
    """Test 12: frames_done=100, frames_total=1000, eta_seconds=42.5
    flips the bar from busy to value mode on the same tick; the ETA
    pill fills; _eta_seconds math untouched (fed via the payload)."""
    snapshot = {
        "state": "running",
        "current_phase": "upscale",
        "eta_seconds": 42.5,
        "progress": {
            "step": "chunk",
            "chunk_index": 1,
            "chunk_total": 12,
            "frames_done": 100,
            "frames_total": 1000,
        },
    }
    win = _BarHarness(qapp, snapshot)
    assert win.stage_bar.bar.maximum() == 1000  # out of busy (0..0)
    assert win._stage_fraction == pytest.approx(0.1)
    assert win._stage_eta == 42.5
    assert "ETA 0:42" in win.stage_bar.eta_label.text()


def test_phase_note_suffix_rendered(qapp, monkeypatch):
    """Test 13: phase_note rides the stage label as a parenthetical
    suffix; an identical note on the next tick does not repaint the
    QLabel (setText fires only on change)."""

    snapshot = {
        "state": "running",
        "current_phase": "upscale",
        "progress": {
            "step": "chunk",
            "chunk_index": 2,
            "chunk_total": 12,
            "phase_note": "upscaling chunk 2/12 (no per-frame ETA during the AI pass)",
        },
    }
    win = _BarHarness(qapp, snapshot)
    assert "no per-frame ETA during the AI pass" in win.stage_label.text()
    calls: list[str] = []
    monkeypatch.setattr(win.stage_label, "setText", lambda t: calls.append(t))
    win._consume_running_snapshot(0, snapshot, run_stage_label(snapshot))
    assert calls == []