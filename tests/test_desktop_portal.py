"""portal.py: Request/Response helper, sessions, versions, host registry."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from gi.repository import GLib

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "desktop"))
from fakeportal import PORTAL_NAME, FakePortal  # noqa: E402
from glibtest import PrivateBus, ThreadedService, run_for, run_until  # noqa: E402

from greffier.desktop import portal  # noqa: E402


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


@pytest.fixture
def client(private_bus: PrivateBus):
    conn = private_bus.connect()
    yield conn
    conn.close_sync(None)


def ask(client, **kwargs) -> list[portal.PortalResponse]:
    got: list[portal.PortalResponse] = []
    portal.call_request(client, "org.example.Test", "Ask", "s", ("tea?",),
                        {"extra": GLib.Variant("b", True)}, got.append, **kwargs)
    return got


def test_tokens_are_valid_path_elements() -> None:
    tokens = {portal.new_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(re.fullmatch(r"[A-Za-z0-9_]+", t) for t in tokens)


def test_request_and_session_paths(client) -> None:
    unique = client.get_unique_name()
    assert portal.sender_component(client) == unique[1:].replace(".", "_")
    assert portal.request_path(client, "tok") == (
        f"/org/freedesktop/portal/desktop/request/{portal.sender_component(client)}/tok")
    assert "/session/" in portal.session_path(client, "tok")


def test_response_success(fake, client) -> None:
    got = ask(client)
    assert run_until(lambda: got)
    assert len(got) == 1 and got[0].ok
    assert got[0].results == {"answer": "yes to tea?"}
    _iface, _method, args = fake.calls[-1]
    assert args[0] == "tea?"
    assert args[1]["extra"] is True and args[1]["handle_token"].startswith("greffier_")
    run_for(0.05)
    assert len(got) == 1  # exactly one callback


def test_response_emitted_before_method_reply_is_not_missed(fake, client) -> None:
    fake.respond_before_reply = True
    got = ask(client)
    assert run_until(lambda: got)
    assert got[0].ok


def test_legacy_portal_ignoring_handle_token(fake, client) -> None:
    fake.legacy_request_path = True
    got = ask(client)
    assert run_until(lambda: got)
    assert got[0].ok and got[0].results["answer"] == "yes to tea?"


def test_user_cancelled(fake, client) -> None:
    fake.responses["Ask"] = (portal.RESPONSE_CANCELLED, {})
    got = ask(client)
    assert run_until(lambda: got)
    assert got[0].cancelled and not got[0].ok


def test_dbus_error_is_reported(fake, client) -> None:
    got: list[portal.PortalResponse] = []
    portal.call_request(client, "org.example.Missing", "Nope", "", (), {}, got.append)
    assert run_until(lambda: got)
    assert got[0].code == portal.RESPONSE_DBUS_ERROR
    assert got[0].error and "GDBus.Error" not in got[0].error


def test_no_portal_at_all(private_bus) -> None:
    conn = private_bus.connect()
    got: list[portal.PortalResponse] = []
    portal.call_request(conn, "org.example.Test", "Ask", "s", ("x",), {}, got.append)
    assert run_until(lambda: got)
    assert got[0].code == portal.RESPONSE_DBUS_ERROR
    assert got[0].error_name == "org.freedesktop.DBus.Error.ServiceUnknown"


def test_response_timeout_and_cancel(fake, client) -> None:
    fake.no_response.add("Ask")
    got = ask(client, response_timeout_ms=100)
    assert run_until(lambda: got, 3)
    assert got[0].code == portal.RESPONSE_TIMEOUT
    assert run_until(lambda: fake.closed_requests)  # we closed the dangling request

    got2: list[portal.PortalResponse] = []
    request = portal.call_request(client, "org.example.Test", "Ask", "s", ("y",), {},
                                  got2.append)
    run_until(lambda: len(fake.method_calls("Ask")) == 2)
    request.cancel()
    assert run_until(lambda: len(fake.closed_requests) == 2)
    run_for(0.05)
    assert got2 == [] and not request.pending


def test_create_session_and_close(fake, client) -> None:
    got = []
    closed = []
    portal.create_session(client, "org.freedesktop.portal.RemoteDesktop",
                          lambda s, r: got.append((s, r)), on_closed=closed.append)
    assert run_until(lambda: got)
    session, response = got[0]
    assert response.ok and session is not None
    assert session.handle == portal.session_path(
        client, fake.method_calls("CreateSession")[0][0]["session_handle_token"])
    assert session.handle in fake.sessions
    session.close()
    assert run_until(lambda: fake.closed_sessions == [session.handle])
    session.close()  # idempotent
    assert closed == []


def test_session_closed_by_portal(fake, client) -> None:
    got = []
    closed = []
    portal.create_session(client, "org.freedesktop.portal.GlobalShortcuts",
                          lambda s, r: got.append(s), on_closed=closed.append)
    assert run_until(lambda: got)
    fake.close_session_from_portal(got[0].handle)
    assert run_until(lambda: closed)
    assert got[0].closed


def test_create_session_refused(fake, client) -> None:
    fake.responses["CreateSession"] = (portal.RESPONSE_OTHER, {})
    got = []
    portal.create_session(client, "org.freedesktop.portal.RemoteDesktop",
                          lambda s, r: got.append((s, r)))
    assert run_until(lambda: got)
    assert got[0][0] is None and got[0][1].code == portal.RESPONSE_OTHER


def test_interface_versions(fake, client) -> None:
    versions = {}
    for iface in (portal.REMOTE_DESKTOP_IFACE, portal.GLOBAL_SHORTCUTS_IFACE,
                  portal.REGISTRY_IFACE, "org.freedesktop.portal.Nope"):
        portal.get_interface_version(client, iface, lambda v, i=iface: versions.__setitem__(i, v))
    assert run_until(lambda: len(versions) == 4)
    assert versions[portal.REMOTE_DESKTOP_IFACE] == 2
    assert versions[portal.GLOBAL_SHORTCUTS_IFACE] == 1
    assert versions[portal.REGISTRY_IFACE] == 1
    assert versions["org.freedesktop.portal.Nope"] is None


def test_register_host_app(private_bus, client) -> None:
    # register_host_app() is synchronous: serve the fake from another thread.
    service = ThreadedService(private_bus, FakePortal, PORTAL_NAME)
    try:
        assert portal.register_host_app(client, "io.github.greffier.Greffier.Daemon") is True
        assert service.result.registered_apps == ["io.github.greffier.Greffier.Daemon"]
    finally:
        service.stop()


def test_register_host_app_async(fake, client) -> None:
    got = []
    portal.register_host_app_async(client, "x.y.Z", got.append)
    assert run_until(lambda: got) and got == [True]
    assert fake.registered_apps == ["x.y.Z"]


def test_register_host_app_without_registry(private_bus, client) -> None:
    service = ThreadedService(private_bus, lambda c: FakePortal(c, interfaces=("RemoteDesktop",)),
                              PORTAL_NAME)
    try:
        assert portal.register_host_app(client, "io.github.greffier.Greffier.Daemon") is False
    finally:
        service.stop()
    # and with no portal on the bus at all
    assert portal.register_host_app(client, "io.github.greffier.Greffier.Daemon") is False
