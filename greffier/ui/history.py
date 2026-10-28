""""Recent Dictations" list of the main window (GetHistory / Transcribed)."""

from __future__ import annotations

from typing import Any

from gi.repository import Adw, GLib, GObject, Gtk, Pango

from greffier.i18n import _
from greffier.ui.client import DaemonClient
from greffier.ui.util import (
    copy_text,
    format_clock,
    format_full_time,
    format_relative_time,
    parse_time,
    show_toast,
)

MAX_ROWS = 100
COLLAPSED_LINES = 4


class HistoryRow(Gtk.ListBoxRow):
    __gtype_name__ = "GreffierHistoryRow"

    def __init__(self, entry: dict[str, Any], model_names: dict[str, str]) -> None:
        super().__init__(activatable=True)
        self.add_css_class("history-row")
        self.entry = entry
        self.text = str(entry.get("text", ""))
        self.time = parse_time(str(entry.get("time", "")))
        self._expanded = False

        box = Gtk.Box(spacing=6, margin_top=10, margin_bottom=10, margin_start=12,
                      margin_end=6)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
        self.label = Gtk.Label(label=self.text, xalign=0, wrap=True, lines=COLLAPSED_LINES,
                               ellipsize=Pango.EllipsizeMode.END,
                               natural_wrap_mode=Gtk.NaturalWrapMode.WORD)
        self.label.add_css_class("history-text")
        self.caption = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.caption.add_css_class("caption")
        self.caption.add_css_class("dim-label")
        column.append(self.label)
        column.append(self.caption)

        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                          tooltip_text=_("Copy"))
        copy.add_css_class("flat")
        copy.add_css_class("circular")
        copy.update_property([Gtk.AccessibleProperty.LABEL], [_("Copy Text")])
        copy.connect("clicked", self._on_copy)
        box.append(column)
        box.append(copy)
        self.set_child(box)

        duration = float(entry.get("duration") or 0)
        model = model_names.get(str(entry.get("model", "")), str(entry.get("model", "")))
        details = [format_full_time(self.time)]
        if duration:
            details.append(_("Duration: {duration}").format(duration=format_clock(duration)))
        if model:
            details.append(_("Model: {model}").format(model=model))
        self.set_tooltip_text("\n".join(d for d in details if d))
        self.update_property([Gtk.AccessibleProperty.LABEL], [self.text])
        self.refresh_time()

    def refresh_time(self) -> None:
        parts = [format_relative_time(self.time)]
        duration = float(self.entry.get("duration") or 0)
        if duration:
            parts.append(format_clock(duration))
        self.caption.set_label(" · ".join(p for p in parts if p))

    def toggle_expanded(self) -> None:
        self._expanded = not self._expanded
        self.label.set_lines(-1 if self._expanded else COLLAPSED_LINES)
        self.label.set_ellipsize(Pango.EllipsizeMode.NONE if self._expanded
                                else Pango.EllipsizeMode.END)

    def highlight(self) -> None:
        self.add_css_class("new")
        GLib.timeout_add(1200, lambda: self.remove_css_class("new") or GLib.SOURCE_REMOVE)

    def _on_copy(self, _button) -> None:
        copy_text(self, self.text)
        show_toast(self, _("Copied to clipboard"))


class HistoryGroup(Adw.PreferencesGroup):
    """Boxed list of recent dictations, newest first, with an empty state."""

    __gtype_name__ = "GreffierHistoryGroup"
    __gsignals__ = {"open-preferences": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self, client: DaemonClient) -> None:
        super().__init__(title=_("Recent Dictations"))
        self.client = client
        self._known_newest = ""
        self._pending = 0

        self.clear_button = Gtk.Button(label=_("_Clear"), use_underline=True,
                                       tooltip_text=_("Clear History"), valign=Gtk.Align.CENTER)
        self.clear_button.add_css_class("flat")
        self.clear_button.set_sensitive(False)
        self.clear_button.connect("clicked", self._on_clear_clicked)
        self.set_header_suffix(self.clear_button)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.list.add_css_class("boxed-list")
        self.list.connect("row-activated", lambda _l, row: row.toggle_expanded())
        self._placeholder_title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._placeholder_title.add_css_class("heading")
        self._placeholder_text = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._placeholder_text.add_css_class("dim-label")
        placeholder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                              margin_top=24, margin_bottom=24, margin_start=18, margin_end=18)
        placeholder.add_css_class("history-placeholder")
        icon = Gtk.Image(icon_name="accessories-text-editor-symbolic", pixel_size=32)
        icon.add_css_class("dim-label")
        placeholder.append(icon)
        placeholder.append(self._placeholder_title)
        placeholder.append(self._placeholder_text)
        self.list.set_placeholder(placeholder)
        self.add(self.list)

        client.connect("transcribed", self._on_transcribed)
        client.connect("notify::connected", lambda *_a: self.refresh())
        client.connect("config-changed", lambda *_a: self._update_placeholder())
        GLib.timeout_add_seconds(30, self._on_clock)
        self._update_placeholder()

    # ------------------------------------------------------------------ #

    def refresh(self, highlight: bool = False) -> None:
        if not self.client.connected:
            self.clear_button.set_sensitive(False)
            self._update_placeholder()
            return
        self.client.get_history(lambda items: self._populate(items, highlight))

    def _populate(self, items: Any, highlight: bool) -> None:
        if not isinstance(items, list):
            return
        names = {m.get("id", ""): m.get("name", "") for m in self.client.models}
        child = self.list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            if isinstance(child, HistoryRow):
                self.list.remove(child)
            child = nxt
        rows = [i for i in items if isinstance(i, dict) and i.get("text")][:MAX_ROWS]
        for entry in rows:
            self.list.append(HistoryRow(entry, names))
        newest = f"{rows[0].get('time')}|{rows[0].get('text')}" if rows else ""
        if highlight and rows and newest != self._known_newest:
            first_row = self.list.get_row_at_index(0)
            if isinstance(first_row, HistoryRow):
                first_row.highlight()
        self._known_newest = newest
        self.clear_button.set_sensitive(bool(rows))
        self._update_placeholder()

    def _update_placeholder(self) -> None:
        if self.client.config is not None and not self.client.get("general", "history_size", 1):
            self._placeholder_title.set_label(_("History Is Turned Off"))
            self._placeholder_text.set_label(
                _("Turn it on in Preferences to find your recent dictations here."))
        else:
            self._placeholder_title.set_label(_("No Dictations Yet"))
            self._placeholder_text.set_label(_("Your recent dictations will appear here."))

    def _on_transcribed(self, _client, _text: str, _meta: dict) -> None:
        # Re-read the history from the service (it owns trimming and privacy
        # settings); several segments can arrive quickly in live mode.
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(150, self._on_pending)

    def _on_pending(self) -> bool:
        self._pending = 0
        self.refresh(highlight=True)
        return GLib.SOURCE_REMOVE

    def _on_clock(self) -> bool:
        row = self.list.get_first_child()
        while row is not None:
            if isinstance(row, HistoryRow):
                row.refresh_time()
            row = row.get_next_sibling()
        return GLib.SOURCE_CONTINUE

    def _on_clear_clicked(self, _button) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Clear History?"),
            body=_("Your recent dictations will be permanently deleted."))
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("clear", _("_Clear"))
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response == "clear":
                self.client.clear_history(on_done=lambda _r: self.refresh())

        dialog.connect("response", on_response)
        dialog.present(self.get_root())
