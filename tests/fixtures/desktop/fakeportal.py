"""A fake xdg-desktop-portal for tests (RemoteDesktop, GlobalShortcuts, Registry).

It owns ``org.freedesktop.portal.Desktop`` on a private bus and follows the
Request/Response protocol: Request-returning methods register a request
object at the path derived from ``handle_token`` and emit ``Response``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gi.repository import Gio, GLib

PORTAL_NAME = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"

PORTAL_XML = """
<node>
  <interface name="org.freedesktop.portal.RemoteDesktop">
    <method name="CreateSession">
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="SelectDevices">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="Start">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="s" name="parent_window" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="NotifyKeyboardKeycode">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="i" name="keycode" direction="in"/>
      <arg type="u" name="state" direction="in"/>
    </method>
    <method name="NotifyKeyboardKeysym">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="i" name="keysym" direction="in"/>
      <arg type="u" name="state" direction="in"/>
    </method>
    <property name="version" type="u" access="read"/>
  </interface>
  <interface name="org.freedesktop.portal.GlobalShortcuts">
    <method name="CreateSession">
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <method name="BindShortcuts">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a(sa{sv})" name="shortcuts" direction="in"/>
      <arg type="s" name="parent_window" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="request_handle" direction="out"/>
    </method>
    <method name="ListShortcuts">
      <arg type="o" name="session_handle" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="request_handle" direction="out"/>
    </method>
    <signal name="Activated">
      <arg type="o" name="session_handle"/>
      <arg type="s" name="shortcut_id"/>
      <arg type="t" name="timestamp"/>
      <arg type="a{sv}" name="options"/>
    </signal>
    <signal name="Deactivated">
      <arg type="o" name="session_handle"/>
      <arg type="s" name="shortcut_id"/>
      <arg type="t" name="timestamp"/>
      <arg type="a{sv}" name="options"/>
    </signal>
    <signal name="ShortcutsChanged">
      <arg type="o" name="session_handle"/>
      <arg type="a(sa{sv})" name="shortcuts"/>
    </signal>
    <property name="version" type="u" access="read"/>
  </interface>
  <interface name="org.freedesktop.host.portal.Registry">
    <method name="Register">
      <arg type="s" name="app_id" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
    </method>
    <property name="version" type="u" access="read"/>
  </interface>
  <interface name="org.example.Test">
    <!-- generic Request-returning method used by the portal helper tests -->
    <method name="Ask">
      <arg type="s" name="question" direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
  </interface>
</node>
"""

REQUEST_XML = """
<node>
  <interface name="org.freedesktop.portal.Request">
    <method name="Close"/>
    <signal name="Response">
      <arg type="u" name="response"/>
      <arg type="a{sv}" name="results"/>
    </signal>
  </interface>
</node>
"""

SESSION_XML = """
<node>
  <interface name="org.freedesktop.portal.Session">
    <method name="Close"/>
    <signal name="Closed">
      <arg type="a{sv}" name="details"/>
    </signal>
    <property name="version" type="u" access="read"/>
  </interface>
</node>
"""


@dataclass
class FakePortal:
    """Configurable fake. Attributes can be changed by tests at any time."""

    conn: Gio.DBusConnection
    interfaces: tuple[str, ...] = ("RemoteDesktop", "GlobalShortcuts", "Registry", "Test")
    rd_version: int = 2
    gs_version: int = 1
    # per method name: (response code, results dict of GLib.Variant)
    responses: dict[str, tuple[int, dict[str, GLib.Variant]]] = field(default_factory=dict)
    respond_before_reply: bool = False  # emit Response before returning the handle
    legacy_request_path: bool = False  # ignore handle_token (very old portals)
    no_response: set[str] = field(default_factory=set)  # methods that never respond
    fail_notify_after: int | None = None  # make the n-th Notify* call fail
    trigger_description: str = "Super+Alt+D"
    calls: list[tuple[str, str, Any]] = field(default_factory=list)  # (iface, method, args)
    notify: list[tuple[str, int, int]] = field(default_factory=list)  # (method, code, state)
    sessions: dict[str, list[int]] = field(default_factory=dict)  # path -> registration ids
    closed_sessions: list[str] = field(default_factory=list)
    closed_requests: list[str] = field(default_factory=list)
    registered_apps: list[str] = field(default_factory=list)
    _counter: int = 0
    _reg_ids: list[int] = field(default_factory=list)
    _requests: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        node = Gio.DBusNodeInfo.new_for_xml(PORTAL_XML)
        for info in node.interfaces:
            short = info.name.rsplit(".", 1)[-1]
            if short in self.interfaces:
                self._reg_ids.append(self.conn.register_object(
                    PORTAL_PATH, info, self._on_call, self._on_get, None))
        self._request_info = Gio.DBusNodeInfo.new_for_xml(REQUEST_XML).interfaces[0]
        self._session_info = Gio.DBusNodeInfo.new_for_xml(SESSION_XML).interfaces[0]

    # -- plumbing ------------------------------------------------------
    def _on_get(self, _conn, _sender, _path, iface, prop):
        if prop == "version":
            if iface.endswith("RemoteDesktop"):
                return GLib.Variant("u", self.rd_version)
            if iface.endswith("GlobalShortcuts"):
                return GLib.Variant("u", self.gs_version)
            return GLib.Variant("u", 1)
        return None

    @staticmethod
    def _sender_part(sender: str) -> str:
        return sender.lstrip(":").replace(".", "_")

    def _new_request(self, sender: str, options: dict[str, Any]) -> str:
        self._counter += 1
        token = options.get("handle_token", f"t{self._counter}")
        if self.legacy_request_path:
            token = f"legacy{self._counter}"
        path = f"{PORTAL_PATH}/request/{self._sender_part(sender)}/{token}"

        def on_call(_conn, _sender, obj_path, _iface, method, _params, invocation):
            if method == "Close":
                self.closed_requests.append(obj_path)
                self._unregister_request(obj_path)
            invocation.return_value(None)

        self._requests[path] = self.conn.register_object(path, self._request_info, on_call,
                                                         None, None)
        return path

    def _unregister_request(self, path: str) -> None:
        reg = self._requests.pop(path, None)
        if reg:
            self.conn.unregister_object(reg)

    def _respond(self, path: str, code: int, results: dict[str, GLib.Variant]) -> None:
        if path not in self._requests:
            return
        self.conn.emit_signal(None, path, "org.freedesktop.portal.Request", "Response",
                              GLib.Variant("(ua{sv})", (code, results)))
        self._unregister_request(path)

    def _new_session(self, sender: str, options: dict[str, Any]) -> str:
        token = options.get("session_handle_token", f"s{self._counter}")
        path = f"{PORTAL_PATH}/session/{self._sender_part(sender)}/{token}"

        def on_call(_conn, _sender, obj_path, _iface, method, _params, invocation):
            if method == "Close":
                self.closed_sessions.append(obj_path)
                self._drop_session(obj_path)
            invocation.return_value(None)

        def on_get(*_args):
            return GLib.Variant("u", 1)

        self.sessions[path] = [self.conn.register_object(path, self._session_info, on_call,
                                                         on_get, None)]
        return path

    def _drop_session(self, path: str) -> None:
        for reg in self.sessions.pop(path, []):
            self.conn.unregister_object(reg)

    def close_session_from_portal(self, path: str) -> None:
        """Emit Session.Closed as if the user revoked access."""
        self.conn.emit_signal(None, path, "org.freedesktop.portal.Session", "Closed",
                              GLib.Variant("(a{sv})", ({},)))
        self._drop_session(path)

    def emit_shortcut(self, signal: str, session: str, shortcut_id: str = "dictate") -> None:
        self.conn.emit_signal(None, PORTAL_PATH, "org.freedesktop.portal.GlobalShortcuts", signal,
                              GLib.Variant("(osta{sv})", (session, shortcut_id, 0, {})))

    # -- methods -------------------------------------------------------
    def _default_results(self, method: str, sender: str, options: dict[str, Any],
                         args: tuple[Any, ...]) -> tuple[int, dict[str, GLib.Variant]]:
        if method == "CreateSession":
            session = self._new_session(sender, options)
            return 0, {"session_handle": GLib.Variant("s", session)}
        if method == "Start":
            return 0, {"devices": GLib.Variant("u", 1),
                       "restore_token": GLib.Variant("s", f"token-{self._counter}")}
        if method in ("BindShortcuts", "ListShortcuts"):
            props = {"description": GLib.Variant("s", "Dictate"),
                     "trigger_description": GLib.Variant("s", self.trigger_description)}
            return 0, {"shortcuts": GLib.Variant("a(sa{sv})", [("dictate", props)])}
        if method == "Ask":
            return 0, {"answer": GLib.Variant("s", f"yes to {args[0]}")}
        return 0, {}

    def _on_call(self, _conn, sender, _path, iface, method, params, invocation):
        args = params.unpack()
        self.calls.append((iface, method, args))
        if method in ("NotifyKeyboardKeysym", "NotifyKeyboardKeycode"):
            session, _opts, code, state = args
            if session not in self.sessions:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.AccessDenied",
                                             "no such session")
                return
            self.notify.append((method, code, state))
            if self.fail_notify_after is not None and len(self.notify) > self.fail_notify_after:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", "boom")
                return
            invocation.return_value(None)
            return
        if method == "Register":
            self.registered_apps.append(args[0])
            invocation.return_value(None)
            return

        options = dict(args[-1])
        path = self._new_request(sender, options)
        if method in self.responses:
            code, results = self.responses[method]
            if method == "CreateSession" and code == 0:
                session = self._new_session(sender, options)
                results = {"session_handle": GLib.Variant("s", session), **results}
        else:
            code, results = self._default_results(method, sender, options, args)
        if method in self.no_response:
            invocation.return_value(GLib.Variant("(o)", (path,)))
            return
        if self.respond_before_reply and not self.legacy_request_path:
            self._respond(path, code, results)
            invocation.return_value(GLib.Variant("(o)", (path,)))
        else:
            invocation.return_value(GLib.Variant("(o)", (path,)))
            GLib.timeout_add(5, lambda: (self._respond(path, code, results), False)[1])

    def method_calls(self, method: str) -> list[tuple[Any, ...]]:
        return [args for _iface, m, args in self.calls if m == method]

    def close(self) -> None:
        for reg in self._reg_ids:
            self.conn.unregister_object(reg)
        for path in list(self.sessions):
            self._drop_session(path)
        for path in list(self._requests):
            self._unregister_request(path)
