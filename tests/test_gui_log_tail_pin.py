# v0.4.5 Slice 1 (D-tail) — the tail-pinned run log widget.

# Design docs/designs/v0.4.5-progress-honesty.md §2/§8 (tests 1-6):
# pin-to-tail by default, user-scroll override, re-engage at bottom
# (8 px tolerance), force-pin on job change, no repaint on identical
# text. Every test drives the SAME surface the 1 Hz tick uses
# (_render_log_tail) plus the MainWindow flags — no second code path.

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QMainWindow

from vidlift.gui import (
    _LOG_PIN_TOLERANCE_PX,
    MainWindow,
)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app):
    """A bare MainWindow-shaped harness: the log-pin machinery is a
    self-contained helper group on the class, so a real (bridge-less)
    instance exercises the production methods; only the bridge client
    is stubbed out (nothing in this file touches it)."""

    class _Client:
        def active_job_count(self):
            return 0

        def history(self, limit=8):
            return []

        def stop(self):
            pass

    win = MainWindow(client=_Client())
    return win


def _scrollbar(win):
    return win.run_log_tail.verticalScrollBar()


def _pump(app, spins=3):
    """Headless Qt delivers the scrollbar's valueChanged asynchronously
    (the delivered event lands on the NEXT event-loop pass, offscreen);
    real user scrolls are event-loop turns by construction, so the
    tests pump a few spins to match the production topology."""
    for _ in range(spins):
        app.processEvents()


def test_tail_pins_to_bottom_by_default(window, app):
    """Test 1: every re-render ends with the scrollbar at maximum."""
    sb = _scrollbar(window)
    window._render_log_tail("alpha\nbravo\ncharlie")
    assert sb.maximum() > 0
    assert sb.value() == sb.maximum()
    window._render_log_tail("alpha\nbravo\ncharlie\ndelta\necho")
    assert sb.value() == sb.maximum()


def test_user_scroll_up_disables_pin(window, app):
    """Test 2: scrolling to top, then a tick with new text, stays at
    the top — the user is inspecting; the pin never yanks them back.
    (Scrollable content: a max smaller than the tolerance cannot express
    a scrolled-away position at all.)"""
    seed = "\n".join(f"line {i}" for i in range(60))
    window._render_log_tail(seed)
    sb = _scrollbar(window)
    assert sb.maximum() > _LOG_PIN_TOLERANCE_PX
    sb.setValue(0)
    _pump(app)
    assert window._log_user_scrolled_up
    window._render_log_tail(seed + "\nnew tail line")
    assert sb.value() == 0


def test_return_to_bottom_reengages_pin(window, app):
    """Test 3: after the override, scrolling back to the bottom (within
    tolerance) re-engages the pin on the next tick."""
    seed = "\n".join(f"line {i}" for i in range(60))
    window._render_log_tail(seed)
    sb = _scrollbar(window)
    sb.setValue(0)
    _pump(app)
    assert window._log_user_scrolled_up
    sb.setValue(sb.maximum())  # back to tail
    _pump(app)
    assert not window._log_user_scrolled_up
    window._render_log_tail(seed + "\nnew tail line")
    assert sb.value() == sb.maximum()


def test_pin_tolerance_threshold(window, app):
    """Test 4: at 7 px above max the pin is still engaged; at 9 px
    above it is not — pinning _LOG_PIN_TOLERANCE_PX = 8 exactly."""
    assert _LOG_PIN_TOLERANCE_PX == 8
    window._render_log_tail("\n".join(f"line {i}" for i in range(60)))
    sb = _scrollbar(window)
    assert sb.maximum() > 32, "test needs a scrollable log"
    sb.setValue(sb.maximum() - 7)
    _pump(app)
    assert not window._log_user_scrolled_up
    sb.setValue(sb.maximum() - 9)
    _pump(app)
    assert window._log_user_scrolled_up


def test_force_pin_on_job_change(window, app):
    """Test 5: user scrolled up; the job set changes; the next tick
    scrolls to bottom ONCE; subsequent ticks stay in user mode."""
    window._render_log_tail("\n".join(f"job one line {i}" for i in range(60)))
    sb = _scrollbar(window)
    sb.setValue(0)
    _pump(app)
    assert window._log_user_scrolled_up
    # A new batch start re-keys the rendered job set (poll_jobs does
    # this from job_ids[0]); the force pin fires exactly once.
    window._log_rendered_key = None
    window._log_force_pin_next = True
    window._render_log_tail("\n".join(f"job two line {i}" for i in range(60)))
    assert sb.value() == sb.maximum()
    assert not window._log_force_pin_next
    # the override is still respected afterwards
    window._render_log_tail("\n".join(f"job two line {i}" for i in range(60)) + "\ngolf")
    assert sb.value() == sb.maximum()  # still pinned (fresh state)
    sb.setValue(0)
    _pump(app)
    assert window._log_user_scrolled_up
    window._render_log_tail(
        "\n".join(f"job two line {i}" for i in range(60)) + "\ngolf\nhotel"
    )
    assert sb.value() == 0


def test_no_repaint_on_identical_content(window, monkeypatch):
    """Test 6 (Rule 5): the same tail text across two ticks calls
    setPlainText at most once — the document is never churned."""
    window._render_log_tail("alpha\nbravo\ncharlie")
    calls: list[str] = []
    original = window.run_log_tail.setPlainText

    def spy(text):
        calls.append(text)
        return original(text)

    monkeypatch.setattr(window.run_log_tail, "setPlainText", spy)
    window._render_log_tail("alpha\nbravo\ncharlie")
    assert calls == []
    window._render_log_tail("alpha\nbravo\ncharlie\ndelta")
    assert len(calls) == 1