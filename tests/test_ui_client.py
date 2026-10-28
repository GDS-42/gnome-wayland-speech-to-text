"""DaemonClient against tests/fake_daemon.py on a private bus (no display needed)."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET

import pytest

pytest.importorskip("gi")
if shutil.which("dbus-daemon") is None:  # pragma: no cover
    pytest.skip("dbus-daemon is not installed", allow_module_level=True)
try:
    import greffier.ui  # noqa: F401 - gi.require_version()
except (ImportError, ValueError) as exc:  # pragma: no cover
    pytest.skip(f"GTK 4 / libadwaita not available: {exc}", allow_module_level=True)

from gi.repository import Gio, GLib

from greffier import paths
from greffier.const import DBUS_IFACE, DBUS_NAME, DBUS_PATH, VERSION
from greffier.ui.client import DaemonClient
from tests.fake_daemon import FakeService, wait_until


@pytest.fixture
def service():
    with FakeService("--fast") as svc:
        yield svc


@pytest.fixture
def client(service):
    client = DaemonClient(service.connection())
    client.connect_to_service()
    assert wait_until(lambda: client.connected and client.models_loaded, 10)
    return client


def _members(xml: str) -> dict[str, set[str]]:
    root = ET.fromstring(xml)
    iface = next(i for i in root.iter("interface") if i.get("name") == DBUS_IFACE)
    out: dict[str, set[str]] = {}
    for kind in ("method", "signal", "property"):
        items = set()
        for node in iface.iter(kind):
            sig = "".join(f"{a.get('direction', '')}:{a.get('type')}"
                          for a in node.iter("arg")) or node.get("type", "")
            items.add(f"{node.get('name')}({sig})")
        out[kind] = items
    return out


def test_fake_daemon_matches_the_contract(service):
    conn = service.connection()
    reply = conn.call_sync(DBUS_NAME, DBUS_PATH, "org.freedesktop.DBus.Introspectable",
                           "Introspect", None, GLib.VariantType("(s)"),
                           Gio.DBusCallFlags.NONE, 5000, None)
    exported = _members(reply.unpack()[0])
    contract = _members(paths.package_data("daemon-interface.xml").read_text("utf-8"))
    # Introspection always prints the direction of method arguments.
    contract["method"] = {m.replace("(:", "(in:").replace(",:", ",in:") for m in
                          contract["method"]}
    assert exported["signal"] == contract["signal"]
    assert exported["property"] == contract["property"]
    assert {m.split("(")[0] for m in exported["method"]} == \
        {m.split("(")[0] for m in contract["method"]}


def test_initial_sync_decodes_json(client):
    assert client.version == VERSION
    assert client.get("general", "first_run_done") is True
    assert client.shortcut == "<Super><Alt>d"
    assert set(client.status) >= {"state", "detail", "model", "model_name", "model_loaded",
                                  "recording_seconds", "shortcut", "injection"}
    assert len(client.models) == 6 and len(client.asr_models()) == 5
    active = client.active_model_info()
    assert active is not None and active["id"] == "parakeet-tdt-0.6b-v3-int8"
    canary = client.model("canary-180m-flash-int8")
    assert canary["installed"] and canary["update_available"]
    assert wait_until(lambda: client.state == "idle", 5)


def test_set_config_round_trip(client):
    seen = []
    client.connect("config-changed", lambda _c, cfg: seen.append(cfg))
    done = []
    client.set_config({"general": {"sounds": False}}, on_done=done.append)
    assert wait_until(lambda: done and seen, 5)
    assert done[0]["general"]["sounds"] is False
    assert client.get("general", "sounds") is False


def test_invalid_patch_reports_error_code(client):
    errors = []
    client.set_config({"general": {"history_size": -5}}, on_error=errors.append)
    assert wait_until(lambda: errors, 5)
    assert errors[0].code == "InvalidArgument"
    assert client.get("general", "history_size") == 50


def test_dictation_cycle(client):
    assert wait_until(lambda: client.state == "idle", 5)
    states, levels, texts = [], [], []
    client.connect("state-changed", lambda _c, state, _d: states.append(state))
    client.connect("level", lambda _c, level: levels.append(level))
    client.connect("transcribed", lambda _c, text, meta: texts.append((text, meta)))
    client.toggle()
    assert wait_until(lambda: client.state == "recording" and len(levels) > 3, 5)
    assert all(0.0 <= lvl <= 1.0 for lvl in levels)
    client.toggle()
    assert wait_until(lambda: texts and client.state == "idle", 5)
    assert "transcribing" in states
    text, meta = texts[0]
    assert text and isinstance(meta, dict) and "duration" in meta
    history = []
    client.get_history(history.append)
    assert wait_until(lambda: history, 5)
    assert history[0][0]["text"] == text


def test_install_progress_until_done(client):
    phases = []
    client.connect("model-progress", lambda _c, mid, phase, *_a: phases.append((mid, phase)))
    client.install_model("whisper-turbo-int8")
    assert wait_until(lambda: ("whisper-turbo-int8", "done") in phases, 10)
    assert ("whisper-turbo-int8", "download") in phases
    assert "whisper-turbo-int8" not in client.progress
    assert wait_until(lambda: client.model("whisper-turbo-int8")["installed"], 5)


def test_diagnostics_and_devices(client):
    report, devices = [], []
    client.get_diagnostics(report.append)
    client.list_audio_devices(devices.append)
    assert wait_until(lambda: report and devices, 5)
    assert {"version", "gnome_shell", "extension", "portals", "tools", "checks"} <= set(report[0])
    assert all({"id", "ok", "level", "message"} <= set(c) for c in report[0]["checks"])
    assert devices[0][0]["is_default"] is True


def test_service_going_away_and_coming_back(service, client):
    errors = []
    client.connect("error", lambda _c, message: errors.append(message))
    service.stop_daemon()
    assert wait_until(lambda: not client.connected, 5)
    client.toggle()
    assert wait_until(lambda: errors, 5)
    assert "not running" in errors[0]
    service.start_daemon()
    assert wait_until(lambda: client.connected and client.models_loaded, 10)


def test_client_without_service(service):
    service.stop_daemon()
    client = DaemonClient(service.connection())
    client.connect_to_service()
    assert wait_until(lambda: client.ready, 5)
    assert not client.connected
