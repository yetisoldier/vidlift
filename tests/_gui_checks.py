"""Bounded event draining for asynchronous GUI preparation in isolated tests."""
import time

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

_application = None


def gui_application():
    """Retain one QApplication across function-scoped GUI fixtures.

    Deferred widget deletions and worker signal shims must not outlive the
    application that owns them. This retains no production settings/services.
    """
    global _application
    _application = QApplication.instance() or QApplication([])
    return _application


def wait_for_checks(window, timeout=5.0):
    gui_application()
    deadline = time.monotonic() + timeout
    while window._preflights and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(5)
    assert not window._preflights, "Preparation did not settle before the test deadline"


def reveal_workflow_widget(window, widget):
    """Select the owning workflow before inspecting its rendered controls."""
    for scroll in (window.setup_scroll, window.subs_setup_scroll):
        if scroll.isAncestorOf(widget):
            window.workflow_tabs.setCurrentWidget(scroll)
            QApplication.processEvents()
            scroll.ensureWidgetVisible(widget)
            QApplication.processEvents()
            return scroll.viewport()
    # Shared controls are outside both scroll areas. mapTo requires an
    # ancestor on older Qt builds, not an unrelated workflow viewport.
    return window.centralWidget()
