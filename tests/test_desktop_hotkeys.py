"""HotkeyManager: backend choice, fallbacks, exclusivity and idempotency."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from gi.repository import Gio, GLib

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "desktop"))
from fakeportal import PORTAL_NAME, FakePortal  # noqa: E402
from glibtest import PrivateBus, run_until  # noqa: E402

from greffier.config import ShortcutConfig  # noqa: E402
from greffier.desktop.gnome_keybinding import GnomeKeybinding  # noqa: E402
from greffier.desktop.hotkeys import HotkeyManager, HotkeyStatus  # noqa: E402

SCHEMAS = Path(__file__).parent / "fixtures" / "desktop" / "schemas"
TOGGLE = ["gdbus", "call", "--session", "--dest", "io.github.greffier.Greffier.Daemon",
          "--object-path", "/io/github/greffier/Greffier/Daemon",
          "--method", "io.github.greffier.Greffier.Daemon1.Toggle"]


@pytest.fixture(scope="module")
def schema_source(tmp_path_factory: pytest.TempPathFactory) -> Gio.SettingsSchemaSource:
    if shutil.which("glib-compile-schemas") is None:
        pytest.skip("glib-compile-schemas not installed")
    out = tmp_path_factory.mktemp("schemas")
    for xml in SCHEMAS.glob("*.gschema.xml"):
        shutil.copy(xml, out)
    subprocess.run(["glib-compile-schemas", str(out)], check=True)
    return Gio.SettingsSchemaSource.new_from_directory(str(out), None, True)


@pytest.fixture
def gnome(schema_source) -> GnomeKeybinding:
    return GnomeKeybinding(schema_source=schema_source, backend=Gio.memory_settings_backend_new())


class FakeShortcuts:
    """Stand-in for GlobalShortcutsPortal (async like the real one)."""

    def __init__(self, available: bool = True, succeed: bool = True) -> None:
        self.version = 1 if available else None
        self.probed = False
        self.succeed = succeed
        self.binds: list[str] = []
        self.closes = 0
        self.bound_accelerator: str | None = None
        self.trigger = "Super+Alt+D"

    def probe(self, callback: Callable[[int | None], None] | None = None) -> None:
        def done() -> bool:
            self.probed = True
            if callback:
                callback(self.version)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(done)

    def available(self) -> bool:
        return self.version is not None

    @property
    def active(self) -> bool:
        return self.bound_accelerator is not None

    def bind(self, accel: str, description: str, callback) -> None:
        if self.active and accel == self.bound_accelerator:
            GLib.idle_add(lambda: (callback(True, self.trigger, ""), False)[1])
            return
        self.binds.append(accel)

        def done() -> bool:
            if self.succeed:
                self.bound_accelerator = accel
                callback(True, self.trigger, "")
            else:
                callback(False, "", "The global shortcut was not confirmed.")
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(5, done)

    def close(self) -> None:
        if self.bound_accelerator is not None:
            self.closes += 1
        self.bound_accelerator = None


def apply(manager: HotkeyManager, **cfg) -> HotkeyStatus:
    got: list[HotkeyStatus] = []
    manager.apply(ShortcutConfig(**cfg), got.append)
    assert run_until(lambda: got)
    return got[0]


@pytest.fixture
def fake_portal() -> FakeShortcuts:
    return FakeShortcuts()


@pytest.fixture
def manager(gnome, fake_portal) -> Iterator[HotkeyManager]:
    m = HotkeyManager(None, lambda: None, lambda: None, TOGGLE, gnome=gnome, portal=fake_portal)
    yield m
    m.close()


def test_toggle_mode_uses_gnome_shortcut(manager, gnome, fake_portal) -> None:
    status = apply(manager)
    assert status == HotkeyStatus("gnome", True, "<Super><Alt>d", "toggle", "")
    assert gnome.current() == ("<Super><Alt>d", " ".join(TOGGLE))
    assert fake_portal.binds == []
    assert manager.status == status


def test_push_to_talk_uses_portal_and_removes_gnome(manager, gnome, fake_portal) -> None:
    apply(manager)
    status = apply(manager, mode="push-to-talk")
    assert (status.backend, status.active, status.mode) == ("portal", True, "push-to-talk")
    assert status.message == ""  # the trigger matches (order-insensitive)
    assert fake_portal.binds == ["<Super><Alt>d"]
    assert not gnome.installed()  # never two bindings for the same keys


def test_push_to_talk_reapply_is_idempotent(manager, fake_portal) -> None:
    apply(manager, mode="push-to-talk")
    apply(manager, mode="push-to-talk")
    apply(manager, mode="push-to-talk")
    assert fake_portal.binds == ["<Super><Alt>d"]  # no new portal dialog


def test_back_to_toggle_closes_portal(manager, gnome, fake_portal) -> None:
    apply(manager, mode="push-to-talk")
    status = apply(manager, mode="toggle")
    assert status.backend == "gnome" and status.active
    assert fake_portal.closes == 1 and not fake_portal.active
    assert gnome.installed()


def test_push_to_talk_without_portal_falls_back(gnome) -> None:
    m = HotkeyManager(None, lambda: None, lambda: None, TOGGLE, gnome=gnome,
                      portal=FakeShortcuts(available=False))
    status = apply(m, mode="push-to-talk")
    assert (status.backend, status.active, status.mode) == ("gnome", True, "toggle")
    assert "GNOME 48" in status.message
    assert gnome.installed()


def test_push_to_talk_refused_falls_back(gnome) -> None:
    portal = FakeShortcuts(succeed=False)
    m = HotkeyManager(None, lambda: None, lambda: None, TOGGLE, gnome=gnome, portal=portal)
    status = apply(m, mode="push-to-talk")
    assert (status.backend, status.active, status.mode) == ("gnome", True, "toggle")
    assert "not confirmed" in status.message and "toggle mode" in status.message


def test_forced_portal_failure_does_not_install_gnome(gnome) -> None:
    m = HotkeyManager(None, lambda: None, lambda: None, TOGGLE, gnome=gnome,
                      portal=FakeShortcuts(succeed=False))
    status = apply(m, backend="portal")
    assert (status.backend, status.active) == ("portal", False)
    assert not gnome.installed()


def test_forced_gnome_in_push_to_talk(manager, fake_portal) -> None:
    status = apply(manager, backend="gnome", mode="push-to-talk")
    assert (status.backend, status.mode, status.active) == ("gnome", "toggle", True)
    assert "Push-to-talk needs" in status.message
    assert fake_portal.binds == []


def test_none_removes_everything(manager, gnome, fake_portal) -> None:
    apply(manager)
    status = apply(manager, backend="none")
    assert (status.backend, status.active) == ("none", False)
    assert not gnome.installed()
    apply(manager, mode="push-to-talk")
    apply(manager, backend="none")
    assert not fake_portal.active


def test_conflict_is_a_warning(manager) -> None:
    status = apply(manager, accelerator="<Super>l")
    assert status.active and "Lock screen" in status.message


def test_coalesced_applies_answer_every_callback(manager, fake_portal) -> None:
    got: list[HotkeyStatus] = []
    manager.apply(ShortcutConfig(mode="push-to-talk"), got.append)
    manager.apply(ShortcutConfig(mode="push-to-talk", accelerator="<Super>F9"), got.append)
    manager.apply(ShortcutConfig(mode="push-to-talk", accelerator="<Super>F10"), got.append)
    assert run_until(lambda: len(got) == 3)
    assert manager.status.accelerator == "<Super>F10"
    assert fake_portal.bound_accelerator == "<Super>F10"
    assert "<Super>F9" not in fake_portal.binds  # superseded before it ran


def test_remove_deletes_gnome_shortcut(manager, gnome) -> None:
    apply(manager)
    manager.remove()
    assert not gnome.installed()
    assert manager.status.backend == "none"


def test_close_keeps_gnome_shortcut(gnome, fake_portal) -> None:
    m = HotkeyManager(None, lambda: None, lambda: None, TOGGLE, gnome=gnome, portal=fake_portal)
    apply(m)
    m.close()
    assert gnome.installed()  # still D-Bus-activates the daemon
    with pytest.raises(RuntimeError):
        m.apply(ShortcutConfig())


# --- integration with the real GlobalShortcuts client -------------------------


@pytest.fixture
def private_bus() -> Iterator[PrivateBus]:
    bus = PrivateBus()
    yield bus
    bus.close()


def test_portal_events_reach_callbacks(private_bus, gnome) -> None:
    service = private_bus.connect()
    fake = FakePortal(service)
    private_bus.own_name(service, PORTAL_NAME)
    log: list[str] = []
    m = HotkeyManager(private_bus.connect(), lambda: log.append("down"),
                      lambda: log.append("up"), TOGGLE, gnome=gnome)
    status = apply(m, mode="push-to-talk")
    assert (status.backend, status.active) == ("portal", True), status
    session = fake.method_calls("BindShortcuts")[0][0]
    fake.emit_shortcut("Activated", session)
    fake.emit_shortcut("Deactivated", session)
    assert run_until(lambda: log == ["down", "up"])
    apply(m, mode="push-to-talk")
    assert len(fake.method_calls("BindShortcuts")) == 1  # idempotent over D-Bus too
    fake.trigger_description = "Ctrl+F12"
    status = apply(m, mode="push-to-talk", accelerator="<Super>F12")
    assert "Ctrl+F12" in status.message  # the user picked another trigger
    m.close()
    assert run_until(lambda: len(fake.closed_sessions) == 2)
    fake.close()
