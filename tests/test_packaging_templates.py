"""Packaging data files and scripts agree with greffier/const.py.

The systemd unit, the D-Bus activation file, the hidden daemon desktop file and
the identifiers hard-coded in packaging/*.sh must all derive from the ids in
greffier.const (docs/ARCHITECTURE.md, "Renaming the project").
"""

from __future__ import annotations

import configparser
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from greffier import const

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
UNIT = DATA / "systemd" / const.SYSTEMD_UNIT
DBUS_SERVICE = DATA / "dbus" / f"{const.DBUS_NAME}.service"
DAEMON_DESKTOP = DATA / f"{const.DAEMON_ID}.desktop"
GUI_DESKTOP = DATA / f"{const.APP_ID}.desktop"
METAINFO = DATA / f"{const.APP_ID}.metainfo.xml"
SCRIPTS = [REPO / "packaging" / "install.sh", REPO / "packaging" / "uninstall.sh"]


def read_ini(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True, delimiters=("=",))
    parser.optionxform = str  # keys are case-sensitive
    parser.read(path, encoding="utf-8")
    return parser


# --------------------------------------------------------------------------- #
# systemd unit
# --------------------------------------------------------------------------- #


def test_unit_file_is_named_after_the_daemon_id() -> None:
    assert UNIT.is_file(), UNIT
    assert f"app-{const.DAEMON_ID}.service" == const.SYSTEMD_UNIT


def test_unit_file_contents() -> None:
    unit = read_ini(UNIT)
    service = unit["Service"]
    assert service["Type"] == "dbus"
    assert service["BusName"] == const.DBUS_NAME
    assert service["ExecStart"] == "@BINDIR@/greffier-daemon"
    assert service["Restart"] == "on-failure"
    assert service["NoNewPrivileges"] == "yes"
    assert "PYTHONUNBUFFERED=1" in service["Environment"].split()
    assert service["Slice"].endswith(".slice")
    assert unit["Unit"]["PartOf"] == "graphical-session.target"
    assert "graphical-session.target" in unit["Unit"]["After"].split()
    assert unit["Unit"]["StartLimitBurst"].isdigit()
    assert unit["Install"]["WantedBy"] == "graphical-session.target"
    assert unit["Unit"]["Documentation"] == const.PROJECT_URL


@pytest.mark.parametrize(
    "option",
    # Each of these breaks PipeWire, D-Bus, Wayland, ydotool or the models in $HOME.
    ["ProtectHome", "PrivateTmp", "ProtectSystem", "MemoryDenyWriteExecute", "PrivateNetwork",
     "PrivateUsers", "RestrictNamespaces", "SystemCallFilter", "RestrictAddressFamilies"],
)
def test_unit_avoids_hardening_that_breaks_the_desktop(option: str) -> None:
    assert option not in read_ini(UNIT)["Service"]


def test_unit_verifies_with_systemd_analyze(tmp_path: Path) -> None:
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze not available")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    daemon = bindir / "greffier-daemon"
    daemon.write_text("#!/bin/sh\nexit 0\n")
    daemon.chmod(daemon.stat().st_mode | stat.S_IXUSR)
    rendered = tmp_path / const.SYSTEMD_UNIT
    rendered.write_text(UNIT.read_text().replace("@BINDIR@", str(bindir)))
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    env = {**os.environ, "XDG_RUNTIME_DIR": str(runtime), "LC_ALL": "C.UTF-8"}
    proc = subprocess.run([analyze, "--user", "verify", str(rendered)], env=env,
                          capture_output=True, text=True, timeout=60, check=False)
    # Unknown keys or bad values are reported as "<file>:<line>: ..." lines.
    problems = [line for line in proc.stderr.splitlines() if str(rendered) in line]
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------- #
# D-Bus activation and desktop files
# --------------------------------------------------------------------------- #


def test_dbus_service_file() -> None:
    assert DBUS_SERVICE.is_file(), DBUS_SERVICE
    section = read_ini(DBUS_SERVICE)["D-BUS Service"]
    assert section["Name"] == const.DBUS_NAME
    assert section["Exec"] == "@BINDIR@/greffier-daemon"
    assert section["SystemdService"] == const.SYSTEMD_UNIT


def test_daemon_desktop_file() -> None:
    entry = read_ini(DAEMON_DESKTOP)["Desktop Entry"]
    assert entry["Type"] == "Application"
    assert entry["Name"] == const.APP_NAME
    assert entry["NoDisplay"] == "true"
    assert entry["X-GNOME-UsesNotifications"] == "true"
    assert entry["Icon"] == const.APP_ID
    assert entry["Exec"].startswith("@BINDIR@/greffier-daemon")
    # DBusActivatable requires the file name to be the bus name.
    if entry.get("DBusActivatable") == "true":
        assert DAEMON_DESKTOP.name == f"{const.DBUS_NAME}.desktop"


@pytest.mark.parametrize("path", sorted(DATA.glob("*.desktop")), ids=lambda p: p.name)
def test_desktop_files_validate(path: Path) -> None:
    validator = shutil.which("desktop-file-validate")
    if validator is None:
        pytest.skip("desktop-file-validate not available")
    proc = subprocess.run([validator, str(path)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_gui_desktop_file_matches_app_id() -> None:
    if not GUI_DESKTOP.is_file():
        pytest.skip(f"{GUI_DESKTOP.name} not written yet")
    entry = read_ini(GUI_DESKTOP)["Desktop Entry"]
    assert entry["Icon"] == const.APP_ID
    assert entry["Exec"].split()[0] in ("greffier", "@BINDIR@/greffier")


def test_metainfo_matches_app_id() -> None:
    if not METAINFO.is_file():
        pytest.skip(f"{METAINFO.name} not written yet")
    text = METAINFO.read_text(encoding="utf-8")
    assert f"<id>{const.APP_ID}</id>" in text
    assert f"{const.APP_ID}.desktop" in text  # <launchable type="desktop-id">


def test_icons_are_named_after_the_app_id() -> None:
    icons = sorted((DATA / "icons" / "hicolor").glob("*/apps/*"))
    if not icons:
        pytest.skip("icons not drawn yet")
    for icon in icons:
        assert icon.stem in (const.APP_ID, f"{const.APP_ID}-symbolic"), icon


# --------------------------------------------------------------------------- #
# Identifiers hard-coded in the shell scripts
# --------------------------------------------------------------------------- #

ASSIGNMENT = re.compile(r'^([A-Z_]+)="([^"$`]*(?:\$\{[A-Z_]+\}[^"$`]*)*)"$', re.MULTILINE)
EXPECTED = {
    "APP_ID": const.APP_ID,
    "DAEMON_ID": const.DAEMON_ID,
    "DBUS_NAME": const.DBUS_NAME,
    "DBUS_PATH": const.DBUS_PATH,
    "DBUS_IFACE": const.DBUS_IFACE,
    "SYSTEMD_UNIT": const.SYSTEMD_UNIT,
    "EXTENSION_UUID": const.EXTENSION_UUID,
    "PROJECT_URL": const.PROJECT_URL,
}


def script_ids(script: Path) -> dict[str, str]:
    """Evaluate the top-level constant assignments of a script with bash."""
    lines = [m.group(0) for m in ASSIGNMENT.finditer(script.read_text(encoding="utf-8"))
             if m.group(1) in EXPECTED or m.group(1) == "SHORTCUT_PATH"]
    names = sorted({line.split("=", 1)[0] for line in lines})
    program = "\n".join(lines + [f'printf "%s=%s\\n" {n} "${{{n}}}"' for n in names])
    out = subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=True)
    return dict(line.split("=", 1) for line in out.stdout.splitlines())


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_ids_match_const(script: Path) -> None:
    found = script_ids(script)
    for name in ("APP_ID", "DAEMON_ID", "DBUS_NAME", "SYSTEMD_UNIT", "EXTENSION_UUID"):
        assert name in found, f"{name} missing from {script.name}"
    for name, value in found.items():
        if name in EXPECTED:
            assert value == EXPECTED[name], f"{script.name}: {name}"


def test_uninstall_shortcut_path_matches_the_daemon() -> None:
    found = script_ids(REPO / "packaging" / "uninstall.sh")
    try:
        from greffier.desktop import gnome_keybinding
    except Exception as exc:  # gi missing, module not written yet…
        pytest.skip(f"greffier.desktop.gnome_keybinding not importable: {exc}")
    assert found["SHORTCUT_PATH"] == gnome_keybinding.GREFFIER_PATH
