"""Preferences dialog: every setting of greffier/config.py, applied live.

Pages: General, Dictation (shortcut and recording), Models, Text (insertion
and processing) and System (diagnostics). Each change is a minimal SetConfig
patch (see binding.py); ConfigChanged updates the widgets while the dialog is
open.
"""

from __future__ import annotations

from typing import Any

from gi.repository import Adw, GLib, Gtk

from greffier.config import ShortcutConfig
from greffier.i18n import _
from greffier.ui.binding import ConfigBinder, Connections
from greffier.ui.client import DaemonClient
from greffier.ui.diagnostics import SystemPage
from greffier.ui.models_page import ModelsPage
from greffier.ui.shortcut_dialog import ShortcutDialog
from greffier.ui.util import accel_label, button_row, shortcut_label, show_toast

DEFAULT_ACCELERATOR = ShortcutConfig().accelerator

# Spoken commands of greffier/text/commands.py (FR_COMMANDS, EN_COMMANDS), shown
# in a help popover; keep in sync with the post-processing rules.
VOICE_COMMANDS: list[tuple[str, str]] = [
    ("« à la ligne », « nouvelle ligne »", "↵"),
    ("« point à la ligne »", ". ↵"),
    ("« nouveau paragraphe »", "↵ ↵"),
    ("« point d’interrogation »", "?"),
    ("« point d’exclamation »", "!"),
    ("« point-virgule »", ";"),
    ("« deux-points »", ":"),
    ("« ouvrez / fermez les guillemets »", "« »"),
    ("« ouvrez / fermez la parenthèse »", "( )"),
    ("“new line”", "↵"),
    ("“new paragraph”", "↵ ↵"),
]


def _radio_row(title: str, subtitle: str, group: Gtk.CheckButton | None = None
               ) -> tuple[Adw.ActionRow, Gtk.CheckButton]:
    check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
    if group is not None:
        check.set_group(group)
    row = Adw.ActionRow(title=title, subtitle=subtitle, activatable_widget=check)
    row.add_prefix(check)
    return row, check


def _gnome_major(version: str) -> int:
    try:
        return int(str(version).split(".")[0])
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# General
# --------------------------------------------------------------------------- #


class GeneralPage(Adw.PreferencesPage):
    __gtype_name__ = "GreffierGeneralPage"

    def __init__(self, client: DaemonClient, binder: ConfigBinder) -> None:
        super().__init__(title=_("General"), icon_name="preferences-system-symbolic",
                         name="general")
        self.client = client

        startup = Adw.PreferencesGroup(title=_("Background Service"))
        startup.add(binder.switch(Adw.SwitchRow(
            title=_("Start at Login"),
            subtitle=_("Keep dictation available as soon as you log in")),
            "general", "autostart"))
        self.add(startup)

        feedback = Adw.PreferencesGroup(title=_("Feedback"))
        feedback.add(binder.switch(Adw.SwitchRow(
            title=_("Sounds"), subtitle=_("Play a sound when recording starts and stops")),
            "general", "sounds"))
        feedback.add(binder.switch(Adw.SwitchRow(
            title=_("Notifications"), subtitle=_("Errors, finished downloads and updates")),
            "general", "notifications"))
        each = binder.switch(Adw.SwitchRow(
            title=_("Show Each Transcription"),
            subtitle=_("Also display the dictated text in a notification")),
            "general", "notify_transcription")
        feedback.add(each)
        binder.on_sync(lambda: each.set_sensitive(bool(client.get("general", "notifications",
                                                                  True))))
        self.add(feedback)

        history = Adw.PreferencesGroup(
            title=_("History"),
            description=_("Recent dictations are listed in the main window so that you can "
                          "copy them again."))
        size = Adw.SpinRow.new_with_range(0, 1000, 10)
        size.set_title(_("Dictations to Keep"))
        size.set_subtitle(_("0 turns the history off"))
        history.add(binder.spin(size, "general", "history_size"))
        persist = binder.switch(Adw.SwitchRow(
            title=_("Keep After Restart"),
            subtitle=_("Save recent dictations on disk. Off by default for privacy")),
            "general", "persist_history")
        history.add(persist)
        binder.on_sync(lambda: persist.set_sensitive(bool(client.get("general", "history_size",
                                                                     1))))
        self.add(history)

        clear_group = Adw.PreferencesGroup()
        clear = button_row(_("_Clear History"), "edit-clear-all-symbolic", self._clear)
        clear.add_css_class("destructive-action")
        clear_group.add(clear)
        self.add(clear_group)

    def _clear(self) -> None:
        dialog = Adw.AlertDialog(heading=_("Clear History?"),
                                 body=_("Your recent dictations will be permanently deleted."))
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("clear", _("_Clear"))
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response == "clear":
                self.client.clear_history(
                    on_done=lambda _r: show_toast(self, _("History cleared")))

        dialog.connect("response", on_response)
        dialog.present(self.get_root())


# --------------------------------------------------------------------------- #
# Dictation (shortcut + recording)
# --------------------------------------------------------------------------- #


class DictationPage(Adw.PreferencesPage):
    __gtype_name__ = "GreffierDictationPage"

    def __init__(self, client: DaemonClient, binder: ConfigBinder,
                 connections: Connections) -> None:
        super().__init__(title=_("Dictation"), icon_name="audio-input-microphone-symbolic",
                         name="dictation")
        self.client = client
        self.binder = binder
        self._ptt_supported: bool | None = None

        # -- shortcut ------------------------------------------------------- #
        shortcut = Adw.PreferencesGroup(title=_("Keyboard Shortcut"))
        self._accel_row = Adw.ActionRow(title=_("Start and Stop Dictation"), activatable=True)
        self._accel_row.connect("activated", lambda *_a: self._change_shortcut())
        self._keys = shortcut_label("")
        self._reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text=_("Reset to Default"))
        self._reset.add_css_class("flat")
        self._reset.add_css_class("circular")
        self._reset.connect("clicked", lambda *_a: self._set_accelerator(DEFAULT_ACCELERATOR))
        change = Gtk.Button(label=_("Change…"), valign=Gtk.Align.CENTER)
        change.connect("clicked", lambda *_a: self._change_shortcut())
        self._accel_row.add_suffix(self._keys)
        self._accel_row.add_suffix(self._reset)
        self._accel_row.add_suffix(change)
        shortcut.add(self._accel_row)

        self._shortcut_warning = Adw.ActionRow(visible=False)
        self._shortcut_warning.add_css_class("warning-row")
        warn = Gtk.Image(icon_name="dialog-warning-symbolic", valign=Gtk.Align.CENTER)
        warn.add_css_class("warning")
        self._shortcut_warning.add_prefix(warn)
        shortcut.add(self._shortcut_warning)
        self.add(shortcut)

        mode = Adw.PreferencesGroup(title=_("Mode"))
        self._toggle_row, self._toggle = _radio_row(
            _("Toggle"), _("Press the shortcut to start, press it again to stop"))
        self._ptt_row, self._ptt = _radio_row(
            _("Push to Talk"), _("Hold the shortcut while you speak. Requires GNOME 48 or "
                                 "later"), self._toggle)
        self._toggle.connect("toggled", self._on_mode_toggled)
        mode.add(self._toggle_row)
        mode.add(self._ptt_row)
        self.add(mode)

        # -- recording ------------------------------------------------------ #
        recording = Adw.PreferencesGroup(title=_("Recording"))
        self._device = Adw.ComboRow(title=_("Microphone"),
                                    model=Gtk.StringList.new([_("System Default")]))
        binder.combo(self._device, "recording", "device", [""])
        recording.add(self._device)

        silence = Adw.ExpanderRow(title=_("Stop on Silence"), show_enable_switch=True,
                                  subtitle=_("Stop recording automatically when you stop "
                                             "speaking"))
        binder.expander(silence, "recording", "auto_stop")
        delay = Adw.SpinRow.new_with_range(0.5, 10.0, 0.5)
        delay.set_digits(1)
        delay.set_title(_("Silence Delay"))
        delay.set_subtitle(_("Seconds of silence before stopping"))
        binder.spin(delay, "recording", "silence_timeout", float)
        silence.add_row(delay)
        recording.add(silence)

        duration = Adw.SpinRow.new_with_range(10, 3600, 10)
        duration.set_title(_("Maximum Duration"))
        duration.set_subtitle(_("Seconds before recording stops in any case"))
        recording.add(binder.spin(duration, "recording", "max_duration"))

        recording.add(binder.switch(Adw.SwitchRow(
            title=_("Insert While Speaking"),
            subtitle=_("Insert the text at each pause instead of when you stop")),
            "recording", "live"))
        self.add(recording)

        # -- advanced ------------------------------------------------------- #
        advanced = Adw.PreferencesGroup(title=_("Advanced"))
        self._backend = Adw.ComboRow(
            title=_("Shortcut Method"),
            model=Gtk.StringList.new([_("Automatic"), _("GNOME Custom Shortcut"),
                                      _("Global Shortcuts Portal"), _("None")]))
        binder.combo(self._backend, "shortcut", "backend", ["auto", "gnome", "portal", "none"])
        advanced.add(self._backend)
        self.add(advanced)

        binder.on_sync(self._sync)
        connections.connect(client, "status-changed", lambda *_a: self._sync_status())
        connections.connect(client, "notify::connected", lambda *_a: self._load())
        self._load()
        self._sync_status()

    def _load(self) -> None:
        """Data that only the running service knows (microphones, GNOME version)."""
        if not self.client.connected:
            return
        self.client.list_audio_devices(self._on_devices)
        self.client.get_diagnostics(self._on_diagnostics, on_error=lambda _e: None)

    # ------------------------------------------------------------------ #

    def _sync(self) -> None:
        if self.client.config is None:
            return
        accel = self.client.shortcut
        self._keys.set_accelerator(accel if accel else "")
        self._reset.set_visible(accel != DEFAULT_ACCELERATOR)
        ptt = self.client.get("shortcut", "mode", "toggle") == "push-to-talk"
        self._accel_row.set_title(_("Hold to Dictate") if ptt else _("Start and Stop Dictation"))
        self._accel_row.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                                        [accel_label(accel)])
        check = self._ptt if ptt else self._toggle
        if not check.get_active():
            check.set_active(True)
        self._ptt_row.set_sensitive(self._ptt_supported is not False or ptt)
        backend = self.client.get("shortcut", "backend", "auto")
        self._backend.set_subtitle({
            "auto": _("A GNOME custom shortcut for toggle mode, the portal for push to talk"),
            "gnome": _("Listed in GNOME Settings › Keyboard › Custom Shortcuts"),
            "portal": _("Requires GNOME 48 or later"),
            "none": _("Bind “greffierctl toggle” to a key yourself"),
        }.get(backend, ""))

    def _sync_status(self) -> None:
        status = self.client.status or {}
        shortcut = status.get("shortcut") or {}
        problem = bool(self.client.connected and shortcut and not shortcut.get("active", True)
                       and self.client.get("shortcut", "backend") != "none"
                       and self.client.shortcut)
        self._shortcut_warning.set_visible(problem)
        if problem:
            self._shortcut_warning.set_title(shortcut.get("message")
                                             or _("The shortcut is not active"))

    def _on_mode_toggled(self, *_args) -> None:
        mode = "toggle" if self._toggle.get_active() else "push-to-talk"
        if self.client.config is None or mode == self.client.get("shortcut", "mode"):
            return
        self.client.set_config({"shortcut": {"mode": mode}},
                               on_error=lambda err: (show_toast(self, err.message, urgent=True),
                                                     self._sync()))

    def _change_shortcut(self) -> None:
        ptt = self.client.get("shortcut", "mode") == "push-to-talk"
        dialog = ShortcutDialog(self.client.shortcut, push_to_talk=ptt)
        dialog.connect("chosen", lambda _d, accel: self._set_accelerator(accel))
        dialog.present(self.get_root())

    def _set_accelerator(self, accel: str) -> None:
        def done(_config) -> None:
            if not accel:
                show_toast(self, _("Shortcut disabled"))

        self.client.set_config({"shortcut": {"accelerator": accel}}, on_done=done,
                               on_error=lambda err: show_toast(self, err.message, urgent=True))

    def _on_devices(self, devices: Any) -> None:
        if not isinstance(devices, list):
            return
        values, labels = [""], [_("System Default")]
        for device in devices:
            if isinstance(device, dict) and device.get("id"):
                values.append(str(device["id"]))
                labels.append(str(device.get("name") or device["id"]))
        current = self.client.get("recording", "device", "")
        if current and current not in values:
            values.append(current)
            labels.append(_("Unavailable Microphone"))
        self.binder.set_combo_values(self._device, values, labels)

    def _on_diagnostics(self, report: Any) -> None:
        if not isinstance(report, dict):
            return
        portals = report.get("portals") or {}
        major = _gnome_major(report.get("gnome_shell", ""))
        if "global_shortcuts" in portals:
            self._ptt_supported = bool(portals.get("global_shortcuts"))
        elif major:
            self._ptt_supported = major >= 48
        if self._ptt_supported is False:
            self._ptt_row.set_subtitle(_("Hold the shortcut while you speak. Not available: "
                                         "requires GNOME 48 or later"))
        self._sync()


# --------------------------------------------------------------------------- #
# Text
# --------------------------------------------------------------------------- #


class TextPage(Adw.PreferencesPage):
    __gtype_name__ = "GreffierTextPage"

    METHODS = ["auto", "paste", "type", "clipboard"]

    def __init__(self, client: DaemonClient, binder: ConfigBinder,
                 connections: Connections) -> None:
        super().__init__(title=_("Text"), icon_name="document-edit-symbolic", name="text")
        self.client = client

        # -- insertion ------------------------------------------------------ #
        insertion = Adw.PreferencesGroup(
            title=_("Insertion"),
            description=_("How the dictated text reaches the focused application."))
        self._method = Adw.ComboRow(
            title=_("Method"),
            model=Gtk.StringList.new([_("Automatic"), _("Paste"), _("Type"),
                                      _("Copy Only")]))
        binder.combo(self._method, "injection", "method", self.METHODS)
        insertion.add(self._method)

        self._paste = Adw.ComboRow(
            title=_("Paste Shortcut"),
            subtitle=_("Automatic uses Ctrl+Shift+V in terminals"),
            model=Gtk.StringList.new([_("Automatic"), accel_label("<Control>v"),
                                      accel_label("<Control><Shift>v"),
                                      accel_label("<Shift>Insert")]))
        binder.combo(self._paste, "injection", "paste_shortcut",
                     ["auto", "ctrl-v", "ctrl-shift-v", "shift-insert"])
        insertion.add(self._paste)

        self._keyboard = Adw.ComboRow(
            title=_("Keyboard Backend"),
            model=Gtk.StringList.new([_("Automatic"), _("GNOME Shell Extension"),
                                      _("Remote Desktop Portal"), "ydotool"]))
        binder.combo(self._keyboard, "injection", "backend",
                     ["auto", "extension", "portal", "ydotool"])
        insertion.add(self._keyboard)

        self._restore = binder.switch(Adw.SwitchRow(
            title=_("Restore Clipboard"),
            subtitle=_("Put back what the clipboard held before pasting")),
            "injection", "restore_clipboard")
        insertion.add(self._restore)
        insertion.add(binder.switch(Adw.SwitchRow(
            title=_("Add a Space After Each Dictation"),
            subtitle=_("Keeps consecutive dictations apart")),
            "injection", "trailing_space"))
        self.add(insertion)

        # -- processing ----------------------------------------------------- #
        processing = Adw.PreferencesGroup(title=_("Processing"))
        commands = Adw.ActionRow(title=_("Voice Commands"),
                                 subtitle=_("Say “à la ligne” or “nouveau paragraphe” to "
                                            "format the text"))
        help_button = Gtk.MenuButton(icon_name="help-about-symbolic", valign=Gtk.Align.CENTER,
                                     tooltip_text=_("List of Voice Commands"),
                                     popover=self._commands_popover())
        help_button.add_css_class("flat")
        switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        commands.add_suffix(help_button)
        commands.add_suffix(switch)
        commands.set_activatable_widget(switch)
        binder.switch(switch, "text", "voice_commands")
        processing.add(commands)
        processing.add(binder.switch(Adw.SwitchRow(
            title=_("French Typography"),
            subtitle=_("Narrow no-break spaces before ; : ! ? and inside « »")),
            "text", "french_typography"))
        self.add(processing)

        # -- replacements --------------------------------------------------- #
        self._replacements = Adw.PreferencesGroup(
            title=_("Replacements"),
            description=_("Fix words the model often gets wrong, or expand your own "
                          "abbreviations."))
        add = Gtk.Button(valign=Gtk.Align.CENTER)
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label=_("_Add"),
                                        use_underline=True))
        add.add_css_class("flat")
        add.connect("clicked", lambda *_a: self._edit(None))
        self._replacements.set_header_suffix(add)
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        empty = Gtk.Label(label=_("No replacements"), margin_top=18, margin_bottom=18)
        empty.add_css_class("dim-label")
        self._list.set_placeholder(empty)
        self._replacements.add(self._list)
        self.add(self._replacements)

        binder.on_sync(self._sync)
        connections.connect(client, "status-changed", lambda *_a: self._sync())

    # ------------------------------------------------------------------ #

    def _commands_popover(self) -> Gtk.Popover:
        grid = Gtk.Grid(column_spacing=18, row_spacing=6, margin_top=12, margin_bottom=12,
                        margin_start=12, margin_end=12)
        title = Gtk.Label(label=_("Voice Commands"), xalign=0)
        title.add_css_class("heading")
        grid.attach(title, 0, 0, 2, 1)
        note = Gtk.Label(label=_("Say them on their own, between two sentences:"), xalign=0,
                         wrap=True, max_width_chars=36)
        note.add_css_class("dim-label")
        grid.attach(note, 0, 1, 2, 1)
        for i, (spoken, result) in enumerate(VOICE_COMMANDS, start=2):
            said = Gtk.Label(label=spoken, xalign=0, wrap=True, max_width_chars=30)
            out = Gtk.Label(label=result, xalign=0)
            out.add_css_class("monospace")
            grid.attach(said, 0, i, 1, 1)
            grid.attach(out, 1, i, 1, 1)
        popover = Gtk.Popover(child=grid)
        return popover

    def _sync(self) -> None:
        method = self.client.get("injection", "method", "auto")
        self._method.set_subtitle({
            "auto": _("Pastes the text: works with accents and any layout"),
            "paste": _("Copies, presses the paste shortcut, restores the clipboard"),
            "type": _("Types key by key; some characters may be skipped"),
            "clipboard": _("Only copies the text; paste it yourself"),
        }.get(method, ""))
        pasting = method in ("auto", "paste")
        self._paste.set_sensitive(pasting)
        self._restore.set_sensitive(pasting)
        self._keyboard.set_sensitive(method != "clipboard")
        injection = (self.client.status or {}).get("injection") or {}
        in_use = {
            "extension": _("GNOME Shell extension"), "portal": _("Remote Desktop portal"),
            "ydotool": "ydotool", "none": _("none available"),
        }.get(injection.get("keyboard", ""), injection.get("keyboard", ""))
        self._keyboard.set_subtitle(_("Currently in use: {backend}").format(backend=in_use)
                                    if in_use else _("Which component presses the keys"))
        self._populate_replacements()

    def _items(self) -> list[dict[str, str]]:
        items = self.client.get("text", "replacements", []) or []
        return [{"spoken": str(i.get("spoken", "")), "written": str(i.get("written", ""))}
                for i in items if isinstance(i, dict)]

    def _populate_replacements(self) -> None:
        items = self._items()
        key = repr(items)
        if getattr(self, "_shown", None) == key:
            return
        self._shown = key
        child = self._list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list.remove(child)
            child = nxt
        for index, item in enumerate(items):
            row = Adw.ActionRow(title=GLib.markup_escape_text(item["spoken"]),
                                subtitle="→ " + GLib.markup_escape_text(item["written"] or "∅"))
            row.set_title_lines(2)
            arrow = Gtk.Image(icon_name="go-next-symbolic", valign=Gtk.Align.CENTER)
            arrow.add_css_class("dim-label")
            edit = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text=_("Edit"))
            edit.add_css_class("flat")
            edit.connect("clicked", lambda _b, i=index: self._edit(i))
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text=_("Delete"))
            delete.add_css_class("flat")
            delete.connect("clicked", lambda _b, i=index: self._delete(i))
            row.add_suffix(edit)
            row.add_suffix(delete)
            row.set_activatable_widget(edit)
            self._list.append(row)

    def _save(self, items: list[dict[str, str]], message: str | None = None) -> None:
        def done(_config) -> None:
            if message:
                show_toast(self, message)

        self.client.set_config({"text": {"replacements": items}}, on_done=done,
                               on_error=lambda err: show_toast(self, err.message, urgent=True))

    def _delete(self, index: int) -> None:
        items = self._items()
        if 0 <= index < len(items):
            removed = items.pop(index)
            self._save(items, _("“{spoken}” removed").format(spoken=removed["spoken"]))

    def _edit(self, index: int | None) -> None:
        items = self._items()
        item = items[index] if index is not None and 0 <= index < len(items) else None
        dialog = Adw.AlertDialog(
            heading=_("Edit Replacement") if item else _("Add Replacement"),
            body=_("When the model writes the first text, the second one is inserted "
                   "instead. Matching ignores case and only applies to whole words."))
        fields = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        fields.add_css_class("boxed-list")
        spoken = Adw.EntryRow(title=_("When I Say"), text=item["spoken"] if item else "")
        written = Adw.EntryRow(title=_("Write"), text=item["written"] if item else "")
        fields.append(spoken)
        fields.append(written)
        dialog.set_extra_child(fields)
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("save", _("_Save") if item else _("_Add"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def validate(*_args) -> None:
            dialog.set_response_enabled("save", bool(spoken.get_text().strip()))

        spoken.connect("changed", validate)
        spoken.set_activates_default(True)
        written.set_activates_default(True)
        validate()

        def on_response(_dialog, response: str) -> None:
            if response != "save" or not spoken.get_text().strip():
                return
            new = {"spoken": spoken.get_text().strip(), "written": written.get_text()}
            current = self._items()
            if item is not None and index is not None and index < len(current):
                current[index] = new
            else:
                current.append(new)
            self._save(current)

        dialog.connect("response", on_response)
        dialog.present(self.get_root())
        spoken.grab_focus()


# --------------------------------------------------------------------------- #
# Dialog
# --------------------------------------------------------------------------- #


class PreferencesDialog(Adw.PreferencesDialog):
    __gtype_name__ = "GreffierPreferencesDialog"

    def __init__(self, client: DaemonClient, on_run_setup=None) -> None:
        super().__init__(search_enabled=True)
        self.client = client
        self.connections = Connections()
        self.binder = ConfigBinder(client, self, self.connections)
        self.add(GeneralPage(client, self.binder))
        self.add(DictationPage(client, self.binder, self.connections))
        self.add(ModelsPage(client, self.binder, self.connections))
        self.add(TextPage(client, self.binder, self.connections))
        self.add(SystemPage(client, self.connections, show_page=self.show_page,
                            on_run_setup=on_run_setup))
        self.connect("closed", self._on_closed)
        if client.connected and not client.models:
            client.refresh_models()

    def show_page(self, name: str) -> None:
        self.set_visible_page_name(name)

    def _on_closed(self, *_args) -> None:
        self.binder.flush_pending()
        self.connections.disconnect_all()

