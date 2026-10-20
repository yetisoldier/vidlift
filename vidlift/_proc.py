"""Windows child-process console suppression (v0.4.4 design §6).

The packaged GUI exe is built ``console=False``, but that only silences
the top-level window: every child process the pipeline spawns on
Windows (ffmpeg, ffprobe, realesrgan-ncnn-vulkan, seconv, tesseract)
opens its own console window unless it is told not to. This helper
produces the exact Popen kwargs that suppress those windows; it is a
pure additive no-op on every other platform.

Rule for future call sites (carried here as the CONTRIBUTING-adjacent
note the design asks for): any new external-command call MUST route
its subprocess kwargs through :func:`popen_kwargs`. There must be no
``os.system`` and no ``shell=True`` path anywhere in VidLift; if one
is ever proposed, it too must route through this module first.
"""

from __future__ import annotations

import os
import subprocess

__all__ = ["popen_kwargs"]


def popen_kwargs() -> dict:
    """Kwargs for subprocess.Popen / subprocess.run suppressing child
    console windows on Windows. Empty dict elsewhere — pure additive.

    The STARTUPINFO/CREATE_NO_WINDOW symbols only exist in the
    subprocess module on Windows; a Windows build missing them is a
    broken interpreter, so this fails loudly instead of silently
    shipping pop-up consoles."""
    if os.name != "nt":
        return {}
    missing = [
        name
        for name in ("STARTUPINFO", "STARTF_USESHOWWINDOW", "SW_HIDE",
                     "CREATE_NO_WINDOW")
        if not hasattr(subprocess, name)
    ]
    if missing:
        raise RuntimeError(
            "subprocess lacks Windows console constants "
            f"({', '.join(missing)}); broken interpreter"
        )
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {
        "startupinfo": si,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }