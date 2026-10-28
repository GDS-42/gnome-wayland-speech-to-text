"""ShellClient: name-owner tracking, Version check, calls; Injector over it."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from gi.repository import GLib

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "desktop"))
from glibtest import PrivateBus, export_object, run_until  # noqa: E402

from greffier import paths  # noqa: E402
from greffier.config import InjectionConfig  # noqa: E402
from greffier.const import SHELL_DBUS_IFACE, SHELL_DBUS_NAME, SHELL_DBUS_PATH  # noqa: E402
from greffier.desktop.injection import ExtensionClipboard, ExtensionKeyboard, Injector  # noqa: E402
from greffier.desktop.shell_client import ShellClient  # noqa: E402

XML = paths.package_data("shell-interface.xml").read_text()


class FakeExtension:
    """Shell1 implemented in Python, like the GNOME Shell extension."""

    def __init__(self, bus: PrivateBus, version: int = 1) -> None:
        self.bus = bus
        self.conn = bus.connect()
        self.clipboard: str | None = "before"
        self.primary: str | None = None
        self.calls: list[tuple[str, tuple]] = []
        self.terminal = False
        export_object(self.conn, SHELL_DBUS_PATH, XML, self.handle,
                      {f"{SHELL_DBUS_IFACE}.Version": GLib.Variant("u", version)})

    def own(self) -> None:
        self.bus.own_name(self.conn, SHELL_DBUS_NAME)

    def release(self) -> None:
        self.bus.release_name(self.conn, SHELL_DBUS_NAME)

    def handle(self, _iface, method, params, _sender):
        args = params.unpack()
        self.calls.append((method, args))
        if method == "SetClipboard":
            self.clipboard = self.primary = args[0]
            return None
        if method == "GetClipboard":
            has = self.clipboard is not None
            return GLib.Variant("(bs)", (has, self.clipboard or ""))
        if method == "Paste":
            used = args[0] if args[0] != "auto" else (
                "ctrl-shift-v" if self.terminal else "ctrl-v")
            return GLib.Variant("(s)", (used,))
        if method == "TypeText":
            return GLib.Variant("(u)", (len(args[0]),))
        if method == "GetFocusInfo":
            return GLib.Variant("(sb)", ("org.gnome.Ptyxis" if self.terminal else "gedit",
                                         self.terminal))
        return RuntimeError("unknown")


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
    assert client.version == 1 and client.running
    ext.release()
    assert run_until(lambda: not client.available)
    assert changes == [True, False]
    client.close()


def test_incompatible_version_is_ignored(private_bus) -> None:
    ext = FakeExtension(private_bus, version=2)
    ext.own()
    client = ShellClient(private_bus.connect())
    assert run_until(lambda: client.running)
    assert client.version == 2
    assert not client.available
    got = []
    client.set_clipboard("x", lambda *a: got.append(a))
    assert run_until(lambda: got)
    assert got[0][0] is False and ext.calls == []
    client.close()


def test_calls_round_trip(private_bus) -> None:
    ext = FakeExtension(private_bus)
    ext.own()
    client = ShellClient(private_bus.connect())
    assert run_until(lambda: client.available)
    got: dict[str, tuple] = {}
    client.set_clipboard("é€", lambda *a: got.setdefault("set", a))
    assert run_until(lambda: "set" in got)
    client.get_clipboard(lambda *a: got.setdefault("get", a))
    client.paste("auto", lambda *a: got.setdefault("paste", a))
    client.type_text("abc", lambda *a: got.setdefault("type", a))
    client.get_focus_info(lambda *a: got.setdefault("focus", a))
    assert run_until(lambda: len(got) == 5)
    assert got["set"] == (True, "")
    assert got["get"] == (True, "é€")
    assert got["paste"] == (True, "ctrl-v", "")
    assert got["type"] == (True, 3, "")
    assert got["focus"] == (True, "gedit", False)
    ext.clipboard = None
    client.get_clipboard(lambda *a: got.__setitem__("get", a))
    assert run_until(lambda: got["get"] == (True, None))
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
    inj.restore_delay_ms = 50
    results = []
    inj.inject("git status", results.append)
    assert run_until(lambda: results and not inj.busy)
    assert results[0].ok and results[0].keyboard == "extension"
    assert [m for m, _a in ext.calls] == ["GetClipboard", "SetClipboard", "Paste",
                                          "GetClipboard", "SetClipboard"]
    assert ext.calls[1] == ("SetClipboard", ("git status ",))
    assert ext.calls[2] == ("Paste", ("auto",))
    assert ext.clipboard == "before" and ext.primary == "before"
    inj.close()
    shell.close()
