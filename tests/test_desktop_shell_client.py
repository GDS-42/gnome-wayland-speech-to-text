"""ShellClient: owner/trust tracking, Version check, calls; Injector over it."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from gi.repository import GLib

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "desktop"))
from glibtest import PrivateBus, run_until  # noqa: E402

from greffier import paths  # noqa: E402
from greffier.config import InjectionConfig  # noqa: E402
from greffier.const import SHELL_DBUS_IFACE, SHELL_DBUS_NAME, SHELL_DBUS_PATH  # noqa: E402
from greffier.desktop.injection import ExtensionClipboard, ExtensionKeyboard, Injector  # noqa: E402
from greffier.desktop.shell_client import (  # noqa: E402
    ERROR_ACCESS_DENIED,
    ERROR_MODIFIERS_HELD,
    ShellClient,
)

XML = paths.package_data("shell-interface.xml").read_text()


class DBusFailure(Exception):
    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.name = name


class FakeExtension:
    """Shell1 v2 implemented in Python; owns org.gnome.Shell like gnome-shell."""

    def __init__(self, bus: PrivateBus, version: int = 2, *, gnome_shell: bool = True) -> None:
        self.bus = bus
        self.conn = bus.connect()
        self.clipboard: str | None = "before"
        self.calls: list[tuple[str, tuple]] = []
        self.terminal = False
        self.fail: DBusFailure | None = None
        self.gnome_shell = gnome_shell
        self._register(version)

    def _register(self, version: int) -> None:
        from gi.repository import Gio

        node = Gio.DBusNodeInfo.new_for_xml(XML)

        def on_call(_conn, _sender, _path, _iface, method, params, invocation):
            self.calls.append((method, params.unpack()))
            if self.fail is not None:
                invocation.return_dbus_error(self.fail.name, str(self.fail))
                return
            invocation.return_value(self.handle(method, params.unpack()))

        def on_get(*_args):
            return GLib.Variant("u", version)

        self.conn.register_object(SHELL_DBUS_PATH, node.lookup_interface(SHELL_DBUS_IFACE),
                                  on_call, on_get, None)

    def own(self) -> None:
        if self.gnome_shell:
            self.bus.own_name(self.conn, "org.gnome.Shell")
        self.bus.own_name(self.conn, SHELL_DBUS_NAME)

    def release(self) -> None:
        self.bus.release_name(self.conn, SHELL_DBUS_NAME)

    def handle(self, method, args):
        if method == "SetClipboard":
            self.clipboard = args[0]
            return None
        if method == "PasteText":
            text, shortcut, _restore = args
            self.clipboard = text
            used = shortcut if shortcut != "auto" else (
                "ctrl-shift-v" if self.terminal else "ctrl-v")
            return GLib.Variant("(s)", (used,))
        if method == "TypeText":
            return GLib.Variant("(u)", (len(args[0]),))
        return GLib.Variant("(sb)", ("org.gnome.Ptyxis" if self.terminal else "gedit",
                                     self.terminal))


@pytest.fixture
def private_bus() -> Iterator[PrivateBus]:
    bus = PrivateBus()
    yield bus
    bus.close()


def test_availability_follows_name_owner(private_bus) -> None:
    ext = FakeExtension(private_bus)
    changes: list[bool] = []
    client = ShellClient(private_bus.connect(), changes.append)
    assert run_until(lambda: client.ready)
    assert not client.available and client.owner is None
    ext.own()
    assert run_until(lambda: client.available)
    assert client.version == 2 and client.trusted and client.problem() == ""
    ext.release()
    assert run_until(lambda: not client.available)
    assert changes == [True, False]
    client.close()


def test_impostor_not_owned_by_gnome_shell_is_ignored(private_bus) -> None:
    shell = private_bus.connect()
    private_bus.own_name(shell, "org.gnome.Shell")  # the real gnome-shell
    impostor = FakeExtension(private_bus, gnome_shell=False)
    impostor.own()
    client = ShellClient(private_bus.connect())
    assert run_until(lambda: client.running and client.version == 2)
    assert not client.trusted and not client.available
    assert "ignored" in client.problem()
    got = []
    client.set_clipboard("secret", lambda *a: got.append(a))
    assert run_until(lambda: got)
    assert got[0][0] is False and impostor.calls == []
    client.close()


def test_impostor_without_gnome_shell_is_ignored(private_bus) -> None:
    FakeExtension(private_bus, gnome_shell=False).own()
    client = ShellClient(private_bus.connect())
    assert run_until(lambda: client.running)
    assert not client.available


def test_old_extension_is_ignored(private_bus) -> None:
    FakeExtension(private_bus, version=1).own()
    client = ShellClient(private_bus.connect())
    assert run_until(lambda: client.running and client.version == 1)
    assert client.trusted and not client.compatible and not client.available
    assert "update it" in client.problem()
    client.close()


def test_access_denied_disables_until_owner_changes(private_bus) -> None:
    ext = FakeExtension(private_bus)
    ext.own()
    client = ShellClient(private_bus.connect())
    assert run_until(lambda: client.available)
    ext.fail = DBusFailure(ERROR_ACCESS_DENIED, "Only the Greffier service may use this")
    got = []
    client.paste_text("x", "auto", True, lambda *a: got.append(a))
    assert run_until(lambda: got)
    assert got[0][0] is False and got[0][3] == ERROR_ACCESS_DENIED
    assert client.denied and not client.available
    assert "refused" in client.problem() and "app-io.github" in client.problem()
    # a restarted gnome-shell (new unique name) gets a new chance
    ext.conn.close_sync(None)
    assert run_until(lambda: not client.running)
    ext2 = FakeExtension(private_bus)
    ext2.own()
    assert run_until(lambda: client.available)
    client.close()


def test_calls_round_trip(private_bus) -> None:
    ext = FakeExtension(private_bus)
    ext.own()
    client = ShellClient(private_bus.connect())
    assert run_until(lambda: client.available)
    got: dict[str, tuple] = {}
    client.set_clipboard("é€", lambda *a: got.setdefault("set", a))
    client.paste_text("salut", "auto", False, lambda *a: got.setdefault("paste", a))
    client.type_text("abc", lambda *a: got.setdefault("type", a))
    client.get_focus_info(lambda *a: got.setdefault("focus", a))
    assert run_until(lambda: len(got) == 4)
    assert got["set"] == (True, "")
    assert got["paste"] == (True, "ctrl-v", "", "")
    assert got["type"] == (True, 3, "")
    assert got["focus"] == (True, "gedit", False)
    assert ("PasteText", ("salut", "auto", False)) in ext.calls
    ext.fail = DBusFailure(ERROR_MODIFIERS_HELD, "Modifier keys are still held down")
    client.paste_text("x", "auto", True, lambda *a: got.__setitem__("held", a))
    assert run_until(lambda: "held" in got)
    assert got["held"][3] == ERROR_MODIFIERS_HELD
    assert client.available  # not a refusal
    client.close()


def test_injector_over_extension(private_bus) -> None:
    ext = FakeExtension(private_bus)
    ext.own()
    ext.terminal = True
    conn = private_bus.connect()
    shell = ShellClient(conn)
    assert run_until(lambda: shell.available)
    inj = Injector(conn, InjectionConfig(), shell=shell,
                   clipboards=[ExtensionClipboard(shell)], keyboards=[ExtensionKeyboard(shell)],
                   check_screen_lock=False)
    results = []
    inj.inject("git status", results.append)
    assert run_until(lambda: results and not inj.busy)
    assert results[0].ok and (results[0].keyboard, results[0].clipboard) == ("extension",
                                                                             "extension")
    assert ext.calls == [("PasteText", ("git status ", "auto", True))]  # one call, no reads
    report = inj.diagnostics()
    json.dumps(report)
    assert report["extension"]["active"] and report["extension"]["usable"]
    assert report["extension"]["message"] == ""
    inj.close()
    shell.close()


def test_diagnostics_reports_untrusted_extension(private_bus) -> None:
    shell_conn = private_bus.connect()
    private_bus.own_name(shell_conn, "org.gnome.Shell")
    FakeExtension(private_bus, gnome_shell=False).own()
    conn = private_bus.connect()
    shell = ShellClient(conn)
    assert run_until(lambda: shell.running and shell.version == 2)
    inj = Injector(conn, InjectionConfig(), shell=shell,
                   clipboards=[ExtensionClipboard(shell)], keyboards=[ExtensionKeyboard(shell)],
                   check_screen_lock=False)
    ext_report = inj.diagnostics()["extension"]
    assert ext_report["active"] and not ext_report["usable"] and not ext_report["trusted"]
    assert "ignored" in ext_report["message"]
    assert inj.describe()["keyboard"] == "none"
    inj.close()
    shell.close()
