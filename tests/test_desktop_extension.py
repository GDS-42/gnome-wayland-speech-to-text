"""Static checks of the GNOME Shell extension (no gnome-shell needed)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from gi.repository import Gio

from greffier import const, paths

EXT_DIR = Path(__file__).parent.parent / "gnome-extension" / const.EXTENSION_UUID
JS_FILES = sorted(EXT_DIR.glob("*.js"))


def node() -> str | None:
    return shutil.which("node") or next(
        (p for p in ("/opt/node22/bin/node", "/usr/local/bin/node") if os.access(p, os.X_OK)),
        None)


def test_extension_files_exist() -> None:
    names = {p.name for p in EXT_DIR.iterdir()}
    assert {"metadata.json", "extension.js", "stylesheet.css"} <= names
    assert JS_FILES


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_node_check_esm(path: Path, tmp_path: Path) -> None:
    exe = node()
    if exe is None:
        pytest.skip("node is not installed")
    # .mjs forces ES module parsing whatever node's default is.
    target = tmp_path / (path.stem + ".mjs")
    target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    proc = subprocess.run([exe, "--check", str(target)], capture_output=True, text=True,
                          timeout=30)
    assert proc.returncode == 0, proc.stderr


def test_metadata() -> None:
    meta = json.loads((EXT_DIR / "metadata.json").read_text())
    assert meta["uuid"] == const.EXTENSION_UUID == EXT_DIR.name
    assert meta["url"] == const.PROJECT_URL
    assert meta["shell-version"] == ["45", "46", "47", "48", "49", "50"]
    assert meta["version-name"] == const.VERSION == "0.1.0"
    assert meta["name"] and meta["description"]
    assert "session-modes" not in meta  # never active on the lock screen


def test_constants_match_python() -> None:
    js = (EXT_DIR / "constants.js").read_text()
    assert f"APP_ID = '{const.APP_ID}'" in js
    assert f"DAEMON_PATH = '{const.DBUS_PATH}'" in js
    assert f"SHELL_PATH = '{const.SHELL_DBUS_PATH}'" in js
    from greffier.desktop.shell_client import SHELL_API_VERSION
    assert f"SHELL_API_VERSION = {SHELL_API_VERSION};" in js
    for state in const.State:
        assert f"'{state.value}'" in js


def _inline_xml(js_file: str, const_name: str) -> str:
    js = (EXT_DIR / js_file).read_text()
    match = re.search(const_name + r" = `(.*?)`;", js, re.S)
    assert match, f"{const_name} not found in {js_file}"
    constants = {"SHELL_IFACE": const.SHELL_DBUS_IFACE, "DAEMON_IFACE": const.DBUS_IFACE}
    return re.sub(r"\$\{(\w+)\}", lambda m: constants[m.group(1)], match.group(1))


def _signature(info: Gio.DBusInterfaceInfo) -> dict[str, object]:
    return {
        "methods": {m.name: ([a.signature for a in m.in_args], [a.signature for a in m.out_args])
                    for m in info.methods},
        "signals": {s.name: [a.signature for a in s.args] for s in info.signals},
        "properties": {p.name: p.signature for p in info.properties},
    }


def test_exported_interface_matches_contract() -> None:
    contract = Gio.DBusNodeInfo.new_for_xml(
        paths.package_data("shell-interface.xml").read_text()).lookup_interface(
        const.SHELL_DBUS_IFACE)
    inline = Gio.DBusNodeInfo.new_for_xml(_inline_xml("dbus.js", "SHELL_XML")).lookup_interface(
        const.SHELL_DBUS_IFACE)
    assert _signature(inline) == _signature(contract)
    js = (EXT_DIR / "dbus.js").read_text()
    for method in contract.methods:
        assert f"{method.name}Async(" in js  # every method checks the caller


def test_daemon_subset_matches_contract() -> None:
    contract = _signature(Gio.DBusNodeInfo.new_for_xml(
        paths.package_data("daemon-interface.xml").read_text()).lookup_interface(const.DBUS_IFACE))
    subset = _signature(Gio.DBusNodeInfo.new_for_xml(
        _inline_xml("daemon.js", "DAEMON_XML")).lookup_interface(const.DBUS_IFACE))
    for kind in ("methods", "signals", "properties"):
        for name, sig in subset[kind].items():
            assert contract[kind][name] == sig, f"{kind} {name}"


FORBIDDEN = [
    (r"\beval\s*\(", "eval"),
    (r"new Function\s*\(", "Function constructor"),
    (r"spawn_command_line_sync|spawn_sync|GLib\.spawn_", "synchronous subprocess"),
    (r"\bimports\.(?!gi\b)", "legacy imports"),
    (r"call_sync\s*\(", "synchronous D-Bus call"),
    (r"Mainloop|imports\.mainloop", "legacy Mainloop"),
    (r"ExtensionUtils", "pre-45 ExtensionUtils"),
]


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_review_guidelines(path: Path) -> None:
    text = path.read_text()
    for pattern, what in FORBIDDEN:
        assert not re.search(pattern, text), f"{what} in {path.name}"
    for match in re.finditer(r"from '([^']+)'", text):
        source = match.group(1)
        assert source.startswith(("gi://", "resource:///org/gnome/shell/", "./")), source


def test_no_work_before_enable() -> None:
    text = (EXT_DIR / "extension.js").read_text()
    assert "export default class" in text and "extends Extension" in text
    assert "constructor(" not in text
    assert "enable()" in text and "disable()" in text
    # disable() must tear down everything enable() created
    enable = text[text.index("enable()"):text.index("disable()")]
    disable = text[text.index("disable()"):]
    for field in set(re.findall(r"this\.(_\w+) = new", enable)):
        assert f"this.{field}?.destroy()" in disable, field
        assert f"this.{field} = null" in disable, field


def test_timeouts_are_tracked() -> None:
    for path in JS_FILES:
        text = path.read_text()
        for match in re.finditer(r"GLib\.timeout_add(?:_seconds)?\(", text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            line = text[line_start:match.start()]
            assert "=" in line, f"untracked timeout in {path.name}: {line.strip()}"


def test_stylesheet_classes_are_used() -> None:
    css = (EXT_DIR / "stylesheet.css").read_text()
    js = "\n".join(p.read_text() for p in JS_FILES)
    for cls in set(re.findall(r"\.(greffier-[\w-]+)", css)):
        assert cls in js, cls


CGROUP_CASES = {
    # systemd user unit (D-Bus/systemd activation): accepted
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "app-io.github.greffier.Greffier.Daemon.service\n": True,
    # XDG autostart launched by gnome-session / gnome-shell: accepted
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "app-gnome-io.github.greffier.Greffier.Daemon-4242.scope\n": True,
    # systemd-xdg-autostart-generator unit: accepted
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "app-io.github.greffier.Greffier.Daemon@autostart.service\n": True,
    # hybrid hierarchy: name=systemd line
    "12:memory:/x\n1:name=systemd:/user.slice/app-io.github.greffier.Greffier.Daemon.service\n"
    "0::/\n": True,
    # a terminal, another app, look-alikes, other controllers: refused
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
    "app-org.gnome.Terminal.slice/vte-spawn-1234.scope\n": False,
    "0::/user.slice/app-io.github.greffier.Greffier.Daemon.service/evil.scope\n": False,
    "0::/user.slice/app-gnome-io.github.greffier.Greffier.Daemon-x1.scope\n": False,
    "0::/app-io.github.greffier.Greffier.DaemonX.service\n": False,
    "4:memory:/app-io.github.greffier.Greffier.Daemon.service\n0::/\n": False,
    "": False,
}


def test_cgroup_verdicts_with_gjs(tmp_path: Path) -> None:
    gjs = shutil.which("gjs")
    if gjs is None:
        pytest.skip("gjs is not installed")
    script = tmp_path / "check.js"
    script.write_text(
        f"import {{cgroupAllowed}} from '{(EXT_DIR / 'trust.js').as_uri()}';\n"
        f"const cases = {json.dumps(CGROUP_CASES)};\n"
        "const out = {};\n"
        "for (const [text, _] of Object.entries(cases)) out[text] = cgroupAllowed(text).ok;\n"
        "print(JSON.stringify(out));\n")
    proc = subprocess.run([gjs, "-m", str(script)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == CGROUP_CASES
