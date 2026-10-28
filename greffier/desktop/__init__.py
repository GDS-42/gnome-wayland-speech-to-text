"""Desktop integration: text injection, global shortcuts, autostart, portals.

Everything in this package runs on the GLib main thread and reports through
callbacks invoked on that thread (see docs/ARCHITECTURE.md §3, §4 and §6).

Start-up order in the daemon::

    portal.register_host_app(bus, DAEMON_ID)   # before any other portal call
    injector = Injector(bus, cfg.injection)    # bus = connection owning DAEMON_ID
    hotkeys = HotkeyManager(bus, on_pressed, on_released, toggle_command)
    hotkeys.apply(cfg.shortcut, callback)
    # on Toggle / shortcut press / release (portal and ydotool wait ~400 ms after it):
    injector.note_shortcut_activity()
"""

from __future__ import annotations

from greffier.desktop import autostart, portal
from greffier.desktop.hotkeys import HotkeyManager, HotkeyStatus
from greffier.desktop.injection import Injector, InjectResult

__all__ = [
    "HotkeyManager",
    "HotkeyStatus",
    "InjectResult",
    "Injector",
    "autostart",
    "portal",
]
