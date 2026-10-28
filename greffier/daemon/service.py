"""D-Bus export of the controller (interface io.github.gds_42.Greffier.Daemon1)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib

from greffier import paths
from greffier.const import APP_ID, DBUS_IFACE, DBUS_PATH, VERSION
from greffier.daemon.controller import Controller, DaemonError
from greffier.i18n import _

log = logging.getLogger(__name__)

ERROR_PREFIX = f"{APP_ID}.Error."


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DaemonError("InvalidArgument", _("Invalid JSON: {error}").format(error=exc)) from exc


class DBusService:
    """Owns the object registration; translates calls into controller methods."""

    def __init__(self, connection: Gio.DBusConnection) -> None:
        self.connection = connection
        self.controller: Controller | None = None
        self.on_quit: Callable[[], None] = lambda: None
        xml = paths.package_data("daemon-interface.xml").read_text(encoding="utf-8")
        node = Gio.DBusNodeInfo.new_for_xml(xml)
        self._iface = node.lookup_interface(DBUS_IFACE)
        self._registration = connection.register_object(
            DBUS_PATH, self._iface, self._on_method_call, self._on_get_property, None
        )
        self._methods: dict[str, Callable[[tuple], Any]] = {
            "Toggle": lambda a: self._ctl.toggle(),
            "Start": lambda a: self._ctl.start_recording(),
            "Stop": lambda a: self._ctl.stop(),
            "Cancel": lambda a: self._ctl.cancel(),
            "GetStatus": lambda a: _json(self._ctl.status()),
            "GetConfig": lambda a: _json(self._ctl.get_config()),
            "SetConfig": lambda a: _json(self._ctl.set_config(self._patch(a[0]))),
            "ListModels": lambda a: _json(self._ctl.list_models()),
            "InstallModel": lambda a: self._ctl.install_model(a[0]),
            "CancelInstall": lambda a: self._ctl.cancel_install(a[0]),
            "RemoveModel": lambda a: self._ctl.remove_model(a[0]),
            "CheckUpdates": lambda a: self._ctl.check_updates(),
            "GetHistory": lambda a: _json(self._ctl.history_items()),
            "ClearHistory": lambda a: self._ctl.clear_history(),
            "ListAudioDevices": lambda a: _json(self._ctl.list_audio_devices()),
            "GetDiagnostics": lambda a: _json(self._ctl.diagnostics()),
            "Quit": lambda a: GLib.idle_add(self._quit),
        }

    @property
    def _ctl(self) -> Controller:
        if self.controller is None:
            raise DaemonError("Failed", _("The service is still starting."))
        return self.controller

    @staticmethod
    def _patch(text: str) -> dict[str, Any]:
        patch = _parse_json(text)
        if not isinstance(patch, dict):
            raise DaemonError("InvalidArgument", _("The settings patch must be a JSON object."))
        return patch

    def _quit(self) -> bool:
        self.on_quit()
        return GLib.SOURCE_REMOVE

    def unregister(self) -> None:
        if self._registration:
            self.connection.unregister_object(self._registration)
            self._registration = 0

    # -- incoming ------------------------------------------------------------- #

    def _on_method_call(self, connection, sender, object_path, interface_name, method_name,
                        parameters, invocation) -> None:
        handler = self._methods.get(method_name)
        if handler is None:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method_name)
            return
        log.debug("%s from %s", method_name, sender)
        try:
            result = handler(parameters.unpack())
            out_args = self._iface.lookup_method(method_name).out_args
            if out_args:
                signature = "(" + "".join(arg.signature for arg in out_args) + ")"
                reply = GLib.Variant(signature, (result,))
            else:
                reply = None
        except DaemonError as exc:
            invocation.return_dbus_error(ERROR_PREFIX + exc.code, str(exc))
            return
        except Exception as exc:  # including a reply that cannot be encoded
            log.exception("%s failed", method_name)
            invocation.return_dbus_error(ERROR_PREFIX + "Failed", str(exc))
            return
        invocation.return_value(reply)

    def _on_get_property(self, connection, sender, object_path, interface_name, name):
        ctl = self.controller
        if name == "State":
            return GLib.Variant("s", str(ctl.state) if ctl else "loading")
        if name == "ActiveModel":
            return GLib.Variant("s", ctl._active_property if ctl else "")
        if name == "Version":
            return GLib.Variant("s", VERSION)
        return None

    # -- outgoing ------------------------------------------------------------- #

    def emit(self, name: str, signature: str, args: tuple) -> None:
        try:
            if name == "PropertiesChanged":
                self.connection.emit_signal(None, DBUS_PATH, "org.freedesktop.DBus.Properties",
                                            name, GLib.Variant(signature, args))
            else:
                self.connection.emit_signal(None, DBUS_PATH, DBUS_IFACE, name,
                                            GLib.Variant(signature, args))
        except GLib.Error as exc:
            log.warning("cannot emit %s: %s", name, exc.message)
