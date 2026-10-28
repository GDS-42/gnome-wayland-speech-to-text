"""First-run assistant shown inside the main window.

Welcome (privacy) → model → shortcut → try it → done. The last step sets
``general.first_run_done`` with SetConfig. Pages live in an ``Adw.Carousel``
driven by buttons only, so that the window keeps a single header bar (and
banner) for the assistant and the main view.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from gi.repository import Adw, GLib, GObject, Gtk

from greffier.const import APP_ID, State
from greffier.i18n import _
from greffier.ui.binding import Connections
from greffier.ui.client import DaemonClient
from greffier.ui.diagnostics import EXTENSION_HELP_URL, PORTAL_CONSENT_HINT, launch_desktop
from greffier.ui.mic_button import MicButton
from greffier.ui.models_page import ModelList
from greffier.ui.shortcut_dialog import ShortcutDialog
from greffier.ui.util import accel_is_valid, accel_label, open_uri, shortcut_label, status_icon


def _title(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER)
    label.add_css_class("title-1")
    return label


def _body(text: str = "", markup: bool = False) -> Gtk.Label:
    label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
    label.add_css_class("onboarding-body")
    if markup:
        label.set_markup(text)
    else:
        label.set_label(text)
    return label


def _pill(label: str, callback: Callable[[], None]) -> Gtk.Button:
    button = Gtk.Button(label=label, use_underline=True, halign=Gtk.Align.CENTER)
    button.add_css_class("pill")
    button.add_css_class("suggested-action")
    button.connect("clicked", lambda *_a: callback())
    return button


class _Page(Gtk.ScrolledWindow):
    """Scrollable, centred column: illustration, title, text, content, button."""

    def __init__(self, name: str) -> None:
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, hexpand=True, vexpand=True)
        self.name = name
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                           valign=Gtk.Align.CENTER, margin_top=12, margin_bottom=24,
                           margin_start=18, margin_end=18)
        self.box.add_css_class("onboarding-page")
        clamp = Adw.Clamp(maximum_size=520, tightening_threshold=400, child=self.box)
        self.set_child(clamp)

    def add(self, widget: Gtk.Widget, space_before: int = 0) -> Gtk.Widget:
        if space_before:
            widget.set_margin_top(space_before)
        self.box.append(widget)
        return widget


class Onboarding(Gtk.Box):
    __gtype_name__ = "GreffierOnboarding"
    __gsignals__ = {"finished": (GObject.SignalFlags.RUN_FIRST, None, ())}

    can_go_back = GObject.Property(type=bool, default=False)

    def __init__(self, client: DaemonClient, *, include_welcome: bool = True) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("onboarding")
        self.client = client
        self.connections = Connections()
        self._diagnostics: dict[str, Any] = {}
        self._ptt_supported: bool | None = None

        self.carousel = Adw.Carousel(interactive=False, allow_scroll_wheel=False,
                                     allow_mouse_drag=False, vexpand=True)
        self.carousel.connect("page-changed", self._on_page_changed)
        self.pages: list[_Page] = []
        if include_welcome:
            self._add_page(self._welcome_page())
        self._add_page(self._model_page())
        self._add_page(self._shortcut_page())
        self._add_page(self._try_page())
        self._add_page(self._done_page())
        dots = Adw.CarouselIndicatorDots(carousel=self.carousel, margin_bottom=12,
                                         margin_top=6)
        self.append(self.carousel)
        self.append(dots)

        self.connections.connect(client, "config-changed", lambda *_a: self._sync_config())
        self.connections.connect(client, "models-updated", lambda *_a: self._sync_models())
        self.connections.connect(client, "model-progress", lambda *_a: self._sync_models())
        self.connections.connect(client, "notify::state", lambda *_a: self._sync_state())
        self.connections.connect(client, "level", lambda _c, lvl: self._mic.set_level(lvl))
        self.connections.connect(client, "notify::connected", lambda *_a: self._on_connected())
        self._sync_config()
        self._sync_models()
        self._sync_state()
        self._on_connected()

    # ------------------------------------------------------------------ #
    # Navigation
    # ------------------------------------------------------------------ #

    def _add_page(self, page: _Page) -> None:
        self.pages.append(page)
        self.carousel.append(page)

    @property
    def position(self) -> int:
        return int(round(self.carousel.get_position()))

    def show_page(self, name: str, animate: bool = True) -> None:
        for page in self.pages:
            if page.name == name:
                self.carousel.scroll_to(page, animate)
                if not animate:
                    self._on_page_changed(self.carousel, self.pages.index(page))
                return

    def go_next(self) -> None:
        index = self.position
        if index + 1 < len(self.pages):
            self.carousel.scroll_to(self.pages[index + 1], True)

    def go_back(self) -> None:
        index = self.position
        if index > 0:
            self.carousel.scroll_to(self.pages[index - 1], True)

    def _on_page_changed(self, _carousel, index: int) -> None:
        self.can_go_back = index > 0
        page = self.pages[index] if 0 <= index < len(self.pages) else None
        if page is None:
            return
        if page.name == "try":
            self._refresh_diagnostics()
            GLib.idle_add(lambda: self._text.grab_focus() and GLib.SOURCE_REMOVE)
        elif page.name == "shortcut":
            self._refresh_diagnostics()

    def finish(self) -> None:
        def done(*_args) -> None:
            self.connections.disconnect_all()
            self.emit("finished")

        self.client.set_config({"general": {"first_run_done": True}}, on_done=done,
                               on_error=done)

    # ------------------------------------------------------------------ #
    # Pages
    # ------------------------------------------------------------------ #

    def _welcome_page(self) -> _Page:
        page = _Page("welcome")
        icon = Gtk.Image(icon_name=APP_ID, pixel_size=128)
        icon.add_css_class("icon-dropshadow")
        page.add(icon)
        page.add(_title(_("Welcome to Greffier")), 6)
        page.add(_body(_("Dictate text into any application with a keyboard shortcut.")))
        features = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                           halign=Gtk.Align.CENTER, margin_top=18, margin_bottom=12)
        for icon_name, title, text in (
            ("security-high-symbolic", _("Private"),
             _("Speech recognition runs on this computer. Your voice never leaves it.")),
            ("network-offline-symbolic", _("Works Offline"),
             _("Once a model is downloaded, no internet connection is needed.")),
            ("input-keyboard-symbolic", _("Works Everywhere"),
             _("The text is typed into the focused window, in any application.")),
        ):
            row = Gtk.Box(spacing=14)
            badge = Gtk.Image(icon_name=icon_name, pixel_size=20, valign=Gtk.Align.CENTER)
            badge.add_css_class("feature-icon")
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                            valign=Gtk.Align.CENTER)
            heading = Gtk.Label(label=title, xalign=0)
            heading.add_css_class("heading")
            desc = Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=42)
            desc.add_css_class("dim-label")
            texts.append(heading)
            texts.append(desc)
            row.append(badge)
            row.append(texts)
            features.append(row)
        page.add(features)
        page.add(_pill(_("_Get Started"), self.go_next), 12)
        return page

    def _model_page(self) -> _Page:
        page = _Page("model")
        page.add(_title(_("Choose a Speech Model")))
        page.add(_body(_("Models are downloaded once, then run on this computer. The "
                         "recommended model suits most people.")))
        self._show_all = False
        self._models = ModelList(self.client, self.connections, onboarding=True)
        page.add(self._models, 12)
        self._more = Gtk.Button(label=_("Show _Other Models"), use_underline=True,
                                halign=Gtk.Align.CENTER)
        self._more.add_css_class("flat")
        self._more.connect("clicked", self._on_more_clicked)
        page.add(self._more)
        self._model_next = _pill(_("_Continue"), self.go_next)
        page.add(self._model_next, 6)
        self._model_hint = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._model_hint.add_css_class("dim-label")
        self._model_hint.add_css_class("caption")
        page.add(self._model_hint)
        return page

    def _shortcut_page(self) -> _Page:
        page = _Page("shortcut")
        icon = Gtk.Image(icon_name="input-keyboard-symbolic", pixel_size=64)
        icon.add_css_class("onboarding-icon")
        page.add(icon)
        page.add(_title(_("Your Dictation Shortcut")), 6)
        self._shortcut_body = page.add(_body())
        self._keys = shortcut_label("")
        self._keys.set_halign(Gtk.Align.CENTER)
        self._keys.add_css_class("large-keycaps")
        page.add(self._keys, 12)
        change = Gtk.Button(label=_("C_hange Shortcut…"), use_underline=True,
                            halign=Gtk.Align.CENTER)
        change.add_css_class("pill")
        change.connect("clicked", lambda *_a: self._change_shortcut())
        page.add(change, 6)

        modes = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        modes.add_css_class("boxed-list")
        self._toggle = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        self._ptt = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=self._toggle)
        toggle_row = Adw.ActionRow(title=_("Toggle"), activatable_widget=self._toggle,
                                   subtitle=_("Press the shortcut to start, press it again "
                                              "to stop"))
        toggle_row.add_prefix(self._toggle)
        self._ptt_row = Adw.ActionRow(title=_("Push to Talk"), activatable_widget=self._ptt,
                                      subtitle=_("Hold the shortcut while you speak. Requires "
                                                 "GNOME 48 or later"))
        self._ptt_row.add_prefix(self._ptt)
        modes.append(toggle_row)
        modes.append(self._ptt_row)
        self._toggle.connect("toggled", self._on_mode_toggled)
        page.add(modes, 18)
        page.add(_pill(_("_Continue"), self.go_next), 18)
        return page

    def _try_page(self) -> _Page:
        page = _Page("try")
        page.add(_title(_("Try It Out")))
        self._try_body = page.add(_body(markup=True))

        card = Gtk.Overlay()
        card.add_css_class("card")
        card.add_css_class("try-card")
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                      min_content_height=140, max_content_height=240,
                                      propagate_natural_height=True)
        self._text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=14,
                                  bottom_margin=14, left_margin=14, right_margin=14)
        self._text.add_css_class("try-text")
        self._text.update_property([Gtk.AccessibleProperty.LABEL], [_("Dictation test area")])
        scroller.set_child(self._text)
        card.set_child(scroller)
        self._placeholder = Gtk.Label(label=_("Your words will appear here"),
                                      halign=Gtk.Align.START, valign=Gtk.Align.START,
                                      margin_top=14, margin_start=16, can_target=False)
        self._placeholder.add_css_class("dim-label")
        card.add_overlay(self._placeholder)
        self._text.get_buffer().connect(
            "changed", lambda buf: self._placeholder.set_visible(buf.get_char_count() == 0))
        page.add(card, 12)

        status = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self._mic = MicButton(disc_size=36)
        self._mic.connect("clicked", lambda *_a: self.client.state != State.TRANSCRIBING
                          and self.client.toggle())
        self._try_state = Gtk.Label()
        self._try_state.add_css_class("dim-label")
        status.append(self._mic)
        status.append(self._try_state)
        page.add(status, 2)

        extension = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        extension.add_css_class("boxed-list")
        self._ext_row = Adw.ActionRow(title=_("GNOME Shell Extension"))
        self._ext_row.set_subtitle_lines(0)
        self._ext_icon = status_icon("warning")
        self._ext_row.add_prefix(self._ext_icon)
        self._ext_button = Gtk.Button(valign=Gtk.Align.CENTER, use_underline=True)
        self._ext_button.connect("clicked", self._on_extension_clicked)
        self._ext_row.add_suffix(self._ext_button)
        extension.append(self._ext_row)
        self._consent_row = Adw.ActionRow(title=_("Allow Remote Interaction"),
                                          subtitle=_(PORTAL_CONSENT_HINT), visible=False)
        self._consent_row.set_subtitle_lines(0)
        info = Gtk.Image(icon_name="dialog-information-symbolic", valign=Gtk.Align.CENTER)
        info.add_css_class("accent")
        self._consent_row.add_prefix(info)
        extension.append(self._consent_row)
        page.add(extension, 12)
        page.add(_pill(_("_Continue"), self.go_next), 18)
        return page

    def _done_page(self) -> _Page:
        page = _Page("done")
        icon = Gtk.Image(icon_name="emblem-ok-symbolic", pixel_size=48,
                         halign=Gtk.Align.CENTER)
        icon.add_css_class("done-icon")
        page.add(icon)
        page.add(_title(_("All Set")), 12)
        self._done_body = page.add(_body(markup=True))
        page.add(_pill(_("_Start Using Greffier"), self.finish), 18)
        return page

    # ------------------------------------------------------------------ #
    # Synchronisation
    # ------------------------------------------------------------------ #

    def _on_connected(self) -> None:
        if self.client.connected and not self.client.models:
            self.client.refresh_models()
        if self.client.connected:
            self._refresh_diagnostics()

    def _sync_config(self) -> None:
        accel = self.client.shortcut
        ptt = self.client.get("shortcut", "mode", "toggle") == "push-to-talk"
        self._keys.set_accelerator(accel if accel_is_valid(accel) else "")
        if ptt:
            text = _("Hold it down in any application while you speak. The text appears at "
                     "your cursor when you release it.")
        else:
            text = _("Press it in any application to start dictating, then again to stop. "
                     "The text appears at your cursor.")
        self._shortcut_body.set_label(text)
        check = self._ptt if ptt else self._toggle
        if not check.get_active():
            check.set_active(True)
        keys = GLib.markup_escape_text(accel_label(accel))
        if not accel_is_valid(accel):
            try_text = _("Click in the box below, then click the microphone and say a few "
                         "words.")
            done_text = _("Click the microphone in the Greffier window to dictate. You can "
                          "set a shortcut in Preferences.")
        elif ptt:
            try_text = _("Click in the box below, hold {shortcut} and say a few words, then "
                         "release it.").format(shortcut=f"<b>{keys}</b>")
            done_text = _("Hold {shortcut} in any application to dictate. Greffier keeps "
                          "working in the background, even when this window is closed."
                          ).format(shortcut=f"<b>{keys}</b>")
        else:
            try_text = _("Click in the box below, press {shortcut} and say a few words, then "
                         "press it again.").format(shortcut=f"<b>{keys}</b>")
            done_text = _("Press {shortcut} in any application to dictate. Greffier keeps "
                          "working in the background, even when this window is closed."
                          ).format(shortcut=f"<b>{keys}</b>")
        self._try_body.set_markup(try_text)
        self._done_body.set_markup(done_text)

    def _filter_model(self, row: Gtk.ListBoxRow) -> bool:
        model = getattr(row, "model", {})
        return bool(self._show_all or model.get("recommended") or model.get("installed")
                    or model.get("id") in self.client.progress)

    def _on_more_clicked(self, _button) -> None:
        self._show_all = not self._show_all
        self._more.set_label(_("Show _Fewer Models") if self._show_all
                             else _("Show _Other Models"))
        self._filter_models()

    def _filter_models(self) -> None:
        # Hide rows instead of using a filter function: hidden rows are skipped by
        # the :last-child styling of boxed lists, filtered ones are not.
        for row in self._models.rows.values():
            row.set_visible(self._filter_model(row))

    def _sync_models(self) -> None:
        self._filter_models()
        installed = self.client.installed_asr()
        installing = [m for m in self.client.asr_models() if m.get("id") in self.client.progress]
        self._model_next.set_sensitive(bool(installed or installing))
        if installed:
            self._model_next.set_label(_("_Continue"))
            self._model_hint.set_visible(False)
        elif installing:
            self._model_next.set_label(_("_Continue While Downloading"))
            self._model_hint.set_label(_("The download goes on in the background."))
            self._model_hint.set_visible(True)
        else:
            self._model_next.set_label(_("_Continue"))
            self._model_hint.set_label(_("Download a model to continue."))
            self._model_hint.set_visible(True)

    def _sync_state(self) -> None:
        state = self.client.state
        self._mic.set_state(state or State.IDLE)
        self._mic.set_sensitive(self.client.connected and state != State.NO_MODEL)
        text = {
            State.RECORDING: _("Listening…"),
            State.TRANSCRIBING: _("Transcribing…"),
            State.LOADING: _("Loading the model…"),
            State.NO_MODEL: _("Waiting for the model download…"),
            State.ERROR: self.client.detail or _("Something went wrong"),
        }.get(state, _("Ready"))
        self._try_state.set_label(text)

    def _refresh_diagnostics(self) -> None:
        if not self.client.connected:
            return
        self.client.get_diagnostics(self._on_diagnostics, on_error=lambda _e: None)

    def _on_diagnostics(self, report: Any) -> None:
        if not isinstance(report, dict):
            return
        self._diagnostics = report
        ext = report.get("extension") or {}
        self._ext_row.remove(self._ext_icon)
        if ext.get("active"):
            self._ext_icon = status_icon("ok")
            self._ext_row.set_subtitle(_("Active: a recording indicator appears in the top bar "
                                         "while you dictate."))
            self._ext_button.set_visible(False)
        elif ext.get("installed"):
            self._ext_icon = status_icon("warning")
            self._ext_row.set_subtitle(_("Installed but not active. Log out and back in, then "
                                         "enable it in Extensions."))
            self._ext_button.set_label(_("_Open Extensions"))
            self._ext_button.set_visible(True)
        else:
            self._ext_icon = status_icon("warning")
            self._ext_row.set_subtitle(_("Optional. It shows a recording indicator in the top "
                                         "bar and inserts text faster and more reliably."))
            self._ext_button.set_label(_("_Learn More"))
            self._ext_button.set_visible(True)
        self._ext_row.add_prefix(self._ext_icon)
        portals = report.get("portals") or {}
        keyboard = ((self.client.status or {}).get("injection") or {}).get("keyboard", "")
        self._consent_row.set_visible(
            keyboard == "portal" or (not ext.get("active") and bool(portals.get("remote_desktop"))
                                     and keyboard in ("", "portal")))
        if "global_shortcuts" in portals:
            self._ptt_supported = bool(portals["global_shortcuts"])
        self._ptt_row.set_sensitive(self._ptt_supported is not False or self._ptt.get_active())
        if self._ptt_supported is False:
            self._ptt_row.set_subtitle(_("Hold the shortcut while you speak. Not available: "
                                         "requires GNOME 48 or later"))

    # ------------------------------------------------------------------ #
    # Actions
    # ------------------------------------------------------------------ #

    def _on_mode_toggled(self, *_args) -> None:
        mode = "toggle" if self._toggle.get_active() else "push-to-talk"
        if self.client.config is not None and mode != self.client.get("shortcut", "mode"):
            self.client.set_config({"shortcut": {"mode": mode}})

    def _change_shortcut(self) -> None:
        ptt = self.client.get("shortcut", "mode") == "push-to-talk"
        dialog = ShortcutDialog(self.client.shortcut, push_to_talk=ptt)
        dialog.connect("chosen", lambda _d, accel: self.client.set_config(
            {"shortcut": {"accelerator": accel}}))
        dialog.present(self.get_root())

    def _on_extension_clicked(self, _button) -> None:
        ext = self._diagnostics.get("extension") or {}
        if ext.get("installed") and launch_desktop("org.gnome.Extensions.desktop"):
            return
        open_uri(self, EXTENSION_HELP_URL)
