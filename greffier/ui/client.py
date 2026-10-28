"""D-Bus client of the Greffier background service.

``DaemonClient`` wraps a ``Gio.DBusProxy`` for ``io.github.greffier.Greffier.Daemon1``
and turns the JSON payloads of the contract (greffier/data/daemon-interface.xml)
into Python objects and GObject signals the views can bind to. Every call is
asynchronous; failures are reported to the caller's ``on_error`` or, by
default, through the ``error`` signal (the window shows them as toasts).

The proxy never auto-starts the service: the window offers a "Start" button
instead (``start_service()``: D-Bus activation, then ``systemctl --user``).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Callable
from typing import Any

from gi.repository import Gio, GLib, GObject

from greffier import paths
from greffier.const import DBUS_IFACE, DBUS_NAME, DBUS_PATH, SYSTEMD_UNIT, State
from greffier.i18n import _

log = logging.getLogger(__name__)

ERROR_PREFIX = "io.github.greffier.Greffier.Error."
_GDBUS_PREFIX = re.compile(r"^GDBus\.Error:[\w.]+:\s*")
_NOT_RUNNING = (
    "org.freedesktop.DBus.Error.ServiceUnknown",
    "org.freedesktop.DBus.Error.NameHasNoOwner",
    "org.freedesktop.DBus.Error.Disconnected",
)

OnDone = Callable[[Any], None]
OnError = Callable[["CallError"], None]

TERMINAL_PHASES = ("done", "error", "cancelled")


class CallError(Exception):
    """A failed D-Bus call: ``code`` is NoModel, Busy, InvalidArgument,
    NotFound, Failed, NotRunning, Timeout or Protocol; ``message`` is user-facing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def error_from_glib(exc: GLib.Error) -> CallError:
    remote = Gio.DBusError.get_remote_error(exc) or ""
    message = _GDBUS_PREFIX.sub("", exc.message or "").strip()
    if remote.startswith(ERROR_PREFIX):
        code = remote[len(ERROR_PREFIX):]
        fallback = {
            "NoModel": _("No speech recognition model is installed."),
            "Busy": _("Greffier is busy. Try again in a moment."),
            "InvalidArgument": _("The setting could not be changed."),
            "NotFound": _("This item no longer exists."),
        }.get(code, _("Something went wrong."))
        return CallError(code, message or fallback)
    if remote in _NOT_RUNNING or exc.matches(Gio.dbus_error_quark(),
                                             Gio.DBusError.SERVICE_UNKNOWN):
        return CallError("NotRunning", _("The Greffier service is not running."))
    if remote == "org.freedesktop.DBus.Error.NoReply" or exc.matches(
            Gio.io_error_quark(), Gio.IOErrorEnum.TIMED_OUT):
        return CallError("Timeout", _("The Greffier service is not responding."))
    return CallError("Failed", message or _("Something went wrong."))


def _decode(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise CallError("Protocol", _("The service sent an invalid reply.")) from exc


def interface_info() -> Gio.DBusInterfaceInfo:
    xml = paths.package_data("daemon-interface.xml").read_text(encoding="utf-8")
    return Gio.DBusNodeInfo.new_for_xml(xml).lookup_interface(DBUS_IFACE)


class DaemonClient(GObject.Object):
    """Asynchronous, signal-based view of the background service."""

    __gtype_name__ = "GreffierDaemonClient"
    __gsignals__ = {
        # (state, detail) — also reflected by the "state"/"detail" properties
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # microphone level in [0, 1] while recording
        "level": (GObject.SignalFlags.RUN_FIRST, None, (float,)),
        # (text, meta dict)
        "transcribed": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        # (model id, phase, done, total, message)
        "model-progress": (GObject.SignalFlags.RUN_FIRST, None,
                           (str, str, GObject.TYPE_UINT64, GObject.TYPE_UINT64, str)),
        # the model list (self.models) was refreshed
        "models-updated": (GObject.SignalFlags.RUN_FIRST, None, ()),
        # full configuration dict (self.config)
        "config-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # GetStatus dict (self.status)
        "status-changed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        # (level, message) — Notice signal of the service
        "notice": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # user-facing message of a failed call without its own error handler
        "error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    #: True once the proxy exists and the service presence is known.
    ready = GObject.Property(type=bool, default=False)
    #: True while the service runs and the initial state has been fetched.
    connected = GObject.Property(type=bool, default=False)
    #: True while start_service() is in progress.
    starting = GObject.Property(type=bool, default=False)
    #: True once ListModels answered since the service appeared.
    models_loaded = GObject.Property(type=bool, default=False)
    state = GObject.Property(type=str, default="")
    detail = GObject.Property(type=str, default="")
    active_model = GObject.Property(type=str, default="")
    version = GObject.Property(type=str, default="")

    def __init__(self, connection: Gio.DBusConnection | None = None) -> None:
        super().__init__()
        self._connection = connection
        self._proxy: Gio.DBusProxy | None = None
        self._sync_serial = 0
        self._status_pending = False
        self._models_serial = 0
        self.config: dict[str, Any] | None = None
        self.status: dict[str, Any] | None = None
        self.models: list[dict[str, Any]] = []
        #: model id -> (phase, done, total) for installs in progress
        self.progress: dict[str, tuple[str, int, int]] = {}

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    def connect_to_service(self) -> None:
        """Create the proxy asynchronously (call once, after construction)."""
        flags = Gio.DBusProxyFlags.DO_NOT_AUTO_START
        info = interface_info()
        if self._connection is not None:
            Gio.DBusProxy.new(self._connection, flags, info, DBUS_NAME, DBUS_PATH, DBUS_IFACE,
                              None, self._on_proxy_ready)
        else:
            Gio.DBusProxy.new_for_bus(Gio.BusType.SESSION, flags, info, DBUS_NAME, DBUS_PATH,
                                      DBUS_IFACE, None, self._on_proxy_ready)

    def _on_proxy_ready(self, _source, result) -> None:
        try:
            self._proxy = Gio.DBusProxy.new_finish(result)
        except GLib.Error as exc:
            log.error("cannot reach the session bus: %s", exc.message)
            self.ready = True
            return
        self._proxy.connect("g-signal", self._on_signal)
        self._proxy.connect("g-properties-changed", self._on_properties_changed)
        self._proxy.connect("notify::g-name-owner", self._on_owner_changed)
        self._on_owner_changed()

    @property
    def running(self) -> bool:
        return self._proxy is not None and self._proxy.get_name_owner() is not None

    def _on_owner_changed(self, *_args) -> None:
        self._sync_serial += 1
        if self.running:
            self._read_properties()
            self._sync(self._sync_serial)
        else:
            self.status = None
            self.progress.clear()
            if self.models_loaded:
                self.models_loaded = False
            if self.connected:
                self.connected = False
            self.state = ""
            self.detail = ""
            if not self.ready:
                self.ready = True

    def _sync(self, serial: int) -> None:
        """Fetch status, configuration and models, then flag as connected."""
        pending = {"status", "config"}

        def step(name: str) -> None:
            pending.discard(name)
            if not pending and serial == self._sync_serial:
                self.connected = True
                if not self.ready:
                    self.ready = True
                self.refresh_models()

        def failed(err: CallError) -> None:
            log.warning("initial synchronisation failed: %s", err.message)
            if serial == self._sync_serial and not self.ready:
                self.ready = True

        def got_status(status: dict) -> None:
            self._set_status(status)
            step("status")

        def got_config(config: dict) -> None:
            self._set_config(config)
            step("config")

        self.call_json("GetStatus", on_done=got_status, on_error=failed)
        self.call_json("GetConfig", on_done=got_config, on_error=failed)

    def _read_properties(self) -> None:
        for name, prop in (("State", "state"), ("ActiveModel", "active_model"),
                           ("Version", "version")):
            value = self._proxy.get_cached_property(name) if self._proxy else None
            if value is not None and self.get_property(prop.replace("_", "-")) != value.unpack():
                self.set_property(prop.replace("_", "-"), value.unpack())

    # ------------------------------------------------------------------ #
    # Starting the service
    # ------------------------------------------------------------------ #

    def start_service(self, on_done: Callable[[bool, str], None] | None = None) -> None:
        """D-Bus activation, then ``systemctl --user start``, then a direct spawn."""
        if self.starting:
            return
        self.starting = True

        def finish(ok: bool, message: str = "") -> None:
            self.starting = False
            if not ok:
                log.warning("cannot start the service: %s", message)
            if on_done is not None:
                on_done(ok, message)

        def on_systemctl(proc: Gio.Subprocess, result) -> None:
            try:
                proc.wait_check_finish(result)
            except GLib.Error as exc:
                if not self._spawn_daemon():
                    finish(False, exc.message)
                    return
            # The name appears asynchronously; give it a moment before reporting.
            GLib.timeout_add(1500, lambda: finish(self.running) or GLib.SOURCE_REMOVE)

        def try_systemctl(reason: str) -> None:
            log.info("D-Bus activation failed (%s), trying systemctl", reason)
            try:
                proc = Gio.Subprocess.new(
                    ["systemctl", "--user", "start", SYSTEMD_UNIT],
                    Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE)
            except GLib.Error as exc:
                if not self._spawn_daemon():
                    finish(False, exc.message)
                else:
                    GLib.timeout_add(1500, lambda: finish(self.running) or GLib.SOURCE_REMOVE)
                return
            proc.wait_check_async(None, on_systemctl)

        def on_activation(bus: Gio.DBusConnection, result) -> None:
            try:
                bus.call_finish(result)
            except GLib.Error as exc:
                try_systemctl(exc.message)
                return
            finish(True)

        bus = self._proxy.get_connection() if self._proxy else self._connection
        if bus is None:
            finish(False, _("The session bus is not available."))
            return
        bus.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                 "StartServiceByName", GLib.Variant("(su)", (DBUS_NAME, 0)),
                 GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 60_000, None, on_activation)

    @staticmethod
    def _spawn_daemon() -> bool:
        exe = shutil.which("greffier-daemon")
        if not exe:
            return False
        try:
            Gio.Subprocess.new([exe], Gio.SubprocessFlags.NONE)
        except GLib.Error:
            return False
        return True

    # ------------------------------------------------------------------ #
    # Calls
    # ------------------------------------------------------------------ #

    def call(self, method: str, args: tuple = (), signature: str = "",
             on_done: OnDone | None = None, on_error: OnError | None = None,
             timeout_ms: int = 25_000) -> None:
        """Call ``method``; ``on_done`` receives the unpacked first out value (or None)."""

        def fail(err: CallError) -> None:
            if on_error is not None:
                on_error(err)
            else:
                self.emit("error", err.message)

        if self._proxy is None or not self.running:
            GLib.idle_add(lambda: fail(CallError(
                "NotRunning", _("The Greffier service is not running."))) or GLib.SOURCE_REMOVE)
            return
        params = GLib.Variant(f"({signature})", args) if signature else None

        def done(proxy: Gio.DBusProxy, result) -> None:
            try:
                reply = proxy.call_finish(result)
            except GLib.Error as exc:
                err = error_from_glib(exc)
                log.debug("%s failed: %s (%s)", method, err.message, err.code)
                fail(err)
                return
            if on_done is not None:
                values = reply.unpack() if reply is not None else ()
                on_done(values[0] if values else None)

        self._proxy.call(method, params, Gio.DBusCallFlags.NONE, timeout_ms, None, done)

    def call_json(self, method: str, args: tuple = (), signature: str = "",
                  on_done: OnDone | None = None, on_error: OnError | None = None) -> None:
        """Like call() for methods returning a JSON string, decoded for ``on_done``."""

        def decoded(text: str) -> None:
            try:
                value = _decode(text)
            except CallError as err:
                if on_error is not None:
                    on_error(err)
                else:
                    self.emit("error", err.message)
                return
            if on_done is not None:
                on_done(value)

        self.call(method, args, signature, decoded, on_error)

    # -- dictation -------------------------------------------------------- #

    def toggle(self) -> None:
        self.call("Toggle")

    def start(self) -> None:
        self.call("Start")

    def stop(self) -> None:
        self.call("Stop")

    def cancel(self) -> None:
        self.call("Cancel")

    # -- configuration ------------------------------------------------------ #

    def set_config(self, patch: dict[str, Any], on_done: OnDone | None = None,
                   on_error: OnError | None = None) -> None:
        """Apply a minimal nested patch; the cache is updated from the reply."""

        def done(config: dict) -> None:
            self._set_config(config)
            if on_done is not None:
                on_done(config)

        self.call_json("SetConfig", (json.dumps(patch, ensure_ascii=False),), "s", done,
                       on_error)

    def get(self, section: str, key: str, default: Any = None) -> Any:
        """Cached configuration value (``default`` before the first sync)."""
        if not self.config:
            return default
        return self.config.get(section, {}).get(key, default)

    def _set_config(self, config: Any) -> None:
        if not isinstance(config, dict):
            return
        changed = config != self.config
        self.config = config
        if changed:
            self.emit("config-changed", config)

    # -- status ------------------------------------------------------------ #

    def refresh_status(self) -> None:
        if self._status_pending:
            return
        self._status_pending = True

        def done(status: dict) -> None:
            self._status_pending = False
            self._set_status(status)

        def failed(err: CallError) -> None:
            self._status_pending = False
            log.debug("GetStatus failed: %s", err.message)

        self.call_json("GetStatus", on_done=done, on_error=failed)

    def _set_status(self, status: Any) -> None:
        if not isinstance(status, dict):
            return
        self.status = status
        state = status.get("state") or self.state
        if state != self.state:
            self.state = state
        self.emit("status-changed", status)

    @property
    def shortcut(self) -> str:
        """Accelerator of the dictation shortcut ("" when disabled)."""
        return self.get("shortcut", "accelerator", "") or ""

    # -- models ------------------------------------------------------------ #

    def refresh_models(self, on_done: OnDone | None = None) -> None:
        self._models_serial += 1
        serial = self._models_serial

        def done(models: list) -> None:
            if serial != self._models_serial or not isinstance(models, list):
                return
            self.models = [m for m in models if isinstance(m, dict)]
            for item in self.models:
                install = item.get("install")
                if isinstance(install, dict) and item.get("id") not in self.progress:
                    self.progress[item["id"]] = (install.get("phase", "download"),
                                                 int(install.get("done", 0)),
                                                 int(install.get("total", 0)))
            self.emit("models-updated")
            if not self.models_loaded:
                self.models_loaded = True
            if on_done is not None:
                on_done(self.models)

        self.call_json("ListModels", on_done=done,
                       on_error=lambda err: log.warning("ListModels: %s", err.message))

    def asr_models(self) -> list[dict[str, Any]]:
        return [m for m in self.models if m.get("kind", "asr") == "asr"]

    def model(self, model_id: str) -> dict[str, Any] | None:
        return next((m for m in self.models if m.get("id") == model_id), None)

    def installed_asr(self) -> list[dict[str, Any]]:
        return [m for m in self.asr_models() if m.get("installed")]

    def is_active(self, model: dict[str, Any]) -> bool:
        if "active" in model:
            return bool(model["active"])
        return bool(model.get("installed")) and model.get("id") == self.get("model", "active")

    def active_model_info(self) -> dict[str, Any] | None:
        return next((m for m in self.asr_models() if self.is_active(m)), None)

    def install_model(self, model_id: str, on_error: OnError | None = None) -> None:
        self.call("InstallModel", (model_id,), "s", on_error=on_error)

    def cancel_install(self, model_id: str) -> None:
        self.call("CancelInstall", (model_id,), "s")

    def remove_model(self, model_id: str, on_done: OnDone | None = None) -> None:
        self.call("RemoveModel", (model_id,), "s", on_done=on_done)

    def check_updates(self, on_error: OnError | None = None) -> None:
        self.call("CheckUpdates", on_error=on_error)

    # -- misc -------------------------------------------------------------- #

    def get_history(self, on_done: OnDone) -> None:
        self.call_json("GetHistory", on_done=on_done,
                       on_error=lambda err: log.warning("GetHistory: %s", err.message))

    def clear_history(self, on_done: OnDone | None = None) -> None:
        self.call("ClearHistory", on_done=on_done)

    def list_audio_devices(self, on_done: OnDone) -> None:
        self.call_json("ListAudioDevices", on_done=on_done,
                       on_error=lambda err: log.warning("ListAudioDevices: %s", err.message))

    def get_diagnostics(self, on_done: OnDone, on_error: OnError | None = None) -> None:
        self.call_json("GetDiagnostics", on_done=on_done, on_error=on_error)

    def inject_text(self, text: str) -> None:
        self.call("InjectText", (text,), "s")

    def quit_service(self, on_done: OnDone | None = None) -> None:
        self.call("Quit", on_done=on_done)

    # ------------------------------------------------------------------ #
    # Signals of the service
    # ------------------------------------------------------------------ #

    def _on_signal(self, _proxy, _sender: str, name: str, params: GLib.Variant) -> None:
        args = params.unpack()
        if name == "Level":
            self.emit("level", max(0.0, min(1.0, float(args[0]))))
        elif name == "StateChanged":
            state, detail = args
            if detail != self.detail:
                self.detail = detail
            if state != self.state:
                self.state = state
            self.emit("state-changed", state, detail)
            self.refresh_status()
        elif name == "Transcribed":
            text, meta = args
            try:
                meta = _decode(meta)
            except CallError:
                meta = {}
            self.emit("transcribed", text, meta if isinstance(meta, dict) else {})
        elif name == "ModelProgress":
            model_id, phase, done, total, message = args
            if phase in TERMINAL_PHASES:
                self.progress.pop(model_id, None)
            else:
                self.progress[model_id] = (phase, int(done), int(total))
            self.emit("model-progress", model_id, phase, done, total, message)
        elif name == "ModelsChanged":
            self.refresh_models()
            self.refresh_status()
        elif name == "ConfigChanged":
            try:
                self._set_config(_decode(args[0]))
            except CallError:
                return
            self.refresh_status()
        elif name == "Notice":
            level, message = args
            self.emit("notice", level, message)

    def _on_properties_changed(self, _proxy, changed: GLib.Variant, _invalidated) -> None:
        values = changed.unpack()
        if "State" in values and values["State"] != self.state:
            self.state = values["State"]
        if "ActiveModel" in values and values["ActiveModel"] != self.active_model:
            self.active_model = values["ActiveModel"]
        if "Version" in values and values["Version"] != self.version:
            self.version = values["Version"]


def state_is_busy(state: str) -> bool:
    return state in (State.LOADING, State.TRANSCRIBING)
