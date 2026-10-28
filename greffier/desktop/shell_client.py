"""Client of the optional GNOME Shell extension (``io.github.greffier.Greffier.Shell1``).

The extension lives inside gnome-shell and can therefore set the clipboard and
press keys without stealing focus. It is optional: :attr:`ShellClient.available`
is true only when

* someone owns ``io.github.greffier.Greffier.Shell`` and speaks interface
  version ≥ :data:`SHELL_API_VERSION`;
* that owner is *also* the owner of ``org.gnome.Shell`` — the real extension
  shares gnome-shell's bus connection, so any other process claiming the name
  is an impostor and is ignored;
* the extension did not refuse us (it checks that we run in the
  ``app-io.github.greffier.Greffier.Daemon.service`` unit and answers
  AccessDenied otherwise, e.g. for a daemon started from a terminal).

The ``bus`` given here must be the connection that owns
``io.github.greffier.Greffier.Daemon``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import cache
from typing import Any

from gi.repository import Gio, GLib

from greffier import paths
from greffier.const import SHELL_DBUS_IFACE, SHELL_DBUS_NAME, SHELL_DBUS_PATH, SYSTEMD_UNIT
from greffier.i18n import _

log = logging.getLogger(__name__)

SHELL_API_VERSION = 2  # minimum Shell1.Version this client understands
GNOME_SHELL_NAME = "org.gnome.Shell"
ERROR_ACCESS_DENIED = "org.freedesktop.DBus.Error.AccessDenied"
ERROR_MODIFIERS_HELD = "io.github.greffier.Greffier.Shell.Error.ModifiersHeld"
CALL_TIMEOUT_MS = 3000
# PasteText may wait up to ~1 s for modifiers plus the clipboard read.
PASTE_TIMEOUT_MS = 5000
# TypeText takes ~5 ms per character in the extension.
TYPE_TIMEOUT_BASE_MS = 5000

# callback(reply or None, error message, D-Bus error name)
_ReplyCallback = Callable[[tuple[Any, ...] | None, str, str], None]


@cache
def interface_info() -> Gio.DBusInterfaceInfo:
    """Shell1 introspection data, parsed from the shared XML contract."""
    xml = paths.package_data("shell-interface.xml").read_text(encoding="utf-8")
    node = Gio.DBusNodeInfo.new_for_xml(xml)
    info = node.lookup_interface(SHELL_DBUS_IFACE)
    if info is None:  # pragma: no cover - broken package
        raise RuntimeError(f"{SHELL_DBUS_IFACE} missing from shell-interface.xml")
    info.cache_build()
    return info


class ShellClient:
    """Asynchronous proxy for the extension, with owner/trust tracking."""

    def __init__(
        self,
        bus: Gio.DBusConnection,
        on_changed: Callable[[bool], None] | None = None,
        *,
        name: str = SHELL_DBUS_NAME,
    ) -> None:
        self._bus = bus
        self._name = name
        self._on_changed = on_changed
        self._proxy: Gio.DBusProxy | None = None
        self._handlers: list[int] = []
        self._cancellable = Gio.Cancellable()
        self._last_available = False
        self._shell_owner: str | None = None
        self._denied_owner: str | None = None
        self._warned_owner: str | None = None
        self.ready = False  # proxy constructed (availability known)
        self._watch_id = Gio.bus_watch_name_on_connection(
            bus, GNOME_SHELL_NAME, Gio.BusNameWatcherFlags.NONE,
            self._on_shell_appeared, self._on_shell_vanished)
        Gio.DBusProxy.new(
            bus,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START | Gio.DBusProxyFlags.DO_NOT_CONNECT_SIGNALS,
            interface_info(),
            name,
            SHELL_DBUS_PATH,
            SHELL_DBUS_IFACE,
            self._cancellable,
            self._on_proxy,
        )

    # -- state ---------------------------------------------------------
    def _on_shell_appeared(self, _conn: Gio.DBusConnection, _name: str, owner: str) -> None:
        self._shell_owner = owner
        self._sync()

    def _on_shell_vanished(self, _conn: Gio.DBusConnection | None, _name: str) -> None:
        self._shell_owner = None
        self._sync()

    def _on_proxy(self, _source: Any, res: Gio.AsyncResult) -> None:
        try:
            proxy = Gio.DBusProxy.new_finish(res)
        except GLib.Error as exc:
            if not exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                log.warning("cannot create the Shell extension proxy: %s", exc.message)
            return
        self._proxy = proxy
        self._handlers = [
            proxy.connect("notify::g-name-owner", self._sync),
            proxy.connect("g-properties-changed", self._sync),
        ]
        self.ready = True
        self._sync()

    def _sync(self, *_args: Any) -> None:
        owner = self.owner
        if owner and not self.trusted and self._shell_owner and self._warned_owner != owner:
            self._warned_owner = owner
            log.warning("%s is owned by %s, not by GNOME Shell (%s): ignoring it",
                        self._name, owner, self._shell_owner)
        now = self.available
        if now != self._last_available:
            self._last_available = now
            log.info("GNOME Shell extension %s", "available" if now else "unavailable")
            if self._on_changed is not None:
                self._on_changed(now)

    @property
    def owner(self) -> str | None:
        """Unique bus name of the extension, ``None`` when it is not running."""
        return self._proxy.get_name_owner() if self._proxy is not None else None

    @property
    def version(self) -> int | None:
        """``Version`` property of the running extension."""
        if self._proxy is None:
            return None
        value = self._proxy.get_cached_property("Version")
        return int(value.unpack()) if value is not None else None

    @property
    def running(self) -> bool:
        """Someone owns the extension's bus name (whatever its version)."""
        return bool(self.owner)

    @property
    def compatible(self) -> bool:
        return (self.version or 0) >= SHELL_API_VERSION

    @property
    def trusted(self) -> bool:
        """The name is owned by gnome-shell's own connection."""
        owner = self.owner
        return bool(owner) and owner == self._shell_owner

    @property
    def denied(self) -> bool:
        """The running extension refused this service (AccessDenied)."""
        owner = self.owner
        return bool(owner) and owner == self._denied_owner

    @property
    def available(self) -> bool:
        """Running, compatible, trusted and willing to serve us."""
        return self.running and self.compatible and self.trusted and not self.denied

    def problem(self) -> str:
        """Localised reason why a running extension is not used (``""`` if none)."""
        if not self.running or self.available:
            return ""
        if not self.trusted:
            return _("Something other than GNOME Shell claims to be the Greffier extension: "
                     "it is ignored.")
        if not self.compatible:
            return _("The Greffier GNOME Shell extension does not match this version of "
                     "Greffier (interface {found}, needs {needed} or later): update it.").format(
                found=self.version, needed=SHELL_API_VERSION)
        return _("The Greffier GNOME Shell extension refused this service, which must run as "
                 "{unit} (not from a terminal); the remote desktop portal is used "
                 "instead.").format(unit=SYSTEMD_UNIT)

    # -- calls ---------------------------------------------------------
    def _call(
        self,
        method: str,
        params: GLib.Variant | None,
        callback: _ReplyCallback,
        timeout_ms: int = CALL_TIMEOUT_MS,
    ) -> None:
        if self._proxy is None or not self.available:
            GLib.idle_add(lambda: (callback(None, "extension not available", ""), False)[1])
            return
        owner = self.owner

        def done(proxy: Gio.DBusProxy, res: Gio.AsyncResult) -> None:
            try:
                reply = proxy.call_finish(res)
            except GLib.Error as exc:
                if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    return
                name = Gio.DBusError.get_remote_error(exc) or ""
                if name == ERROR_ACCESS_DENIED:
                    log.warning("the GNOME Shell extension refused this service; is it "
                                "running as %s?", SYSTEMD_UNIT)
                    self._denied_owner = owner
                    self._sync()
                elif name != ERROR_MODIFIERS_HELD:
                    log.warning("Shell1.%s failed: %s", method, exc.message)
                callback(None, exc.message, name)
                return
            callback(tuple(reply.unpack()), "", "")

        self._proxy.call(method, params, Gio.DBusCallFlags.NO_AUTO_START, timeout_ms,
                         self._cancellable, done)

    def set_clipboard(self, text: str, callback: Callable[[bool, str], None]) -> None:
        """CLIPBOARD + PRIMARY ← ``text``; ``callback(ok, error)``."""
        self._call("SetClipboard", GLib.Variant("(s)", (text,)),
                   lambda r, err, _name: callback(r is not None, err))

    def paste_text(
        self,
        text: str,
        shortcut: str,
        restore: bool,
        callback: Callable[[bool, str, str, str], None],
    ) -> None:
        """Clipboard + paste + restore in the shell.

        ``callback(ok, used shortcut, error message, D-Bus error name)``.
        """
        self._call("PasteText", GLib.Variant("(ssb)", (text, shortcut, restore)),
                   lambda r, err, name: callback(r is not None, r[0] if r else shortcut,
                                                 err, name),
                   PASTE_TIMEOUT_MS)

    def type_text(self, text: str, callback: Callable[[bool, int, str], None]) -> None:
        """Type ``text``; ``callback(ok, characters typed, error)``."""
        timeout = TYPE_TIMEOUT_BASE_MS + 10 * len(text)
        self._call("TypeText", GLib.Variant("(s)", (text,)),
                   lambda r, err, _name: callback(r is not None, int(r[0]) if r else 0, err),
                   timeout)

    def get_focus_info(self, callback: Callable[[bool, str, bool], None]) -> None:
        """``callback(ok, wm_class, is_terminal)`` for the focused window."""
        self._call("GetFocusInfo", None,
                   lambda r, _err, _name: callback(r is not None, r[0] if r else "",
                                                   bool(r[1]) if r else False))

    def close(self) -> None:
        """Disconnect from the bus; pending callbacks are dropped."""
        self._cancellable.cancel()
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._proxy is not None:
            for handler in self._handlers:
                self._proxy.disconnect(handler)
            self._handlers = []
            self._proxy = None
        self._on_changed = None
