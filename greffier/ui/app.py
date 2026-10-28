"""``greffier``: the graphical interface (entry point ``greffier.ui.app:main``)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from greffier.const import APP_ID, APP_NAME, PROJECT_URL, VERSION
from greffier.i18n import _
from greffier.ui.client import DaemonClient
from greffier.ui.diagnostics import format_report
from greffier.ui.util import accel_is_valid
from greffier.ui.window import MainWindow

log = logging.getLogger("greffier.ui")

UI_DIR = Path(__file__).resolve().parent
SOURCE_ICONS = UI_DIR.parents[1] / "data" / "icons"  # running from a source checkout


class GreffierApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.client: DaemonClient | None = None
        self.window: MainWindow | None = None
        self.add_main_option("preferences", ord("p"), GLib.OptionFlags.NONE,
                             GLib.OptionArg.NONE, _("Open the preferences"), None)
        self.add_main_option("models", ord("m"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Open the speech model manager"), None)
        self.add_main_option("version", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             _("Show the version and exit"), None)

    # ------------------------------------------------------------------ #

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        if options.contains("version"):
            print(f"{APP_NAME} {VERSION}")
            return 0
        return -1

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        GLib.set_application_name(APP_NAME)
        Gtk.Window.set_default_icon_name(APP_ID)
        display = Gdk.Display.get_default()
        if display is not None:
            css = Gtk.CssProvider()
            css.load_from_path(str(UI_DIR / "style.css"))
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            if SOURCE_ICONS.is_dir():
                theme = Gtk.IconTheme.get_for_display(display)
                theme.add_search_path(str(SOURCE_ICONS))
                for sub in ("hicolor/scalable/apps", "hicolor/symbolic/apps"):
                    theme.add_search_path(str(SOURCE_ICONS / sub))

        for name, callback, accels in (
            ("preferences", lambda *_a: self._present().show_preferences(), ["<Control>comma"]),
            ("shortcuts", self._show_shortcuts, ["<Control>question"]),
            ("about", self._show_about, []),
            ("quit", lambda *_a: self.quit(), ["<Control>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.set_accels_for_action("window.close", ["<Control>w"])

        self.client = DaemonClient()
        self.client.connect_to_service()

    def do_activate(self) -> None:
        self._present()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict().end().unpack()
        window = self._present()
        if options.get("models"):
            window.show_preferences("models")
        elif options.get("preferences"):
            window.show_preferences()
        return 0

    def _present(self) -> MainWindow:
        if self.window is None:
            self.window = MainWindow(self, self.client)
            self.window.connect("destroy", self._on_window_destroyed)
        self.window.present()
        return self.window

    def _on_window_destroyed(self, *_args) -> None:
        self.window = None

    # ------------------------------------------------------------------ #
    # Dialogs
    # ------------------------------------------------------------------ #

    def _shortcut_items(self) -> list[tuple[str, list[tuple[str, str]]]]:
        client = self.client
        accel = client.shortcut if client else ""
        ptt = client is not None and client.get("shortcut", "mode") == "push-to-talk"
        dictation: list[tuple[str, str]] = []
        if accel_is_valid(accel):
            dictation.append((_("Dictate while held, in any application") if ptt else
                              _("Start or stop dictation, in any application"), accel))
        dictation.append((_("Cancel dictation"), "Escape"))
        general = [
            (_("Preferences"), "<Control>comma"),
            (_("Keyboard shortcuts"), "<Control>question"),
            (_("Close window"), "<Control>w"),
            (_("Quit"), "<Control>q"),
        ]
        return [(_("Dictation"), dictation), (_("General"), general)]

    def _show_shortcuts(self, *_args) -> None:
        window = self._present()
        groups = self._shortcut_items()
        if hasattr(Adw, "ShortcutsDialog"):  # libadwaita >= 1.8
            dialog = Adw.ShortcutsDialog()
            for title, items in groups:
                section = Adw.ShortcutsSection(title=title)
                for label, accel in items:
                    section.add(Adw.ShortcutsItem(title=label, accelerator=accel))
                dialog.add(section)
            dialog.present(window)
            return
        shortcuts = Gtk.ShortcutsWindow(transient_for=window, modal=True)
        section = Gtk.ShortcutsSection(section_name="main", visible=True)
        for title, items in groups:
            group = Gtk.ShortcutsGroup(title=title)
            for label, accel in items:
                group.add_shortcut(Gtk.ShortcutsShortcut(title=label, accelerator=accel))
            section.add_group(group)
        shortcuts.add_section(section)
        shortcuts.present()

    def _show_about(self, *_args) -> None:
        window = self._present()
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name=_("The Greffier Contributors"),
            version=VERSION,
            website=PROJECT_URL,
            issue_url=f"{PROJECT_URL}/issues",
            license_type=Gtk.License.GPL_3_0,
            copyright=_("© 2026 The Greffier Contributors"),
            comments=_("Local and private voice dictation for GNOME. Speech recognition runs "
                       "entirely on your computer."),
        )
        credits = _("translator-credits")
        if credits != "translator-credits":
            about.set_translator_credits(credits)
        about.add_credit_section(_("Speech Recognition"), [
            "sherpa-onnx https://github.com/k2-fsa/sherpa-onnx",
            "Silero VAD https://github.com/snakers4/silero-vad",
        ])
        client = self.client
        models = [m for m in (client.asr_models() if client else []) if m.get("homepage")]
        if models:
            about.add_acknowledgement_section(_("Speech Models"), [
                f"{m.get('name')} ({m.get('vendor')}, {m.get('license')}) {m.get('homepage')}"
                for m in models])

        def present(report: Any = None) -> None:
            about.set_debug_info(format_report(report if isinstance(report, dict) else None,
                                               client.status if client else None))
            about.set_debug_info_filename("greffier-diagnostics.txt")
            about.present(window)

        if client is not None and client.connected:
            client.get_diagnostics(present, on_error=lambda _err: present())
        else:
            present()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("GREFFIER_DEBUG") else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s")
    GLib.set_prgname(APP_ID)
    app = GreffierApplication()
    return app.run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
