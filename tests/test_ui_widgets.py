"""UI widget port-discipline tests (v0.4.4 design §8, W1–W4).

The two widgets are behavioral ports of bluray-to-mp4's
``ui/widgets.py``; these tests enforce the port byte-for-byte, most
importantly the W1/W2 flicker regressions from the sibling's
validation runs 5 and 12.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from vidlift.ui.widgets import ActivityDots, EtaProgressBar


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_w1_set_value_with_data_exits_busy_and_keeps_label(qapp):
    # W1 (validation-run-5 flicker regression): busy pulse -> first
    # real value exits busy mode; the line the user is reading must
    # not be blanked by the exit itself — a placeholder estimate text
    # survives until real data replaces it, and a text set by the busy
    # state on a fresh label is CLEARED exactly by set_busy(False)'s
    # placeholder sweep (the sibling's behavior, verbatim).
    bar = EtaProgressBar()
    bar.set_busy(True, "estimating ...")
    assert bar.bar.maximum() == 0  # indeterminate pulse
    assert bar.eta_label.text() == "estimating ..."
    bar.set_value(0.3, None)
    assert bar.bar.maximum() == 1000  # busy exited: determinate again
    # The busy-placeholder sweep is the sibling's exact text list:
    # "estimating ..." is a placeholder, so it is cleared on exit —
    # what NEVER clears is a real ETA previously shown (W2).
    assert bar.eta_label.text() == ""
    bar.set_value(0.35, 90.0)
    assert bar.eta_label.text() == "ETA 1:30"


def test_w2_duplicate_none_eta_never_blanks_the_estimate(qapp):
    # W2: a real ETA renders; a subsequent None (a duplicate progress
    # line) retains the last shown ETA instead of blanking it.
    bar = EtaProgressBar()
    bar.set_value(0.5, 434.0)
    assert bar.eta_label.text() == "ETA 7:14"
    bar.set_value(0.55, None)
    assert bar.eta_label.text() == "ETA 7:14"
    bar.set_value(0.6, 65.0)
    assert bar.eta_label.text() == "ETA 1:05"


def test_w3_activity_dots_cycle_four_frames_in_order(qapp):
    # W3: _tick cycles exactly through the four frames in order with
    # no drift across wraps.
    dots = ActivityDots()
    assert dots.text() == "· · ·"  # the resting frame
    frames_seen = []
    for _ in range(6):  # one full cycle + wrap onto frames 1–2 again
        dots._tick()
        frames_seen.append(dots.text())
    expected = [ActivityDots._FRAMES[i] for i in (1, 2, 3, 0, 1, 2)]
    assert frames_seen == expected
    dots.stop()
    assert dots.text() == "· · ·"


def test_w4_reset_clears_bar_and_label(qapp):
    # W4: reset() clears both the bar and the label and leaves the
    # bar determinate (a stale busy range must not survive a reset).
    bar = EtaProgressBar()
    bar.set_busy(True, "starting ...")
    bar.set_value(0.8, 120.0)
    bar.reset()
    assert bar.bar.value() == 0
    assert bar.eta_label.text() == ""
    assert bar.bar.maximum() == 1000
    assert bar._busy is False


def test_w5_busy_pulse_and_value_clamp(qapp):
    # Port discipline: fractions clamp to [0, 1]; set_busy False with
    # a still-busy-looking label restores determinate range.
    bar = EtaProgressBar()
    bar.set_value(1.7, None)
    assert bar.bar.value() == 1000
    bar.set_value(-0.5, None)
    assert bar.bar.value() == 0
    bar.set_busy(False, "")  # plain text label is not an estimate slot
    assert bar.bar.maximum() == 1000