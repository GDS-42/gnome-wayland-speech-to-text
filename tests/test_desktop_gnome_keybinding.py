"""GNOME custom shortcut management against private schemas + memory backend."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from gi.repository import Gio

from greffier.desktop.gnome_keybinding import (
    CUSTOM_BASE_PATH,
    CUSTOM_SCHEMA,
    GREFFIER_PATH,
    MEDIA_KEYS_SCHEMA,
    GnomeKeybinding,
)

SCHEMAS = Path(__file__).parent / "fixtures" / "desktop" / "schemas"
TOGGLE = ["gdbus", "call", "--session", "--dest", "io.github.greffier.Greffier.Daemon",
          "--object-path", "/io/github/greffier/Greffier/Daemon",
          "--method", "io.github.greffier.Greffier.Daemon1.Toggle"]


@pytest.fixture(scope="module")
def schema_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("glib-compile-schemas") is None:
        pytest.skip("glib-compile-schemas not installed")
    out = tmp_path_factory.mktemp("schemas")
    for xml in SCHEMAS.glob("*.gschema.xml"):
        shutil.copy(xml, out)
    subprocess.run(["glib-compile-schemas", "--strict", str(out)], check=True)
    return out


@pytest.fixture
def source(schema_dir: Path) -> Gio.SettingsSchemaSource:
    return Gio.SettingsSchemaSource.new_from_directory(str(schema_dir), None, True)


@pytest.fixture
def backend() -> Gio.SettingsBackend:
    return Gio.memory_settings_backend_new()


@pytest.fixture
def kb(source: Gio.SettingsSchemaSource, backend: Gio.SettingsBackend) -> GnomeKeybinding:
    return GnomeKeybinding(schema_source=source, backend=backend)


def settings(source, backend, schema: str, path: str | None = None) -> Gio.Settings:
    return Gio.Settings.new_full(source.lookup(schema, False), backend, path)


def add_custom(source, backend, slot: str, name: str, binding: str) -> str:
    path = f"{CUSTOM_BASE_PATH}{slot}/"
    custom = settings(source, backend, CUSTOM_SCHEMA, path)
    custom.set_string("name", name)
    custom.set_string("binding", binding)
    custom.set_string("command", "true")
    media = settings(source, backend, MEDIA_KEYS_SCHEMA)
    media.set_strv("custom-keybindings", [*media.get_strv("custom-keybindings"), path])
    return path


def test_install_creates_named_entry(kb, source, backend) -> None:
    assert kb.available()
    assert not kb.installed()
    result = kb.install("<Super><Alt>d", TOGGLE)
    assert result.ok and result.changed
    assert result.message == "" and result.conflicts == []
    media = settings(source, backend, MEDIA_KEYS_SCHEMA)
    assert media.get_strv("custom-keybindings") == [GREFFIER_PATH]
    custom = settings(source, backend, CUSTOM_SCHEMA, GREFFIER_PATH)
    assert custom.get_string("name") == "Greffier"
    assert custom.get_string("binding") == "<Super><Alt>d"
    assert custom.get_string("command") == " ".join(TOGGLE)
    assert kb.current() == ("<Super><Alt>d", " ".join(TOGGLE))


def test_install_is_idempotent_and_keeps_other_shortcuts(kb, source, backend) -> None:
    other = add_custom(source, backend, "custom0", "Terminal", "<Super>t")
    kb.install("<Super><Alt>d", TOGGLE)
    again = kb.install("<Super><Alt>d", TOGGLE)
    assert again.ok and not again.changed
    media = settings(source, backend, MEDIA_KEYS_SCHEMA)
    assert media.get_strv("custom-keybindings") == [other, GREFFIER_PATH]


def test_install_updates_accelerator(kb, source, backend) -> None:
    kb.install("<Super><Alt>d", TOGGLE)
    result = kb.install("<Control><Alt>space", TOGGLE)
    assert result.ok and result.changed
    assert kb.current() == ("<Control><Alt>space", " ".join(TOGGLE))
    media = settings(source, backend, MEDIA_KEYS_SCHEMA)
    assert media.get_strv("custom-keybindings").count(GREFFIER_PATH) == 1


def test_command_is_quoted(kb) -> None:
    kb.install("<Super><Alt>d", ["/opt/my apps/gdbus", "call", "100%"])
    assert kb.current() == ("<Super><Alt>d", "'/opt/my apps/gdbus' call 100%%")


def test_invalid_accelerator(kb) -> None:
    result = kb.install("<Super><Bogus>d", TOGGLE)
    assert not result.ok
    assert "not a valid shortcut" in result.message
    assert not kb.installed()


def test_remove(kb, source, backend) -> None:
    other = add_custom(source, backend, "custom0", "Terminal", "<Super>t")
    kb.install("<Super><Alt>d", TOGGLE)
    assert kb.remove(sync=True)
    media = settings(source, backend, MEDIA_KEYS_SCHEMA)
    assert media.get_strv("custom-keybindings") == [other]
    custom = settings(source, backend, CUSTOM_SCHEMA, GREFFIER_PATH)
    assert custom.get_user_value("binding") is None
    assert not kb.installed()
    assert kb.current() is None
    assert not kb.remove()


@pytest.mark.parametrize(
    ("accel", "who"),
    [
        ("<Super>l", "Lock screen"),  # media-keys (as)
        ("<Mod4>L", "Lock screen"),  # aliases and case are normalised
        ("<Super>Home", "Switch to workspace 1"),  # wm.keybindings
        ("<Super>m", "Show the notification list"),  # shell, second value of the list
        ("<Super><Alt>Up", "Keybinding to shift between overview states"),
        ("<Super>Left", "View split on left"),  # mutter
        ("<Super>Escape", "Restore the keyboard shortcuts"),  # mutter wayland
    ],
)
def test_conflicts_with_builtin_bindings(kb, accel: str, who: str) -> None:
    result = kb.install(accel, TOGGLE)
    assert result.ok  # a conflict is a warning, not a failure
    assert [c.name for c in result.conflicts] == [who]
    assert who in result.message
    assert "Settings" in result.message


def test_conflict_with_other_custom_shortcut(kb, source, backend) -> None:
    add_custom(source, backend, "custom3", "Whisper", "<Alt><Super>D")
    result = kb.install("<Super><Alt>d", TOGGLE)
    assert result.ok
    assert [(c.schema, c.name) for c in result.conflicts] == [(CUSTOM_SCHEMA, "Whisper")]
    assert "Alt+Super+D" in result.message and "“Whisper”" in result.message


def test_conflict_with_changed_user_value(kb, source, backend) -> None:
    wm = settings(source, backend, "org.gnome.desktop.wm.keybindings")
    wm.set_strv("show-desktop", ["<Super><Alt>d"])
    assert [c.key for c in kb.find_conflicts("<Super><Alt>d")] == ["show-desktop"]
    assert kb.find_conflicts("") == []


def test_unavailable_without_schemas(backend, tmp_path: Path) -> None:
    (tmp_path / "org.example.gschema.xml").write_text(
        '<schemalist><schema id="org.example" path="/org/example/">'
        '<key name="x" type="b"><default>false</default></key></schema></schemalist>')
    subprocess.run(["glib-compile-schemas", str(tmp_path)], check=True)
    other = Gio.SettingsSchemaSource.new_from_directory(str(tmp_path), None, True)
    kb = GnomeKeybinding(schema_source=other, backend=backend)
    assert not kb.available()
    result = kb.install("<Super><Alt>d", TOGGLE)
    assert not result.ok and "not available" in result.message
    assert not kb.remove()
    assert not kb.installed()
    assert kb.find_conflicts("<Super>l") == []


def test_default_source_with_environment(schema_dir: Path) -> None:
    """The documented setup: GSETTINGS_SCHEMA_DIR + GSETTINGS_BACKEND=memory."""
    script = textwrap.dedent(f"""
        from greffier.desktop.gnome_keybinding import GnomeKeybinding
        kb = GnomeKeybinding()
        assert kb.available()
        result = kb.install("<Super>l", {TOGGLE!r})
        assert result.ok and result.conflicts, result
        assert kb.installed()
        assert kb.remove()
        print("ok")
    """)
    env = dict(os.environ, GSETTINGS_SCHEMA_DIR=str(schema_dir), GSETTINGS_BACKEND="memory")
    out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True,
                         text=True, cwd=Path(__file__).parent.parent, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"
