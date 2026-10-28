"""Push-to-talk through the GlobalShortcuts portal (GNOME 48+).

Unlike GNOME custom shortcuts, the portal reports both key press
(``Activated``) and key release (``Deactivated``). The user confirms the
binding once in a GNOME dialog; GNOME remembers it per application id, which
is why the daemon registers with ``org.freedesktop.host.portal.Registry`` and
runs as ``app-<id>.service``.

Shortcut triggers use the XDG "shortcuts" format: modifiers ``CTRL``,
``ALT``, ``SHIFT``, ``NUM``, ``LOGO`` joined with ``+`` and an xkb keysym
name, e.g. ``LOGO+ALT+d``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib

from greffier.desktop import portal
from greffier.desktop.portal import (
    GLOBAL_SHORTCUTS_IFACE,
    PORTAL_BUS_NAME,
    PORTAL_OBJECT_PATH,
    PortalResponse,
    PortalSession,
)
from greffier.i18n import _

log = logging.getLogger(__name__)

SHORTCUT_ID = "dictate"

_TRIGGER_MODIFIERS = {
    "control": "CTRL",
    "ctrl": "CTRL",
    "ctl": "CTRL",
    "primary": "CTRL",
    "alt": "ALT",
    "mod1": "ALT",
    "shift": "SHIFT",
    "super": "LOGO",
    "mod4": "LOGO",
    "meta": "LOGO",
    "numlock": "NUM",
    "mod2": "NUM",
}
_MOD_RE = re.compile(r"<\s*([A-Za-z0-9_]+)\s*>")

BindCallback = Callable[[bool, str, str], None]  # (ok, trigger description, message)


def accelerator_to_trigger(accelerator: str) -> str:
    """GTK accelerator → XDG shortcuts trigger (``<Super><Alt>d`` → ``LOGO+ALT+d``).

    Modifiers keep their order of appearance; single letters are lower-cased
    like GTK does. Raises ``ValueError`` for empty/invalid input or a modifier
    the portal format cannot express (Hyper).
    """
    text = (accelerator or "").strip()
    if not text:
        raise ValueError("empty accelerator")
    mods: list[str] = []
    pos = 0
    while (match := _MOD_RE.match(text, pos)) is not None:
        raw = match.group(1).lower()
        pos = match.end()
        if raw == "release":
            continue
        mod = _TRIGGER_MODIFIERS.get(raw)
        if mod is None:
            raise ValueError(f"modifier <{match.group(1)}> cannot be used with the portal")
        if mod not in mods:
            mods.append(mod)
    key = text[pos:].strip()
    if not key or any(c in key for c in "<> +"):
        raise ValueError(f"invalid accelerator {accelerator!r}")
    if len(key) == 1:
        key = key.lower()
    return "+".join([*mods, key])


class GlobalShortcutsPortal:
    """One portal session holding Greffier's single "dictate" shortcut."""

    def __init__(
        self,
        bus: Gio.DBusConnection,
        on_activated: Callable[[], None],
        on_deactivated: Callable[[], None],
        *,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self._bus = bus
        self._on_activated = on_activated
        self._on_deactivated = on_deactivated
        self._on_closed = on_closed
        self.version: int | None = None
        self.probed = False
        self._session: PortalSession | None = None
        self._request: portal.PortalRequest | None = None
        self.bound_accelerator: str | None = None
        self.trigger_description = ""
        self._pressed = False
        self._signal_ids: list[int] = []
        self._generation = 0

    # -- availability --------------------------------------------------
    def probe(self, callback: Callable[[int | None], None] | None = None) -> None:
        """Read the interface version (``None``: portal or backend missing)."""

        def done(version: int | None) -> None:
            self.version = version
            self.probed = True
            if callback is not None:
                callback(version)

        portal.get_interface_version(self._bus, GLOBAL_SHORTCUTS_IFACE, done)

    def available(self) -> bool:
        return self.version is not None

    @property
    def active(self) -> bool:
        """A session exists and our shortcut is bound in it."""
        return self._session is not None and not self._session.closed \
            and self.bound_accelerator is not None

    # -- signals -------------------------------------------------------
    def _subscribe(self) -> None:
        if self._signal_ids:
            return
        for name, handler in (("Activated", self._on_signal_activated),
                              ("Deactivated", self._on_signal_deactivated),
                              ("ShortcutsChanged", self._on_signal_changed)):
            self._signal_ids.append(self._bus.signal_subscribe(
                PORTAL_BUS_NAME, GLOBAL_SHORTCUTS_IFACE, name, PORTAL_OBJECT_PATH, None,
                Gio.DBusSignalFlags.NONE, handler))

    def _unsubscribe(self) -> None:
        for sub in self._signal_ids:
            self._bus.signal_unsubscribe(sub)
        self._signal_ids = []

    def _ours(self, params: GLib.Variant) -> tuple[bool, tuple[Any, ...]]:
        values = params.unpack()
        handle = values[0] if values else None
        return (self._session is not None and handle == self._session.handle), values

    def _on_signal_activated(self, *args: Any) -> None:
        ours, values = self._ours(args[-1])
        if ours and values[1] == SHORTCUT_ID:
            self._pressed = True
            log.debug("portal shortcut activated")
            self._on_activated()

    def _on_signal_deactivated(self, *args: Any) -> None:
        ours, values = self._ours(args[-1])
        if ours and values[1] == SHORTCUT_ID and self._pressed:
            self._pressed = False
            log.debug("portal shortcut deactivated")
            self._on_deactivated()

    def _on_signal_changed(self, *args: Any) -> None:
        ours, values = self._ours(args[-1])
        if ours:
            self._update_trigger(values[1])
            log.info("portal shortcut changed by the user: %s", self.trigger_description)

    def _update_trigger(self, shortcuts: Any) -> None:
        for shortcut_id, props in shortcuts or []:
            if shortcut_id == SHORTCUT_ID:
                self.trigger_description = str(dict(props).get("trigger_description", ""))

    # -- session -------------------------------------------------------
    def _session_closed(self, session: PortalSession) -> None:
        if session is not self._session:
            return
        self._session = None
        self.bound_accelerator = None
        if self._pressed:
            self._pressed = False
            self._on_deactivated()
        if self._on_closed is not None:
            self._on_closed()

    def bind(self, accelerator: str, description: str, callback: BindCallback) -> None:
        """Bind (or re-bind) the shortcut; idempotent for an unchanged accelerator.

        A changed accelerator closes the old session and opens a new one, so
        two portal bindings never coexist.
        """
        if self.active and accelerator == self.bound_accelerator:
            GLib.idle_add(lambda: (callback(True, self.trigger_description, ""), False)[1])
            return
        try:
            trigger = accelerator_to_trigger(accelerator)
        except ValueError:
            GLib.idle_add(lambda: (callback(False, "", _(
                "“{accelerator}” cannot be used as a global shortcut.").format(
                    accelerator=accelerator)), False)[1])
            return
        self.close()
        if not self.available():
            GLib.idle_add(lambda: (callback(False, "", _(
                "The GlobalShortcuts portal is not available (it needs GNOME 48 or later).")),
                False)[1])
            return
        self._generation += 1
        generation = self._generation
        self._subscribe()

        def fail(response: PortalResponse) -> None:
            if response.cancelled:
                msg = _("The global shortcut was not confirmed.")
            elif response.code == portal.RESPONSE_DBUS_ERROR:
                msg = _("The GlobalShortcuts portal failed: {error}").format(error=response.error)
            else:
                msg = _("The GlobalShortcuts portal could not bind the shortcut.")
            if self._session is not None:
                self._session.close()
                self._session = None
            self._request = None
            callback(False, "", msg)

        def created(session: PortalSession | None, response: PortalResponse) -> None:
            if generation != self._generation:
                if session is not None:
                    session.close()
                return
            if session is None:
                fail(response)
                return
            self._session = session
            shortcuts = [(SHORTCUT_ID, {
                "description": GLib.Variant("s", description),
                "preferred_trigger": GLib.Variant("s", trigger),
            })]
            self._request = portal.call_request(
                self._bus, GLOBAL_SHORTCUTS_IFACE, "BindShortcuts", "oa(sa{sv})s",
                (session.handle, shortcuts, ""), {}, bound)

        def bound(response: PortalResponse) -> None:
            if generation != self._generation or self._session is None:
                return
            self._request = None
            if not response.ok:
                fail(response)
                return
            self._update_trigger(response.results.get("shortcuts"))
            self.bound_accelerator = accelerator
            # Read back what the user actually accepted.
            self._request = portal.call_request(
                self._bus, GLOBAL_SHORTCUTS_IFACE, "ListShortcuts", "o",
                (self._session.handle,), {}, listed)

        def listed(response: PortalResponse) -> None:
            if generation != self._generation:
                return
            self._request = None
            if response.ok:
                self._update_trigger(response.results.get("shortcuts"))
            log.info("portal shortcut bound: %s", self.trigger_description or trigger)
            callback(True, self.trigger_description, "")

        self._request = portal.create_session(
            self._bus, GLOBAL_SHORTCUTS_IFACE, created, on_closed=self._session_closed)

    def close(self) -> None:
        """Close the session (removes the binding); safe to call repeatedly."""
        self._generation += 1
        if self._request is not None:
            self._request.cancel()
            self._request = None
        if self._session is not None:
            self._session.close()
            self._session = None
        self.bound_accelerator = None
        self.trigger_description = ""
        if self._pressed:
            self._pressed = False
            self._on_deactivated()
        self._unsubscribe()
