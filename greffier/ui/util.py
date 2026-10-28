"""Helpers shared by the views: formatting and libadwaita/GTK compatibility shims.

The interface targets libadwaita >= 1.5 and GTK >= 4.14 (Ubuntu 24.04,
Fedora 40) while looking its best on newer releases: widgets introduced later
(Adw.Spinner, Adw.ButtonRow, Adw.ShortcutLabel…) are feature-detected here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Graphene, Gsk, Gtk

from greffier.i18n import _, current_language, ngettext

# --------------------------------------------------------------------------- #
# Versions
# --------------------------------------------------------------------------- #


def adw_minor() -> int:
    """libadwaita minor version (the major is 1 for the whole 1.x series)."""
    return Adw.get_minor_version() if Adw.get_major_version() == 1 else 99


def animations_enabled(widget: Gtk.Widget | None = None) -> bool:
    settings = widget.get_settings() if widget is not None else Gtk.Settings.get_default()
    return bool(settings is None or settings.props.gtk_enable_animations)


# --------------------------------------------------------------------------- #
# Languages
# --------------------------------------------------------------------------- #

# Language names written in the language itself (endonyms): they need no
# translation and are what people look for in a language list.
LANGUAGE_NAMES: dict[str, str] = {
    "af": "Afrikaans", "ar": "العربية", "be": "Беларуская", "bg": "Български",
    "bn": "বাংলা", "bs": "Bosanski", "ca": "Català", "cs": "Čeština", "cy": "Cymraeg",
    "da": "Dansk", "de": "Deutsch", "el": "Ελληνικά", "en": "English", "es": "Español",
    "et": "Eesti", "eu": "Euskara", "fa": "فارسی", "fi": "Suomi", "fil": "Filipino",
    "fr": "Français", "ga": "Gaeilge", "gl": "Galego", "he": "עברית", "hi": "हिन्दी",
    "hr": "Hrvatski", "hu": "Magyar", "hy": "Հայերեն", "id": "Bahasa Indonesia",
    "is": "Íslenska", "it": "Italiano", "ja": "日本語", "ka": "ქართული", "kk": "Қазақ",
    "ko": "한국어", "lt": "Lietuvių", "lv": "Latviešu", "mk": "Македонски",
    "ms": "Bahasa Melayu", "mt": "Malti", "nl": "Nederlands", "no": "Norsk",
    "pl": "Polski", "pt": "Português", "ro": "Română", "ru": "Русский",
    "sk": "Slovenčina", "sl": "Slovenščina", "sr": "Српски", "sv": "Svenska",
    "sw": "Kiswahili", "ta": "தமிழ்", "th": "ไทย", "tr": "Türkçe", "uk": "Українська",
    "ur": "اردو", "vi": "Tiếng Việt", "yue": "粵語", "zh": "中文",
}


def ui_language() -> str:
    """Two-letter code of the interface language ("en" when untranslated)."""
    return (current_language() or "en").split("_")[0].split("-")[0]


def language_name(code: str) -> str:
    if code in ("", "auto"):
        return _("Automatic")
    return LANGUAGE_NAMES.get(code, code)


def model_languages(model: dict[str, Any]) -> list[str]:
    """Language codes a model accepts ("*" expands to every known language)."""
    langs = list(model.get("languages") or [])
    if "*" in langs:
        langs = list(LANGUAGE_NAMES)
    return sorted(langs, key=lambda c: GLib.utf8_collate_key(language_name(c).lower(), -1))


def languages_summary(model: dict[str, Any]) -> str:
    """Short description of the languages of a model, for row subtitles."""
    langs = list(model.get("languages") or [])
    if "*" in langs:
        text = _("Multilingual")
    elif len(langs) <= 4:
        text = ", ".join(language_name(c) for c in model_languages(model))
    else:
        text = ngettext("{n} language", "{n} languages", len(langs)).format(n=len(langs))
    if model.get("language_mode") == "auto" and len(langs) != 1:
        # Translators: e.g. "25 languages, detected automatically"
        text = _("{languages}, detected automatically").format(languages=text)
    return text


def describe(model: dict[str, Any]) -> str:
    """Model description in the interface language (falls back to English)."""
    desc = model.get("description") or {}
    if isinstance(desc, str):
        return desc
    return desc.get(ui_language()) or desc.get("en") or next(iter(desc.values()), "")


# --------------------------------------------------------------------------- #
# Numbers, sizes, durations and dates
# --------------------------------------------------------------------------- #

_TRAILING_ZERO = re.compile(r"(\d)[.,]0(?=\D|$)")


def format_size(nbytes: float) -> str:
    """Human size with GLib's localised units ("487 MB", "1.7 GB")."""
    nbytes = max(0, int(nbytes))
    if nbytes >= 1_000_000_000:
        nbytes = round(nbytes / 100_000_000) * 100_000_000
    elif nbytes >= 1_000_000:
        nbytes = round(nbytes / 1_000_000) * 1_000_000
    return _TRAILING_ZERO.sub(r"\1", GLib.format_size(nbytes))


def format_ram(megabytes: float) -> str:
    return format_size(megabytes * 1_000_000)


def format_percent(fraction: float) -> str:
    # Translators: a percentage; French uses "{percent} %"
    return _("{percent}%").format(percent=int(max(0.0, min(1.0, fraction)) * 100))


def format_clock(seconds: float) -> str:
    """Elapsed time as m:ss or h:mm:ss (tabular, locale-neutral)."""
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def parse_time(value: str) -> GLib.DateTime | None:
    """ISO-8601 → local GLib.DateTime (naive strings are read as local time)."""
    if not value:
        return None
    try:
        dt = GLib.DateTime.new_from_iso8601(value, GLib.TimeZone.new_local())
    except (TypeError, GLib.Error):
        return None
    return dt.to_local() if dt is not None else None


def _uses_12h_clock() -> bool:
    source = Gio.SettingsSchemaSource.get_default()
    if source is None or source.lookup("org.gnome.desktop.interface", True) is None:
        return False
    try:
        return Gio.Settings.new("org.gnome.desktop.interface").get_string("clock-format") == "12h"
    except GLib.Error:  # pragma: no cover - broken schema
        return False


def format_time_of_day(dt: GLib.DateTime) -> str:
    # Translators: GLib.DateTime.format() codes, 12-hour and 24-hour clock
    fmt = _("%-l:%M %p") if _uses_12h_clock() else _("%H:%M")
    return dt.format(fmt) or ""


def format_relative_time(dt: GLib.DateTime | None, now: GLib.DateTime | None = None) -> str:
    """"Just now", "5 minutes ago", "14:32", "Yesterday at 14:32", "Oct 21"."""
    if dt is None:
        return ""
    now = now or GLib.DateTime.new_now_local()
    seconds = now.difference(dt) / 1_000_000
    if seconds < 60:
        return _("Just now")
    if seconds < 3600:
        minutes = int(seconds // 60)
        return ngettext("{n} minute ago", "{n} minutes ago", minutes).format(n=minutes)
    today = GLib.DateTime.new_local(now.get_year(), now.get_month(), now.get_day_of_month(),
                                    0, 0, 0)
    if dt.compare(today) >= 0:
        return format_time_of_day(dt)
    if dt.compare(today.add_days(-1)) >= 0:
        return _("Yesterday at {time}").format(time=format_time_of_day(dt))
    if dt.get_year() == now.get_year():
        # Translators: date without year, see GLib.DateTime.format(); French: "%-d %B"
        return dt.format(_("%B %-d")) or ""
    # Translators: date with year, see GLib.DateTime.format(); French: "%-d %B %Y"
    return dt.format(_("%B %-d, %Y")) or ""


def format_full_time(dt: GLib.DateTime | None) -> str:
    if dt is None:
        return ""
    # Translators: full date, see GLib.DateTime.format(); French: "%A %-d %B %Y"
    date = dt.format(_("%A, %B %-d, %Y")) or ""
    return _("{date} at {time}").format(date=date, time=format_time_of_day(dt))


# --------------------------------------------------------------------------- #
# Shortcuts
# --------------------------------------------------------------------------- #


def accel_is_valid(accel: str) -> bool:
    if not accel:
        return False
    ok, key, _mods = Gtk.accelerator_parse(accel)
    return bool(ok and key)


def accel_label(accel: str) -> str:
    """"<Super><Alt>d" → "Super+Alt+D" (localised by GTK)."""
    if not accel_is_valid(accel):
        return _("Disabled")
    _ok, key, mods = Gtk.accelerator_parse(accel)
    return Gtk.accelerator_get_label(key, mods)


def shortcut_label(accel: str = "") -> Gtk.Widget:
    """Keycaps for an accelerator (Adw.ShortcutLabel on libadwaita >= 1.8)."""
    cls = getattr(Adw, "ShortcutLabel", None) or Gtk.ShortcutLabel
    label = cls()
    label.set_disabled_text(_("Disabled"))
    label.set_accelerator(accel if accel_is_valid(accel) else "")
    label.set_valign(Gtk.Align.CENTER)
    return label


def split_template(template: str, placeholder: str) -> tuple[str, str]:
    """Split a translated sentence around a widget placeholder like {shortcut}."""
    before, sep, after = template.partition(placeholder)
    if not sep:
        return template, ""
    return before.rstrip(), after.lstrip()


# --------------------------------------------------------------------------- #
# Widgets
# --------------------------------------------------------------------------- #


class WrapBox(Gtk.Widget):
    """Lays children out like words in a paragraph (stand-in for Adw.WrapBox < 1.7)."""

    __gtype_name__ = "GreffierWrapBox"

    def __init__(self, child_spacing: int = 6, line_spacing: int = 4) -> None:
        super().__init__()
        self._spacing = child_spacing
        self._line_spacing = line_spacing

    def append(self, child: Gtk.Widget) -> None:
        child.set_parent(self)
        self.queue_resize()

    def remove(self, child: Gtk.Widget) -> None:
        if child.get_parent() is self:
            child.unparent()
            self.queue_resize()

    def do_dispose(self) -> None:
        child = self.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            child.unparent()
            child = nxt
        Gtk.Widget.do_dispose(self)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def _children(self) -> list[Gtk.Widget]:
        out, child = [], self.get_first_child()
        while child is not None:
            if child.get_visible():
                out.append(child)
            child = child.get_next_sibling()
        return out

    def _lines(self, width: int) -> list[list[tuple[Gtk.Widget, int, int]]]:
        """[(child, width, height)] per line for the given available width."""
        lines: list[list[tuple[Gtk.Widget, int, int]]] = [[]]
        used = 0
        for child in self._children():
            minimum, natural, _b1, _b2 = child.measure(Gtk.Orientation.HORIZONTAL, -1)
            w = max(minimum, min(natural, width))
            if lines[-1] and used + self._spacing + w > width:
                lines.append([])
                used = 0
            used += (self._spacing if lines[-1] else 0) + w
            h = child.measure(Gtk.Orientation.VERTICAL, w)[1]
            lines[-1].append((child, w, h))
        return [line for line in lines if line]

    def do_measure(self, orientation: Gtk.Orientation, for_size: int):
        children = self._children()
        if not children:
            return 0, 0, -1, -1
        if orientation == Gtk.Orientation.HORIZONTAL:
            sizes = [c.measure(orientation, -1) for c in children]
            minimum = max(size[0] for size in sizes)
            natural = sum(size[1] for size in sizes) + self._spacing * (len(sizes) - 1)
            return minimum, natural, -1, -1
        if for_size < 0:
            for_size = self.do_measure(Gtk.Orientation.HORIZONTAL, -1)[1]
        lines = self._lines(for_size)
        height = sum(max(h for _c, _w, h in line) for line in lines)
        height += self._line_spacing * (len(lines) - 1)
        return height, height, -1, -1

    def do_size_allocate(self, width: int, height: int, _baseline: int) -> None:
        rtl = self.get_direction() == Gtk.TextDirection.RTL
        y = 0
        for line in self._lines(width):
            line_height = max(h for _c, _w, h in line)
            x = 0
            for child, w, _h in line:
                left = width - x - w if rtl else x
                transform = Gsk.Transform().translate(Graphene.Point().init(left, y))
                child.allocate(w, line_height, -1, transform)
                x += w + self._spacing
            y += line_height + self._line_spacing


def wrap_box(child_spacing: int = 6, line_spacing: int = 4) -> Gtk.Widget:
    """Container that wraps its children like words (Adw.WrapBox on libadwaita >= 1.7)."""
    if hasattr(Adw, "WrapBox"):
        return Adw.WrapBox(child_spacing=child_spacing, line_spacing=line_spacing)
    return WrapBox(child_spacing, line_spacing)


def wrap_box_append(box: Gtk.Widget, widget: Gtk.Widget) -> None:
    box.append(widget)


def spinner() -> Gtk.Widget:
    """Indeterminate spinner (Adw.Spinner on libadwaita >= 1.6)."""
    if hasattr(Adw, "Spinner"):
        return Adw.Spinner()
    return Gtk.Spinner(spinning=True)


class _CompatButtonRow(Gtk.ListBoxRow):
    """Minimal stand-in for Adw.ButtonRow (libadwaita < 1.6)."""

    __gtype_name__ = "GreffierCompatButtonRow"
    __gsignals__ = {"activated": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, title: str, icon_name: str | None) -> None:
        super().__init__(activatable=True)
        self.add_css_class("button-row-compat")
        box = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        if icon_name:
            box.append(Gtk.Image(icon_name=icon_name))
        self._label = Gtk.Label(label=title, use_underline=True)
        box.append(self._label)
        self.set_child(box)
        self._list = None
        self._handler = 0
        self.connect("notify::parent", self._on_parent)

    def set_title(self, title: str) -> None:
        self._label.set_label(title)

    def _on_parent(self, *_args) -> None:
        if self._list is not None and self._handler:
            self._list.disconnect(self._handler)
        self._list, self._handler = self.get_parent(), 0
        if isinstance(self._list, Gtk.ListBox):
            self._handler = self._list.connect(
                "row-activated", lambda _box, row: row is self and self.emit("activated"))


def button_row(title: str, icon_name: str | None = None,
               on_activated: Callable[[], None] | None = None) -> Gtk.ListBoxRow:
    """A centred, clickable row in a boxed list (emits "activated")."""
    if hasattr(Adw, "ButtonRow"):
        row = Adw.ButtonRow(title=title, use_underline=True)
        if icon_name:
            row.set_start_icon_name(icon_name)
    else:
        row = _CompatButtonRow(title, icon_name)
    if on_activated is not None:
        row.connect("activated", lambda *_a: on_activated())
    return row


def status_icon(level: str) -> Gtk.Image:
    """Icon for a check result: ok / warning / error."""
    names = {
        "ok": ("emblem-ok-symbolic", "success", _("OK")),
        "warning": ("dialog-warning-symbolic", "warning", _("Warning")),
        "error": ("dialog-error-symbolic", "error", _("Error")),
    }
    icon, css, label = names.get(level, names["warning"])
    image = Gtk.Image(icon_name=icon, valign=Gtk.Align.CENTER)
    image.add_css_class(css)
    image.update_property([Gtk.AccessibleProperty.LABEL], [label])
    return image


def badge(text: str, style: str = "accent") -> Gtk.Label:
    """Small pill label (Recommended, Installed, Update Available…)."""
    label = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
    label.add_css_class("badge")
    label.add_css_class(style)
    return label


def clear_box(box: Gtk.Box | Gtk.ListBox) -> None:
    """Remove every child of a Gtk.Box or Gtk.ListBox."""
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def copy_text(widget: Gtk.Widget, text: str) -> None:
    widget.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(text))


def open_uri(widget: Gtk.Widget, uri: str) -> None:
    root = widget.get_root()
    Gtk.UriLauncher.new(uri).launch(root if isinstance(root, Gtk.Window) else None, None, None)


def show_toast(widget: Gtk.Widget, message: str, *, urgent: bool = False,
               timeout: int | None = None) -> None:
    """Toast in the enclosing preferences dialog, or else in the main window."""
    toast = Adw.Toast.new(message)
    toast.set_use_markup(False)
    if urgent:
        toast.set_priority(Adw.ToastPriority.HIGH)
    if timeout is not None:
        toast.set_timeout(timeout)
    dialog = widget.get_ancestor(Adw.PreferencesDialog)
    if dialog is not None:
        dialog.add_toast(toast)
        return
    root = widget.get_root()
    if hasattr(root, "add_toast"):
        root.add_toast(toast)


def first(items: Iterable[Any], predicate: Callable[[Any], bool]) -> Any:
    return next((item for item in items if predicate(item)), None)
