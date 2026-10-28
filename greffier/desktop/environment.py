"""Cheap probes of the desktop environment.

Synchronous helpers here only look at environment variables and the file
system (microseconds). Anything that needs D-Bus is asynchronous and reports
through a callback invoked on the GLib main thread.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gi.repository import Gio, GLib

from greffier.const import EXTENSION_UUID

log = logging.getLogger(__name__)

SHELL_BUS_NAME = "org.gnome.Shell"
SHELL_OBJECT_PATH = "/org/gnome/Shell"
SHELL_EXTENSIONS_IFACE = "org.gnome.Shell.Extensions"
SCREENSAVER_BUS_NAME = "org.gnome.ScreenSaver"
SCREENSAVER_PATH = "/org/gnome/ScreenSaver"

DBUS_TIMEOUT_MS = 2000

# GNOME Shell ExtensionState (js/misc/extensionUtils.js); 1 is ENABLED in
# GNOME 45 and was renamed ACTIVE in 46 with the same value.
EXTENSION_STATE_ACTIVE = 1


def session_type() -> str:
    """``wayland``, ``x11``, ``tty`` or ``unknown`` (from the environment)."""
    value = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if value in ("wayland", "x11", "tty"):
        return value
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return value or "unknown"


def is_wayland() -> bool:
    """True when running in a Wayland session (wl-copy needs WAYLAND_DISPLAY)."""
    return bool(os.environ.get("WAYLAND_DISPLAY")) or session_type() == "wayland"


def is_gnome() -> bool:
    """True when XDG_CURRENT_DESKTOP mentions GNOME."""
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "")
    return "gnome" in desktops.lower().split(":")


def have_tool(name: str) -> bool:
    """True if an executable called ``name`` is on ``PATH``."""
    return find_tool(name) is not None


def find_tool(name: str) -> str | None:
    """Absolute path of executable ``name`` on ``PATH``, or ``None``."""
    return shutil.which(name)


def _runtime_dir() -> Path:
    value = os.environ.get("XDG_RUNTIME_DIR")
    return Path(value) if value else Path(f"/run/user/{os.getuid()}")


def ydotoold_socket_candidates() -> list[Path]:
    """Where ydotool looks for the daemon socket, by priority.

    ``YDOTOOL_SOCKET`` wins; ydotool ≥ 1.0 defaults to
    ``$XDG_RUNTIME_DIR/.ydotool_socket``; the Fedora/Arch system service uses
    ``/tmp/.ydotool_socket``.
    """
    out: list[Path] = []
    env = os.environ.get("YDOTOOL_SOCKET")
    if env:
        out.append(Path(env))
    out.append(_runtime_dir() / ".ydotool_socket")
    out.append(Path("/tmp/.ydotool_socket"))
    return out


def ydotoold_socket() -> Path | None:
    """Path of a usable ydotoold socket, or ``None``.

    The socket must exist, be writable and belong to root or to us: anyone
    can create ``/tmp/.ydotool_socket``, and a fake daemon there would
    receive every key we send.
    """
    for path in ydotoold_socket_candidates():
        try:
            st = path.stat()
        except OSError:
            continue
        if not stat.S_ISSOCK(st.st_mode):
            continue
        if st.st_uid not in (0, os.getuid()):
            log.debug("ignoring ydotoold socket %s owned by uid %d", path, st.st_uid)
            continue
        if os.access(path, os.R_OK | os.W_OK):
            return path
        log.debug("ydotoold socket %s exists but is not writable", path)
    return None


def extension_dirs() -> list[Path]:
    """Directories where GNOME Shell looks for extensions (user first)."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    dirs = [Path(data_home)]
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs += [Path(d) for d in data_dirs.split(":") if d]
    return [d / "gnome-shell" / "extensions" for d in dirs]


def extension_installed(uuid: str = EXTENSION_UUID) -> bool:
    """True if the extension's ``metadata.json`` exists in a known location."""
    return any((d / uuid / "metadata.json").is_file() for d in extension_dirs())


def extension_enabled_in_settings(uuid: str = EXTENSION_UUID) -> bool | None:
    """Whether ``org.gnome.shell enabled-extensions`` lists ``uuid``.

    ``None`` when the gnome-shell schema is not installed (not GNOME).
    """
    source = Gio.SettingsSchemaSource.get_default()
    schema = source.lookup("org.gnome.shell", True) if source else None
    if schema is None:
        return None
    settings = Gio.Settings.new_full(schema, None, None)
    if schema.has_key("disable-user-extensions") and settings.get_boolean(
        "disable-user-extensions"
    ):
        return False
    enabled = uuid in settings.get_strv("enabled-extensions")
    if schema.has_key("disabled-extensions") and uuid in settings.get_strv(
        "disabled-extensions"
    ):
        return False
    return enabled


def _get_property(
    bus: Gio.DBusConnection,
    name: str,
    path: str,
    iface: str,
    prop: str,
    callback: Callable[[object | None], None],
    *,
    auto_start: bool = False,
) -> None:
    """Async ``org.freedesktop.DBus.Properties.Get``; ``callback(value|None)``."""

    def done(conn: Gio.DBusConnection, res: Gio.AsyncResult) -> None:
        try:
            reply = conn.call_finish(res)
        except GLib.Error as exc:
            log.debug("cannot read %s.%s on %s: %s", iface, prop, name, exc.message)
            callback(None)
            return
        callback(reply.unpack()[0])

    flags = Gio.DBusCallFlags.NONE if auto_start else Gio.DBusCallFlags.NO_AUTO_START
    bus.call(
        name,
        path,
        "org.freedesktop.DBus.Properties",
        "Get",
        GLib.Variant("(ss)", (iface, prop)),
        GLib.VariantType("(v)"),
        flags,
        DBUS_TIMEOUT_MS,
        None,
        done,
    )


def gnome_shell_version(bus: Gio.DBusConnection, callback: Callable[[str], None]) -> None:
    """Report GNOME Shell's ``ShellVersion`` (e.g. ``"48.2"``), ``""`` if unknown."""

    def got(value: object | None) -> None:
        callback(value if isinstance(value, str) else "")

    _get_property(bus, SHELL_BUS_NAME, SHELL_OBJECT_PATH, "org.gnome.Shell", "ShellVersion", got)


def shell_major(version: str) -> int:
    """Major number of a Shell version string (``"48.2"`` → 48, ``""`` → 0)."""
    head = version.split(".", 1)[0]
    return int(head) if head.isdigit() else 0


def extension_info(
    bus: Gio.DBusConnection,
    callback: Callable[[dict[str, object] | None], None],
    uuid: str = EXTENSION_UUID,
) -> None:
    """``org.gnome.Shell.Extensions.GetExtensionInfo(uuid)`` as a dict.

    ``callback(None)`` if GNOME Shell is not reachable; ``{}`` if the
    extension is unknown to it.
    """

    def done(conn: Gio.DBusConnection, res: Gio.AsyncResult) -> None:
        try:
            reply = conn.call_finish(res)
        except GLib.Error as exc:
            log.debug("GetExtensionInfo failed: %s", exc.message)
            callback(None)
            return
        callback(dict(reply.unpack()[0]))

    bus.call(
        SHELL_BUS_NAME,
        SHELL_OBJECT_PATH,
        SHELL_EXTENSIONS_IFACE,
        "GetExtensionInfo",
        GLib.Variant("(s)", (uuid,)),
        GLib.VariantType("(a{sv})"),
        Gio.DBusCallFlags.NO_AUTO_START,
        DBUS_TIMEOUT_MS,
        None,
        done,
    )


def screen_locked(bus: Gio.DBusConnection, callback: Callable[[bool], None]) -> None:
    """``org.gnome.ScreenSaver.GetActive``; ``False`` when it cannot be asked."""

    def done(conn: Gio.DBusConnection, res: Gio.AsyncResult) -> None:
        try:
            reply = conn.call_finish(res)
        except GLib.Error:
            callback(False)
            return
        callback(bool(reply.unpack()[0]))

    bus.call(
        SCREENSAVER_BUS_NAME,
        SCREENSAVER_PATH,
        "org.gnome.ScreenSaver",
        "GetActive",
        None,
        GLib.VariantType("(b)"),
        Gio.DBusCallFlags.NO_AUTO_START,
        500,
        None,
        done,
    )


# --------------------------------------------------------------------------- #
# Asynchronous child processes
# --------------------------------------------------------------------------- #


@dataclass
class CommandResult:
    """Outcome of :func:`run_command`."""

    ok: bool  # spawned, exited normally with status 0
    status: int | None = None  # exit status (None: not spawned, killed or timed out)
    stdout: bytes = b""
    stderr: bytes = b""
    error: str = ""  # spawn failure or timeout, for logs

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", "replace").strip()


def run_command(
    argv: list[str],
    callback: Callable[[CommandResult], None],
    *,
    stdin: bytes | None = None,
    capture_stdout: bool = False,
    capture_stderr: bool = False,
    timeout_ms: int = 5000,
    env: dict[str, str] | None = None,
) -> Gio.Subprocess | None:
    """Run ``argv`` with ``Gio.Subprocess`` without blocking the main loop.

    Streams that are not captured go to ``/dev/null``: tools that fork into
    the background (``wl-copy``) would otherwise keep our pipes open forever.
    ``callback`` is invoked exactly once, on the main thread; after
    ``timeout_ms`` the child is killed and the result has ``ok=False``.
    """
    flags = Gio.SubprocessFlags.NONE
    if stdin is not None:
        flags |= Gio.SubprocessFlags.STDIN_PIPE
    sp = Gio.SubprocessFlags
    flags |= sp.STDOUT_PIPE if capture_stdout else sp.STDOUT_SILENCE
    flags |= sp.STDERR_PIPE if capture_stderr else sp.STDERR_SILENCE

    launcher = Gio.SubprocessLauncher.new(flags)
    for key, value in (env or {}).items():
        launcher.setenv(key, value, True)
    try:
        proc = launcher.spawnv(argv)
    except GLib.Error as exc:
        log.debug("cannot run %s: %s", argv[0], exc.message)
        result = CommandResult(False, error=exc.message)
        GLib.idle_add(lambda: (callback(result), GLib.SOURCE_REMOVE)[1])
        return None

    cancellable = Gio.Cancellable()
    state = {"timer": 0, "timed_out": False}

    def on_timeout() -> bool:
        state["timer"] = 0
        state["timed_out"] = True
        log.warning("%s did not finish within %d ms; killing it", argv[0], timeout_ms)
        cancellable.cancel()
        proc.force_exit()
        return GLib.SOURCE_REMOVE

    if timeout_ms > 0:
        state["timer"] = GLib.timeout_add(timeout_ms, on_timeout)

    def done(p: Gio.Subprocess, res: Gio.AsyncResult) -> None:
        if state["timer"]:
            GLib.source_remove(state["timer"])
            state["timer"] = 0
        out = err = b""
        try:
            _ok, out_b, err_b = p.communicate_finish(res)
            out = out_b.get_data() if out_b is not None else b""
            err = err_b.get_data() if err_b is not None else b""
        except GLib.Error as exc:
            msg = "timeout" if state["timed_out"] else exc.message
            callback(CommandResult(False, error=msg))
            return
        if p.get_if_exited():
            status = p.get_exit_status()
            callback(CommandResult(status == 0, status, out or b"", err or b""))
        else:
            callback(CommandResult(False, None, out or b"", err or b"", "killed by a signal"))

    stdin_buf = GLib.Bytes.new(stdin) if stdin is not None else None
    proc.communicate_async(stdin_buf, cancellable, done)
    return proc


def current_xkb_layout() -> str:
    """Active GNOME input source as an xkb id (``"fr+oss"``), ``""`` if unknown.

    GNOME Shell keeps the most recently used source first in
    ``org.gnome.desktop.input-sources mru-sources``. IBus sources (input
    methods) are reported as ``""`` since their keymap is usually Latin.
    """
    source = Gio.SettingsSchemaSource.get_default()
    schema = source.lookup("org.gnome.desktop.input-sources", True) if source else None
    if schema is None:
        return ""
    settings = Gio.Settings.new_full(schema, None, None)
    for key in ("mru-sources", "sources"):
        if not schema.has_key(key):
            continue
        items = settings.get_value(key).unpack()
        if items:
            kind, ident = items[0]
            return ident if kind == "xkb" else ""
    return ""
