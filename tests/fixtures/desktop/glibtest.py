"""Test helpers for greffier.desktop: a GLib main loop runner and a private bus.

The private bus is a dedicated ``dbus-daemon`` with *no* service directories,
so nothing real (xdg-desktop-portal, gnome-shell…) can be D-Bus-activated by
a test; the environment of the test process is not modified.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from gi.repository import Gio, GLib

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def run_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Run a real GLib.MainLoop until ``predicate()`` is true or ``timeout`` s."""
    if predicate():
        return True
    loop = GLib.MainLoop()
    live = {"check": True, "expire": True}

    def check() -> bool:
        if predicate():
            live["check"] = False
            loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def expire() -> bool:
        live["expire"] = False
        loop.quit()
        return GLib.SOURCE_REMOVE

    check_id = GLib.timeout_add(2, check)
    expire_id = GLib.timeout_add(int(timeout * 1000), expire)
    loop.run()
    if live["check"]:
        GLib.source_remove(check_id)
    if live["expire"]:
        GLib.source_remove(expire_id)
    return predicate()


def run_for(seconds: float) -> None:
    """Run the main loop for ``seconds``."""
    run_until(lambda: False, seconds)


class PrivateBus:
    """A throw-away dbus-daemon; ``connect()`` returns new client connections."""

    def __init__(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="greffier-bus-")
        socket = os.path.join(self._dir, "bus")
        config = Path(self._dir, "bus.conf")
        config.write_text(BUS_CONFIG.format(socket=socket))
        self._proc = subprocess.Popen(
            ["dbus-daemon", f"--config-file={config}", "--nofork", "--print-address=1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        assert self._proc.stdout is not None
        self.address = self._proc.stdout.readline().strip()
        self._connections: list[Gio.DBusConnection] = []

    def connect(self) -> Gio.DBusConnection:
        conn = Gio.DBusConnection.new_for_address_sync(
            self.address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        conn.set_exit_on_close(False)
        self._connections.append(conn)
        return conn

    def own_name(self, conn: Gio.DBusConnection, name: str) -> None:
        """Own ``name`` on ``conn`` synchronously (RequestName)."""
        reply = conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                               "org.freedesktop.DBus", "RequestName",
                               GLib.Variant("(su)", (name, 4)), None, 0, 2000, None)
        assert reply.unpack()[0] == 1, f"could not own {name}"

    def release_name(self, conn: Gio.DBusConnection, name: str) -> None:
        conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                       "ReleaseName", GLib.Variant("(s)", (name,)), None, 0, 2000, None)

    def close(self) -> None:
        for conn in self._connections:
            if not conn.is_closed():
                conn.close_sync(None)
        self._proc.terminate()
        try:
            self._proc.wait(5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        if self._proc.stdout is not None:
            self._proc.stdout.close()


def export_object(
    conn: Gio.DBusConnection,
    path: str,
    xml: str,
    handler: Callable[..., GLib.Variant | None | Exception],
    properties: dict[str, GLib.Variant] | None = None,
) -> list[int]:
    """Export every interface in ``xml`` at ``path``.

    ``handler(interface, method, params, sender)`` returns the reply variant,
    ``None`` for an empty reply, or an Exception to reply with an error.
    """
    node = Gio.DBusNodeInfo.new_for_xml(xml)
    ids = []

    def on_call(_conn, sender, _path, iface, method, params, invocation):
        try:
            result = handler(iface, method, params, sender)
        except Exception as exc:  # reply with a D-Bus error
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(exc))
            return
        if isinstance(result, Exception):
            invocation.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(result))
        else:
            invocation.return_value(result)

    def on_get(_conn, _sender, _path, iface, prop):
        return (properties or {}).get(f"{iface}.{prop}") or (properties or {}).get(prop)

    for info in node.interfaces:
        ids.append(conn.register_object(path, info, on_call, on_get, None))
    return ids


class ThreadedService:
    """Run ``setup(conn)`` on a private connection served by its own thread.

    Needed to test synchronous D-Bus calls (they block the default main loop,
    so an in-thread fake service could never answer).
    """

    def __init__(self, bus: PrivateBus, setup: Callable[[Gio.DBusConnection], object],
                 name: str | None = None) -> None:
        import threading

        self._ready = threading.Event()
        self._loop: GLib.MainLoop | None = None
        self._bus = bus
        self._setup = setup
        self._name = name
        self.result: object = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("service thread did not start")

    def _run(self) -> None:
        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        try:
            conn = self._bus.connect()
            self.result = self._setup(conn)
            if self._name:
                self._bus.own_name(conn, self._name)
            self._loop = GLib.MainLoop.new(ctx, False)
            ctx.invoke_full(GLib.PRIORITY_DEFAULT, lambda: (self._ready.set(), False)[1])
            self._loop.run()
            conn.close_sync(None)
        finally:
            ctx.pop_thread_default()

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.quit()
        self._thread.join(5)
