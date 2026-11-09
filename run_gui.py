"""Frozen Windows GUI entry point (v0.4.4 design §2/§5).

Two unification duties live here so the GUI and any MCP agent share
one registry by default:

- When frozen, set ``VIDLIFT_STATE_DIR`` to the platform state dir
  BEFORE the bridge (registry/benchmark cache) resolves its default:
  Windows ``%LOCALAPPDATA%\\YetiWurks\\VidLift``, Linux
  ``~/.config/vidlift``, macOS ``~/Library/Application
  Support/VidLift``.
- Name the application and organization BEFORE the ``MainWindow`` is
  constructed so ``QSettings`` picks the canonical path
  (``YetiWurks/VidLift``) — the engine choice persists there (§7).
"""

from __future__ import annotations

import os
import platform
import sys


def _frozen_state_dir_env() -> None:
    """Pin the shared state dir for frozen builds (dev keeps the
    benchmark cache's own default). Idempotent: an explicit env var
    (or an already-resolved path) always wins."""
    if os.environ.get("VIDLIFT_STATE_DIR"):
        return
    if not getattr(sys, "frozen", False):
        return
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(
            __import__("pathlib").Path.home() / "AppData/Local"
        )
        default = f"{base}\\YetiWurks\\VidLift"
    elif system == "Darwin":
        default = str(
            __import__("pathlib").Path.home()
            / "Library/Application Support/VidLift"
        )
    else:
        default = str(__import__("pathlib").Path.home() / ".config" / "vidlift")
    os.environ["VIDLIFT_STATE_DIR"] = default


def main() -> int:
    _frozen_state_dir_env()
    from PyQt6.QtWidgets import QApplication

    # QSettings canonical path BEFORE MainWindow (v0.4.4 §7: the
    # engine choice persists under YetiWurks/VidLift everywhere).
    QApplication.setApplicationName("VidLift")
    QApplication.setOrganizationName("YetiWurks")

    from vidlift.gui import main as gui_main

    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())