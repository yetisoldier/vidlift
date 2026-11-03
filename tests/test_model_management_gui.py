"""v0.4.16 model-management GUI block (impl spec §3 tests 8-9).

The pill + Install button + QThreadPool worker + run-panel render.
pytest-qt is NOT in this suite; the house pattern is a real
QApplication + explicit ``processEvents`` pumping (the
test_monitor_gui.py pattern) with a REAL LocalBridgeClient over a tmp
state dir (executor OFF — the JobExecutor must never engage for an
install) and an injected fake ``fetch`` — never the network, never the
real model, never ``~/.vidlift``.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QApplication

import vidlift.gui as gui_mod
from vidlift.mcp_bridge.client_local import LocalBridgeClient
from vidlift.transcribe import (
    DEFAULT_MODEL,
    MODEL_SHA256,
    TranscriptionError,
)

from _bcanary import register_b

marker = register_b


# ---- shared fixtures / helpers --------------------------------------------------

_STUB_BIN = b"model-bytes-stub-header" + bytes(range(256)) * 6100
_STUB_SHA = hashlib.sha256(_STUB_BIN).hexdigest()  # > 1 MiB: >= MIN_DOWNLOAD_SIZE
_FAKE_DL = bytes(range(256)) * (2 * 1024 * 1024 // 256)  # 2 MiB fake download
_SUPPORT_SIZES = {
    "config.json": 2257,
    "tokenizer.json": 2203239,
    "vocabulary.txt": 459861,
}


@pytest.fixture(autouse=True)
def _transcribe_engine_on(monkeypatch):
    """The install/resolve seam refuses honestly under the suite-wide
    engine-off guard (conftest); the GUI install block drives the REAL
    resolve path, so the env guard is lifted for these tests."""
    monkeypatch.delenv("VIDLIFT_TRANSCRIPTION_ENGINE", raising=False)


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("VIDLIFT_STATE_DIR", raising=False)
    c = LocalBridgeClient(state_dir=tmp_path / "bridge", start_executor=False)
    yield c
    c.stop()


@pytest.fixture
def window(qapp, client, tmp_path, monkeypatch):
    """A REAL MainWindow over the tmp-dir client with isolated
    QSettings (the suite's R3 house pattern) + QMessageBox.warning
    recorded instead of shown."""
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QMessageBox

    ini = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)

    def _settings(self):
        return ini

    monkeypatch.setattr(gui_mod.MainWindow, "_settings", _settings)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    warnings: list[tuple[str, str]] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append((a[1], str(a[2]))))
    )
    win = gui_mod.MainWindow(client=client)
    win._test_warnings = warnings
    yield win
    win.job_ids = []
    win.close()
    qapp.processEvents()


def _wait_for(qapp, predicate, timeout: float = 10.0) -> bool:
    """Pump the Qt event loop until ``predicate()`` (the queued-signal
    delivery) or the deadline."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _patch_state_root(monkeypatch, tmp_path, subdir: str = "root") -> Path:
    """Point the window's pill/install resolution at an explicit
    per-test root (the suite-wide conftest isolation already keeps
    default_state_dir() off ~/.vidlift; this makes the OBSERVED root
    explicit and controllable)."""
    cold = tmp_path / subdir
    cold.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gui_mod, "default_state_dir", lambda: cold)
    return cold


def _seed_installed(root, tag: str = "medium") -> Path:
    target = Path(root) / "whisper" / "models" / tag
    target.mkdir(parents=True, exist_ok=True)
    (target / "model.bin").write_bytes(_STUB_BIN)
    (target / ".complete").write_text(f"{tag}\n{'0' * 64}\n", encoding="utf-8")
    return target


def _injected(monkeypatch, fetch, capture=None):
    """Monkeypatch the runnable CLASS so every construction carries the
    test's fake fetch (§2.8d's injection seam)."""
    real_runnable = gui_mod.ModelInstallRunnable

    class _InjectedRunnable(real_runnable):
        def __init__(self, tag, **kwargs):
            if capture is not None:
                capture.append(kwargs)
            kwargs["fetch"] = fetch
            super().__init__(tag, **kwargs)

    monkeypatch.setattr(gui_mod, "ModelInstallRunnable", _InjectedRunnable)


# ==========================================================================
# The pill states (G-1/G-2 + §2.8c's binding strings)
# ==========================================================================


def test_g1_pill_not_installed_with_install_button(window, tmp_path, monkeypatch):
    marker("model_g1_pill_not_installed")
    _patch_state_root(monkeypatch, tmp_path, "cold")
    window._refresh_subs_model_pill()
    assert window.subs_model_pill.text() == (
        "Subs model (medium): not installed — first subtitles use "
        "downloads ~1.5 GB"
    )
    assert window.subs_model_button.isVisibleTo(window)
    assert window.subs_model_button.isEnabled()
    assert window.subs_model_button.text() == "Install"


def test_g2_pill_installed_hides_button(window, tmp_path, monkeypatch):
    marker("model_g2_pill_installed")
    root = _patch_state_root(monkeypatch, tmp_path, "warm")
    _seed_installed(root)
    window._refresh_subs_model_pill()
    assert window.subs_model_pill.text() == "Subs model (medium): installed"
    assert not window.subs_model_button.isVisibleTo(window)


def test_g2b_construction_time_pill_has_truth(qapp, client, tmp_path, monkeypatch):
    """The pill's disk predicate ran at CONSTRUCTION (the pill is not
    blank before the first tick)."""
    marker("model_g2b_construction_truth")
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QMessageBox

    root = tmp_path / "warm2"
    _seed_installed(root)
    monkeypatch.setattr(gui_mod, "default_state_dir", lambda: root)
    ini = QSettings(str(tmp_path / "settings2.ini"), QSettings.Format.IniFormat)
    monkeypatch.setattr(
        gui_mod.MainWindow, "_settings", lambda self: ini
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    win = gui_mod.MainWindow(client=client)
    try:
        assert win.subs_model_pill.text() == "Subs model (medium): installed"
        assert not win.subs_model_button.isVisible()
    finally:
        win.job_ids = []
        win.close()
        qapp.processEvents()


# ==========================================================================
# The Install action (G-3..G-6)
# ==========================================================================


def test_g3_install_runs_to_completion_on_threadpool(
    window, qapp, tmp_path, monkeypatch
):
    """G-3: clicking Install schedules a QThreadPool runnable whose
    injected fake fetch feeds progress beats and lands the cache — the
    pill ends 'installed', the button hides, the board records the
    install, and the JobExecutor/registry are NEVER engaged (no job
    records created). The fetch gates on an event so the mid-flight
    render is deterministic."""
    marker("model_g3_install_completion")
    root = _patch_state_root(monkeypatch, tmp_path, "installs")
    monkeypatch.setitem(MODEL_SHA256, "medium", hashlib.sha256(_FAKE_DL).hexdigest())

    beats: list[tuple[int, int | None]] = []
    release = threading.Event()

    def fake_fetch(url, target_path, on_progress=None):
        release.wait(timeout=15)  # hold mid-flight for the render asserts
        path = Path(target_path)
        name = path.name
        if name.endswith(".part"):
            name = name[: -len(".part")]
        if name == "model.bin":
            done = 0
            with open(path, "wb") as fh:
                for i in range(0, len(_FAKE_DL), 1024 * 1024):
                    chunk = _FAKE_DL[i : i + 1024 * 1024]
                    fh.write(chunk)
                    done += len(chunk)
                    beats.append((done, len(_FAKE_DL)))
                    if on_progress is not None:
                        on_progress(done, len(_FAKE_DL))
        else:
            path.write_bytes(b"x" * _SUPPORT_SIZES[name])

    constructed: list[dict] = []
    _injected(monkeypatch, fake_fetch, constructed)
    assert not window.client.registry.queued()
    assert not window.client.registry.running()

    try:
        window.subs_model_button.click()
        qapp.processEvents()
        # mid-flight: the disabled "Downloading…" state per §2.8c
        assert window._subs_model_installing is True
        assert window.subs_model_button.isEnabled() is False
        assert window.subs_model_button.text() == "Downloading…"
        assert (
            "(total size unknown)" in window.subs_model_pill.text()
            or "downloading" in window.subs_model_pill.text().lower()
        )
    finally:
        release.set()

    assert constructed, "the runnable was constructed on the GUI thread"
    settled = _wait_for(
        qapp,
        lambda: window._subs_model_installing is False
        and not window.subs_model_button.isVisibleTo(window),
    )
    assert settled, "the install never settled"
    target = root / "whisper" / "models" / "medium"
    assert (target / ".complete").is_file()
    assert (target / "model.bin").is_file()
    assert beats, "the fake fetch fed progress beats"
    assert window._subs_model_installing is False
    assert window.subs_model_pill.text() == "Subs model (medium): installed"
    # NEVER the executor: no job records were created by the install
    assert not window.client.registry.queued()
    assert not window.client.registry.running()
    assert not list(window.client.registry.history())
    QThreadPool.globalInstance().waitForDone(15000)


def test_g4_install_failure_is_honest_and_retryable(
    window, qapp, tmp_path, monkeypatch
):
    """G-4: a fetch failure lands the warning box (with the retry
    wording), the pill honestly returns to not-installed, the button
    re-enables, the in-flight guard clears (a second click starts a
    second attempt), and the board record carries the error."""
    marker("model_g4_install_failure")
    _patch_state_root(monkeypatch, tmp_path, "fail")

    def fake_fetch(url, target_path, on_progress=None):
        raise TranscriptionError(
            "transcription model download failed (model.bin): check network access"
        )

    _injected(monkeypatch, fake_fetch)
    window.subs_model_button.click()
    settled = _wait_for(qapp, lambda: window._subs_model_installing is False)
    assert settled, "the failed install must settle (never hang the guard)"
    qapp.processEvents()
    assert len(window._test_warnings) == 1
    _title, body = window._test_warnings[0]
    assert "The model was NOT installed" in body
    assert "network" in body
    assert window.subs_model_pill.text() == (
        "Subs model (medium): not installed — first subtitles use "
        "downloads ~1.5 GB"
    )
    assert window.subs_model_button.isVisibleTo(window)
    assert window.subs_model_button.isEnabled()
    board_snap = window.client.services.model_install_board.snapshot()
    assert board_snap["state"] == "not-installed"
    assert board_snap["error"] == (
        "transcription model download failed (model.bin): check network access"
    )
    # the guard cleared: a second click schedules again
    window.subs_model_button.click()
    settled2 = _wait_for(qapp, lambda: len(window._test_warnings) == 2)
    assert settled2
    qapp.processEvents()
    assert window._subs_model_installing is False
    QThreadPool.globalInstance().waitForDone(15000)


def test_g5_in_flight_guard_exactly_one_runnable(
    window, qapp, tmp_path, monkeypatch
):
    """G-5: the click guard collapses a double-click (and a third
    click) to ONE runnable + ONE board.begin."""
    marker("model_g5_in_flight_guard")
    _patch_state_root(monkeypatch, tmp_path, "guard")
    release = threading.Event()
    scheduled: list[int] = []

    def fake_fetch(url, target_path, on_progress=None):
        scheduled.append(1)
        release.wait(timeout=15)
        raise TranscriptionError("released by the test")

    _injected(monkeypatch, fake_fetch)
    window.subs_model_button.click()
    try:
        window._install_subs_model()  # second click mid-flight
        window._install_subs_model()  # third, why not
        time.sleep(0.2)
        qapp.processEvents()
        assert len(scheduled) == 1, "the in-flight guard must hold"
    finally:
        release.set()
    settled = _wait_for(qapp, lambda: window._subs_model_installing is False)
    assert settled
    QThreadPool.globalInstance().waitForDone(20000)


def test_g6_unknown_total_never_fabricates_a_percent(
    window, tmp_path, monkeypatch
):
    """G-6: with ``bytes_total is None`` the pill names the download
    honestly ("(total size unknown)") — never a percent; the button is
    disabled while downloading. Driven directly through the board +
    one refresh (the render path the install worker's beats feed)."""
    marker("model_g6_unknown_total")
    _patch_state_root(monkeypatch, tmp_path, "unknown")
    board = window.client.services.model_install_board
    board.begin(DEFAULT_MODEL)
    board.update(bytes_done=734003200, bytes_total=None, file="model.bin")
    window._refresh_subs_model_pill()
    text = window.subs_model_pill.text()
    assert "(total size unknown)" in text
    assert "%" not in text
    assert window.subs_model_button.isEnabled() is False
    assert window.subs_model_button.text() == "Downloading…"


def test_g7_lock_wait_surfaces_then_completes(window, qapp, tmp_path, monkeypatch):
    """G-7: a second downloader (the GUI install) over a held lock
    surfaces its wait (an on_lock_wait beat carries 'waiting') and
    completes after the winner finishes + frees — WITHOUT a fetch of
    its own (the re-fast-path rule; zero double downloads)."""
    marker("model_g7_lock_wait")
    root = _patch_state_root(monkeypatch, tmp_path, "wait")
    target = root / "whisper" / "models" / "medium"
    target.mkdir(parents=True)
    from vidlift.transcribe import _model_download_lock

    holder_cm = _model_download_lock(target)
    holder_cm.__enter__()  # the pre-existing fresh lock (the race winner)
    started = threading.Event()
    release = threading.Event()

    def hold_then_win():
        started.set()
        release.wait(timeout=15)
        # the winner completes its cache and frees the lock
        (target / "model.bin").write_bytes(_STUB_BIN)
        (target / ".complete").write_text("medium\n0\n", encoding="utf-8")
        try:
            holder_cm.__exit__(None, None, None)
        except Exception:  # a double exit would raise; cleanup stays best-effort
            pass

    scheduled: list[int] = []

    def fake_fetch(url, target_path, on_progress=None):
        scheduled.append(1)
        raise AssertionError("the waiter must fast-path, never fetch")

    _injected(monkeypatch, fake_fetch)

    seen_infos: list[dict] = []
    real_progress = window._on_subs_model_progress

    def spying_progress(info):
        seen_infos.append(dict(info))
        real_progress(info)

    window._on_subs_model_progress = spying_progress
    holder = threading.Thread(target=hold_then_win, daemon=True)
    holder.start()
    board = window.client.services.model_install_board
    try:
        window.subs_model_button.click()
        waited = _wait_for(
            qapp,
            lambda: any(bool(info.get("waiting")) for info in seen_infos),
            timeout=10,
        )
        assert waited, "the lock wait surfaced through the progress signal"
    finally:
        release.set()
        holder.join(timeout=16)
        try:
            holder_cm.__exit__(None, None, None)
        except Exception:
            pass
    settled = _wait_for(
        qapp, lambda: board.snapshot()["state"] == "installed", timeout=25
    )
    assert settled, "the wait surfaced the lock and completed after it freed"
    assert started.is_set()
    assert scheduled == [], "no download ran while waiting (fast-path won)"
    QThreadPool.globalInstance().waitForDone(25000)


# ==========================================================================
# The run-panel render + stage bar (G-8/G-9/G-10/G-11)
# ==========================================================================


def _running_snapshot(progress: dict, phase: str) -> dict:
    return {
        "job_id": "vl-test-download",
        "state": "running",
        "current_phase": phase,
        "progress": dict(progress),
        "activity_age_seconds": 0.5,
        "eta_seconds": None,
        "log_tail": [],
        "commands": [],
        "phases": [],
        "started_at": time.time(),
        "last_activity_at": time.time(),
        "engine_simulated": False,
        "engine_note": None,
        "verification": None,
        "manifest_path": None,
        "error": None,
        "file_path": "/media/movie.avi",
    }


def test_g8_g9_g10_run_panel_download_render():
    """G-8/G-9/G-10: the download label renders in BOTH the transcribe
    (Mode B) and subtitles (Mode A) phases; a known total renders the
    percent; a None total renders the honest unknown and never a
    percent."""
    marker("model_g8_g9_g10_run_panel_render")
    from vidlift.gui import run_stage_label

    with_total = _running_snapshot(
        {
            "model_download": {
                "bytes_done": 734003200,
                "bytes_total": 1_531_000_000,
            },
            "step": "downloading transcription model (model.bin)",
        },
        "transcribe",
    )
    label = run_stage_label(with_total)
    assert label.startswith("downloading transcription model — ")
    assert "0.73 GB / 1.53 GB" in label and "(48%)" in label

    sub_phase = dict(with_total, current_phase="subtitles")
    assert run_stage_label(sub_phase) == label

    without_total = _running_snapshot(
        {"model_download": {"bytes_done": 123456, "bytes_total": None}},
        "transcribe",
    )
    label2 = run_stage_label(without_total)
    assert label2.startswith("downloading transcription model — ")
    assert "(total size unknown)" in label2
    assert "%" not in label2

    # a phase WITHOUT the download fact (or without bytes) keeps the
    # phase's own label — no download render over non-download work
    plain = _running_snapshot({"step": "transcribing segment 3"}, "transcribe")
    assert "downloading transcription model" not in run_stage_label(plain)


class _BarSpy:
    """The stage-bar recorder: set_value/set_busy calls in order."""

    def __init__(self):
        self.calls: list[tuple] = []

    def set_value(self, fraction, eta_seconds=None):
        self.calls.append(("value", fraction, eta_seconds))

    def set_busy(self, busy, text=""):
        self.calls.append(("busy", busy, text))

    def reset(self):
        self.calls.append(("reset",))


def test_g8b_stage_bar_determinate_only_with_total(
    window, qapp, tmp_path, monkeypatch
):
    """G-8b/G-10b/G-11: driving the REAL _consume_running_snapshot with
    a transcribe-phase download beat — with a total the stage bar takes
    ``set_value`` at the done/total fraction (exit busy) and the pill
    counts the same tick's bytes; with total None the bar stays busy
    (honest indeterminate)."""
    marker("model_g8b_stage_bar")
    _patch_state_root(monkeypatch, tmp_path, "tick")
    from vidlift.gui import run_stage_label

    # a REAL slate seeds run_counts/run_total/_batch_slots — the exact
    # attributes _consume_running_snapshot's bar accounting reads
    window.job_ids = ["vl-test-download"]
    window._begin_job_slate(["vl-test-download"])

    spy = _BarSpy()
    real_bar = window.stage_bar
    window.stage_bar = spy

    def consume(progress: dict, phase: str):
        snapshot = _running_snapshot(progress, phase)
        window._consume_running_snapshot(0, snapshot, run_stage_label(snapshot))

    try:
        consume(
            {
                "model_download": {
                    "bytes_done": 500_000_000,
                    "bytes_total": 1_000_000_000,
                }
            },
            "transcribe",
        )
        value_calls = [c for c in spy.calls if c[0] == "value"]
        assert value_calls, f"no determinate render: {spy.calls}"
        assert 0.49 <= value_calls[-1][1] <= 0.51
        assert spy.calls[-1][0] == "value", (
            "the download beat's set_value is the LAST bar write"
        )
        # G-11: the pill counted the same tick's bytes
        assert "0.50 GB / 1.00 GB" in window.subs_model_pill.text()

        # total None: the honest-indeterminate busy pulse stays
        spy.calls.clear()
        consume({"model_download": {"bytes_done": 500, "bytes_total": None}}, "transcribe")
        assert spy.calls and spy.calls[-1][0] == "busy"
    finally:
        window.stage_bar = real_bar


def test_g11b_pill_surfaces_in_run_download_during_tick(window, tmp_path, monkeypatch):
    """G-11 (idle-board complement): the tick path hands the pill the
    polled snapshot's model_download so an IN-RUN download counts bytes
    even when the board is empty (board covers idle installs; the tick
    covers in-run truth)."""
    marker("model_g11b_pill_in_run")
    _patch_state_root(monkeypatch, tmp_path, "tick2")
    from vidlift.gui import run_stage_label

    window.job_ids = ["vl-test-download"]
    window._begin_job_slate(["vl-test-download"])
    window._consume_running_snapshot(
        0,
        _running_snapshot(
            {
                "model_download": {
                    "bytes_done": 123,
                    "bytes_total": 1000,
                }
            },
            "transcribe",
        ),
        run_stage_label(
            _running_snapshot(
                {"model_download": {"bytes_done": 123, "bytes_total": 1000}},
                "transcribe",
            )
        ),
    )
    assert "downloading" in window.subs_model_pill.text()
    assert "0.00 GB" in window.subs_model_pill.text()