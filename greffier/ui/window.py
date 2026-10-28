"""Main window: microphone hero, active model, recent dictations, onboarding."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from gi.repository import Adw, Gio, GLib, Gtk

from greffier import paths
from greffier.const import APP_NAME, State
from greffier.i18n import _
from greffier.ui.client import DaemonClient
from greffier.ui.history import HistoryGroup
from greffier.ui.mic_button import MicButton
from greffier.ui.onboarding import Onboarding
from greffier.ui.preferences import PreferencesDialog
from greffier.ui.util import (
    accel_is_valid,
    format_clock,
    language_name,
    languages_summary,
    shortcut_label,
    spinner,
    split_template,
)

log = logging.getLogger(__name__)

DEFAULT_SIZE = (440, 680)
DECIDE_TIMEOUT_S = 4


def _state_file():
    return paths.state_dir() / "window.json"


class MainView(Gtk.ScrolledWindow):
    """Hero microphone, state, shortcut hint, active model and history."""

    __gtype_name__ = "GreffierMainView"

    def __init__(self, window: MainWindow, client: DaemonClient) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.window = window
        self.client = client
        self._rec_started = 0.0
        self._timer = 0

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=30, margin_top=6,
                         margin_bottom=24, margin_start=12, margin_end=12)
        clamp = Adw.Clamp(maximum_size=560, tightening_threshold=420, child=column)
        self.set_child(clamp)

        # -- hero ------------------------------------------------------------ #
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                       halign=Gtk.Align.FILL)
        hero.add_css_class("hero")
        self.mic = MicButton()
        self.mic.connect("clicked", lambda *_a: client.state != State.TRANSCRIBING
                         and client.toggle())
        hero.append(self.mic)

        self.title = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.title.add_css_class("title-1")
        self.title.add_css_class("hero-title")
        hero.append(self.title)

        self.timer = Gtk.Label()
        self.timer.add_css_class("numeric")
        self.timer.add_css_class("hero-timer")
        self.timer_revealer = Gtk.Revealer(child=self.timer, transition_duration=200,
                                           transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        hero.append(self.timer_revealer)

        self.hint_box = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.hint_before = Gtk.Label()
        self.hint_after = Gtk.Label()
        self.hint_keys = shortcut_label("")
        for widget in (self.hint_before, self.hint_keys, self.hint_after):
            self.hint_box.append(widget)
        self.hint_box.add_css_class("hero-hint")
        self.hint_text = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self.hint_text.add_css_class("hero-hint")
        hero.append(self.hint_box)
        hero.append(self.hint_text)

        self.cancel = Gtk.Button(label=_("_Cancel"), use_underline=True,
                                 halign=Gtk.Align.CENTER,
                                 tooltip_text=_("Discard the recording (Esc)"))
        self.cancel.add_css_class("hero-cancel")
        self.cancel.connect("clicked", lambda *_a: client.cancel())
        self.cancel_revealer = Gtk.Revealer(child=self.cancel, transition_duration=200,
                                            transition_type=Gtk.RevealerTransitionType
                                            .SLIDE_DOWN)
        hero.append(self.cancel_revealer)
        column.append(hero)

        # -- active model ------------------------------------------------------ #
        model_group = Adw.PreferencesGroup(title=_("Speech Model"))
        self.model_row = Adw.ActionRow(activatable=True)
        self.model_row.connect("activated", lambda *_a: window.show_preferences("models"))
        self.model_spinner = spinner()
        self.model_spinner.set_visible(False)
        self.model_row.add_suffix(self.model_spinner)
        self.model_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        model_group.add(self.model_row)
        column.append(model_group)

        # -- history ---------------------------------------------------------- #
        self.history = HistoryGroup(client)
        column.append(self.history)

        # Esc cancels a recording while the window has the focus.
        controller = Gtk.ShortcutController()
        controller.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("Escape"),
            Gtk.CallbackAction.new(self._on_escape)))
        self.add_controller(controller)

        client.connect("notify::state", lambda *_a: self.sync())
        client.connect("notify::detail", lambda *_a: self.sync())
        client.connect("notify::connected", lambda *_a: self.sync())
        client.connect("notify::ready", lambda *_a: self.sync())
        client.connect("notify::starting", lambda *_a: self.sync())
        client.connect("config-changed", lambda *_a: self.sync())
        client.connect("status-changed", self._on_status)
        client.connect("models-updated", lambda *_a: self.sync_model())
        client.connect("level", lambda _c, level: self.mic.set_level(level))
        self.sync()

    # ------------------------------------------------------------------ #

    def _on_escape(self, *_args) -> bool:
        if self.client.state == State.RECORDING:
            self.client.cancel()
            return True
        return False

    def _on_status(self, _client, status: dict) -> None:
        if status.get("state") == State.RECORDING:
            seconds = float(status.get("recording_seconds") or 0)
            self._rec_started = time.monotonic() - seconds
        self.sync()

    def _set_hint(self, template: str | None, accel: str = "", text: str = "") -> None:
        """Show "before [keys] after" when ``template`` is set, else plain ``text``."""
        if template and accel_is_valid(accel):
            before, after = split_template(template, "{shortcut}")
            self.hint_before.set_label(before)
            self.hint_before.set_visible(bool(before))
            self.hint_after.set_label(after)
            self.hint_after.set_visible(bool(after))
            self.hint_keys.set_accelerator(accel)
            self.hint_box.set_visible(True)
            self.hint_text.set_visible(False)
        else:
            self.hint_box.set_visible(False)
            self.hint_text.set_label(text)
            self.hint_text.set_visible(bool(text))

    def sync(self) -> None:
        client = self.client
        state = client.state if client.connected else ""
        accel = client.shortcut
        shortcut = (client.status or {}).get("shortcut") or {}
        shortcut_ok = accel_is_valid(accel) and shortcut.get("active", True) and \
            client.get("shortcut", "backend", "auto") != "none"
        ptt = client.get("shortcut", "mode", "toggle") == "push-to-talk"
        click_hint = _("Click the microphone to start dictating")

        self.mic.set_state(state or State.IDLE)
        # Still sensitive while transcribing: it shows progress, clicks are ignored.
        self.mic.set_sensitive(client.connected and state != State.NO_MODEL)
        recording = state == State.RECORDING
        self.cancel_revealer.set_reveal_child(recording)
        self.timer_revealer.set_reveal_child(recording)

        if not client.ready:
            self.title.set_label(_("Connecting…"))
            self._set_hint(None)
        elif client.starting:
            self.title.set_label(_("Starting…"))
            self._set_hint(None, text=_("Starting the Greffier service"))
        elif not client.connected:
            self.title.set_label(_("Service Not Running"))
            self._set_hint(None, text=_("Start the Greffier service to dictate"))
        elif state == State.NO_MODEL:
            self.title.set_label(_("No Model Installed"))
            self._set_hint(None, text=_("Download a speech model to start dictating"))
        elif state == State.LOADING:
            self.title.set_label(_("Loading Model…"))
            self._set_hint(_("You can already press {shortcut}") if shortcut_ok else None,
                           accel, client.detail or click_hint)
        elif recording:
            self.title.set_label(_("Listening…"))
            if ptt:
                template = _("Release {shortcut} to stop")
            else:
                template = _("Press {shortcut} again to stop")
            self._set_hint(template if shortcut_ok else None, accel,
                           _("Click the microphone again to stop"))
        elif state == State.TRANSCRIBING:
            self.title.set_label(_("Transcribing…"))
            self._set_hint(None, text=_("The text will be typed into the focused window"))
        elif state == State.ERROR:
            self.title.set_label(_("Something Went Wrong"))
            self._set_hint(None, text=client.detail)
        else:
            self.title.set_label(_("Ready to Dictate"))
            template = _("Hold {shortcut} anywhere") if ptt else _("Press {shortcut} anywhere")
            self._set_hint(template if shortcut_ok else None, accel,
                           shortcut.get("message") if accel and not shortcut_ok
                           and shortcut.get("message") else click_hint)

        if recording and not self._timer:
            if not self._rec_started:
                self._rec_started = time.monotonic()
            self._timer = GLib.timeout_add(250, self._on_timer)
            self._on_timer()
        elif not recording and self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
            self._rec_started = 0.0
        self.sync_model()

    def _on_timer(self) -> bool:
        elapsed = time.monotonic() - (self._rec_started or time.monotonic())
        self.timer.set_label(format_clock(elapsed))
        return GLib.SOURCE_CONTINUE

    def sync_model(self) -> None:
        client = self.client
        model = client.active_model_info()
        status = client.status or {}
        if not client.connected:
            self.model_row.set_title(_("Unavailable"))
            self.model_row.set_subtitle(_("The Greffier service is stopped"))
            self.model_row.set_sensitive(False)
            self.model_spinner.set_visible(False)
            return
        self.model_row.set_sensitive(True)
        name = (model or {}).get("name") or status.get("model_name")
        if not name:
            self.model_row.set_title(_("No Model Installed"))
            self.model_row.set_subtitle(_("Choose a model to start dictating"))
            self.model_spinner.set_visible(False)
            return
        self.model_row.set_title(GLib.markup_escape_text(name))
        parts = []
        if model:
            if model.get("vendor"):
                parts.append(model["vendor"])
            if model.get("language_mode") == "auto":
                parts.append(languages_summary(model))
            else:
                lang = client.get("model", "language", "auto")
                parts.append(_("Language detected automatically") if lang == "auto"
                             else language_name(lang))
        loading = client.state == State.LOADING
        if loading:
            parts.append(_("Loading…"))
        self.model_spinner.set_visible(loading)
        self.model_row.set_subtitle(GLib.markup_escape_text(" · ".join(parts)))


class MainWindow(Adw.ApplicationWindow):
    __gtype_name__ = "GreffierMainWindow"

    def __init__(self, application: Adw.Application, client: DaemonClient) -> None:
        super().__init__(application=application, title=APP_NAME)
        self.client = client
        self._prefs: PreferencesDialog | None = None
        self._onboarding: Onboarding | None = None
        self._decided = False
        self._auto_start_tried = False
        self._decide_timeout = 0
        self.set_size_request(360, 400)
        self._restore_size()

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView(top_bar_style=Adw.ToolbarStyle.FLAT)
        header = Adw.HeaderBar()
        self.back_button = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text=_("Back"),
                                      visible=False)
        self.back_button.connect("clicked", lambda *_a: self._onboarding and
                                 self._onboarding.go_back())
        header.pack_start(self.back_button)
        self.window_title = Adw.WindowTitle(title="")
        header.set_title_widget(self.window_title)
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("_Preferences"), "app.preferences")
        section.append(_("_Keyboard Shortcuts"), "app.shortcuts")
        section.append(_("_About Greffier"), "app.about")
        menu.append_section(None, section)
        self.menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                          primary=True, tooltip_text=_("Main Menu"))
        header.pack_end(self.menu_button)
        toolbar.add_top_bar(header)

        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", self._on_banner_clicked)
        toolbar.add_top_bar(self.banner)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=200)
        waiting = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                          valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        wait_spinner = spinner()
        wait_spinner.set_size_request(32, 32)
        waiting.append(wait_spinner)
        self.waiting_label = Gtk.Label(label=_("Connecting…"))
        self.waiting_label.add_css_class("dim-label")
        waiting.append(self.waiting_label)
        self.stack.add_named(waiting, "waiting")
        self.main_view = MainView(self, client)
        self.stack.add_named(self.main_view, "main")
        toolbar.set_content(self.stack)
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 400sp or max-height: 600sp"))
        breakpoint.add_setter(self.main_view.mic, "disc-size", 88)
        self.add_breakpoint(breakpoint)

        client.connect("notify::ready", lambda *_a: self._maybe_decide())
        client.connect("notify::connected", self._on_connected_changed)
        client.connect("notify::models-loaded", lambda *_a: self._maybe_decide())
        client.connect("notify::starting", lambda *_a: self._update_banner())
        client.connect("notify::state", lambda *_a: self._update_banner())
        client.connect("models-updated", lambda *_a: self._update_banner())
        client.connect("config-changed", self._on_config_changed)
        client.connect("notice", self._on_notice)
        client.connect("error", lambda _c, message: self.add_toast(
            Adw.Toast(title=message, priority=Adw.ToastPriority.HIGH)))
        self.connect("close-request", self._on_close_request)
        self._update_banner()
        self._maybe_decide()

    # ------------------------------------------------------------------ #
    # Views
    # ------------------------------------------------------------------ #

    def _maybe_decide(self) -> None:
        """Pick the first view once the service presence (and models) are known."""
        client = self.client
        if self._decided or not client.ready:
            return
        if not client.connected:
            if not self._auto_start_tried and not client.starting:
                self._auto_start_tried = True
                self.waiting_label.set_label(_("Starting Greffier…"))
                client.start_service(lambda ok, _msg: ok or self._decide())
            elif not client.starting:
                self._decide()
            return
        if client.models_loaded:
            self._decide()
        elif not self._decide_timeout:
            self._decide_timeout = GLib.timeout_add_seconds(DECIDE_TIMEOUT_S, self._decide)

    def _decide(self) -> bool:
        if self._decide_timeout:
            GLib.source_remove(self._decide_timeout)
            self._decide_timeout = 0
        if self._decided:
            return GLib.SOURCE_REMOVE
        self._decided = True
        client = self.client
        first_run = client.config is not None and not client.get("general", "first_run_done",
                                                                 True)
        no_model = client.connected and client.models_loaded and not client.installed_asr()
        if client.connected and (first_run or no_model):
            self.show_onboarding(include_welcome=first_run)
        else:
            self.show_main()
        return GLib.SOURCE_REMOVE

    def _on_connected_changed(self, *_args) -> None:
        if not self._decided:
            self._maybe_decide()
        self._update_banner()

    def show_main(self) -> None:
        if self._onboarding is not None:
            onboarding = self._onboarding
            self._onboarding = None
            self.stack.set_visible_child_name("main")
            GLib.timeout_add(400, lambda: self.stack.remove(onboarding) or GLib.SOURCE_REMOVE)
        else:
            self.stack.set_visible_child_name("main")
        self.back_button.set_visible(False)
        self.main_view.history.refresh()
        self._update_banner()

    def show_onboarding(self, include_welcome: bool = True) -> None:
        if self._onboarding is not None:
            return
        self._onboarding = Onboarding(self.client, include_welcome=include_welcome)
        self._onboarding.connect("finished", lambda *_a: self.show_main())
        self._onboarding.connect("notify::can-go-back", lambda ob, _p: self.back_button
                                 .set_visible(ob.can_go_back))
        self.stack.add_named(self._onboarding, "onboarding")
        self.stack.set_visible_child_name("onboarding")
        self._update_banner()

    def run_setup_again(self) -> None:
        if self._prefs is not None:
            self._prefs.close()
        self.client.set_config({"general": {"first_run_done": False}})
        self.show_onboarding(include_welcome=True)

    def show_preferences(self, page: str | None = None) -> None:
        if self._prefs is None:
            self._prefs = PreferencesDialog(self.client, on_run_setup=self.run_setup_again)
            self._prefs.connect("closed", self._on_prefs_closed)
            self._prefs.present(self)
        if page:
            self._prefs.show_page(page)

    def _on_prefs_closed(self, *_args) -> None:
        self._prefs = None

    def _on_config_changed(self, _client, config: dict) -> None:
        general = config.get("general") or {}
        if self._decided and self._onboarding is None and general.get("first_run_done") is False \
                and self.stack.get_visible_child_name() == "main":
            self.show_onboarding(include_welcome=True)

    # ------------------------------------------------------------------ #
    # Banner and toasts
    # ------------------------------------------------------------------ #

    def _update_banner(self) -> None:
        client = self.client
        mode = ""
        if client.ready and not client.connected and (self._decided or not client.starting):
            mode = "starting" if client.starting else "stopped"
        elif client.connected and client.state == State.NO_MODEL and self._onboarding is None \
                and self._decided:
            mode = "no-model"
        self._banner_mode = mode
        if mode == "stopped":
            self.banner.set_title(_("The Greffier service is not running"))
            self.banner.set_button_label(_("_Start"))
        elif mode == "starting":
            self.banner.set_title(_("Starting the Greffier service…"))
            self.banner.set_button_label(None)
        elif mode == "no-model":
            self.banner.set_title(_("No speech recognition model is installed"))
            self.banner.set_button_label(_("_Choose a Model"))
        self.banner.set_revealed(bool(mode))

    def _on_banner_clicked(self, _banner) -> None:
        if self._banner_mode == "stopped":
            def done(ok: bool, message: str) -> None:
                if not ok:
                    self.add_toast(Adw.Toast(
                        title=_("Could not start the service: {error}").format(error=message),
                        priority=Adw.ToastPriority.HIGH))
                elif not self._decided:
                    self._maybe_decide()

            self.client.start_service(done)
        elif self._banner_mode == "no-model":
            self.show_preferences("models")

    def add_toast(self, toast: Adw.Toast) -> None:
        if self._prefs is not None and self._prefs.get_mapped():
            self._prefs.add_toast(toast)
        else:
            self.toasts.add_toast(toast)

    def _on_notice(self, _client, level: str, message: str) -> None:
        if not message:
            return
        toast = Adw.Toast(title=GLib.markup_escape_text(message))
        if level == "error":
            toast.set_priority(Adw.ToastPriority.HIGH)
            toast.set_timeout(8)
        self.add_toast(toast)

    # ------------------------------------------------------------------ #
    # Size persistence
    # ------------------------------------------------------------------ #

    def _restore_size(self) -> None:
        width, height = DEFAULT_SIZE
        maximized = False
        try:
            data: Any = json.loads(_state_file().read_text(encoding="utf-8"))
            width = max(360, int(data.get("width", width)))
            height = max(400, int(data.get("height", height)))
            maximized = bool(data.get("maximized", False))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.set_default_size(width, height)
        if maximized:
            self.maximize()

    def _on_close_request(self, *_args) -> bool:
        width, height = self.get_default_size()
        data = {"width": width, "height": height, "maximized": self.is_maximized()}
        try:
            path = _state_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            log.debug("cannot save the window size: %s", exc)
        return False
