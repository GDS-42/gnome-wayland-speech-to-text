"""xdg-desktop-portal plumbing: Request/Response, sessions, tokens, versions.

Portal methods that need user interaction return a *request* object path and
later emit ``org.freedesktop.portal.Request.Response(u code, a{sv} results)``
on it. To avoid missing a fast reply, :func:`call_request` subscribes to the
predictable request path (derived from our unique bus name and a
``handle_token``) *before* calling the method, as recommended by the portal
documentation.

Everything is asynchronous and runs on the GLib main context of the caller;
callbacks are invoked on the main thread.
"""

from __future__ import annotations

import itertools
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gi.repository import Gio, GLib

log = logging.getLogger(__name__)

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
REQUEST_IFACE = "org.freedesktop.portal.Request"
SESSION_IFACE = "org.freedesktop.portal.Session"
REGISTRY_IFACE = "org.freedesktop.host.portal.Registry"
REMOTE_DESKTOP_IFACE = "org.freedesktop.portal.RemoteDesktop"
GLOBAL_SHORTCUTS_IFACE = "org.freedesktop.portal.GlobalShortcuts"

# Response codes (org.freedesktop.portal.Request::Response)
RESPONSE_SUCCESS = 0
RESPONSE_CANCELLED = 1  # the user dismissed the dialog
RESPONSE_OTHER = 2  # the interaction ended some other way
RESPONSE_DBUS_ERROR = -1  # not a portal code: the method call itself failed
RESPONSE_TIMEOUT = -2  # not a portal code: no Response before our deadline

DEFAULT_CALL_TIMEOUT_MS = 10_000
# A Response may take as long as the user needs to answer a dialog.
DEFAULT_RESPONSE_TIMEOUT_MS = 5 * 60_000

_counter = itertools.count(1)


def new_token(prefix: str = "greffier") -> str:
    """A fresh handle token, valid as an object path element ([A-Za-z0-9_])."""
    return f"{prefix}_{next(_counter)}_{secrets.token_hex(4)}"


def sender_component(bus: Gio.DBusConnection) -> str:
    """Our unique name as used in request/session paths (``:1.42`` → ``1_42``)."""
    unique = bus.get_unique_name() or ""
    return unique.lstrip(":").replace(".", "_")


def request_path(bus: Gio.DBusConnection, token: str) -> str:
    """Object path the portal will use for a request created with ``token``."""
    return f"{PORTAL_OBJECT_PATH}/request/{sender_component(bus)}/{token}"


def session_path(bus: Gio.DBusConnection, token: str) -> str:
    """Object path the portal will use for a session created with ``token``."""
    return f"{PORTAL_OBJECT_PATH}/session/{sender_component(bus)}/{token}"


def dbus_error_message(exc: GLib.Error) -> str:
    """Error message without the ``GDBus.Error:name:`` prefix."""
    remote = Gio.DBusError.get_remote_error(exc)
    message = exc.message or ""
    if remote:
        prefix = f"GDBus.Error:{remote}: "
        if message.startswith(prefix):
            message = message[len(prefix):]
    return message


def unpack_vardict(value: Any) -> dict[str, Any]:
    """Unpack an ``a{sv}`` (GLib.Variant or already-unpacked dict) recursively."""
    if isinstance(value, GLib.Variant):
        value = value.unpack()
    return dict(value or {})


@dataclass
class PortalResponse:
    """Outcome of a portal request."""

    code: int
    results: dict[str, Any] = field(default_factory=dict)
    error: str = ""  # D-Bus error message when code == RESPONSE_DBUS_ERROR
    error_name: str = ""  # remote D-Bus error name, if any

    @property
    def ok(self) -> bool:
        return self.code == RESPONSE_SUCCESS

    @property
    def cancelled(self) -> bool:
        return self.code == RESPONSE_CANCELLED


ResponseCallback = Callable[[PortalResponse], None]


class PortalRequest:
    """One in-flight portal request. Use :func:`call_request` to create it."""

    def __init__(
        self,
        bus: Gio.DBusConnection,
        callback: ResponseCallback,
        response_timeout_ms: int,
    ) -> None:
        self._bus = bus
        self._callback: ResponseCallback | None = callback
        self._sub_id = 0
        self._timeout_id = 0
        self._response_timeout_ms = response_timeout_ms
        self.token = new_token()
        self.path = request_path(bus, self.token)

    # -- internals -----------------------------------------------------
    def _subscribe(self, path: str) -> None:
        self._unsubscribe()
        self.path = path
        self._sub_id = self._bus.signal_subscribe(
            PORTAL_BUS_NAME,
            REQUEST_IFACE,
            "Response",
            path,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_response,
        )

    def _unsubscribe(self) -> None:
        if self._sub_id:
            self._bus.signal_unsubscribe(self._sub_id)
            self._sub_id = 0

    def _arm_timeout(self) -> None:
        if self._response_timeout_ms > 0 and not self._timeout_id:
            self._timeout_id = GLib.timeout_add(self._response_timeout_ms, self._on_timeout)

    def _on_timeout(self) -> bool:
        self._timeout_id = 0
        log.warning("portal request %s timed out", self.path)
        self.close()
        self._finish(PortalResponse(RESPONSE_TIMEOUT, error="timeout"))
        return GLib.SOURCE_REMOVE

    def _on_response(
        self,
        _conn: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _iface: str,
        _signal: str,
        params: GLib.Variant,
    ) -> None:
        code, results = params.unpack()
        self._finish(PortalResponse(int(code), dict(results)))

    def _on_called(self, conn: Gio.DBusConnection, res: Gio.AsyncResult) -> None:
        if self._callback is None:
            return
        try:
            reply = conn.call_finish(res)
        except GLib.Error as exc:
            self._finish(
                PortalResponse(RESPONSE_DBUS_ERROR, error=dbus_error_message(exc),
                               error_name=Gio.DBusError.get_remote_error(exc) or "")
            )
            return
        handle = reply.unpack()[0]
        if handle != self.path:
            # xdg-desktop-portal < 0.9 did not honour handle_token.
            log.debug("portal returned unexpected request path %s", handle)
            self._subscribe(handle)
        self._arm_timeout()

    def _finish(self, response: PortalResponse) -> None:
        callback, self._callback = self._callback, None
        self._unsubscribe()
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0
        if callback is not None:
            callback(response)

    # -- public --------------------------------------------------------
    @property
    def pending(self) -> bool:
        return self._callback is not None

    def close(self) -> None:
        """Ask the portal to close the request (dismisses its dialog)."""
        self._bus.call(
            PORTAL_BUS_NAME,
            self.path,
            REQUEST_IFACE,
            "Close",
            None,
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            DEFAULT_CALL_TIMEOUT_MS,
            None,
            None,
        )

    def cancel(self) -> None:
        """Close the request and drop the callback without invoking it."""
        if self._callback is None:
            return
        self._callback = None
        self._unsubscribe()
        if self._timeout_id:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = 0
        self.close()


def call_request(
    bus: Gio.DBusConnection,
    interface: str,
    method: str,
    signature: str,
    args: tuple[Any, ...],
    options: dict[str, GLib.Variant] | None,
    callback: ResponseCallback,
    *,
    response_timeout_ms: int = DEFAULT_RESPONSE_TIMEOUT_MS,
    call_timeout_ms: int = DEFAULT_CALL_TIMEOUT_MS,
) -> PortalRequest:
    """Call a Request-returning portal method and wait for its Response.

    ``signature``/``args`` describe the arguments *before* the trailing
    ``a{sv}`` options (every such portal method ends with one); a
    ``handle_token`` is added to ``options``. ``callback`` receives exactly one
    :class:`PortalResponse` (also on D-Bus errors and timeouts).
    """
    request = PortalRequest(bus, callback, response_timeout_ms)
    opts = dict(options or {})
    opts["handle_token"] = GLib.Variant("s", request.token)
    params = GLib.Variant(f"({signature}a{{sv}})", (*args, opts))
    request._subscribe(request.path)
    bus.call(
        PORTAL_BUS_NAME,
        PORTAL_OBJECT_PATH,
        interface,
        method,
        params,
        GLib.VariantType("(o)"),
        Gio.DBusCallFlags.NONE,
        call_timeout_ms,
        None,
        request._on_called,
    )
    return request


class PortalSession:
    """A portal session object (``org.freedesktop.portal.Session``)."""

    def __init__(
        self,
        bus: Gio.DBusConnection,
        handle: str,
        on_closed: Callable[[PortalSession], None] | None = None,
    ) -> None:
        self._bus = bus
        self.handle = handle
        self.closed = False
        self._on_closed = on_closed
        self._sub_id = bus.signal_subscribe(
            PORTAL_BUS_NAME,
            SESSION_IFACE,
            "Closed",
            handle,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_closed_signal,
        )

    def _on_closed_signal(self, *_args: Any) -> None:
        log.info("portal session %s closed by the portal", self.handle)
        self._mark_closed()
        if self._on_closed is not None:
            self._on_closed(self)

    def _mark_closed(self) -> None:
        self.closed = True
        if self._sub_id:
            self._bus.signal_unsubscribe(self._sub_id)
            self._sub_id = 0

    def close(self) -> None:
        """Close the session (idempotent). ``on_closed`` is *not* called."""
        if self.closed:
            return
        self._mark_closed()
        self._bus.call(
            PORTAL_BUS_NAME,
            self.handle,
            SESSION_IFACE,
            "Close",
            None,
            None,
            Gio.DBusCallFlags.NO_AUTO_START,
            DEFAULT_CALL_TIMEOUT_MS,
            None,
            None,
        )


def create_session(
    bus: Gio.DBusConnection,
    interface: str,
    callback: Callable[[PortalSession | None, PortalResponse], None],
    *,
    options: dict[str, GLib.Variant] | None = None,
    on_closed: Callable[[PortalSession], None] | None = None,
) -> PortalRequest:
    """``<interface>.CreateSession`` → ``callback(session or None, response)``."""
    token = new_token("greffier_session")
    opts = dict(options or {})
    opts["session_handle_token"] = GLib.Variant("s", token)
    expected = session_path(bus, token)

    def done(response: PortalResponse) -> None:
        if not response.ok:
            callback(None, response)
            return
        handle = str(response.results.get("session_handle") or expected)
        callback(PortalSession(bus, handle, on_closed), response)

    return call_request(bus, interface, "CreateSession", "", (), opts, done)


def get_interface_version(
    bus: Gio.DBusConnection,
    interface: str,
    callback: Callable[[int | None], None],
) -> None:
    """Read the ``version`` property of a portal interface (``None`` if absent).

    xdg-desktop-portal only exports interfaces that a backend implements, so
    ``None`` means "unavailable here".
    """

    def done(conn: Gio.DBusConnection, res: Gio.AsyncResult) -> None:
        try:
            reply = conn.call_finish(res)
        except GLib.Error as exc:
            log.debug("portal interface %s unavailable: %s", interface, exc.message)
            callback(None)
            return
        value = reply.unpack()[0]
        callback(int(value) if isinstance(value, int) else None)

    bus.call(
        PORTAL_BUS_NAME,
        PORTAL_OBJECT_PATH,
        "org.freedesktop.DBus.Properties",
        "Get",
        GLib.Variant("(ss)", (interface, "version")),
        GLib.VariantType("(v)"),
        Gio.DBusCallFlags.NONE,
        DEFAULT_CALL_TIMEOUT_MS,
        None,
        done,
    )


def register_host_app(bus: Gio.DBusConnection, app_id: str, timeout_ms: int = 3000) -> bool:
    """Tell xdg-desktop-portal our app id (``org.freedesktop.host.portal.Registry``).

    Must run once, before any other portal call on ``bus``: the portal
    identifies a connection the first time it sees it. Returns ``True`` when
    the registration was accepted, ``False`` when the Registry is missing
    (xdg-desktop-portal < 1.19) or refused it (e.g. the app id was already
    known from the ``app-<id>.service`` systemd unit, which is also fine).

    This is the only synchronous call in the package; it happens once at
    start-up, before the main loop serves anything.
    """
    try:
        bus.call_sync(
            PORTAL_BUS_NAME,
            PORTAL_OBJECT_PATH,
            REGISTRY_IFACE,
            "Register",
            GLib.Variant("(sa{sv})", (app_id, {})),
            None,
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
        )
    except GLib.Error as exc:
        log.info("portal host registry: %s", dbus_error_message(exc))
        return False
    log.debug("registered with the portal as %s", app_id)
    return True


def register_host_app_async(
    bus: Gio.DBusConnection, app_id: str, callback: Callable[[bool], None]
) -> None:
    """Asynchronous variant of :func:`register_host_app`."""

    def done(conn: Gio.DBusConnection, res: Gio.AsyncResult) -> None:
        try:
            conn.call_finish(res)
        except GLib.Error as exc:
            log.info("portal host registry: %s", exc.message)
            callback(False)
            return
        callback(True)

    bus.call(
        PORTAL_BUS_NAME,
        PORTAL_OBJECT_PATH,
        REGISTRY_IFACE,
        "Register",
        GLib.Variant("(sa{sv})", (app_id, {})),
        None,
        Gio.DBusCallFlags.NONE,
        DEFAULT_CALL_TIMEOUT_MS,
        None,
        done,
    )
