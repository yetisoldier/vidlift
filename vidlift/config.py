"""Monitor settings defaults (v0.4.6 design §2.1/§3.3).

The sibling bluray-to-mp4 ships a JSON Settings file with the
monitor_* keys; VidLift's GUI persists through QSettings
(``vidlift.gui.MainWindow._settings``), so this module carries only
the constants both the GUI and the monitor share — the baked default
token, the default bind, and the persisted key names. A QSettings
value that never lands reads as the default; ``monitor/token`` stored
empty means local-only open (the handler serves /health to anyone and
401s nothing when the token is empty).

Private repo, so LAN monitoring ships ON by default (the sibling's
v0.1.3+ call): the read-only API binds 0.0.0.0:8765 with the baked
token. Rotate MONITOR_TOKEN_DEFAULT if this repo's visibility ever
changes.
"""

from __future__ import annotations

from .monitor import MONITOR_TOKEN_DEFAULT

__all__ = [
    "MONITOR_HOST_DEFAULT",
    "MONITOR_PORT_DEFAULT",
    "MONITOR_TOKEN_DEFAULT",
    "MONITOR_ENABLED_DEFAULT",
    "QSETTINGS_MONITOR_KEYS",
    "TRANSCRIPTION_DEVICE_DEFAULT",
    "TRANSCRIPTION_ENABLED_DEFAULT",
    "TRANSCRIPTION_LANGUAGE_DEFAULT",
    "TRANSCRIPTION_MODEL_DEFAULT",
    "TRANSCRIPTION_VAD_DEFAULT",
    "QSETTINGS_TRANSCRIPTION_KEYS",
    "DENOISE_DEFAULT",
    "QSETTINGS_DENOISE_KEYS",
]

MONITOR_HOST_DEFAULT = "0.0.0.0"  # LAN-visible, same as the sibling
MONITOR_PORT_DEFAULT = 8765
MONITOR_ENABLED_DEFAULT = True  # ON by default (design §4.1)

# ---- v0.4.14 (transcription subtitles) ------------------------------------
#
# Eric's LOCKED decisions (2026-11-01; do not relitigate):
# 1. Mode A (upscale fallback transcription): ENABLED by default. The
#    GUI checkbox / config override stays (a user may disable), but the
#    SHIPPED default is ON.
# 2. Model delivery: first-run download of the `medium` model into the
#    machine-shared cache under the state dir (sha256-pinned per model);
#    config pin override for a large-v3 tag or a user model path.

# Mode A switch — ON by default (ERIC'S LOCKED DEFAULT). The planner's
# absent-default reads this constant (plan.build_file_plan), so the CLI
# run path and the GUI checkbox default all land on it from HERE.
TRANSCRIPTION_ENABLED_DEFAULT = True
# The default model tag (first-run download; sha-pinned in
# vidlift.transcribe.MODEL_SHA256).
TRANSCRIPTION_MODEL_DEFAULT = "medium"
# device auto -> cuda when CTranslate2 sees one, else the documented
# int8 CPU fallback (~1x realtime).
TRANSCRIPTION_DEVICE_DEFAULT = "auto"
# Empty = per-file auto-detect (the design's language rule).
TRANSCRIPTION_LANGUAGE_DEFAULT = ""
# VAD prefilter stays ON (the design's hallucination discipline).
TRANSCRIPTION_VAD_DEFAULT = True
# The QSettings keys the GUI persists for the Mode A checkbox (the
# monitor/QSETTINGS pattern).
QSETTINGS_TRANSCRIPTION_KEYS = {
    "enabled": "transcription/enabled",
    "model": "transcription/model",
    "device": "transcription/device",
    "language": "transcription/language",
}

# ---- v0.4.15 (opt-in denoise) ----------------------------------------------
#
# The prepare-chain denoise mode: "off" (default — byte-identical
# v0.4.14 prepare commands), "light", or "strong" (the hqdn3d presets
# in vidlift.run.DENOISE_PRESETS). OFF by default: the guidance (GUI
# tooltip, CLI help, docs) describes exactly when to flip it; a clean
# digital source must never pay denoise's detail cost by accident.
DENOISE_DEFAULT = "off"

# The QSettings keys the GUI persists for the denoise combo (the
# monitor/QSETTINGS pattern, v0.4.15).
QSETTINGS_DENOISE_KEYS = {
    "mode": "denoise/mode",
}

# The QSettings keys the GUI persists (§2.1's settings keys, QSettings
# spelling). monitor/token overrides MONITOR_TOKEN_DEFAULT when set.
QSETTINGS_MONITOR_KEYS = {
    "enabled": "monitor/enabled",
    "host": "monitor/host",
    "port": "monitor/port",
    "token": "monitor/token",
}