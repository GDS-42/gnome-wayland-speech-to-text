"""The "Greffier" GNOME custom keyboard shortcut (toggle mode).

GNOME Settings → Keyboard → Custom Shortcuts stores each shortcut as a
relocatable ``org.gnome.settings-daemon.plugins.media-keys.custom-keybinding``
object; ``org.gnome.settings-daemon.plugins.media-keys custom-keybindings``
lists their paths. gnome-settings-daemon runs the command when the keys are
pressed, so the shortcut works even if the daemon crashed (the ``gdbus call``
D-Bus-activates it) and the user can see and change it in Settings.

GSettings writes go asynchronously to dconf: nothing here blocks the main loop.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field

from gi.repository import Gio

from greffier.const import APP_NAME
from greffier.i18n import _

log = logging.getLogger(__name__)

MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = f"{MEDIA_KEYS_SCHEMA}.custom-keybinding"
CUSTOM_LIST_KEY = "custom-keybindings"
CUSTOM_BASE_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
GREFFIER_PATH = f"{CUSTOM_BASE_PATH}greffier/"

# Schemas whose keys hold accelerators ("s" or "as") GNOME reacts to.
CONFLICT_SCHEMAS = (
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.shell.keybindings",
    "org.gnome.mutter.keybindings",
    "org.gnome.mutter.wayland.keybindings",
    MEDIA_KEYS_SCHEMA,
)

_MODIFIER_ALIASES = {
    "control": "Control",
    "ctrl": "Control",
    "ctl": "Control",
    "primary": "Control",
    "shift": "Shift",
    "alt": "Alt",
    "mod1": "Alt",
    "super": "Super",
    "mod4": "Super",
    "meta": "Meta",
    "hyper": "Hyper",
}
# Order and names used by gtk_accelerator_get_label().
_MODIFIER_ORDER = ("Shift", "Control", "Alt", "Super", "Hyper", "Meta")
_MODIFIER_LABELS = {"Control": "Ctrl"}
_MOD_RE = re.compile(r"<\s*([A-Za-z0-9_]+)\s*>")


# --------------------------------------------------------------------------- #
# Accelerators
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Accelerator:
    """A parsed GTK accelerator: modifier set + key name (lower case)."""

    modifiers: frozenset[str]
    key: str

    @classmethod
    def parse(cls, text: str) -> Accelerator | None:
        """Parse ``<Super><Alt>d`` style text; ``None`` if empty or invalid."""
        text = (text or "").strip()
        if not text or text.lower() == "disabled":
            return None
        mods: set[str] = set()
        pos = 0
        while True:
            match = _MOD_RE.match(text, pos)
            if not match:
                break
            name = _MODIFIER_ALIASES.get(match.group(1).lower())
            if name is None:
                if match.group(1).lower() == "release":
                    pos = match.end()
                    continue
                return None
            mods.add(name)
            pos = match.end()
        key = text[pos:].strip()
        if not key or "<" in key or ">" in key or " " in key:
            return None
        return cls(frozenset(mods), key.lower())

    def label(self) -> str:
        """Human readable form, e.g. ``Alt+Super+D`` (GTK's order)."""
        mods = [_MODIFIER_LABELS.get(m, m) for m in _MODIFIER_ORDER if m in self.modifiers]
        key = self.key.upper() if len(self.key) == 1 else self.key.capitalize()
        return "+".join([*mods, key])

    def to_gtk(self) -> str:
        """Canonical GTK form, e.g. ``<Alt><Super>d``."""
        mods = [m for m in _MODIFIER_ORDER if m in self.modifiers]
        return "".join(f"<{m}>" for m in mods) + self.key


def accelerator_label(text: str) -> str:
    """``<Super><Alt>d`` → ``Alt+Super+D`` (the input itself if unparsable)."""
    acc = Accelerator.parse(text)
    return acc.label() if acc else text


def command_line(argv: list[str]) -> str:
    """Join ``argv`` for a custom shortcut ``command``.

    gnome-settings-daemon parses it like a desktop-file Exec line
    (``g_app_info_create_from_commandline``): shell quoting, and ``%`` must be
    doubled.
    """
    return shlex.join(argv).replace("%", "%%")


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Conflict:
    """Another GNOME binding that uses the same keys."""

    accelerator: str  # as stored by the other binding
    schema: str  # schema id, or CUSTOM_SCHEMA for custom shortcuts
    key: str  # settings key, or the custom shortcut path
    name: str  # what the user knows it as


@dataclass
class KeybindingResult:
    ok: bool
    message: str = ""  # localised warning or error (empty when all is fine)
    conflicts: list[Conflict] = field(default_factory=list)
    changed: bool = False  # something was written


# --------------------------------------------------------------------------- #
# The custom shortcut
# --------------------------------------------------------------------------- #


class GnomeKeybinding:
    """Create, update, inspect and delete Greffier's GNOME custom shortcut.

    ``schema_source``/``backend`` default to the system ones; tests pass a
    private schema directory and a memory backend.
    """

    def __init__(
        self,
        *,
        schema_source: Gio.SettingsSchemaSource | None = None,
        backend: Gio.SettingsBackend | None = None,
        path: str = GREFFIER_PATH,
        name: str = APP_NAME,
    ) -> None:
        self._source = schema_source
        self._backend = backend
        self.path = path
        self.name = name

    # -- schema helpers ------------------------------------------------
    def _lookup(self, schema_id: str) -> Gio.SettingsSchema | None:
        source = self._source or Gio.SettingsSchemaSource.get_default()
        return source.lookup(schema_id, True) if source is not None else None

    def _settings(self, schema_id: str, path: str | None = None) -> Gio.Settings | None:
        schema = self._lookup(schema_id)
        if schema is None:
            return None
        return Gio.Settings.new_full(schema, self._backend, path)

    def available(self) -> bool:
        """gnome-settings-daemon's media-keys schemas are installed."""
        return all(self._lookup(s) is not None for s in (MEDIA_KEYS_SCHEMA, CUSTOM_SCHEMA))

    def _paths(self) -> list[str]:
        media = self._settings(MEDIA_KEYS_SCHEMA)
        return list(media.get_strv(CUSTOM_LIST_KEY)) if media is not None else []

    # -- inspection ----------------------------------------------------
    def installed(self) -> bool:
        """Our path is listed in ``custom-keybindings``."""
        return self.available() and self.path in self._paths()

    def current(self) -> tuple[str, str] | None:
        """``(binding, command)`` of the installed shortcut, or ``None``."""
        if not self.installed():
            return None
        custom = self._settings(CUSTOM_SCHEMA, self.path)
        assert custom is not None
        return custom.get_string("binding"), custom.get_string("command")

    def find_conflicts(self, accelerator: str) -> list[Conflict]:
        """Other GNOME bindings (custom shortcuts and built-in keys) using ``accelerator``."""
        wanted = Accelerator.parse(accelerator)
        if wanted is None:
            return []
        conflicts: list[Conflict] = []
        for path in self._paths():
            if path == self.path:
                continue
            custom = self._settings(CUSTOM_SCHEMA, path)
            if custom is None:
                break
            binding = custom.get_string("binding")
            if Accelerator.parse(binding) == wanted:
                label = custom.get_string("name") or path.rstrip("/").rsplit("/", 1)[-1]
                conflicts.append(Conflict(binding, CUSTOM_SCHEMA, path, label))
        for schema_id in CONFLICT_SCHEMAS:
            schema = self._lookup(schema_id)
            if schema is None:
                continue
            settings = Gio.Settings.new_full(schema, self._backend, None)
            for key in schema.list_keys():
                if key == CUSTOM_LIST_KEY:
                    continue
                type_string = schema.get_key(key).get_value_type().dup_string()
                if type_string == "s":
                    values = [settings.get_string(key)]
                elif type_string == "as":
                    values = list(settings.get_strv(key))
                else:
                    continue
                for value in values:
                    if Accelerator.parse(value) == wanted:
                        summary = schema.get_key(key).get_summary() or key
                        conflicts.append(Conflict(value, schema_id, key, summary))
                        break
        return conflicts

    @staticmethod
    def conflict_message(accelerator: str, conflicts: list[Conflict]) -> str:
        """Localised warning describing ``conflicts`` (``""`` if none)."""
        if not conflicts:
            return ""
        names = ", ".join(_("“{name}”").format(name=c.name) for c in conflicts)
        return _("{shortcut} is also used by {names}: only one of them will work. Change one "
                 "of them in Settings › Keyboard › Keyboard Shortcuts.").format(
            shortcut=accelerator_label(accelerator), names=names)

    # -- changes -------------------------------------------------------
    def install(self, accelerator: str, command: list[str] | str) -> KeybindingResult:
        """Create or update the shortcut; conflicts are reported, not fatal."""
        if not self.available():
            return KeybindingResult(False, _(
                "GNOME keyboard shortcuts are not available (gnome-settings-daemon is missing)."))
        parsed = Accelerator.parse(accelerator)
        if parsed is None:
            return KeybindingResult(False, _("“{accelerator}” is not a valid shortcut.").format(
                accelerator=accelerator))
        cmd = command if isinstance(command, str) else command_line(command)
        custom = self._settings(CUSTOM_SCHEMA, self.path)
        media = self._settings(MEDIA_KEYS_SCHEMA)
        assert custom is not None and media is not None

        changed = False
        custom.delay()
        for key, value in (("name", self.name), ("command", cmd), ("binding", accelerator)):
            if custom.get_string(key) != value:
                custom.set_string(key, value)
                changed = True
        custom.apply()
        paths = list(media.get_strv(CUSTOM_LIST_KEY))
        if self.path not in paths:
            media.set_strv(CUSTOM_LIST_KEY, [*paths, self.path])
            changed = True
        if changed:
            log.info("GNOME custom shortcut set to %s", accelerator)
        conflicts = self.find_conflicts(accelerator)
        if conflicts:
            log.warning("shortcut %s conflicts with %s", accelerator,
                        ", ".join(f"{c.schema}:{c.key}" for c in conflicts))
        return KeybindingResult(True, self.conflict_message(accelerator, conflicts), conflicts,
                                changed)

    def remove(self, *, sync: bool = False) -> bool:
        """Delete the shortcut; returns True if something was removed.

        ``sync=True`` waits for dconf (use it in short-lived processes such as
        an uninstall command, which could exit before the write happens).
        """
        if not self.available():
            return False
        media = self._settings(MEDIA_KEYS_SCHEMA)
        custom = self._settings(CUSTOM_SCHEMA, self.path)
        assert media is not None and custom is not None
        paths = list(media.get_strv(CUSTOM_LIST_KEY))
        removed = self.path in paths
        if removed:
            media.set_strv(CUSTOM_LIST_KEY, [p for p in paths if p != self.path])
        for key in ("name", "command", "binding"):
            if custom.get_user_value(key) is not None:
                custom.reset(key)
                removed = True
        if removed:
            log.info("GNOME custom shortcut removed")
        if sync:
            Gio.Settings.sync()
        return removed
