"""Client of the optional GNOME Shell extension (``io.github.greffier.Greffier.Shell1``).

The extension lives inside gnome-shell and can therefore set the clipboard and
press keys without stealing focus. It is optional: :attr:`ShellClient.available`
tracks whether it is running (name owner present) *and* speaks the interface
version this code was written for.

Security note: the extension only accepts calls from the owner of the daemon's
bus name, so the ``bus`` given here must be the connection that owns
``io.github.greffier.Greffier.Daemon``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import cache
from typing import Any

from gi.repository import Gio, GLib

from greffier import paths
from greffier.const import SHELL_DBUS_IFACE, SHELL_DBUS_NAME, SHELL_DBUS_PATH

log = logging.getLogger(__name__)

SHELL_API_VERSION = 1  # Shell1.Version this client understands
CALL_TIMEOUT_MS = 3000
# TypeText takes ~5 ms per character in the extension.
TYPE_TIMEOUT_BASE_MS = 5000


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
    """Asynchronous proxy for the extension, with name-owner tracking."""

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
        self.ready = False  # proxy constructed (availability known)
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
    def _on_proxy(self, _source: Any, res: Gio.AsyncResult) -> None:
        try:
            proxy = Gio.DBusProxy.new_finish(res)
        except GLib.Error as exc:
            if not exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                log.warning("cannot create the Shell extension proxy: %s", exc.message)
            return
        self._proxy = proxy
        self._handlers = [
            proxy.connect("notify::g-name-owner", self._on_owner_changed),
            proxy.connect("g-properties-changed", self._on_owner_changed),
        ]
        self.ready = True
        self._on_owner_changed()

    def _on_owner_changed(self, *_args: Any) -> None:
        now = self.available
        if self.owner and self.version not in (None, SHELL_API_VERSION):
            log.warning("GNOME Shell extension speaks Shell1 version %s, expected %s; ignoring it",
                        self.version, SHELL_API_VERSION)
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
        """The extension owns its bus name (whatever its version)."""
        return bool(self.owner)

    @property
    def available(self) -> bool:
        """Running and compatible."""
        return self.running and self.version == SHELL_API_VERSION

    # -- calls ---------------------------------------------------------
    def _call(
        self,
        method: str,
        params: GLib.Variant | None,
        callback: Callable[[tuple[Any, ...] | None, str], None],
        timeout_ms: int = CALL_TIMEOUT_MS,
    ) -> None:
        if self._proxy is None or not self.available:
            GLib.idle_add(lambda: (callback(None, "extension not available"), False)[1])
            return

        def done(proxy: Gio.DBusProxy, res: Gio.AsyncResult) -> None:
            try:
                reply = proxy.call_finish(res)
            except GLib.Error as exc:
                log.warning("Shell1.%s failed: %s", method, exc.message)
                callback(None, exc.message)
                return
            callback(tuple(reply.unpack()), "")

        self._proxy.call(method, params, Gio.DBusCallFlags.NO_AUTO_START, timeout_ms,
                         self._cancellable, done)

    def set_clipboard(self, text: str, callback: Callable[[bool, str], None]) -> None:
        """CLIPBOARD + PRIMARY ← ``text``; ``callback(ok, error)``."""
        self._call("SetClipboard", GLib.Variant("(s)", (text,)),
                   lambda r, err: callback(r is not None, err))

    def get_clipboard(self, callback: Callable[[bool, str | None], None]) -> None:
        """``callback(ok, text or None if the clipboard holds no text)``."""

        def done(reply: tuple[Any, ...] | None, _err: str) -> None:
            if reply is None:
                callback(False, None)
            else:
                has_text, text = reply
                callback(True, text if has_text else None)

        self._call("GetClipboard", None, done)

    def paste(self, shortcut: str, callback: Callable[[bool, str, str], None]) -> None:
        """Press the paste shortcut; ``callback(ok, used shortcut, error)``."""
        self._call("Paste", GLib.Variant("(s)", (shortcut,)),
                   lambda r, err: callback(r is not None, r[0] if r else shortcut, err))

    def type_text(self, text: str, callback: Callable[[bool, int, str], None]) -> None:
        """Type ``text``; ``callback(ok, characters typed, error)``."""
        timeout = TYPE_TIMEOUT_BASE_MS + 10 * len(text)
        self._call("TypeText", GLib.Variant("(s)", (text,)),
                   lambda r, err: callback(r is not None, int(r[0]) if r else 0, err), timeout)

    def get_focus_info(self, callback: Callable[[bool, str, bool], None]) -> None:
        """``callback(ok, wm_class, is_terminal)`` for the focused window."""
        self._call("GetFocusInfo", None,
                   lambda r, _err: callback(r is not None, r[0] if r else "",
                                            bool(r[1]) if r else False))

    def close(self) -> None:
        """Disconnect from the bus; pending callbacks are dropped."""
        self._cancellable.cancel()
        if self._proxy is not None:
            for handler in self._handlers:
                self._proxy.disconnect(handler)
            self._handlers = []
            self._proxy = None
        self._on_changed = None
