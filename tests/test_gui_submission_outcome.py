"""v0.4.22.0 submission seam (spec §7.3.2) — the §6.1 ``_submit_gui_job``
contract, pinned against scripted clients.

The trim removed the HEAD-era receipts (``submit_gui_job`` +
registry transactions, deferred as BRIDGE-1). One reviewed row now
enqueues through the .19 bridge methods and folds into exactly three
states:

- ``rejected``: a proven pre-mutation validation failure
  (``ValueError`` / ``FileNotFoundError`` / ``BatchTooLarge``);
  nothing changed, retry is allowed;
- ``unknown``: any other exception — row marked uncertain, retry
  disabled for that batch ("Inspect Recent jobs"), rather than risking
  a duplicate job;
- ``accepted``: exactly one non-empty job id handed back.

Conventions: QT_QPA_PLATFORM=offscreen, QSettings isolated to
tmp_path, scripted client double (the v0.4.13 boundary harness
style behind ``test_v0414_mode_b_gui``), REAL ``MainWindow`` consume
surface.
"""

from __future__ import annotations

import os

import pytest

from _gui_checks import gui_application

pytest.importorskip("PyQt6")

os.environ.setdefault("VIDLIFT_MONITOR_AUTOSTART", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QMessageBox

from vidlift.gui import MainWindow


@pytest.fixture
def qapp():
    app = gui_application()
    yield app


class _ScriptedEnqueueClient:
    """Minimal LocalBridgeClient double: the two enqueue surfaces only.

    Each call records its kwargs (the subs options dict shape is part
    of the §6.1 contract) and then raises or returns per script.
    """

    def __init__(self, subs_result=None, run_result=None, subs_error=None,
                 run_error=None):
        self.subs_result = subs_result
        self.run_result = run_result
        self.subs_error = subs_error
        self.run_error = run_error
        self.subs_calls: list[tuple] = []
        self.run_calls: list[tuple] = []

    def active_job_count(self):
        return 0

    def enqueue_subs(self, paths, **options):
        self.subs_calls.append((list(paths), dict(options)))
        if self.subs_error is not None:
            raise self.subs_error
        return list(self.subs_result or [])

    def enqueue_run(self, paths, options=None, engine="auto"):
        self.run_calls.append(
            (list(paths), None if options is None else dict(options), engine)
        )
        if self.run_error is not None:
            raise self.run_error
        return list(self.run_result or [])

    def stop(self, **kwargs):
        pass


@pytest.fixture
def seam_window(qapp, monkeypatch, tmp_path):
    """Factory: one REAL MainWindow per call with the scripted client.

    QSettings are pinned to a per-test ini file (house convention); the
    widget is deleted and events drained before the next test to keep
    deferred deletions inside the owning application.
    """
    windows: list = []

    def _make(client) -> MainWindow:
        def _settings(self):
            return QSettings(
                str(tmp_path / "settings.ini"), QSettings.Format.IniFormat
            )

        monkeypatch.setattr(MainWindow, "_settings", _settings)
        monkeypatch.setattr(
            QMessageBox, "information",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok),
        )
        win = MainWindow(client=client)
        windows.append(win)
        return win

    yield _make
    for win in windows:
        win.deleteLater()
    qapp.processEvents()


SUBS_OPTIONS = {"replace_source": False, "suppress_srt": False, "device": "auto"}


def test_validation_error_maps_to_rejected(seam_window):
    """§6.1: ValueError (bridge device validation) is provably
    pre-mutation -> "rejected", error carried, retry allowed."""
    client = _ScriptedEnqueueClient(
        subs_error=ValueError("device must be one of auto|cuda|cpu")
    )
    win = seam_window(client)
    outcome = win._submit_gui_job("subs", "/no/such/job.mp4", dict(SUBS_OPTIONS, device="bogus"))
    assert outcome.state == "rejected"
    assert isinstance(outcome.error, ValueError)
    assert outcome.job_ids == ()
    # The options dict must reach enqueue_subs EXACTLY as built: the
    # §6.1 shape, no batch_id key.
    assert client.subs_calls == [
        (["/no/such/job.mp4"],
         {"replace_source": False, "suppress_srt": False, "device": "bogus"})
    ]


def test_file_not_found_maps_to_rejected(seam_window):
    """The .19 bridge's missing-file validation is pre-write -> rejected."""
    client = _ScriptedEnqueueClient(
        subs_error=FileNotFoundError("no such file on this machine: /gone.mp4")
    )
    win = seam_window(client)
    outcome = win._submit_gui_job("subs", "/gone.mp4", dict(SUBS_OPTIONS))
    assert outcome.state == "rejected"
    assert isinstance(outcome.error, FileNotFoundError)


def test_generic_exception_maps_to_unknown(seam_window):
    """§6.1: an exception that is NOT a proven validation failure
    (mid-enqueue OSError, registry fault...) must fail CLOSED as
    "unknown" — the row is uncertain and retry stays disabled."""
    client = _ScriptedEnqueueClient(subs_error=OSError("synthetic registry fault"))
    win = seam_window(client)
    outcome = win._submit_gui_job("subs", "/media/ep.mkv", dict(SUBS_OPTIONS))
    assert outcome.state == "unknown"
    assert isinstance(outcome.error, OSError)
    assert outcome.job_ids == ()


def test_run_kind_generic_exception_maps_to_unknown(seam_window):
    """Same fail-closed rule for the Mode A (run) enqueue surface."""
    client = _ScriptedEnqueueClient(run_error=RuntimeError("synthetic post-record failure"))
    win = seam_window(client)
    outcome = win._submit_gui_job("run", "/media/ep.avi", {"engine_opts": 1}, engine="ncnn")
    assert outcome.state == "unknown"
    assert isinstance(outcome.error, RuntimeError)
    assert client.run_calls == [(["/media/ep.avi"], {"engine_opts": 1}, "ncnn")]


def test_single_job_id_maps_to_accepted(seam_window):
    """Exactly one non-empty id -> accepted with the tuple identity."""
    client = _ScriptedEnqueueClient(subs_result=["VJ7"])
    win = seam_window(client)
    outcome = win._submit_gui_job("subs", "/media/ep.mkv", dict(SUBS_OPTIONS))
    assert outcome.state == "accepted"
    assert outcome.job_ids == ("VJ7",)
    assert outcome.error is None


def test_run_kind_single_job_id_maps_to_accepted(seam_window):
    client = _ScriptedEnqueueClient(run_result=["VJ8"])
    win = seam_window(client)
    outcome = win._submit_gui_job("run", "/media/ep.avi", {}, engine="auto")
    assert outcome.state == "accepted"
    assert outcome.job_ids == ("VJ8",)


def test_zero_ids_maps_to_unknown(seam_window):
    """§6.1: an enqueue that yields NO exact ids cannot be
    distinguished from a possibly-recorded write -> "unknown"."""
    client = _ScriptedEnqueueClient(subs_result=[])
    win = seam_window(client)
    outcome = win._submit_gui_job("subs", "/media/ep.mkv", dict(SUBS_OPTIONS))
    assert outcome.state == "unknown"
    assert outcome.error is None
    assert outcome.job_ids == ()


def test_missing_job_id_maps_to_unknown(seam_window):
    """§6.1: a single EMPTY id is a missing receipt -> "unknown"
    (never "accepted" on a falsy id)."""
    client = _ScriptedEnqueueClient(subs_result=[""])
    win = seam_window(client)
    outcome = win._submit_gui_job("subs", "/media/ep.mkv", dict(SUBS_OPTIONS))
    assert outcome.state == "unknown"
    assert outcome.job_ids == ()
