"""Injector decision logic and sequencing, with fake backends on a real GLib.MainLoop."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from gi.repository import Gio, GLib

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "desktop"))
from glibtest import PrivateBus, export_object, run_for, run_until  # noqa: E402

from greffier.config import InjectionConfig  # noqa: E402
from greffier.desktop.injection import Injector, InjectResult  # noqa: E402


class World:
    """The shared system clipboard plus an event log with timestamps."""

    def __init__(self, clipboard: str | None = "old") -> None:
        self.clipboard = clipboard
        self.log: list[tuple[float, tuple[Any, ...]]] = []

    def record(self, *event: Any) -> None:
        self.log.append((time.monotonic(), event))

    def events(self) -> list[tuple[Any, ...]]:
        return [e for _t, e in self.log]

    def time_of(self, *prefix: Any) -> float:
        return next(t for t, e in self.log if e[:len(prefix)] == prefix)


def later(fn, ms: int = 1) -> None:
    GLib.timeout_add(ms, lambda: (fn(), GLib.SOURCE_REMOVE)[1])


class FakeClipboard:
    def __init__(self, world: World, name: str = "extension", settle_ms: int = 30, *,
                 available: bool = True, fail_set: bool = False, fail_get: bool = False,
                 hang: bool = False) -> None:
        self.world, self.name, self.settle_ms = world, name, settle_ms
        self._available, self.fail_set, self.fail_get, self.hang = (
            available, fail_set, fail_get, hang)

    def available(self) -> bool:
        return self._available

    def set_text(self, text, callback) -> None:
        self.world.record("set", self.name, text)
        if self.hang:
            return
        if self.fail_set:
            later(lambda: callback(False, "boom"))
            return
        self.world.clipboard = text
        later(lambda: callback(True, ""))

    def get_text(self, callback) -> None:
        self.world.record("get", self.name)
        if self.fail_get:
            later(lambda: callback(False, None))
        else:
            value = self.world.clipboard
            later(lambda: callback(True, value))


class FakeKeyboard:
    def __init__(self, world: World, name: str, *, available: bool = True,
                 fail: bool = False, typed: int | None = None, message: str = "") -> None:
        self.world, self.name = world, name
        self._available, self.fail, self.typed, self.message = available, fail, typed, message

    def available(self) -> bool:
        return self._available

    def paste(self, shortcut, callback) -> None:
        self.world.record("paste", self.name, shortcut, self.world.clipboard)
        used = "ctrl-v" if shortcut == "auto" else shortcut
        later(lambda: callback(not self.fail, used, self.message if self.fail else ""))

    def type_text(self, text, callback) -> None:
        self.world.record("type", self.name, text)
        count = len(text) if self.typed is None else self.typed
        later(lambda: callback(not self.fail, 0 if self.fail else count, ""))


class FakeShell:
    def __init__(self, terminal: bool) -> None:
        self.available = True
        self.running = True
        self.version = 1
        self.terminal = terminal

    def get_focus_info(self, callback) -> None:
        later(lambda: callback(True, "org.gnome.Ptyxis" if self.terminal else "gedit",
                               self.terminal))


def make(world: World, cfg: InjectionConfig | None = None, *, clipboards=None, keyboards=None,
         shell=None, restore_ms: int = 60) -> Injector:
    clipboards = clipboards if clipboards is not None else [FakeClipboard(world)]
    keyboards = keyboards if keyboards is not None else [
        FakeKeyboard(world, "extension"), FakeKeyboard(world, "portal"),
        FakeKeyboard(world, "ydotool")]
    inj = Injector(None, cfg or InjectionConfig(), clipboards=clipboards, keyboards=keyboards,
                   shell=shell, check_screen_lock=False)
    inj.restore_delay_ms = restore_ms
    return inj


def inject(inj: Injector, text: str, timeout: float = 5) -> InjectResult:
    got: list[InjectResult] = []
    inj.inject(text, got.append)
    assert run_until(lambda: got and not inj.busy, timeout)
    return got[0]


# --------------------------------------------------------------------------- #
# paste
# --------------------------------------------------------------------------- #


def test_paste_sequence_and_restore() -> None:
    world = World("old")
    inj = make(world, restore_ms=400)
    result = inject(inj, "Bonjour")
    assert result == InjectResult(True, "paste", "extension", "extension", "")
    assert world.events() == [
        ("get", "extension"),  # save the previous clipboard
        ("set", "extension", "Bonjour "),  # trailing space separates dictations
        ("paste", "extension", "auto", "Bonjour "),  # the extension resolves auto itself
        ("get", "extension"),  # still ours?
        ("set", "extension", "old"),  # restore
    ]
    settle = world.time_of("paste") - world.time_of("set", "extension", "Bonjour ")
    assert settle >= 0.025  # extension settle delay (~30 ms)
    restore = world.log[3][0] - world.time_of("paste")
    assert restore >= 0.39  # ~400 ms for the app to read the clipboard
    assert world.clipboard == "old"


def test_result_delivered_before_restore() -> None:
    world = World("old")
    inj = make(world, restore_ms=300)
    got: list[InjectResult] = []
    inj.inject("x", got.append)
    assert run_until(lambda: got)
    assert inj.busy  # still holding the queue for the restore
    assert world.clipboard == "x "
    assert run_until(lambda: not inj.busy)
    assert world.clipboard == "old"


def test_no_restore_when_clipboard_changed_meanwhile() -> None:
    world = World("old")
    inj = make(world, restore_ms=150)
    got: list[InjectResult] = []
    inj.inject("x", got.append)
    assert run_until(lambda: got)
    world.clipboard = "user copied this"
    assert run_until(lambda: not inj.busy)
    assert world.clipboard == "user copied this"
    assert ("set", "extension", "old") not in world.events()


def test_no_restore_when_reading_failed() -> None:
    world = World("old")
    inj = make(world, clipboards=[FakeClipboard(world, fail_get=True)])
    assert inject(inj, "x").ok
    assert world.clipboard == "x "
    assert world.events().count(("get", "extension")) == 1  # no verification read either


def test_no_restore_when_previous_was_not_text() -> None:
    world = World(None)  # empty / image
    inj = make(world)
    assert inject(inj, "x").ok
    assert world.clipboard == "x "
    assert [e[0] for e in world.events()] == ["get", "set", "paste"]


def test_restore_disabled() -> None:
    world = World("old")
    inj = make(world, InjectionConfig(restore_clipboard=False))
    assert inject(inj, "x").ok
    assert [e[0] for e in world.events()] == ["set", "paste"]
    assert world.clipboard == "x "


@pytest.mark.parametrize(("terminal", "shortcut"), [(True, "ctrl-shift-v"), (False, "ctrl-v")])
def test_auto_shortcut_for_portal_uses_focus_info(terminal: bool, shortcut: str) -> None:
    world = World()
    inj = make(world, InjectionConfig(backend="portal"), shell=FakeShell(terminal))
    result = inject(inj, "ls")
    assert result.keyboard == "portal"
    assert ("paste", "portal", shortcut, "ls ") in world.events()


def test_auto_shortcut_without_extension_is_ctrl_v() -> None:
    world = World()
    inj = make(world, InjectionConfig(backend="ydotool"), shell=None)
    inject(inj, "x")
    assert ("paste", "ydotool", "ctrl-v", "x ") in world.events()


def test_explicit_shortcut_passed_through() -> None:
    world = World()
    inj = make(world, InjectionConfig(paste_shortcut="shift-insert"))
    inject(inj, "x")
    assert ("paste", "extension", "shift-insert", "x ") in world.events()


def test_keyboard_fallback_order() -> None:
    world = World()
    keyboards = [FakeKeyboard(world, "extension", fail=True),
                 FakeKeyboard(world, "portal", available=False),
                 FakeKeyboard(world, "ydotool")]
    result = inject(make(world, keyboards=keyboards), "x")
    assert result == InjectResult(True, "paste", "ydotool", "extension", "")
    assert [e[1] for e in world.events() if e[0] == "paste"] == ["extension", "ydotool"]


def test_clipboard_backend_fallback() -> None:
    world = World()
    clipboards = [FakeClipboard(world, "extension", fail_set=True),
                  FakeClipboard(world, "wl-copy", settle_ms=80)]
    result = inject(make(world, clipboards=clipboards), "x")
    assert result.clipboard == "wl-copy" and result.ok
    settle = world.time_of("paste") - world.time_of("set", "wl-copy")
    assert settle >= 0.075  # wl-copy needs longer to settle


def test_all_keyboards_fail_leaves_text_in_clipboard() -> None:
    world = World("old")
    keyboards = [FakeKeyboard(world, "extension", fail=True),
                 FakeKeyboard(world, "portal", fail=True, message="Remote keyboard access was "
                                                                   "refused.")]
    result = inject(make(world, keyboards=keyboards), "important")
    assert result.ok and result.method == "clipboard" and result.keyboard == "none"
    assert "refused" in result.message and "Ctrl+V" in result.message
    assert world.clipboard == "important "  # never restored: the text must not be lost


def test_no_keyboard_backend_copies_only() -> None:
    world = World("old")
    inj = make(world, keyboards=[FakeKeyboard(world, "extension", available=False)])
    assert inj.describe() == {"method": "paste", "keyboard": "none", "clipboard": "extension"}
    result = inject(inj, "x")
    assert (result.ok, result.method, result.clipboard) == (True, "clipboard", "extension")
    assert "Ctrl+V" in result.message
    assert world.clipboard == "x "
    assert "get" not in [e[0] for e in world.events()]  # nothing to restore


def test_forced_backend_unavailable() -> None:
    world = World()
    keyboards = [FakeKeyboard(world, "extension"), FakeKeyboard(world, "portal", available=False)]
    result = inject(make(world, InjectionConfig(backend="portal"), keyboards=keyboards), "x")
    assert result.method == "clipboard" and "“portal”" in result.message
    assert "paste" not in [e[0] for e in world.events()]


def test_no_clipboard_backend_types_instead() -> None:
    world = World()
    result = inject(make(world, clipboards=[FakeClipboard(world, available=False)]), "x")
    assert (result.ok, result.method, result.keyboard) == (True, "type", "extension")
    assert "typed instead" in result.message


def test_nothing_works() -> None:
    world = World()
    inj = make(world, clipboards=[], keyboards=[])
    result = inject(inj, "x")
    assert not result.ok and result.method == "none"
    assert "wl-clipboard" in result.message


# --------------------------------------------------------------------------- #
# type / clipboard methods
# --------------------------------------------------------------------------- #


def test_type_method() -> None:
    world = World("old")
    result = inject(make(world, InjectionConfig(method="type")), "été")
    assert result == InjectResult(True, "type", "extension", "none", "")
    assert world.events() == [("type", "extension", "été ")]
    assert world.clipboard == "old"


def test_type_partial_copies_full_text() -> None:
    world = World("old")
    keyboards = [FakeKeyboard(world, "extension", typed=2)]
    result = inject(make(world, InjectionConfig(method="type"), keyboards=keyboards), "日本語")
    assert result.ok and result.method == "type"
    assert "could not be typed" in result.message
    assert world.clipboard == "日本語 "


def test_type_with_ydotool_warns_about_layout() -> None:
    world = World()
    keyboards = [FakeKeyboard(world, "extension", fail=True), FakeKeyboard(world, "ydotool")]
    result = inject(make(world, InjectionConfig(method="type"), keyboards=keyboards), "x")
    assert result.keyboard == "ydotool" and "US keyboard layout" in result.message


def test_type_without_keyboard_copies() -> None:
    world = World()
    result = inject(make(world, InjectionConfig(method="type"), keyboards=[]), "x")
    assert (result.ok, result.method) == (True, "clipboard")
    assert world.clipboard == "x "


def test_clipboard_method() -> None:
    world = World("old")
    inj = make(world, InjectionConfig(method="clipboard"))
    assert inj.describe()["keyboard"] == "none"
    result = inject(inj, "x")
    assert result == InjectResult(True, "clipboard", "none", "extension", result.message)
    assert "Ctrl+V" in result.message
    assert world.events() == [("set", "extension", "x ")]


def test_clipboard_method_without_backend() -> None:
    world = World()
    result = inject(make(world, InjectionConfig(method="clipboard"), clipboards=[]), "x")
    assert not result.ok and result.message


# --------------------------------------------------------------------------- #
# queueing, text preparation, robustness
# --------------------------------------------------------------------------- #


def test_queue_serialises_and_restores_original_once() -> None:
    world = World("old")
    inj = make(world, restore_ms=120)
    results: list[InjectResult] = []
    inj.inject("one", results.append)
    inj.inject("two", results.append)
    assert run_until(lambda: len(results) == 2 and not inj.busy, 5)
    assert [r.ok for r in results] == [True, True]
    events = world.events()
    assert events == [
        ("get", "extension"),
        ("set", "extension", "one "),
        ("paste", "extension", "auto", "one "),
        # no restore in between: the original is handed to the next dictation
        ("set", "extension", "two "),
        ("paste", "extension", "auto", "two "),
        ("get", "extension"),
        ("set", "extension", "old"),
    ]
    # the second dictation did not touch the clipboard before the app read the first one
    assert world.time_of("set", "extension", "two ") - world.time_of("paste") >= 0.11
    assert world.clipboard == "old"


def test_queue_mixed_methods() -> None:
    world = World("old")
    inj = make(world, restore_ms=50)
    results: list[InjectResult] = []
    inj.inject("a", results.append)
    inj.update_config(InjectionConfig(method="type"))
    inj.inject("b", results.append)
    assert run_until(lambda: len(results) == 2 and not inj.busy)
    assert [r.method for r in results] == ["paste", "type"]
    assert world.clipboard == "old"  # restored before typing


@pytest.mark.parametrize(("text", "trailing", "expected"), [
    ("Bonjour.", True, "Bonjour. "),
    ("ligne\n", True, "ligne\n"),
    ("déjà ", True, "déjà "),
    ("Bonjour.", False, "Bonjour."),
])
def test_trailing_space(text: str, trailing: bool, expected: str) -> None:
    world = World(None)
    inject(make(world, InjectionConfig(trailing_space=trailing)), text)
    assert world.clipboard == expected


def test_empty_text_is_a_no_op() -> None:
    world = World()
    result = inject(make(world), "")
    assert result.ok and world.events() == []


def test_watchdog_unblocks_queue() -> None:
    world = World()
    inj = make(world, clipboards=[FakeClipboard(world, hang=True)],
               cfg=InjectionConfig(restore_clipboard=False))
    inj.job_timeout_ms = 200
    results: list[InjectResult] = []
    inj.inject("stuck", results.append)
    inj.inject("next", results.append)
    assert run_until(lambda: len(results) == 2, 3)
    assert not results[0].ok and "too long" in results[0].message


def test_callback_exception_does_not_break_queue() -> None:
    world = World(None)
    inj = make(world)
    results: list[InjectResult] = []

    def explode(_r: InjectResult) -> None:
        raise RuntimeError("caller bug")

    inj.inject("a", explode)
    inj.inject("b", results.append)
    assert run_until(lambda: results)
    assert results[0].ok


def test_close_drops_queue() -> None:
    world = World()
    inj = make(world)
    results: list[InjectResult] = []
    inj.inject("a", results.append)
    inj.inject("b", results.append)
    inj.close()
    run_for(0.1)
    assert results == []
    with pytest.raises(RuntimeError):
        inj.inject("c", results.append)


def test_describe_and_diagnostics_are_json() -> None:
    world = World()
    inj = make(world)
    assert inj.describe() == {"method": "paste", "keyboard": "extension",
                              "clipboard": "extension"}
    report = inj.diagnostics()
    json.dumps(report)
    assert set(report) >= {"extension", "portals", "tools"}
    assert set(report["extension"]) >= {"installed", "enabled", "active"}
    assert set(report["portals"]) == {"global_shortcuts", "remote_desktop", "registry"}
    assert set(report["tools"]) >= {"wl-copy", "ydotool", "ydotoold"}


# --------------------------------------------------------------------------- #
# screen lock
# --------------------------------------------------------------------------- #

SCREENSAVER_XML = """
<node><interface name="org.gnome.ScreenSaver">
  <method name="GetActive"><arg type="b" direction="out"/></method>
</interface></node>
"""


@pytest.fixture
def private_bus() -> Iterator[PrivateBus]:
    bus = PrivateBus()
    yield bus
    bus.close()


def test_locked_screen_copies_instead_of_typing(private_bus) -> None:
    service = private_bus.connect()
    export_object(service, "/org/gnome/ScreenSaver", SCREENSAVER_XML,
                  lambda *_a: GLib.Variant("(b)", (True,)))
    private_bus.own_name(service, "org.gnome.ScreenSaver")
    world = World("old")
    inj = Injector(private_bus.connect(), InjectionConfig(),
                   clipboards=[FakeClipboard(world)], keyboards=[FakeKeyboard(world, "portal")])
    result = inject(inj, "secret")
    assert (result.ok, result.method) == (True, "clipboard")
    assert "locked" in result.message
    assert "paste" not in [e[0] for e in world.events()]


def test_unlocked_or_unknown_screen_pastes(private_bus) -> None:
    world = World()
    conn: Gio.DBusConnection = private_bus.connect()  # no ScreenSaver: assume unlocked
    inj = Injector(conn, InjectionConfig(), clipboards=[FakeClipboard(world)],
                   keyboards=[FakeKeyboard(world, "portal")])
    assert inject(inj, "x").method == "paste"


def test_watchdog_after_copy_reports_clipboard() -> None:
    world = World()

    class Hanging(FakeKeyboard):
        def paste(self, shortcut, callback) -> None:
            self.world.record("paste", self.name, shortcut, self.world.clipboard)

    inj = make(world, InjectionConfig(restore_clipboard=False),
               keyboards=[Hanging(world, "extension")])
    inj.job_timeout_ms = 200
    result = inject(inj, "texte")
    assert (result.ok, result.method, result.clipboard) == (True, "clipboard", "extension")
    assert "too long" in result.message and world.clipboard == "texte "
