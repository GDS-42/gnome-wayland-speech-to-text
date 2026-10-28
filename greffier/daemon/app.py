"""greffier-daemon entry point: a Gio.Application hosting the controller."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from typing import Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from greffier.const import APP_ID, APP_NAME, DAEMON_ID, VERSION
from greffier.daemon.controller import Controller, Deps
from greffier.daemon.service import DBusService
from greffier.i18n import _

log = logging.getLogger("greffier.daemon")


def build_deps(bus: Gio.DBusConnection) -> Deps:
    """Wire the real components (imported lazily so `--help` stays instant)."""
    from greffier import config as cfgmod
    from greffier import paths
    from greffier.audio.capture import Recorder, list_devices
    from greffier.audio.sounds import SoundPlayer
    from greffier.desktop import autostart, environment, portal
    from greffier.desktop.hotkeys import HotkeyManager
    from greffier.desktop.injection import Injector
    from greffier.models.catalog import Catalog
    from greffier.models.manager import InstallCancelled, ModelManager
    from greffier.stt.engine import load_engine
    from greffier.stt.transcriber import Transcriber
    from greffier.stt.vad import VoiceActivity
    from greffier.text.postprocess import postprocess

    # Must precede every other portal call made on this connection.
    portal.register_host_app(bus, DAEMON_ID)

    cfg = cfgmod.load()
    catalog = Catalog.load(paths.catalog_cache_file())
    models = ModelManager(catalog, paths.models_dir(), paths.downloads_dir())

    def environment_report() -> dict[str, Any]:
        # The GNOME Shell version comes from the injector's cached async probes.
        return {"session_type": environment.session_type()}

    return Deps(
        models=models,
        recorder_factory=Recorder,
        list_devices=list_devices,
        engine_loader=load_engine,
        transcriber_factory=Transcriber,
        vad_factory=lambda path: VoiceActivity(path),
        postprocess=postprocess,
        injector=Injector(bus, cfg.injection),
        hotkeys_factory=lambda pressed, released, command: HotkeyManager(
            bus, pressed, released, command),
        sounds=SoundPlayer(),
        fetch_catalog=lambda url, cache: Catalog.fetch_remote(url, cache),
        set_autostart=autostart.set_enabled,
        environment=environment_report,
        install_cancelled=InstallCancelled,
    )


class DaemonApplication(Gio.Application):
    def __init__(self, replace: bool = False) -> None:
        flags = Gio.ApplicationFlags.ALLOW_REPLACEMENT
        if replace:
            flags |= Gio.ApplicationFlags.REPLACE
        super().__init__(application_id=DAEMON_ID, flags=flags)
        self.service: DBusService | None = None
        self.controller: Controller | None = None

    # D-Bus registration happens before startup; the object is exported now
    # and calls are only dispatched once the main loop runs (after startup).
    def do_dbus_register(self, connection: Gio.DBusConnection, object_path: str) -> bool:
        Gio.Application.do_dbus_register(self, connection, object_path)
        self.service = DBusService(connection)
        self.service.on_quit = self.quit
        return True

    def do_dbus_unregister(self, connection: Gio.DBusConnection, object_path: str) -> None:
        if self.service is not None:
            self.service.unregister()
        Gio.Application.do_dbus_unregister(self, connection, object_path)

    def do_startup(self) -> None:
        Gio.Application.do_startup(self)
        self.hold()  # a service: stay alive without windows
        # Not Gst.init(): ensure_gst() first pins the C++ unwinder so that an
        # exception thrown by sherpa-onnx cannot abort the whole process when
        # GStreamer has pulled libunwind into the global symbol scope.
        from greffier.audio.capture import ensure_gst

        ensure_gst()
        open_gui = Gio.SimpleAction.new("open-gui", None)
        open_gui.connect("activate", lambda *_a: launch_gui())
        self.add_action(open_gui)

        bus = self.get_dbus_connection()
        if self.service is None or bus is None:
            log.critical("not connected to the session bus")
            self.release()
            return
        self.controller = Controller(build_deps(bus), self.service.emit, self.send_message)
        self.service.controller = self.controller
        self.controller.start()
        log.info("%s %s ready", APP_NAME, VERSION)

    def do_activate(self) -> None:
        pass  # launched again (e.g. from the autostart file): nothing to show

    def do_shutdown(self) -> None:
        if self.controller is not None:
            self.controller.shutdown()
        Gio.Application.do_shutdown(self)

    def do_name_lost(self) -> bool:
        log.info("replaced by another instance")
        self.quit()
        return True

    def send_message(self, ident: str, title: str, body: str, *, open_gui: bool = True) -> None:
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_icon(Gio.ThemedIcon.new(APP_ID))
        if open_gui:
            notification.set_default_action("app.open-gui")
        self.send_notification(ident, notification)


def launch_gui() -> None:
    try:
        from gi.repository import Gio as _Gio

        info = _Gio.DesktopAppInfo.new(f"{APP_ID}.desktop")
        if info is not None and info.launch([], None):
            return
    except (TypeError, GLib.Error):
        pass
    try:
        Gio.Subprocess.new(["greffier"], Gio.SubprocessFlags.NONE)
    except GLib.Error as exc:
        log.warning("cannot open the window: %s", exc.message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="greffier-daemon",
        description=_("Greffier background service (voice dictation)."),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help=_("debug logging"))
    parser.add_argument("--replace", action="store_true",
                        help=_("replace a running instance"))
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    app = DaemonApplication(replace=args.replace)
    for signum in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, lambda: app.quit() or False)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
