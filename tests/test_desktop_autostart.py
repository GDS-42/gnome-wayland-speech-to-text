"""autostart.py: systemd user unit or XDG autostart fallback (fake systemctl)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures" / "desktop"))
from glibtest import run_until  # noqa: E402

from greffier.const import DAEMON_ID, SYSTEMD_UNIT  # noqa: E402
from greffier.desktop import autostart  # noqa: E402

SYSTEMCTL = """#!/bin/sh
echo "$*" >> "$FAKE_DIR/systemctl.argv"
state_file="$FAKE_DIR/unit-state"
case "$2" in
  is-enabled)
    state=$(cat "$state_file" 2>/dev/null || echo not-found)
    echo "$state"
    [ "$state" = enabled ] && exit 0 || exit 1 ;;
  enable)
    [ -n "$FAKE_FAIL" ] && { echo "Failed to enable unit: Access denied" >&2; exit 1; }
    echo enabled > "$state_file" ;;
  disable)
    echo disabled > "$state_file" ;;
esac
exit 0
"""


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "systemctl"
    script.write_text(SYSTEMCTL)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "sysdata"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    return tmp_path


def install_unit(env: Path, state: str = "disabled") -> None:
    unit_dir = env / "data" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / SYSTEMD_UNIT).write_text("[Service]\nExecStart=greffier-daemon\n")
    (env / "unit-state").write_text(state)


def calls(env: Path) -> list[str]:
    path = env / "systemctl.argv"
    return path.read_text().splitlines() if path.exists() else []


def test_xdg_fallback_without_unit(env: Path) -> None:
    if any((d / SYSTEMD_UNIT).is_file() for d in autostart.unit_search_path()):
        pytest.skip("a real unit is installed on this machine")
    assert not autostart.unit_installed()
    assert not autostart.is_enabled()
    autostart.set_enabled(True)
    entry = env / "config" / "autostart" / f"{DAEMON_ID}.desktop"
    assert entry == autostart.autostart_file()
    text = entry.read_text()
    assert "[Desktop Entry]" in text and "Exec=" in text and "Type=Application" in text
    assert "X-GNOME-Autostart-enabled=true" in text
    assert autostart.is_enabled()
    autostart.set_enabled(False)
    assert not entry.exists() and not autostart.is_enabled()
    assert calls(env) == []  # systemctl never asked when there is no unit


def test_exec_line_uses_dbus_activation_without_binary(env: Path, monkeypatch) -> None:
    monkeypatch.setattr(autostart.shutil, "which",
                        lambda name: None if name == "greffier-daemon" else "/usr/bin/" + name)
    exec_line = next(line for line in autostart.desktop_entry().splitlines()
                     if line.startswith("Exec="))
    assert "gdbus call --session --dest io.github.greffier.Greffier.Daemon" in exec_line
    assert "org.freedesktop.DBus.Peer.Ping" in exec_line


def test_hidden_entry_counts_as_disabled(env: Path) -> None:
    entry = autostart.autostart_file()
    entry.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nType=Application\nHidden=true\n")
    assert not autostart.is_enabled()


def test_systemd_unit_enable_disable(env: Path) -> None:
    install_unit(env)
    stale = autostart.autostart_file()
    stale.parent.mkdir(parents=True)
    stale.write_text("[Desktop Entry]\nType=Application\nExec=old\n")
    assert autostart.unit_installed()
    autostart.set_enabled(True)
    assert f"--user enable {SYSTEMD_UNIT}" in calls(env)
    assert not stale.exists()  # never started twice
    assert autostart.is_enabled()
    autostart.set_enabled(False)
    assert f"--user disable {SYSTEMD_UNIT}" in calls(env)
    assert not autostart.is_enabled()


def test_systemctl_failure_raises(env: Path, monkeypatch) -> None:
    install_unit(env)
    monkeypatch.setenv("FAKE_FAIL", "1")
    with pytest.raises(autostart.AutostartError, match="Access denied"):
        autostart.set_enabled(True)


def test_static_unit_falls_back_to_xdg(env: Path) -> None:
    install_unit(env, "static")
    autostart.set_enabled(True)
    assert autostart.autostart_file().exists()
    assert autostart.is_enabled()
    assert not any(c.startswith("--user enable") for c in calls(env))


def test_set_enabled_async(env: Path) -> None:
    install_unit(env)
    got = []
    autostart.set_enabled_async(True, lambda ok, msg: got.append((ok, msg)))
    assert run_until(lambda: got)
    assert got == [(True, "")]
    assert autostart.is_enabled()
    os.environ["FAKE_FAIL"] = "1"
    try:
        (env / "unit-state").write_text("disabled")
        got.clear()
        autostart.set_enabled_async(True, lambda ok, msg: got.append((ok, msg)))
        assert run_until(lambda: got)
        assert got[0][0] is False and "Access denied" in got[0][1]
    finally:
        del os.environ["FAKE_FAIL"]
