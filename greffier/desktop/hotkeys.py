"""Global shortcut management: GNOME custom shortcut or GlobalShortcuts portal.

* ``toggle`` mode (default): a GNOME custom shortcut runs ``toggle_command``
  (a ``gdbus call …Toggle`` argv supplied by the daemon). The daemon is not
  involved in key handling at all.
* ``push-to-talk`` mode: the GlobalShortcuts portal; ``Activated`` calls
  ``on_pressed`` and ``Deactivated`` calls ``on_released``.

Only one backend holds the shortcut at a time: switching removes the other.
:meth:`HotkeyManager.apply` is idempotent, so it can be called after every
configuration change without making the portal show its dialog again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass

from gi.repository import Gio, GLib

from greffier.config import ShortcutConfig
from greffier.desktop.global_shortcuts import GlobalShortcutsPortal
from greffier.desktop.gnome_keybinding import GnomeKeybinding, accelerator_label
from greffier.i18n import _

log = logging.getLogger(__name__)


@dataclass
class HotkeyStatus:
    """What is actually bound (reported by the daemon's GetStatus)."""

    backend: str  # gnome | portal | none
    active: bool
    accelerator: str
    mode: str  # effective mode: toggle | push-to-talk
    message: str = ""  # localised warning/error, "" when all is fine

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


StatusCallback = Callable[[HotkeyStatus], None]


def _join(*parts: str) -> str:
    return " ".join(p for p in parts if p)


_TOKEN_ALIASES = {"control": "ctrl", "primary": "ctrl", "logo": "super", "meta": "super",
                  "mod4": "super", "mod1": "alt"}


def same_trigger(description: str, accelerator: str) -> bool:
    """Whether a portal ``trigger_description`` (e.g. ``Super+Alt+D``) names
    the same keys as a GTK accelerator, whatever the order and spelling."""
    def tokens(text: str) -> frozenset[str]:
        parts = [p.strip().lower() for p in text.replace("<", "+").replace(">", "+").split("+")]
        return frozenset(_TOKEN_ALIASES.get(p, p) for p in parts if p)

    return tokens(description) == tokens(accelerator)


class HotkeyManager:
    """Owns Greffier's global shortcut."""

    def __init__(
        self,
        bus: Gio.DBusConnection,
        on_pressed: Callable[[], None],
        on_released: Callable[[], None],
        toggle_command: list[str],
        *,
        gnome: GnomeKeybinding | None = None,
        portal: GlobalShortcutsPortal | None = None,
    ) -> None:
        self._bus = bus
        self._on_pressed = on_pressed
        self._on_released = on_released
        self._toggle_command = list(toggle_command)
        self._gnome = gnome or GnomeKeybinding()
        self._portal = portal or GlobalShortcutsPortal(
            bus, self._pressed, self._released, on_closed=self._portal_closed)
        self.status = HotkeyStatus("none", False, "", "toggle")
        self._applied: tuple[str, str, str] | None = None  # (backend, accelerator, mode)
        self._pending: tuple[ShortcutConfig, StatusCallback | None] | None = None
        self._busy = False
        self._closed = False

    # -- portal callbacks ----------------------------------------------
    def _pressed(self) -> None:
        if not self._closed:
            self._on_pressed()

    def _released(self) -> None:
        if not self._closed:
            self._on_released()

    def _portal_closed(self) -> None:
        if self.status.backend == "portal":
            log.warning("the GlobalShortcuts session was closed")
            self.status = HotkeyStatus("portal", False, self.status.accelerator, self.status.mode,
                                       _("The global shortcut was removed by the system."))
            self._applied = None

    # -- apply ---------------------------------------------------------
    def apply(self, cfg: ShortcutConfig, callback: StatusCallback | None = None) -> None:
        """Bind the shortcut described by ``cfg``; ``callback(status)`` when done
        (always from the main loop, never from inside this call).

        Calls made while a portal request is in flight are coalesced: only
        the latest configuration is applied (and every callback is answered).
        """
        if self._closed:
            raise RuntimeError("HotkeyManager is closed")
        if self._pending is not None and self._pending[1] is not None:
            previous_cb = self._pending[1]
            chained = callback

            def both(status: HotkeyStatus) -> None:
                previous_cb(status)
                if chained is not None:
                    chained(status)

            callback = both
        self._pending = (cfg, callback)
        if not self._busy:
            self._next()

    def _next(self) -> None:
        if self._pending is None or self._closed:
            self._busy = False
            return
        cfg, callback = self._pending
        self._pending = None
        self._busy = True

        def done(status: HotkeyStatus) -> None:
            self.status = status
            level = logging.INFO if status.active else logging.WARNING
            log.log(level, "shortcut %s via %s (%s), active=%s %s", status.accelerator,
                    status.backend, status.mode, status.active, status.message)
            if callback is not None:
                try:
                    callback(status)
                except Exception:
                    log.exception("hotkey status callback failed")
            # Let a coalesced apply() run after this one.
            GLib.idle_add(lambda: (self._next(), GLib.SOURCE_REMOVE)[1])

        if self._portal.probed:
            # Always answer asynchronously, even when nothing needs D-Bus.
            GLib.idle_add(lambda: (self._plan(cfg, done), GLib.SOURCE_REMOVE)[1])
        else:
            self._portal.probe(lambda _v: self._plan(cfg, done))

    def _plan(self, cfg: ShortcutConfig, done: StatusCallback) -> None:
        if self._closed:
            return
        accel = cfg.accelerator
        if cfg.backend == "none" or not accel.strip():
            self._remove_all()
            self._applied = None
            done(HotkeyStatus("none", False, accel, cfg.mode))
            return
        if cfg.backend == "gnome":
            note = ""
            if cfg.mode == "push-to-talk":
                note = _("Push-to-talk needs the GlobalShortcuts portal; the GNOME shortcut "
                         "works in toggle mode.")
            self._use_gnome(cfg, note, done)
        elif cfg.backend == "portal":
            self._use_portal(cfg, done, fallback=False)
        elif cfg.mode == "push-to-talk":
            if self._portal.available():
                self._use_portal(cfg, done, fallback=True)
            else:
                self._use_gnome(cfg, _(
                    "Push-to-talk needs the GlobalShortcuts portal (GNOME 48 or later); the "
                    "shortcut works in toggle mode instead."), done)
        elif self._gnome.available():
            self._use_gnome(cfg, "", done)
        elif self._portal.available():
            # Not GNOME (no gnome-settings-daemon): the portal can toggle too.
            self._use_portal(cfg, done, fallback=False)
        else:
            self._remove_all()
            self._applied = None
            done(HotkeyStatus("none", False, accel, cfg.mode, _(
                "No way to register a global shortcut was found: set one up in your "
                "desktop settings that runs “{command}”.").format(
                    command=" ".join(self._toggle_command))))

    def _remove_all(self) -> None:
        self._portal.close()
        self._gnome.remove()

    def _use_gnome(self, cfg: ShortcutConfig, note: str, done: StatusCallback) -> None:
        self._portal.close()  # never two bindings for the same keys
        result = self._gnome.install(cfg.accelerator, self._toggle_command)
        self._applied = ("gnome", cfg.accelerator, "toggle") if result.ok else None
        done(HotkeyStatus("gnome", result.ok, cfg.accelerator, "toggle",
                          _join(note, result.message)))

    def _use_portal(self, cfg: ShortcutConfig, done: StatusCallback, *, fallback: bool) -> None:
        mode = cfg.mode
        key = ("portal", cfg.accelerator, mode)
        if self._applied == key and self._portal.active:
            done(HotkeyStatus("portal", True, cfg.accelerator, mode, self.status.message))
            return
        if self._gnome.installed():
            self._gnome.remove()  # never two bindings for the same keys
        description = _("Dictate (hold to talk)") if mode == "push-to-talk" \
            else _("Start or stop dictation")

        def bound(ok: bool, trigger: str, message: str) -> None:
            if self._closed:
                return
            if ok:
                self._applied = key
                note = ""
                if trigger and not same_trigger(trigger, cfg.accelerator):
                    note = _("The system assigned {trigger} to Greffier instead of "
                             "{accelerator}.").format(
                        trigger=trigger, accelerator=accelerator_label(cfg.accelerator))
                done(HotkeyStatus("portal", True, cfg.accelerator, mode, note))
                return
            self._applied = None
            if fallback and self._gnome.available():
                self._use_gnome(cfg, _(
                    "Push-to-talk is unavailable ({reason}); the shortcut works in toggle "
                    "mode instead.").format(reason=message.rstrip(".")), done)
                return
            done(HotkeyStatus("portal", False, cfg.accelerator, mode, message))

        self._portal.bind(cfg.accelerator, description, bound)

    # -- teardown ------------------------------------------------------
    def remove(self) -> None:
        """Delete every binding, including the GNOME custom shortcut (uninstall)."""
        self._pending = None
        self._busy = False  # an in-flight portal request is abandoned by close()
        self._portal.close()
        self._gnome.remove(sync=True)
        self._applied = None
        self.status = HotkeyStatus("none", False, self.status.accelerator, self.status.mode)

    def close(self) -> None:
        """Release the portal session. The GNOME shortcut stays: it D-Bus-activates
        the daemon the next time it is pressed."""
        if self._closed:
            return
        self._closed = True
        self._pending = None
        self._portal.close()
