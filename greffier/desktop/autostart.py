"""Start the Greffier service with the session.

Preferred: the systemd user unit ``app-io.github.greffier.Greffier.Daemon.service``
(``systemctl --user enable``). When the unit is not installed (e.g. a pip
install without the data files) or cannot be enabled (no ``[Install]``
section), an XDG autostart entry ``~/.config/autostart/<DAEMON_ID>.desktop``
is used instead.

:func:`is_enabled`/:func:`set_enabled` are synchronous (the contract the GUI
and CLI use; ``systemctl`` answers in a few milliseconds). The daemon can use
:func:`set_enabled_async` to stay off its main loop.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from greffier.const import APP_ID, APP_NAME, DAEMON_ID, DBUS_NAME, DBUS_PATH, SYSTEMD_UNIT
from greffier.desktop.environment import CommandResult, run_command
from greffier.i18n import _

log = logging.getLogger(__name__)

SYSTEMCTL_TIMEOUT_S = 10
_ENABLED_STATES = {"enabled", "enabled-runtime", "linked", "linked-runtime", "alias"}


class AutostartError(OSError):
    """Enabling or disabling autostart failed (message is localised)."""


# --------------------------------------------------------------------------- #
# Locations
# --------------------------------------------------------------------------- #


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def autostart_file() -> Path:
    """XDG autostart entry used when there is no systemd unit."""
    return _config_home() / "autostart" / f"{DAEMON_ID}.desktop"


def unit_search_path() -> list[Path]:
    """systemd user unit directories (see ``systemd.unit(5)``)."""
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    dirs = [_config_home() / "systemd/user", Path("/etc/systemd/user")]
    if runtime:
        dirs.append(Path(runtime) / "systemd/user")
    dirs += [Path("/run/systemd/user"), data_home / "systemd/user"]
    dirs += [Path(d) / "systemd/user" for d in data_dirs.split(":") if d]
    dirs += [Path("/usr/local/lib/systemd/user"), Path("/usr/lib/systemd/user")]
    return dirs


def unit_installed() -> bool:
    """The systemd user unit file exists somewhere systemd looks."""
    return any((d / SYSTEMD_UNIT).is_file() for d in unit_search_path())


def _systemctl() -> str | None:
    return shutil.which("systemctl")


# --------------------------------------------------------------------------- #
# XDG autostart entry
# --------------------------------------------------------------------------- #


def _desktop_quote(arg: str) -> str:
    """Quote one Exec argument (Desktop Entry spec)."""
    if arg and not any(c in arg for c in ' \t\n"\'\\><~|&;$*?#()`%'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'.replace("%", "%%")


def _exec_line() -> str:
    daemon = shutil.which("greffier-daemon")
    if daemon:
        argv = [daemon]
    else:
        # D-Bus activation: works whenever the D-Bus service file is installed.
        argv = ["gdbus", "call", "--session", "--dest", DBUS_NAME, "--object-path", DBUS_PATH,
                "--method", "org.freedesktop.DBus.Peer.Ping"]
    return " ".join(_desktop_quote(a) for a in argv)


def desktop_entry() -> str:
    """Content of the XDG autostart file."""
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        f"Name={APP_NAME}",
        "Comment=" + _("Voice dictation service").replace("\n", " "),
        f"Exec={_exec_line()}",
        f"Icon={APP_ID}",
        "Terminal=false",
        "NoDisplay=true",
        "X-GNOME-Autostart-enabled=true",
        "",
    ])


def _xdg_enabled() -> bool:
    path = autostart_file()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    lines = {line.strip().lower() for line in text.splitlines()}
    return "hidden=true" not in lines and "x-gnome-autostart-enabled=false" not in lines


def _write_xdg(enabled: bool) -> None:
    path = autostart_file()
    try:
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(desktop_entry(), encoding="utf-8")
            os.replace(tmp, path)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        raise AutostartError(_("Could not update {path}: {error}").format(
            path=path, error=exc.strerror or exc)) from exc


# --------------------------------------------------------------------------- #
# systemd
# --------------------------------------------------------------------------- #


def _unit_state() -> str | None:
    """``systemctl --user is-enabled`` output, ``None`` if it cannot be asked."""
    systemctl = _systemctl()
    if systemctl is None or not unit_installed():
        return None
    try:
        proc = subprocess.run([systemctl, "--user", "is-enabled", SYSTEMD_UNIT],
                              capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT_S,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("systemctl is-enabled failed: %s", exc)
        return None
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "not-found"


def _use_systemd(state: str | None) -> bool:
    # "static": the unit has no [Install] section and cannot be enabled.
    return state is not None and state not in ("not-found", "static", "masked", "bad")


def is_enabled() -> bool:
    """True when the service starts with the session (either mechanism)."""
    state = _unit_state()
    if state in _ENABLED_STATES:
        return True
    return _xdg_enabled()


def set_enabled(enabled: bool) -> None:
    """Enable or disable autostart. Raises :class:`AutostartError` on failure."""
    state = _unit_state()
    if _use_systemd(state):
        systemctl = _systemctl()
        assert systemctl is not None
        verb = "enable" if enabled else "disable"
        try:
            proc = subprocess.run([systemctl, "--user", verb, SYSTEMD_UNIT],
                                  capture_output=True, text=True,
                                  timeout=SYSTEMCTL_TIMEOUT_S, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AutostartError(_("Could not run systemctl: {error}").format(error=exc)) from exc
        if proc.returncode != 0:
            raise AutostartError(_("systemctl --user {verb} failed: {error}").format(
                verb=verb, error=proc.stderr.strip() or proc.returncode))
        log.info("systemd unit %s %sd", SYSTEMD_UNIT, verb)
        _write_xdg(False)  # never start twice
        return
    _write_xdg(enabled)
    log.info("XDG autostart entry %s", "written" if enabled else "removed")


def set_enabled_async(enabled: bool, callback: Callable[[bool, str], None] | None = None) -> None:
    """Non-blocking :func:`set_enabled`; ``callback(ok, localised error)``."""
    systemctl = _systemctl()

    def finish(ok: bool, message: str = "") -> None:
        if callback is not None:
            callback(ok, message)

    def xdg() -> None:
        try:
            _write_xdg(enabled)
        except AutostartError as exc:
            finish(False, str(exc))
            return
        finish(True)

    if systemctl is None or not unit_installed():
        xdg()
        return

    def got_state(result: CommandResult) -> None:
        state = result.stdout.decode().strip() or "not-found"
        if not _use_systemd(state):
            xdg()
            return
        verb = "enable" if enabled else "disable"

        def changed(res: CommandResult) -> None:
            if not res.ok:
                finish(False, _("systemctl --user {verb} failed: {error}").format(
                    verb=verb, error=res.stderr_text or res.error or res.status))
                return
            try:
                _write_xdg(False)
            except AutostartError as exc:
                log.warning("%s", exc)
            finish(True)

        run_command([systemctl, "--user", verb, SYSTEMD_UNIT], changed, capture_stderr=True,
                    timeout_ms=SYSTEMCTL_TIMEOUT_S * 1000)

    run_command([systemctl, "--user", "is-enabled", SYSTEMD_UNIT], got_state,
                capture_stdout=True, timeout_ms=SYSTEMCTL_TIMEOUT_S * 1000)
