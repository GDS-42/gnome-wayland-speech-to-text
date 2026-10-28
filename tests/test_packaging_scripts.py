"""Static checks and simulations of packaging/install.sh and uninstall.sh."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "packaging" / "install.sh"
UNINSTALL = REPO / "packaging" / "uninstall.sh"
SCRIPTS = [INSTALL, UNINSTALL]

# Variables that would let a script reach the developer's real session.
SESSION_VARS = ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DISPLAY",
                "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "SUDO_USER", "GREFFIER_HOME",
                "GREFFIER_MODELS_DIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                "XDG_STATE_HOME")


# Session tools are replaced by stubs that fail, as on a machine without a
# GNOME session: the scripts must cope, and the real session is never touched.
SESSION_TOOLS = ("systemctl", "gsettings", "gnome-extensions", "gnome-shell", "gdbus",
                 "journalctl", "busctl", "dconf", "update-desktop-database",
                 "gtk-update-icon-cache", "selinuxenabled", "restorecon")


def isolated_env(home: Path, stubs: Path | None = None, **extra: str) -> dict[str, str]:
    session = home.parent / f"{home.name}-session-stubs"
    session.mkdir(parents=True, exist_ok=True)
    for tool in SESSION_TOOLS:
        (session / tool).write_text("#!/bin/sh\nexit 1\n")
        (session / tool).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in SESSION_VARS}
    path = f"{session}:{env.get('PATH', '/usr/bin:/bin')}"
    env.update(HOME=str(home), NO_COLOR="1", LC_ALL="C.UTF-8",
               PATH=f"{stubs}:{path}" if stubs else path)
    env.update(extra)
    return env


def run(script: Path, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(script), *args], env=env, capture_output=True,
                          text=True, timeout=60, stdin=subprocess.DEVNULL, check=False)


def all_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*"))


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(script: Path) -> None:
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_shellcheck(script: Path) -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck not installed")
    proc = subprocess.run([shellcheck, "-x", str(script)], capture_output=True, text=True,
                          env={**os.environ, "LC_ALL": "C.UTF-8"}, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_are_executable(script: Path) -> None:
    assert os.access(script, os.X_OK)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_help(script: Path, tmp_path: Path) -> None:
    proc = run(script, "--help", env=isolated_env(tmp_path))
    assert proc.returncode == 0
    assert proc.stdout.startswith("Utilisation")
    assert "--dry-run" in proc.stdout


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_unknown_option(script: Path, tmp_path: Path) -> None:
    proc = run(script, "--frobnicate", env=isolated_env(tmp_path))
    assert proc.returncode == 2
    assert "Option inconnue" in proc.stderr


def test_install_dry_run_changes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    proc = run(INSTALL, "--dry-run", "--no-deps", "--model", "parakeet-tdt-0.6b-v3-int8",
               env=isolated_env(home))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[simulation]" in proc.stdout
    assert "Simulation terminée" in proc.stdout
    assert all_files(home) == []


def test_uninstall_dry_run_changes_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = home / ".config" / "greffier" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[general]\n")
    before = all_files(home)
    proc = run(UNINSTALL, "--dry-run", "--purge", env=isolated_env(home))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "--purge" in proc.stdout
    assert all_files(home) == before


def test_uninstall_needs_yes_without_a_terminal(tmp_path: Path) -> None:
    proc = run(UNINSTALL, env=isolated_env(tmp_path))
    assert proc.returncode == 1
    assert "--yes" in proc.stderr


def test_install_rejects_odd_model_ids(tmp_path: Path) -> None:
    proc = run(INSTALL, "--dry-run", "--no-deps", "--model", "../etc", env=isolated_env(tmp_path))
    assert proc.returncode == 1
    assert "invalide" in proc.stderr


def test_install_warns_about_unknown_model(tmp_path: Path) -> None:
    proc = run(INSTALL, "--dry-run", "--no-deps", "--model", "no-such-model",
               env=isolated_env(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "absent du catalogue" in proc.stdout


def test_install_refuses_sudo(tmp_path: Path) -> None:
    if os.geteuid() != 0:
        pytest.skip("only meaningful when the tests run as root")
    proc = run(INSTALL, "--dry-run", "--no-deps", env=isolated_env(tmp_path, SUDO_USER="alice"))
    assert proc.returncode == 1
    assert "sudo" in proc.stderr


def test_fedora_dry_run_proposes_dnf(tmp_path: Path) -> None:
    """Fedora code path: missing RPMs are listed and dnf is only simulated."""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "calls.log"
    (stubs / "rpm").write_text(
        '#!/bin/sh\nfor a; do last="$a"; done\n'
        '[ "$last" = wl-clipboard ] && exit 1\nexit 0\n')
    for forbidden in ("dnf", "sudo"):
        (stubs / forbidden).write_text(f'#!/bin/sh\necho "{forbidden} $*" >> "{log}"\nexit 99\n')
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    os_release = tmp_path / "os-release"
    os_release.write_text('NAME="Fedora Linux"\nID=fedora\nVERSION_ID=43\n'
                          'PRETTY_NAME="Fedora Linux 43 (Workstation Edition)"\n')
    env = isolated_env(tmp_path / "home", stubs, GREFFIER_OS_RELEASE=str(os_release),
                       GREFFIER_PYTHON=sys.executable)
    proc = run(INSTALL, "--dry-run", "--yes", env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Fedora Linux 43" in proc.stdout
    assert "Paquets Fedora manquants : wl-clipboard" in proc.stdout
    assert "dnf install -y wl-clipboard" in proc.stdout
    assert not log.exists(), log.read_text()  # dry run: nothing executed


def test_makefile_parses() -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make not installed")
    proc = subprocess.run([make, "-n", "-C", str(REPO), "help", "test", "lint", "validate",
                           "pot", "dist", "clean", "run-daemon"],
                          capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr


def test_ci_workflow() -> None:
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
    jobs = workflow["jobs"]
    assert {"lint", "test-ubuntu", "test-fedora", "release"} <= set(jobs)
    assert "fedora:latest" in jobs["test-fedora"]["container"]
    assert "refs/tags/" in jobs["release"]["if"]
    for job in jobs.values():
        for step in job["steps"]:
            if "uses" in step:
                action, _, ref = step["uses"].partition("@")
                assert ref.startswith("v") and ref[1:].isdigit(), f"{action} pinned to {ref!r}"
