"""End-to-end: packaging/install.sh into a throw-away HOME, then uninstall.sh.

The session tools (systemctl, gsettings, gnome-extensions, gdbus…) are replaced
by stubs that log their arguments, so the test never touches the developer's
real session, dconf database or systemd user manager. pip is skipped with the
documented ``GREFFIER_INSTALL_SKIP_PIP=1`` hook (no network, a few seconds).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from greffier import const

REPO = Path(__file__).resolve().parent.parent
SHORTCUT = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/greffier/"
OTHER_SHORTCUT = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
ENTRY_POINTS = ("greffier", "greffier-daemon", "greffierctl")
SESSION_VARS = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DISPLAY",
                "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "SUDO_USER", "GREFFIER_HOME",
                "GREFFIER_MODELS_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                "XDG_STATE_HOME", "GREFFIER_PYTHON")

# One shell stub per session tool. gsettings keeps its values in files so that
# what install.sh writes is what uninstall.sh reads back.
STUB = r"""#!/bin/sh
name=$(basename "$0")
printf '%s %s\n' "$name" "$*" >> "$STUB_LOG"
case "$name" in
  systemctl)
    case "$2" in is-active|is-enabled|is-failed) exit 1 ;; esac ;;
  gsettings)
    mkdir -p "$STUB_STATE"
    case "$1" in
      writable) echo true ;;
      get) if [ -f "$STUB_STATE/$2 $3" ]; then cat "$STUB_STATE/$2 $3"; else echo "@as []"; fi ;;
      set) printf '%s\n' "$4" > "$STUB_STATE/$2 $3" ;;
    esac ;;
  gnome-extensions)
    # Wayland, first install: the running Shell does not know the extension.
    case "$1" in info|enable) exit 2 ;; esac ;;
  gnome-shell) echo "GNOME Shell 49.1" ;;
  gdbus) echo "(false,)" ;;
  selinuxenabled) exit 1 ;;
  sudo|dnf) exit 99 ;;
esac
exit 0
"""
STUBBED = ("systemctl", "gsettings", "gnome-extensions", "gnome-shell", "gdbus", "journalctl",
           "busctl", "dconf", "update-desktop-database", "gtk-update-icon-cache",
           "selinuxenabled", "restorecon", "sudo", "dnf")


@dataclass
class Sandbox:
    root: Path
    home: Path
    log: Path
    state: Path
    env: dict[str, str]

    @property
    def data(self) -> Path:
        return self.home / ".local" / "share"

    @property
    def venv(self) -> Path:
        return self.data / "greffier" / "venv"

    def run(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(["bash", str(script), *args], env=self.env, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL, check=False)
        assert proc.returncode == 0, f"{script.name} {args}\n{proc.stdout}\n{proc.stderr}"
        return proc

    def calls(self, tool: str) -> list[str]:
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line.startswith(tool + " ")]

    def gsetting(self, schema: str, key: str) -> str:
        path = self.state / f"{schema} {key}"
        return path.read_text().strip() if path.exists() else "@as []"

    def set_gsetting(self, schema: str, key: str, value: str) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / f"{schema} {key}").write_text(value + "\n")

    def files(self) -> set[str]:
        """Every path under HOME except the inside of the virtualenv."""
        return {str(p.relative_to(self.home)) for p in self.home.rglob("*")
                if self.venv not in p.parents}

    def seed_user_data(self) -> None:
        for path in (self.home / ".config/greffier/config.toml",
                     self.data / "greffier/models/parakeet-tdt-0.6b-v3-int8/tokens.txt",
                     self.home / ".cache/greffier/catalog.json",
                     self.home / ".local/state/greffier/history.jsonl"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for tool in STUBBED:
        (stubs / tool).write_text(STUB)
        (stubs / tool).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in SESSION_VARS}
    env.update(
        HOME=str(home),
        PATH=f"{stubs}:{env.get('PATH', '/usr/bin:/bin')}",
        NO_COLOR="1",
        LC_ALL="C.UTF-8",
        STUB_LOG=str(tmp_path / "calls.log"),
        STUB_STATE=str(tmp_path / "gsettings"),
        GREFFIER_INSTALL_SKIP_PIP="1",
    )
    return Sandbox(tmp_path, home, tmp_path / "calls.log", tmp_path / "gsettings", env)


def installed_paths(sb: Sandbox) -> dict[str, Path]:
    return {
        "unit": sb.home / ".config/systemd/user" / const.SYSTEMD_UNIT,
        "dbus": sb.data / "dbus-1/services" / f"{const.DBUS_NAME}.service",
        "daemon_desktop": sb.data / "applications" / f"{const.DAEMON_ID}.desktop",
        "manifest": sb.data / "greffier/installed-files.txt",
        "uninstaller": sb.data / "greffier/uninstall.sh",
        **{name: sb.home / ".local/bin" / name for name in ENTRY_POINTS},
    }


def assert_installed(sb: Sandbox, source: Path) -> None:
    paths = installed_paths(sb)
    for path in paths.values():
        assert path.is_file(), path
    bindir = sb.venv / "bin"
    assert (bindir / "python").exists()
    assert f"ExecStart={bindir}/greffier-daemon\n" in paths["unit"].read_text()
    dbus = paths["dbus"].read_text()
    assert f"Exec={bindir}/greffier-daemon\n" in dbus
    assert f"SystemdService={const.SYSTEMD_UNIT}\n" in dbus
    assert f"Exec={bindir}/greffier-daemon\n" in paths["daemon_desktop"].read_text()
    for name in ENTRY_POINTS:
        launcher = paths[name]
        assert os.access(launcher, os.X_OK)
        assert f"exec '{bindir}/{name}' \"$@\"" in launcher.read_text()
    # Optional files from the GUI engineer, installed whenever they exist.
    if (source / "data" / f"{const.APP_ID}.desktop").is_file():
        desktop = sb.data / "applications" / f"{const.APP_ID}.desktop"
        assert f"Exec={bindir}/greffier" in desktop.read_text()
    if (source / "data" / f"{const.APP_ID}.metainfo.xml").is_file():
        assert (sb.data / "metainfo" / f"{const.APP_ID}.metainfo.xml").is_file()
    icons = source / "data" / "icons" / "hicolor"
    for icon in icons.rglob("*.svg"):
        assert (sb.data / "icons/hicolor" / icon.relative_to(icons)).is_file(), icon
    manifest = paths["manifest"].read_text().split()
    assert manifest, "empty manifest"
    for entry in manifest:
        assert entry.startswith(str(sb.home) + "/") and Path(entry).is_file(), entry
    rendered = [paths["unit"], paths["dbus"], *(sb.data / "applications").glob("*.desktop")]
    for path in rendered:
        assert "@BINDIR@" not in path.read_text(), path


def test_install_then_uninstall(sandbox: Sandbox) -> None:
    sb = sandbox
    sb.set_gsetting("org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings",
                    f"['{OTHER_SHORTCUT}', '{SHORTCUT}']")
    foreign = sb.home / ".local/bin/my-tool"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("#!/bin/sh\n")

    out = sb.run(REPO / "packaging/install.sh", "--no-deps", "--no-autostart", "--no-extension")
    assert_installed(sb, REPO)
    assert "C'est prêt" in out.stdout
    assert "Super+Alt+D" in out.stdout
    systemctl = sb.calls("systemctl")
    assert "systemctl --user daemon-reload" in systemctl
    assert not [c for c in systemctl if c.split()[2] in ("enable", "start", "restart")]
    assert not sb.calls("sudo") and not sb.calls("dnf")
    assert not sb.calls("gnome-extensions")
    assert not (sb.data / "gnome-shell").exists()

    # Idempotent: a second run (= upgrade) leaves exactly the same files.
    first = sb.files()
    sb.run(REPO / "packaging/install.sh", "--no-deps", "--no-autostart", "--no-extension")
    assert sb.files() == first

    # Plain uninstall (from the copy in ~/.local/share/greffier): user data stays.
    sb.seed_user_data()
    sb.run(installed_paths(sb)["uninstaller"], "--yes")
    for name, path in installed_paths(sb).items():
        assert not path.exists(), name
    assert not sb.venv.exists()
    assert (sb.home / ".config/greffier/config.toml").is_file()
    assert (sb.data / "greffier/models/parakeet-tdt-0.6b-v3-int8/tokens.txt").is_file()
    assert sb.gsetting("org.gnome.settings-daemon.plugins.media-keys",
                       "custom-keybindings") == f"['{OTHER_SHORTCUT}']"
    assert any("reset-recursively" in c and SHORTCUT in c for c in sb.calls("gsettings"))
    assert any("disable --now" in c for c in sb.calls("systemctl"))

    # Reinstall, then purge: nothing of Greffier is left in HOME.
    sb.run(REPO / "packaging/install.sh", "--no-deps", "--no-autostart", "--no-extension")
    out = sb.run(REPO / "packaging/uninstall.sh", "--purge", "--yes")
    assert "dont modèles téléchargés" in out.stdout
    leftovers = sorted(p for p in sb.files() if "greffier" in p.lower())
    assert leftovers == [], leftovers
    assert foreign.is_file()


def test_broken_virtualenv_is_recreated(sandbox: Sandbox) -> None:
    sb = sandbox
    sb.run(REPO / "packaging/install.sh", "--no-deps", "--no-autostart", "--no-extension")
    cfg = sb.venv / "pyvenv.cfg"
    cfg.write_text(cfg.read_text().replace("include-system-site-packages = true",
                                           "include-system-site-packages = false"))
    out = sb.run(REPO / "packaging/install.sh", "--no-deps", "--no-autostart", "--no-extension")
    assert "recréé" in out.stdout
    assert "include-system-site-packages = true" in cfg.read_text()


def make_fake_checkout(root: Path) -> Path:
    """A source tree with every optional component present (extension, GUI files)."""
    src = root / "src"
    shutil.copytree(REPO / "packaging", src / "packaging")
    shutil.copytree(REPO / "data", src / "data")
    shutil.copy(REPO / "pyproject.toml", src / "pyproject.toml")
    (src / "greffier" / "data").mkdir(parents=True)
    shutil.copy(REPO / "greffier" / "__init__.py", src / "greffier" / "__init__.py")
    shutil.copy(REPO / "greffier/data/catalog.json", src / "greffier/data/catalog.json")

    data = src / "data"
    gui = data / f"{const.APP_ID}.desktop"
    if not gui.exists():
        gui.write_text(textwrap.dedent(f"""\
            [Desktop Entry]
            Type=Application
            Name=Greffier
            Exec=greffier
            TryExec=greffier
            Icon={const.APP_ID}
            Categories=Utility;
            """))
    for size, name in (("scalable", const.APP_ID), ("symbolic", f"{const.APP_ID}-symbolic")):
        icon = data / "icons/hicolor" / size / "apps" / f"{name}.svg"
        if not icon.exists():
            icon.parent.mkdir(parents=True, exist_ok=True)
            icon.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>\n')

    ext = src / "gnome-extension" / const.EXTENSION_UUID
    shutil.rmtree(ext, ignore_errors=True)
    (ext / "po").mkdir(parents=True)
    metadata = {"uuid": const.EXTENSION_UUID, "name": "Greffier", "description": "test",
                "shell-version": ["48", "49", "50"], "gettext-domain": "greffier-shell"}
    (ext / "metadata.json").write_text(json.dumps(metadata) + "\n")
    (ext / "extension.js").write_text("export default class Ext {}\n")
    (ext / "po" / "fr.po").write_text(
        'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        'msgid "Dictation"\nmsgstr "Dictée"\n')
    (ext / "extension.js~").write_text("editor backup\n")
    (ext / ".eslintrc.yml").write_text("junk\n")
    return src


def test_full_install_with_extension_and_autostart(sandbox: Sandbox, tmp_path: Path) -> None:
    sb = sandbox
    src = make_fake_checkout(tmp_path)
    sb.set_gsetting("org.gnome.shell", "enabled-extensions", "['other@example.org']")
    stray = sb.home / ".local/bin/greffier"  # e.g. left by an old `pip install --user`
    stray.parent.mkdir(parents=True)
    stray.write_text("#!/bin/sh\necho old\n")

    out = sb.run(src / "packaging/install.sh", "--no-deps")
    assert_installed(sb, src)
    assert (sb.home / ".local/bin/greffier.avant-greffier").read_text() == "#!/bin/sh\necho old\n"

    # Autostart: the unit is enabled and (re)started.
    systemctl = sb.calls("systemctl")
    assert f"systemctl --user enable --quiet {const.SYSTEMD_UNIT}" in systemctl
    assert f"systemctl --user restart {const.SYSTEMD_UNIT}" in systemctl

    # Extension copied without junk, enabled for the next login, user told to relog.
    ext = sb.data / "gnome-shell/extensions" / const.EXTENSION_UUID
    assert (ext / "metadata.json").is_file() and (ext / "extension.js").is_file()
    assert not (ext / "extension.js~").exists() and not (ext / ".eslintrc.yml").exists()
    if shutil.which("msgfmt"):
        assert (ext / "locale/fr/LC_MESSAGES/greffier-shell.mo").is_file()
    assert sb.gsetting("org.gnome.shell", "enabled-extensions") == \
        f"['other@example.org', '{const.EXTENSION_UUID}']"
    assert "déconnectez-vous puis reconnectez-vous" in out.stdout

    sb.run(src / "packaging/uninstall.sh", "--purge", "--yes")
    assert not ext.exists()
    assert sb.gsetting("org.gnome.shell", "enabled-extensions") == "['other@example.org']"
    assert f"gnome-extensions disable {const.EXTENSION_UUID}" in sb.calls("gnome-extensions")
    leftovers = sorted(p for p in sb.files()
                       if "greffier" in p.lower() and not p.endswith(".avant-greffier"))
    assert leftovers == [], leftovers
