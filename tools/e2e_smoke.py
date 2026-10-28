#!/usr/bin/env python3
"""End-to-end smoke test of the real daemon, without a desktop.

What it exercises: D-Bus activation-free start of ``greffier-daemon`` on a
private session bus, model installation through the real downloader (served
from a local HTTP server instead of GitHub), model loading, a dictation where
the microphone is replaced by a WAV file played in real time, transcription
with the real engine, post-processing and the injection fallback chain (no
GNOME here, so the text ends up "copied" or reported as not inserted).

Usage::

    python tools/e2e_smoke.py --archives /tmp/claude-0/dl --wav path/to/fr.wav \
        [--model parakeet-tdt-0.6b-v3-int8] [--expect "votre pays"]

``--archives`` must contain the .tar.bz2 archives and ``silero_vad.onnx`` named
as in the catalog URLs.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from greffier.const import DBUS_IFACE, DBUS_NAME, DBUS_PATH  # noqa: E402


class RangeHandler(http.server.SimpleHTTPRequestHandler):
    """Static files with single-range support (enough for resumable downloads)."""

    def log_message(self, *args) -> None:
        pass

    def send_head(self):  # noqa: C901 - small and local
        rng = self.headers.get("Range")
        path = Path(self.translate_path(self.path))
        if not rng or not path.is_file():
            return super().send_head()
        size = path.stat().st_size
        start = int(rng.split("=")[1].split("-")[0])
        fh = path.open("rb")
        fh.seek(start)
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
        self.send_header("Content-Length", str(size - start))
        self.end_headers()
        return fh


def serve(directory: Path) -> int:
    handler = functools.partial(RangeHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def prepare_home(home: Path, port: int) -> None:
    catalog = json.loads((ROOT / "greffier/data/catalog.json").read_text())
    catalog["revision"] = "9999-01-01.1"
    for model in catalog["models"]:
        name = model["download"]["url"].rsplit("/", 1)[1]
        model["download"]["url"] = f"http://127.0.0.1:{port}/{name}"
    (home / "cache").mkdir(parents=True)
    (home / "cache/catalog.json").write_text(json.dumps(catalog))
    (home / "config").mkdir(parents=True)
    (home / "config/config.toml").write_text(
        "[model]\ncheck_updates = false\n[general]\nsounds = false\n"
    )


class Bus:
    def __init__(self, address: str) -> None:
        flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                 | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
        self.conn = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
        self.signals: list[tuple[str, tuple]] = []
        self.conn.signal_subscribe(DBUS_NAME, DBUS_IFACE, None, DBUS_PATH, None,
                                   Gio.DBusSignalFlags.NONE, self._on_signal)

    def _on_signal(self, _c, _s, _p, _i, name, params) -> None:
        args = params.unpack()
        self.signals.append((name, args))
        if name in ("StateChanged", "Transcribed", "Notice") or (
                name == "ModelProgress" and args[1] not in ("download", "extract", "verify")):
            print(f"  ← {name} {args}", flush=True)

    def call(self, method: str, signature: str | None = None, *args):
        params = GLib.Variant(signature, args) if signature else None
        reply = self.conn.call_sync(DBUS_NAME, DBUS_PATH, DBUS_IFACE, method, params, None,
                                    Gio.DBusCallFlags.NONE, 30000, None)
        values = reply.unpack()
        return values[0] if values else None

    def wait(self, predicate, timeout: float) -> bool:
        ctx = GLib.MainContext.default()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            while ctx.pending():
                ctx.iteration(False)
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def status(self) -> dict:
        return json.loads(self.call("GetStatus"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archives", type=Path, required=True)
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--model", default="parakeet-tdt-0.6b-v3-int8")
    parser.add_argument("--expect", default="")
    parser.add_argument("--keep", action="store_true", help="keep the temporary home")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="greffier-e2e-"))
    home = tmp / "home"
    port = serve(args.archives)
    prepare_home(home, port)
    test_bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
    test_bus.up()
    address = test_bus.get_bus_address()
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=address, GREFFIER_HOME=str(home),
               GREFFIER_LANG="en", XDG_SESSION_TYPE="wayland", XDG_CURRENT_DESKTOP="GNOME",
               GREFFIER_TEST_ALLOW_LOOPBACK="1",
               GREFFIER_AUDIO_SOURCE=(f"filesrc location={args.wav} ! decodebin ! "
                                      "audioconvert ! identity sync=true"))
    log = (tmp / "daemon.log").open("w")
    daemon = subprocess.Popen([sys.executable, "-m", "greffier.daemon.app", "--verbose"],
                              env=env, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    ok = False
    try:
        bus = Bus(address)
        print("• waiting for the daemon", flush=True)
        if not bus.wait(lambda: _owned(bus), 30):
            raise SystemExit("daemon did not start")
        print(f"  state: {bus.status()['state']}")
        print(f"• installing {args.model} from the local server", flush=True)
        started = time.monotonic()
        bus.call("InstallModel", "(s)", args.model)
        if not bus.wait(lambda: any(n == "ModelProgress" and a[0] == args.model
                                    and a[1] in ("done", "error") for n, a in bus.signals), 900):
            raise SystemExit("install timed out")
        print(f"  installed in {time.monotonic() - started:.1f} s")
        bus.wait(lambda: any(n == "ModelProgress" and a[0] == "silero-vad"
                             and a[1] in ("done", "error") for n, a in bus.signals), 60)
        if not bus.wait(lambda: bus.status()["model_loaded"], 120):
            raise SystemExit("model did not load")
        print("• dictating (the WAV file replaces the microphone)", flush=True)
        bus.call("Toggle")
        levels = len([1 for n, _ in bus.signals if n == "Level"])
        bus.wait(lambda: False, 7.0)  # let the file play in real time
        levels = len([1 for n, _ in bus.signals if n == "Level"]) - levels
        bus.call("Toggle")
        started = time.monotonic()
        if not bus.wait(lambda: any(n == "Transcribed" for n, _ in bus.signals), 120):
            raise SystemExit("no transcription")
        text, meta = next(a for n, a in bus.signals if n == "Transcribed")
        print(f"\n  TEXT: {text!r}\n  META: {meta}\n  levels received: {levels}"
              f"\n  transcribed {time.monotonic() - started:.2f} s after stop")
        history = json.loads(bus.call("GetHistory"))
        assert history and history[0]["text"] == text
        diag = json.loads(bus.call("GetDiagnostics"))
        for check in diag["checks"]:
            print(f"  [{check['level']:>7}] {check['message']}")
        ok = args.expect.lower() in text.lower()
        print("\nRESULT:", "PASS" if ok else f"FAIL (expected {args.expect!r})")
    finally:
        with contextlib.suppress(Exception):
            Bus(address).call("Quit")
        try:
            daemon.wait(10)
        except subprocess.TimeoutExpired:
            daemon.kill()
        log.close()
        print(f"daemon log: {tmp / 'daemon.log'} (exit {daemon.returncode})")
        test_bus.down()
    return 0 if ok else 1


def _owned(bus: Bus) -> bool:
    reply = bus.conn.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                               "org.freedesktop.DBus", "NameHasOwner",
                               GLib.Variant("(s)", (DBUS_NAME,)), None,
                               Gio.DBusCallFlags.NONE, 1000, None)
    return reply.unpack()[0]


if __name__ == "__main__":
    sys.exit(main())
