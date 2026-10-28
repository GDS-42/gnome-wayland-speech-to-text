"""GlobalShortcuts portal: binding flow, signals, idempotency."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "desktop"))
from fakeportal import PORTAL_NAME, FakePortal  # noqa: E402
from glibtest import PrivateBus, run_for, run_until  # noqa: E402

from greffier.desktop import portal  # noqa: E402
from greffier.desktop.global_shortcuts import SHORTCUT_ID, GlobalShortcutsPortal  # noqa: E402


@pytest.fixture(scope="module")
def private_bus() -> Iterator[PrivateBus]:
    bus = PrivateBus()
    yield bus
    bus.close()


@pytest.fixture
def fake(private_bus: PrivateBus) -> Iterator[FakePortal]:
    conn = private_bus.connect()
    fake = FakePortal(conn)
    private_bus.own_name(conn, PORTAL_NAME)
    yield fake
    fake.close()
    private_bus.release_name(conn, PORTAL_NAME)
    conn.close_sync(None)


class Events:
    def __init__(self) -> None:
        self.log: list[str] = []

    def pressed(self) -> None:
        self.log.append("pressed")

    def released(self) -> None:
        self.log.append("released")


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture
def gs(private_bus, fake, events) -> Iterator[GlobalShortcutsPortal]:
    shortcuts = GlobalShortcutsPortal(private_bus.connect(), events.pressed, events.released)
    shortcuts.probe()
    assert run_until(lambda: shortcuts.probed)
    yield shortcuts
    shortcuts.close()


def bind(gs: GlobalShortcutsPortal, accel: str = "<Super><Alt>d") -> tuple[bool, str, str]:
    got: list[tuple[bool, str, str]] = []
    gs.bind(accel, "Dictate", lambda *a: got.append(a))
    assert run_until(lambda: got)
    return got[0]


def test_bind_sends_preferred_trigger_and_reads_back(gs, fake) -> None:
    assert gs.available() and gs.version == 1
    ok, trigger, msg = bind(gs)
    assert (ok, trigger, msg) == (True, "Super+Alt+D", "")
    session, shortcuts, parent, _opts = fake.method_calls("BindShortcuts")[0]
    assert parent == ""
    assert shortcuts == [(SHORTCUT_ID, {"description": "Dictate",
                                        "preferred_trigger": "LOGO+ALT+d"})]
    assert fake.method_calls("ListShortcuts")[0][0] == session
    assert gs.active and gs.bound_accelerator == "<Super><Alt>d"


def test_activated_and_deactivated(gs, fake, events) -> None:
    bind(gs)
    session = fake.method_calls("BindShortcuts")[0][0]
    fake.emit_shortcut("Activated", session)
    fake.emit_shortcut("Deactivated", session)
    fake.emit_shortcut("Activated", session, "other-id")  # not ours
    fake.emit_shortcut("Activated", "/org/freedesktop/portal/desktop/session/x/y")  # other session
    assert run_until(lambda: events.log == ["pressed", "released"])
    run_for(0.05)
    assert events.log == ["pressed", "released"]


def test_rebind_same_accelerator_is_a_no_op(gs, fake) -> None:
    bind(gs)
    assert bind(gs)[0]
    assert len(fake.method_calls("CreateSession")) == 1
    assert len(fake.method_calls("BindShortcuts")) == 1


def test_changed_accelerator_replaces_the_session(gs, fake) -> None:
    bind(gs)
    first = fake.method_calls("BindShortcuts")[0][0]
    assert bind(gs, "<Control><Alt>space")[0]
    assert run_until(lambda: first in fake.closed_sessions)
    assert fake.method_calls("BindShortcuts")[1][1][0][1]["preferred_trigger"] == "CTRL+ALT+space"
    assert len(fake.sessions) == 1


def test_user_cancels_dialog(gs, fake) -> None:
    fake.responses["BindShortcuts"] = (portal.RESPONSE_CANCELLED, {})
    ok, _trigger, msg = bind(gs)
    assert not ok and "not confirmed" in msg
    assert not gs.active
    assert run_until(lambda: fake.closed_sessions)


def test_release_sent_when_session_closes_while_pressed(gs, fake, events) -> None:
    closed = []
    gs._on_closed = lambda: closed.append(True)
    bind(gs)
    session = fake.method_calls("BindShortcuts")[0][0]
    fake.emit_shortcut("Activated", session)
    assert run_until(lambda: events.log == ["pressed"])
    fake.close_session_from_portal(session)
    assert run_until(lambda: closed)
    assert events.log == ["pressed", "released"]  # recording never stays stuck
    assert not gs.active


def test_invalid_accelerator(gs) -> None:
    ok, _t, msg = bind(gs, "<Hyper>x")
    assert not ok and "cannot be used" in msg


def test_portal_missing(private_bus, events) -> None:
    shortcuts = GlobalShortcutsPortal(private_bus.connect(), events.pressed, events.released)
    shortcuts.probe()
    assert run_until(lambda: shortcuts.probed)
    assert not shortcuts.available()
    ok, _t, msg = bind(shortcuts)
    assert not ok and "GNOME 48" in msg
