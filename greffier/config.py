"""User configuration: typed schema, TOML persistence and validated patches.

The daemon is the only writer (single source of truth): the GUI and the CLI
send partial patches over D-Bus (``SetConfig``) and receive the full,
validated configuration back. Hand-edited files are tolerated: unknown keys
are ignored and invalid values fall back to their default with a warning, so
a typo can never prevent the service from starting.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import tomllib
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path
from typing import Any

from greffier import paths
from greffier.const import DEFAULT_CATALOG_URL

log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when a patch contains an unknown key or an invalid value."""


def _choice(default: str, *choices: str) -> Any:
    assert default in choices
    return field(default=default, metadata={"choices": choices})


def _range(default: float, lo: float, hi: float) -> Any:
    return field(default=default, metadata={"range": (lo, hi)})


@dataclass
class GeneralConfig:
    autostart: bool = True
    sounds: bool = True
    notifications: bool = True  # errors, finished downloads, updates
    notify_transcription: bool = False  # also show each transcription
    history_size: int = _range(50, 0, 1000)
    persist_history: bool = False  # privacy first: history lives in RAM only
    first_run_done: bool = False  # onboarding completed in the GUI


@dataclass
class ShortcutConfig:
    accelerator: str = "<Super><Alt>d"  # GTK accelerator syntax
    # auto: GNOME custom shortcut in toggle mode (most reliable, visible in
    # GNOME Settings); GlobalShortcuts portal in push-to-talk mode (GNOME 48+)
    backend: str = _choice("auto", "auto", "gnome", "portal", "none")
    # push-to-talk needs key-release events, i.e. the portal backend
    mode: str = _choice("toggle", "toggle", "push-to-talk")


@dataclass
class RecordingConfig:
    device: str = ""  # "" = system default source, else AudioDevice.id
    auto_stop: bool = False  # stop after `silence_timeout` s of silence
    silence_timeout: float = _range(2.0, 0.5, 10.0)
    max_duration: int = _range(300, 10, 3600)  # safety net, seconds
    live: bool = False  # insert text at each pause instead of at the end


@dataclass
class ModelConfig:
    active: str = "parakeet-tdt-0.6b-v3-int8"
    language: str = "fr"  # ISO 639-1, or "auto" for models that detect it
    threads: int = _range(0, 0, 64)  # 0 = automatic
    provider: str = _choice("cpu", "cpu", "cuda")
    preload: bool = True  # load the model when the service starts
    unload_after: int = _range(0, 0, 1440)  # idle minutes before freeing RAM, 0 = never
    catalog_url: str = DEFAULT_CATALOG_URL
    check_updates: bool = True  # refresh the remote catalog once a day


@dataclass
class InjectionConfig:
    # auto: paste through the clipboard (robust with accents and any layout)
    method: str = _choice("auto", "auto", "paste", "type", "clipboard")
    # auto: Ctrl+Shift+V in terminals, Ctrl+V elsewhere (needs the extension)
    paste_shortcut: str = _choice("auto", "auto", "ctrl-v", "ctrl-shift-v", "shift-insert")
    # which component synthesises key presses
    backend: str = _choice("auto", "auto", "extension", "portal", "ydotool")
    restore_clipboard: bool = True  # put back the previous (text) clipboard
    trailing_space: bool = True  # separate consecutive dictations


@dataclass
class Replacement:
    spoken: str = ""  # what the model writes (matched case-insensitively, whole words)
    written: str = ""  # what should be typed instead


@dataclass
class TextConfig:
    voice_commands: bool = True  # « à la ligne », « nouveau paragraphe »…
    french_typography: bool = False  # narrow no-break spaces before ; : ! ? and in « »
    replacements: list[Replacement] = field(default_factory=list)


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    shortcut: ShortcutConfig = field(default_factory=ShortcutConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    model: ModelConfig = field(default_factory=ModelConfig)
    injection: InjectionConfig = field(default_factory=InjectionConfig)
    text: TextConfig = field(default_factory=TextConfig)


_SECTION_TYPES = {
    "general": GeneralConfig,
    "shortcut": ShortcutConfig,
    "recording": RecordingConfig,
    "model": ModelConfig,
    "injection": InjectionConfig,
    "text": TextConfig,
}


# --------------------------------------------------------------------------- #
# Conversion and validation
# --------------------------------------------------------------------------- #


def _default_of(f) -> Any:
    if f.default is not MISSING:
        return f.default
    return f.default_factory()  # type: ignore[misc]


def _check_text(where: str, value: str) -> None:
    """Reject text TOML or UTF-8 cannot carry (lone surrogates, control characters).

    Newlines and tabs are allowed: replacements may insert them on purpose.
    """
    for char in value:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise ConfigError(f"{where}: invalid Unicode text")
        if (code < 0x20 and char not in "\n\t") or 0x7F <= code < 0xA0:
            raise ConfigError(f"{where}: control characters are not allowed")


def _coerce(section: str, f, value: Any) -> Any:
    """Validate one value against its field; raise ConfigError if invalid."""
    where = f"{section}.{f.name}"
    kind = f.type if isinstance(f.type, str) else getattr(f.type, "__name__", str(f.type))
    if kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected true/false, got {value!r}")
    elif kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            else:
                raise ConfigError(f"{where}: expected an integer, got {value!r}")
    elif kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected a number, got {value!r}")
        value = float(value)
    elif kind == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected a string, got {value!r}")
        _check_text(where, value)
    elif kind == "list[Replacement]":
        if not isinstance(value, list):
            raise ConfigError(f"{where}: expected a list")
        items = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("spoken"), str) \
                    or not isinstance(item.get("written", ""), str):
                raise ConfigError(f"{where}: items need 'spoken' and 'written' strings")
            _check_text(where, item["spoken"])
            _check_text(where, item.get("written", ""))
            if item["spoken"].strip():
                items.append(Replacement(item["spoken"], item.get("written", "")))
        value = items
    else:  # pragma: no cover - schema programming error
        raise TypeError(f"unsupported field type {kind} for {where}")

    choices = f.metadata.get("choices")
    if choices and value not in choices:
        raise ConfigError(f"{where}: {value!r} is not one of {', '.join(choices)}")
    bounds = f.metadata.get("range")
    if bounds and not bounds[0] <= value <= bounds[1]:
        raise ConfigError(f"{where}: {value!r} is outside [{bounds[0]}, {bounds[1]}]")
    return value


def _section_from_dict(name: str, data: dict[str, Any], strict: bool, base=None):
    cls = _SECTION_TYPES[name]
    obj = base if base is not None else cls()
    known = {f.name: f for f in fields(cls)}
    for key, value in data.items():
        f = known.get(key)
        if f is None:
            if strict:
                raise ConfigError(f"unknown setting {name}.{key}")
            log.warning("ignoring unknown setting %s.%s", name, key)
            continue
        try:
            setattr(obj, key, _coerce(name, f, value))
        except ConfigError as exc:
            if strict:
                raise
            log.warning("%s — using default %r", exc, _default_of(f))
            setattr(obj, key, _default_of(f))
    return obj


def from_dict(data: dict[str, Any], strict: bool = False) -> Config:
    """Build a Config from nested dicts. Non-strict mode never raises."""
    cfg = Config()
    for name, section in data.items():
        if name not in _SECTION_TYPES:
            if strict:
                raise ConfigError(f"unknown section {name!r}")
            log.warning("ignoring unknown section [%s]", name)
            continue
        if not isinstance(section, dict):
            if strict:
                raise ConfigError(f"section {name!r} must be a table")
            continue
        setattr(cfg, name, _section_from_dict(name, section, strict))
    return cfg


def to_dict(cfg: Config) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in _SECTION_TYPES:
        section = getattr(cfg, name)
        values: dict[str, Any] = {}
        for f in fields(section):
            value = getattr(section, f.name)
            if f.name == "replacements":
                value = [{"spoken": r.spoken, "written": r.written} for r in value]
            values[f.name] = value
        out[name] = values
    return out


def apply_patch(cfg: Config, patch: dict[str, Any]) -> Config:
    """Return a new Config with ``patch`` (nested, partial) applied.

    Raises ConfigError on unknown keys or invalid values; ``cfg`` is untouched.
    """
    if not isinstance(patch, dict):
        raise ConfigError("patch must be an object")
    merged = to_dict(cfg)
    for name, section in patch.items():
        if name not in _SECTION_TYPES:
            raise ConfigError(f"unknown section {name!r}")
        if not isinstance(section, dict):
            raise ConfigError(f"section {name!r} must be an object")
        merged[name].update(section)
    return from_dict(merged, strict=True)


def diff_sections(old: Config, new: Config) -> set[str]:
    """Names of the sections whose content changed (used to react to patches)."""
    a, b = to_dict(old), to_dict(new)
    return {name for name in _SECTION_TYPES if a[name] != b[name]}


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        # JSON string escapes are a subset of TOML basic-string escapes; DEL is
        # the one control character JSON leaves raw but TOML forbids.
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    raise TypeError(f"cannot serialise {value!r}")


def dumps(cfg: Config) -> str:
    lines = ["# Greffier configuration — edited by the application.",
             "# Hand edits are fine while the service is stopped.", ""]
    for name, values in to_dict(cfg).items():
        tables = {k: v for k, v in values.items() if isinstance(v, list)}
        lines.append(f"[{name}]")
        for key, value in values.items():
            if key not in tables:
                lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
        for key, items in tables.items():
            for item in items:
                lines.append(f"[[{name}.{key}]]")
                lines.extend(f"{k} = {_toml_value(v)}" for k, v in item.items())
                lines.append("")
    return "\n".join(lines)


def load(path: Path | None = None) -> Config:
    path = path or paths.config_file()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Config()
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        log.error("cannot read %s (%s); using defaults", path, exc)
        return Config()
    return from_dict(data)


def save(cfg: Config, path: Path | None = None) -> None:
    """Atomically write the configuration (write to temp file, then rename)."""
    path = path or paths.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(dumps(cfg))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
