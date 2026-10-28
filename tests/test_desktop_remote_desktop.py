"""RemoteDesktop portal keyboard: session flow, restore token, idle close, keys."""

from __future__ import annotations

import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "desktop"))
from fakeportal import PORTAL_NAME, FakePortal  # noqa: E402
from glibtest import PrivateBus, run_for, run_until  # noqa: E402

from greffier.desktop import environment, keysyms, portal  # noqa: E402
from greffier.desktop.remote_desktop import RemoteDesktopKeyboard, token_file  # noqa: E402

PRESS, RELEASE = 1, 0


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


@pytest.fixture(autouse=True)
def latin_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(environment, "current_xkb_layout", lambda: "fr")


@pytest.fixture
def keyboard(private_bus: PrivateBus, fake: FakePortal) -> Iterator[RemoteDesktopKeyboard]:
    kb = RemoteDesktopKeyboard(private_bus.connect(), idle_close_ms=150)
    kb.probe()
    assert run_until(lambda: kb.probed)
    yield kb
    kb.close()


def paste(kb: RemoteDesktopKeyboard, shortcut: str = "auto") -> tuple[bool, str, str]:
    got: list[tuple[bool, str, str]] = []
    kb.paste(shortcut, lambda *a: got.append(a))
    assert run_until(lambda: got)
    return got[0]


def test_probe_reports_version(keyboard: RemoteDesktopKeyboard) -> None:
    assert keyboard.version == 2
    assert keyboard.available()


def test_first_paste_creates_session_and_saves_token(keyboard, fake) -> None:
    ok, used, msg = paste(keyboard)
    assert (ok, used, msg) == (True, "ctrl-v", "")
    select = fake.method_calls("SelectDevices")[0]
    assert select[1]["types"] == 1  # KEYBOARD only
    assert select[1]["persist_mode"] == 2
    assert "restore_token" not in select[1]
    assert fake.method_calls("Start")[0][1] == ""  # no parent window
    assert fake.notify == [
        ("NotifyKeyboardKeysym", keysyms.KEY_Control_L, PRESS),
        ("NotifyKeyboardKeysym", keysyms.KEY_v, PRESS),
        ("NotifyKeyboardKeysym", keysyms.KEY_v, RELEASE),
        ("NotifyKeyboardKeysym", keysyms.KEY_Control_L, RELEASE),
    ]
    path = token_file()
    assert path.read_text() .startswith("token-")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_session_reused_then_closed_when_idle_and_token_restored(keyboard, fake) -> None:
    paste(keyboard)
    paste(keyboard, "ctrl-shift-v")
    assert len(fake.method_calls("CreateSession")) == 1  # reused while busy
    assert keyboard.session_open
    assert run_until(lambda: fake.closed_sessions, 2)  # idle close (indicator goes away)
    assert not keyboard.session_open
    saved = token_file().read_text()
    paste(keyboard)
    assert len(fake.method_calls("CreateSession")) == 2
    assert fake.method_calls("SelectDevices")[1][1]["restore_token"] == saved


def test_refusal_backs_off(keyboard, fake) -> None:
    fake.responses["Start"] = (portal.RESPONSE_CANCELLED, {})
    ok, _used, msg = paste(keyboard)
    assert not ok and "refused" in msg
    assert not keyboard.available()  # no dialog at every dictation
    assert run_until(lambda: fake.closed_sessions)  # half-open session closed
    assert fake.notify == []


def test_keyboard_not_granted(keyboard, fake) -> None:
    fake.responses["Start"] = (0, {})  # devices = 0
    ok, _used, msg = paste(keyboard)
    assert not ok and "Keyboard access" in msg


def test_type_text_sends_keysyms(keyboard, fake) -> None:
    got = []
    keyboard.type_text("é€\n", lambda *a: got.append(a))
    assert run_until(lambda: got)
    assert got[0] == (True, 3, "")
    assert [(code, state) for _m, code, state in fake.notify] == [
        (0xE9, PRESS), (0xE9, RELEASE), (0x010020AC, PRESS), (0x010020AC, RELEASE),
        (keysyms.KEY_Return, PRESS), (keysyms.KEY_Return, RELEASE)]


def test_non_latin_layout_uses_keycodes(keyboard, fake, monkeypatch) -> None:
    monkeypatch.setattr(environment, "current_xkb_layout", lambda: "ru")
    assert paste(keyboard, "ctrl-shift-v")[0]
    assert fake.notify == [
        ("NotifyKeyboardKeycode", 29, PRESS), ("NotifyKeyboardKeycode", 42, PRESS),
        ("NotifyKeyboardKeycode", 47, PRESS), ("NotifyKeyboardKeycode", 47, RELEASE),
        ("NotifyKeyboardKeycode", 42, RELEASE), ("NotifyKeyboardKeycode", 29, RELEASE)]


def test_modifiers_released_after_error(keyboard, fake) -> None:
    fake.fail_notify_after = 1  # Control_L press ok, "v" press fails
    ok, _used, msg = paste(keyboard)
    assert not ok and msg
    pressed = {c for _m, c, s in fake.notify if s == PRESS}
    released = {c for _m, c, s in fake.notify if s == RELEASE}
    assert keysyms.KEY_Control_L in released
    assert pressed <= released


def test_session_revoked_by_user(keyboard, fake) -> None:
    paste(keyboard)
    handle = next(iter(fake.sessions))
    fake.close_session_from_portal(handle)
    assert run_until(lambda: not keyboard.session_open)
    assert paste(keyboard)[0]  # a new session is created
    assert len(fake.method_calls("CreateSession")) == 2


def test_no_portal(private_bus) -> None:
    kb = RemoteDesktopKeyboard(private_bus.connect())
    kb.probe()
    assert run_until(lambda: kb.probed)
    assert kb.version is None and not kb.available()
    ok, _used, msg = paste(kb)
    assert not ok and msg
    kb.close()
    run_for(0.01)


def test_cancel_pending_dialog_sends_no_keys(keyboard, fake) -> None:
    fake.no_response.add("Start")  # the user never answers the consent dialog
    assert keyboard.needs_consent
    got = []
    keyboard.paste("ctrl-v", lambda *a: got.append(a))
    assert run_until(lambda: fake.method_calls("Start"))
    keyboard.cancel_pending()
    assert run_until(lambda: got)
    assert got[0][0] is False
    assert run_until(lambda: fake.closed_requests and fake.closed_sessions)
    run_for(0.05)
    assert fake.notify == []
