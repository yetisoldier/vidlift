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
]

MONITOR_HOST_DEFAULT = "0.0.0.0"  # LAN-visible, same as the sibling
MONITOR_PORT_DEFAULT = 8765
MONITOR_ENABLED_DEFAULT = True  # ON by default (design §4.1)

# The QSettings keys the GUI persists (§2.1's settings keys, QSettings
# spelling). monitor/token overrides MONITOR_TOKEN_DEFAULT when set.
QSETTINGS_MONITOR_KEYS = {
    "enabled": "monitor/enabled",
    "host": "monitor/host",
    "port": "monitor/port",
    "token": "monitor/token",
}