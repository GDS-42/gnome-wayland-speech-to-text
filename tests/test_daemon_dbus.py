"""The D-Bus API and greffierctl, against the controller (with fakes) on a private bus."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
from gi.repository import Gio, GLib

from greffier.const import DBUS_IFACE, DBUS_NAME, DBUS_PATH
from greffier.daemon.controller import Controller
from greffier.daemon.service import DBusService
from tests.daemon_fakes import make_deps, run_until

pytestmark = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="needs dbus-daemon")


@pytest.fixture
def daemon(tmp_path):
    test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    test_bus.up()
    address = test_bus.get_bus_address()
    flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
             | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
    server_conn = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
    client_conn = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
    service = DBusService(server_conn)
    deps = make_deps(tmp_path, ["parakeet-tdt-0.6b-v3-int8", "silero-vad"])
    ctl = Controller(deps, service.emit, lambda *a, **k: None, config_path=tmp_path / "c.toml")
    service.controller = ctl
    ctl.start()
    owner = Gio.bus_own_name_on_connection(server_conn, DBUS_NAME, Gio.BusNameOwnerFlags.NONE,
                                           None, None)
    run_until(lambda: ctl.status()["model_loaded"])
    yield {"ctl": ctl, "client": client_conn, "address": address, "deps": deps}
    Gio.bus_unown_name(owner)
    ctl.shutdown()
    service.unregister()
    server_conn.close_sync(None)
    client_conn.close_sync(None)
    test_bus.down()


def call(conn, method, signature=None, *args, iface=DBUS_IFACE):
    params = GLib.Variant(signature, args) if signature else None
    reply = {}

    def done(c, res):
        try:
            reply["value"] = c.call_finish(res)
        except GLib.Error as exc:
            reply["error"] = exc

    conn.call(DBUS_NAME, DBUS_PATH, iface, method, params, None,
              Gio.DBusCallFlags.NONE, 5000, None, done)
    assert run_until(lambda: reply)
    if "error" in reply:
        raise reply["error"]
    value = reply["value"].unpack()
    return value[0] if value else None


def test_methods_and_errors(daemon):
    conn = daemon["client"]
    status = json.loads(call(conn, "GetStatus"))
    assert status["state"] == "idle" and status["model_loaded"]
    config = json.loads(call(conn, "SetConfig", "(s)", json.dumps({"model": {"threads": 2}})))
    assert config["model"]["threads"] == 2
    with pytest.raises(GLib.Error) as err:
        call(conn, "SetConfig", "(s)", "{not json")
    assert "InvalidArgument" in Gio.DBusError.get_remote_error(err.value)
    with pytest.raises(GLib.Error) as err:
        call(conn, "InstallModel", "(s)", "does-not-exist")
    assert Gio.DBusError.get_remote_error(err.value).endswith("Error.NotFound")
    models = json.loads(call(conn, "ListModels"))
    assert any(m["active"] for m in models)
    assert json.loads(call(conn, "GetDiagnostics"))["checks"]


def test_signals_and_properties(daemon):
    conn = daemon["client"]
    seen: list[tuple[str, tuple]] = []
    sub = conn.signal_subscribe(None, None, None, DBUS_PATH, None, Gio.DBusSignalFlags.NONE,
                                lambda c, s, p, i, name, params: seen.append((name,
                                                                              params.unpack())))
    call(conn, "Toggle")
    call(conn, "Toggle")
    assert run_until(lambda: any(n == "Transcribed" for n, _a in seen))
    names = {n for n, _a in seen}
    assert {"StateChanged", "PropertiesChanged", "Level", "Transcribed"} <= names
    props = call(conn, "GetAll", "(s)", DBUS_IFACE, iface="org.freedesktop.DBus.Properties")
    assert props["ActiveModel"] == "parakeet-tdt-0.6b-v3-int8" and props["Version"]
    conn.signal_unsubscribe(sub)


def run_cli(daemon, *args) -> subprocess.CompletedProcess:
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=daemon["address"], GREFFIER_LANG="en")
    proc = subprocess.Popen([sys.executable, "-m", "greffier.cli", *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert run_until(lambda: proc.poll() is not None, timeout=20)
    out, err = proc.communicate()
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def test_cli(daemon):
    res = run_cli(daemon, "status")
    assert res.returncode == 0, res.stderr
    assert "State: idle" in res.stdout
    res = run_cli(daemon, "models", "list")
    assert "parakeet-tdt-0.6b-v3-int8" in res.stdout
    res = run_cli(daemon, "config", "set", "recording.auto_stop", "true")
    assert res.returncode == 0, res.stderr
    assert daemon["ctl"].cfg.recording.auto_stop is True
    res = run_cli(daemon, "config", "get", "recording.auto_stop")
    assert res.stdout.strip() == "true"
    res = run_cli(daemon, "models", "install", "canary-180m-flash-int8")
    assert res.returncode == 0, res.stderr
    assert "installed" in res.stdout
    res = run_cli(daemon, "models", "remove", "nope")
    assert res.returncode == 1
    assert res.stderr.strip() == "Error: nope is not installed."
    res = run_cli(daemon, "doctor")
    assert "Model: Parakeet" in res.stdout
