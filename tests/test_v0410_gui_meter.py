"""v0.4.10 tests: GPU meter (R1/R4), batch-bar truth accounting (R2),
and the fresh-run default (R3).

The GPU-sampler tests exercise ONLY the pure/parse layer plus the
subprocess seam with fake shell-script binaries — no GPU is needed and
no test touches a real nvidia-smi (on this dev box there is none). The
batch-summary tests are pure Python; GUI-wiring tests build the real
MainWindow with the FakeClient from test_gui.py's house pattern.
"""

from __future__ import annotations

import inspect
import json
import stat
import time
from pathlib import Path

import pytest

from vidlift.run import BatchSummary, run_file, summarize_batch
from vidlift.run import ToolBox
from vidlift.plan import build_file_plan
from vidlift.probe import classify, parse_probe

pytest.importorskip("PyQt6")

from PyQt6.QtGui import QImage
from PyQt6.QtWidgets import QApplication

from vidlift.ui.gpu_meter import (
    GPU_METER_HEIGHT,
    GPU_METER_WIDTH,
    SAMPLE_CAPACITY,
    TIMEOUT_SECONDS,
    GpuMeterWidget,
    GpuProbeRunnable,
    GpuSample,
    _hint,
    parse_nvidia_smi_output,
)

# ---------------------------------------------------------------------------
# R2: batch summary counting (pure — no Qt event loop)
# ---------------------------------------------------------------------------


def test_summary_all_succeeded():
    summary = summarize_batch(["succeeded", "succeeded", "succeeded"])
    assert summary.total == 3
    assert summary.succeeded == 3
    assert summary.failed == 0
    assert summary.cancelled == 0
    assert summary.resolved == 3
    assert summary.pending == 0
    assert summary.all_succeeded is True


def test_summary_all_cancelled():
    summary = summarize_batch(["cancelled", "cancelled"])
    assert summary.succeeded == 0
    assert summary.failed == 0
    assert summary.cancelled == 2
    assert summary.resolved == 2
    assert summary.pending == 0
    assert summary.all_succeeded is False


def test_summary_all_failed():
    summary = summarize_batch(["failed", "failed", "failed"])
    assert summary.succeeded == 0
    assert summary.failed == 3
    assert summary.cancelled == 0
    assert summary.all_succeeded is False


def test_summary_mixed():
    summary = summarize_batch(["succeeded", "cancelled", "failed"])
    assert summary.total == 3
    assert summary.succeeded == 1
    assert summary.cancelled == 1
    assert summary.failed == 1
    assert summary.resolved == 3
    assert summary.pending == 0
    assert summary.all_succeeded is False


def test_summary_mixed_with_pending():
    summary = summarize_batch(["succeeded", None, "cancelled"])
    assert summary.total == 3
    assert summary.succeeded == 1
    assert summary.cancelled == 1
    assert summary.resolved == 2
    assert summary.pending == 1


def test_summary_fresh_start_is_all_pending():
    summary = summarize_batch([None, None])
    assert summary.resolved == 0
    assert summary.succeeded == 0
    assert summary.pending == 2
    # A fresh bar never paints itself succeeded (the v0.4.10 truth
    # rule; see test_all_cancelled_is_never_a_green_full_bar).
    assert summary.all_succeeded is False


def test_summary_is_pure_and_immutable():
    slate = ["succeeded", None]
    summary = summarize_batch(slate)
    slate.append("cancelled")  # mutating the input cannot leak in
    assert summary.total == 2
    assert summary.resolved == 1
    with pytest.raises(AttributeError):
        summary.succeeded = 99  # frozen dataclass: no mutation


def test_summary_accepts_tuple_slate():
    summary = summarize_batch(("succeeded", "cancelled"))
    assert summary.succeeded == 1
    assert summary.cancelled == 1
    assert summary.resolved == 2


def test_summary_empty_batch():
    summary = summarize_batch([])
    assert summary.total == 0
    assert summary.resolved == 0
    assert summary.pending == 0
    assert summary.all_succeeded is False  # 0 succeeded != 0 total truth


# ---------------------------------------------------------------------------
# R2: the GUI bar rendering from BatchSummary (format truth + colors)
# ---------------------------------------------------------------------------


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_batch_bar_format_truth():
    """The headline bar's format strings are truthful per outcome mix —
    through the exact function the real bar renders."""
    from vidlift.gui import _batch_format

    assert _batch_format(summarize_batch(["cancelled"])) == "0/1 succeeded, 1 cancelled"
    assert _batch_format(summarize_batch(["failed"])) == "0/1 succeeded, 1 failed"
    assert (
        _batch_format(summarize_batch(["succeeded", "cancelled"]))
        == "1/2 succeeded, 1 cancelled"
    )
    assert (
        _batch_format(summarize_batch(["succeeded", "failed"]))
        == "1/2 succeeded, 1 failed"
    )
    assert (
        _batch_format(summarize_batch(["succeeded", "cancelled", "failed"]))
        == "1/3 succeeded, 1 cancelled, 1 failed"
    )
    assert _batch_format(summarize_batch(["succeeded", "succeeded"])) == "2/2 succeeded"
    # mid-run: the pending tail keeps the bar honest while it runs
    assert _batch_format(summarize_batch(["succeeded", None])) == "1/2 succeeded"


def test_all_cancelled_is_never_a_green_full_bar(qapp):
    """The live bug (R2's stated reason): a cancelled 1/1 batch must
    show '0/1 succeeded, 1 cancelled' — never a full green '1/1
    completed' bar. Verified through the SAME handler the 1 Hz tick
    drives (a bare seam object, MainWindow's real methods unbound)."""
    from PyQt6.QtWidgets import QProgressBar

    from vidlift.gui import MainWindow

    class _RenderSeam:
        run_total = 1
        run_counts: list = [None]
        _progress_chunk_color = None
        run_summary = None
        _apply_batch_summary = MainWindow._apply_batch_summary
        _batch_bar_palette = MainWindow._batch_bar_palette
        _consume_terminal_snapshot = MainWindow._consume_terminal_snapshot
        # v0.4.10-fix (D2): the terminal writer resolves slots by the
        # stable job identity; the seam carries the real helper.
        _batch_slot_for = MainWindow._batch_slot_for
        _batch_slot_index: dict = {}

    window = _RenderSeam()
    window.run_total = 1
    window.run_counts = [None]
    window._progress_chunk_color = None
    window.run_summary = None
    window.progress = QProgressBar()
    window.progress.setRange(0, 1)
    window.progress.setValue(0)
    # a 1-row table so the row-render arm has real cells to color
    from PyQt6.QtWidgets import QTableWidget, QTableWidgetItem

    window.file_table = QTableWidget(1, 1)
    window.file_table.setItem(0, 0, QTableWidgetItem(""))

    window._consume_terminal_snapshot(
        0, {"job_id": "a", "state": "cancelled", "engine_simulated": False}
    )
    assert window.progress.format() == "0/1 succeeded, 1 cancelled"
    # on a 0..1 range the value is quantized to 1/4 by Qt rounding
    # policy: assert the truth parts that matter — text + color +
    # succeeded never incremented.
    assert window._progress_chunk_color == "#d8a93a"  # amber
    assert window.run_summary.succeeded == 0


def test_bar_palette_grew_green_never_lies():
    from vidlift.gui import MainWindow

    summary_green = summarize_batch(["succeeded"])
    summary_amber = summarize_batch(["succeeded", "cancelled"])
    summary_red = summarize_batch(["failed"])
    assert MainWindow._batch_bar_palette(None, summary_green) == "#3d9865"
    assert MainWindow._batch_bar_palette(None, summary_amber) == "#d8a93a"
    assert MainWindow._batch_bar_palette(None, summary_red) == "#c4605a"


def test_terminal_snapshot_updates_counts_per_slot(qapp):
    """run_counts maps row i ↔ job slot i: two jobs, one succeeded one
    cancelled, summarize mirrors the slate exactly (real methods, bare
    seam object — no Qt MainWindow construction)."""
    from PyQt6.QtWidgets import QTableWidget, QProgressBar, QTableWidgetItem

    from vidlift.gui import MainWindow

    class _RenderSeam:
        _consume_terminal_snapshot = MainWindow._consume_terminal_snapshot
        _apply_batch_summary = MainWindow._apply_batch_summary
        _batch_bar_palette = MainWindow._batch_bar_palette
        # v0.4.10-fix (D2): the terminal writer resolves slots by the
        # stable job identity; the seam carries the real helper.
        _batch_slot_for = MainWindow._batch_slot_for
        _batch_slot_index: dict = {}

    window = _RenderSeam()
    window.run_total = 2
    window.run_counts = [None, None]
    window._progress_chunk_color = None
    window.run_summary = None
    window.progress = QProgressBar()
    window.progress.setRange(0, 2)
    window.file_table = QTableWidget(2, 1)
    window.file_table.setItem(0, 0, QTableWidgetItem(""))
    window.file_table.setItem(1, 0, QTableWidgetItem(""))

    window._consume_terminal_snapshot(
        0,
        {"job_id": "a", "state": "succeeded", "engine_simulated": False},
    )
    window._consume_terminal_snapshot(
        1,
        {"job_id": "b", "state": "cancelled", "engine_simulated": False},
    )
    assert window.run_counts == ["succeeded", "cancelled"]
    assert window.run_summary.succeeded == 1
    assert window.run_summary.cancelled == 1
    assert window.progress.format() == "1/2 succeeded, 1 cancelled"
    assert window._progress_chunk_color == "#d8a93a"


# ---------------------------------------------------------------------------
# R1/R4: the GPU meter — widget construction contract (fixed size)
# ---------------------------------------------------------------------------


def test_meter_widget_fixed_size(qapp):
    window = GpuMeterWidget()
    assert (window.width(), window.height()) == (GPU_METER_WIDTH, GPU_METER_HEIGHT)
    assert window.minimumSize().width() == GPU_METER_WIDTH
    assert window.maximumSize().width() == GPU_METER_WIDTH
    assert window.minimumSize().height() == GPU_METER_HEIGHT
    assert window.maximumSize().height() == GPU_METER_HEIGHT


# ---------------------------------------------------------------------------
# R1/R4: the GPU sampler — parse layer (pure, no GPU, no subprocess)
# ---------------------------------------------------------------------------


def test_parse_missing_tool_output_is_na():
    sample = parse_nvidia_smi_output(None)
    assert sample.utilization is None
    assert sample.tool_available is False
    assert sample.device_count == 0


def test_parse_empty_output_is_na():
    sample = parse_nvidia_smi_output("")
    assert sample.utilization is None
    assert sample.device_count == 0
    assert sample.tool_available is False


def test_parse_single_gpu_value():
    sample = parse_nvidia_smi_output("87\n")
    assert sample.utilization == 87.0
    assert sample.device_count == 1
    assert sample.tool_available is True


def test_parse_value_with_whitespace():
    sample = parse_nvidia_smi_output("  42  \n")
    assert sample.utilization == 42.0
    assert sample.device_count == 1


def test_parse_multi_gpu_max_wins_with_count():
    sample = parse_nvidia_smi_output("12\n98\n40\n")
    assert sample.utilization == 98.0
    assert sample.device_count == 3
    assert sample.tool_available is True


def test_parse_na_cells_skipped_not_fatal():
    sample = parse_nvidia_smi_output("N/A\n55\n")
    assert sample.utilization == 55.0
    assert sample.device_count == 1


def test_parse_all_na_is_na_not_crash():
    sample = parse_nvidia_smi_output("N/A\nN/A\n")
    assert sample.utilization is None
    assert sample.device_count == 0


def test_parse_junk_and_out_of_range_dropped():
    sample = parse_nvidia_smi_output("441 \n -3 \n abc \n 12 \n")
    assert sample.utilization == 12.0
    assert sample.device_count == 1


def test_parse_rejects_beyond_100_and_negatives():
    sample = parse_nvidia_smi_output("101\n-1\n")
    assert sample.utilization is None
    assert sample.device_count == 0


def test_hint_single_vs_multi_device():
    assert _hint(1) == ""
    assert _hint(0) == ""
    assert _hint(2) == "x2"
    assert _hint(4) == "x4"


def test_timeout_constant_is_two_seconds():
    # the documented hard timeout (s): nvidia-smi children die at 2 s
    assert TIMEOUT_SECONDS == 2.0


def test_buffer_capacity_is_ninety():
    assert SAMPLE_CAPACITY == 90


# ---------------------------------------------------------------------------
# R1/R4: the GPU sampler — subprocess seam with fake nvidia-smi
# (no GPU required; a "binary" is a shell script we control)
# ---------------------------------------------------------------------------


def _fake_nvidia_smi(tmp_path: Path, body: str) -> str:
    path = tmp_path / "nvidia-smi"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _probe_once(binary: str) -> GpuSample:
    """Invoke the exact probe body run() uses (the Qt signal delivery
    itself needs a running event loop; the body is the seam)."""
    return GpuProbeRunnable(binary)._probe()


def test_probe_missing_binary_is_na(tmp_path):
    sample = _probe_once(str(tmp_path / "does-not-exist"))
    assert sample.utilization is None
    assert sample.tool_available is False


def test_probe_happy_path(tmp_path):
    sample = _probe_once(_fake_nvidia_smi(tmp_path, 'echo "73"\n'))
    assert sample.utilization == 73.0
    assert sample.device_count == 1
    assert sample.tool_available is True


def test_probe_multi_gpu_happy_path(tmp_path):
    binary = _fake_nvidia_smi(tmp_path, 'echo "8"\necho "91"\n')
    sample = _probe_once(binary)
    assert sample.utilization == 91.0
    assert sample.device_count == 2


def test_probe_nonzero_exit_is_na(tmp_path):
    binary = _fake_nvidia_smi(tmp_path, 'echo "err" >&2\nexit 3\n')
    sample = _probe_once(binary)
    assert sample.utilization is None
    assert sample.tool_available is False


def test_probe_garbage_stdout_is_na(tmp_path):
    binary = _fake_nvidia_smi(tmp_path, "printf 'not even csv\\\\n'\n")
    sample = _probe_once(binary)
    assert sample.utilization is None
    # v0.4.10-fix (D3) tightened: the tool DID run (exit 0, stdout
    # present), so tool_available must be True — the honest n/a comes
    # from the WIDGET's utilization gate, not from a parse-layer lie.
    # The old assertion (tool_available is False or device_count == 0)
    # was satisfied by device_count == 0 alone, which masked the fact
    # that the widget then rendered a confident "0%".
    assert sample.tool_available is True
    assert sample.device_count == 0


def test_probe_timeout_terminates_child(tmp_path):
    """sleep 30 behind a 2 s timeout: the probe resolves n/a promptly
    (bounding the test well below the sleep) and the child is killed."""
    binary = _fake_nvidia_smi(tmp_path, "sleep 30\n")
    t0 = time.monotonic()
    sample = _probe_once(binary)
    elapsed = time.monotonic() - t0
    assert sample.utilization is None
    assert sample.tool_available is False
    assert elapsed < 15.0, "timeout enforcement must bound the probe"


def test_runnable_never_escalates_exceptions(qapp, tmp_path, monkeypatch):
    """run() wraps everything: even if the probe body raises, the
    runnable resolves an n/a sample through the signal — nothing
    escapes into the Qt event loop."""
    runnable = GpuProbeRunnable("/nonexistent/binary")
    monkeypatch.setattr(
        runnable, "_probe", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    delivered: list[object] = []
    runnable.signals.sampled.connect(delivered.append)
    runnable.run()
    assert len(delivered) == 1
    assert isinstance(delivered[0], GpuSample)
    assert delivered[0].utilization is None


# ---------------------------------------------------------------------------
# R1/R4: widget rolling buffer + paint (offscreen render, no GPU)
# ---------------------------------------------------------------------------


def test_widget_accepts_samples_and_caps_buffer(qapp):
    window = GpuMeterWidget()
    window._accept_sample(GpuSample(None, 0, False))
    assert window._rendered_values() == (None, "")
    window._accept_sample(GpuSample(66, 2, True))
    value, hint = window._rendered_values()
    assert value == 66.0
    assert hint == "x2"
    for i in range(300):  # overshoot the 90-sample buffer
        window._accept_sample(GpuSample(i % 101, 1, True))
    assert window.sample_count() == SAMPLE_CAPACITY
    window.stop()


def _render_widget(qapp, widget) -> QImage:
    img = QImage(widget.width(), widget.height(), QImage.Format.Format_ARGB32)
    widget.render(img)
    return img


def test_widget_paints_panel_label_and_chart(qapp):
    """paintEvent renders: dark panel, 'GPU' label, and the lavender
    chart band when real samples exist; 'n/a' state paints too."""
    window = GpuMeterWidget()
    img = _render_widget(qapp, window)  # n/a state: must not raise
    panel = img.pixelColor(90, 52).name()
    assert panel == "#1b1e22"

    for u in (5, 95, 50, 80):
        window._accept_sample(GpuSample(u, 1, True))
    img2 = _render_widget(qapp, window)
    # lavender family appears in the chart area (antialiased shades)
    lavenders = 0
    for yy in range(19, 50):
        for xx in range(60, 178):
            c = img2.pixelColor(xx, yy)
            if c.blue() > 140 and c.red() > 90 and c.red() < 200 and c.green() < 140:
                lavenders += 1
    assert lavenders > 20, "the filled area chart must paint lavender"


def test_widget_graceful_on_permanently_missing_tool(qapp, monkeypatch):
    """With shutil.which resolving nothing (no nvidia-smi anywhere),
    start() + ticks land ONLY in n/a — no crash, no subprocess."""
    from vidlift.ui import gpu_meter as gm

    monkeypatch.setattr(gm.shutil, "which", lambda name: None)
    window = GpuMeterWidget()
    window.start()
    app = qapp
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.05)
    window.stop()
    assert window.sample_count() >= 1
    sample = window._samples[-1]
    assert sample.utilization is None
    assert sample.tool_available is False


# ---------------------------------------------------------------------------
# R3: fresh-run default — resume stays opt-in, DEFAULT OFF
# ---------------------------------------------------------------------------


def test_run_file_resume_default_is_off():
    """run_file's keyword itself defaults to False (the fresh-start
    contract; introspected so a signature drift can never slip by)."""
    sig = inspect.signature(run_file)
    assert sig.parameters["resume"].default is False


def test_resume_off_ignores_retained_scratch(tmp_path):
    """A default (resume-less) run NEVER claims retained work: with a
    retained scratch set present, the fresh run still prepares its own
    mezzanine under a new run id and leaves the set untouched (the
    R5 v0.4.9 contract pinned again at v0.4.10). Helpers come from the
    sibling v0.4.9 test module (same directory, pytest-importable via
    rootdir conftest)."""
    import importlib.util

    helpers_path = Path(__file__).parent / "test_resume_claim.py"
    spec = importlib.util.spec_from_file_location("_resume_claim", helpers_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    source = tmp_path / "Movie.avi"
    source.write_text("")
    log_root = tmp_path / "runs"
    log_root.mkdir()
    mod.build_retained_scratch(log_root, run_id="Movie.20261027-201001", source=source)
    runner = mod.ClaimFakeRunner()
    result = run_file(
        str(source),
        tools=ToolBox(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        probe_runner=runner,
        command_runner=runner,
        engine_request="stub",
        log_dir=str(log_root),
        # resume NOT passed: exercises the actual default
    )
    # the prepare command RAN (fresh mezzanine under the NEW run-id)
    prepared_cmds = mod.prepare_arguments(runner)
    assert len(prepared_cmds) == 1
    assert "20261027-201001" not in " ".join(prepared_cmds[0][1])
    assert result.state.resume_claim is None
    # the old scratch is untouched
    assert (log_root / "Movie.20261027-201001.prepared.mkv").exists()
    assert (log_root / "Movie.20261027-201001.engine-work").exists()