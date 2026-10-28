"""Model manager: one row per speech model, with download progress and actions.

``ModelRow`` is shared by the Models preferences page and the onboarding.
Data comes from ListModels (``DaemonClient.models``) and ModelProgress
(``DaemonClient.progress``); actions are InstallModel, CancelInstall,
RemoveModel and SetConfig ``{"model": {"active": id}}``.
"""

from __future__ import annotations

from typing import Any

from gi.repository import Adw, GLib, Graphene, Gsk, Gtk, Pango

from greffier.i18n import _, ngettext, pgettext
from greffier.ui.binding import ConfigBinder, Connections
from greffier.ui.client import DaemonClient
from greffier.ui.util import (
    badge,
    clear_box,
    describe,
    format_percent,
    format_ram,
    format_size,
    language_name,
    languages_summary,
    model_languages,
    show_toast,
    spinner,
    ui_language,
    wrap_box,
    wrap_box_append,
)

PHASE_LABELS = {
    "download": lambda: _("Downloading…"),
    "verify": lambda: _("Verifying…"),
    "extract": lambda: _("Installing…"),
}


# --------------------------------------------------------------------------- #
# Small widgets
# --------------------------------------------------------------------------- #


class DotMeter(Gtk.Widget):
    """Five dots, ``value`` of them filled (speed and accuracy hints)."""

    __gtype_name__ = "GreffierDotMeter"
    DOT = 7
    GAP = 3
    COUNT = 5

    def __init__(self, value: int, label: str) -> None:
        super().__init__(accessible_role=Gtk.AccessibleRole.METER, valign=Gtk.Align.CENTER)
        self.add_css_class("dot-meter")
        self._value = max(0, min(self.COUNT, int(value)))
        self.update_property(
            [Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.VALUE_MIN,
             Gtk.AccessibleProperty.VALUE_MAX, Gtk.AccessibleProperty.VALUE_NOW,
             Gtk.AccessibleProperty.VALUE_TEXT],
            [label, 0.0, float(self.COUNT), float(self._value),
             _("{value} out of {max}").format(value=self._value, max=self.COUNT)])

    def do_measure(self, orientation: Gtk.Orientation, _for_size: int):
        if orientation == Gtk.Orientation.HORIZONTAL:
            size = self.COUNT * self.DOT + (self.COUNT - 1) * self.GAP
        else:
            size = self.DOT
        return size, size, -1, -1

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        color = self.get_color()
        empty = color.copy()
        empty.alpha = color.alpha * 0.22
        y = (self.get_height() - self.DOT) / 2
        rtl = self.get_direction() == Gtk.TextDirection.RTL
        for i in range(self.COUNT):
            index = self.COUNT - 1 - i if rtl else i
            rect = Graphene.Rect().init(index * (self.DOT + self.GAP), y, self.DOT, self.DOT)
            rounded = Gsk.RoundedRect()
            rounded.init_from_rect(rect, self.DOT / 2)
            snapshot.push_rounded_clip(rounded)
            snapshot.append_color(color if i < self._value else empty, rect)
            snapshot.pop()


def _meter(title: str, value: int) -> Gtk.Box:
    box = Gtk.Box(spacing=6)
    label = Gtk.Label(label=title)
    label.add_css_class("caption")
    label.add_css_class("dim-label")
    box.append(label)
    box.append(DotMeter(value, title))
    return box


def activation_patch(client: DaemonClient, model: dict[str, Any]) -> dict[str, Any]:
    """SetConfig patch selecting ``model``, fixing the language if it is unsupported."""
    patch: dict[str, Any] = {"active": model["id"]}
    lang = client.get("model", "language", "auto")
    langs = list(model.get("languages") or [])
    mode = model.get("language_mode", "auto")
    supported = "*" in langs or lang in langs
    if (lang == "auto" and mode == "required") or (lang != "auto" and not supported):
        for candidate in (ui_language(), "fr", "en"):
            if "*" in langs or candidate in langs:
                patch["language"] = candidate
                break
        else:
            patch["language"] = "auto" if mode != "required" or not langs else langs[0]
    return {"model": patch}


# --------------------------------------------------------------------------- #
# Model row
# --------------------------------------------------------------------------- #


class ModelRow(Gtk.ListBoxRow):
    """Card-like row describing one model, with its install state and actions."""

    __gtype_name__ = "GreffierModelRow"

    def __init__(self, client: DaemonClient, model: dict[str, Any], *,
                 onboarding: bool = False) -> None:
        super().__init__(activatable=False, selectable=False)
        self.add_css_class("model-row")
        self.client = client
        self.model = model
        self.model_id = model["id"]
        self.onboarding = onboarding

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12,
                        margin_bottom=12, margin_start=14, margin_end=12)
        header = Gtk.Box(spacing=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                         valign=Gtk.Align.CENTER)
        # Name and badges wrap like words on narrow screens.
        self._title_line = wrap_box(child_spacing=6, line_spacing=4)
        self._title_line.add_css_class("model-title")
        self._name = Gtk.Label(xalign=0, wrap=True)
        self._name.add_css_class("heading")
        wrap_box_append(self._title_line, self._name)
        self._badges: list[Gtk.Widget] = []
        self._subtitle = Gtk.Label(xalign=0, wrap=True)
        self._subtitle.add_css_class("caption")
        self._subtitle.add_css_class("dim-label")
        titles.append(self._title_line)
        titles.append(self._subtitle)
        self._actions = Gtk.Box(spacing=6, valign=Gtk.Align.CENTER)
        header.append(titles)
        header.append(self._actions)

        self._description = Gtk.Label(xalign=0, wrap=True,
                                      natural_wrap_mode=Gtk.NaturalWrapMode.WORD)
        self._description.add_css_class("model-description")

        self._facts = Gtk.Box(spacing=18)
        self._facts.add_css_class("model-facts")

        self._progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER)
        self._progress.add_css_class("model-progress")
        progress_text = Gtk.Box(spacing=6)
        self._phase = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self._phase.add_css_class("caption")
        self._percent = Gtk.Label(xalign=1)
        self._percent.add_css_class("caption")
        self._percent.add_css_class("numeric")
        progress_text.append(self._phase)
        progress_text.append(self._percent)
        self._progress_box.append(self._progress)
        self._progress_box.append(progress_text)
        self._progress_revealer = Gtk.Revealer(child=self._progress_box,
                                               transition_type=Gtk.RevealerTransitionType
                                               .SLIDE_DOWN)

        outer.append(header)
        outer.append(self._description)
        outer.append(self._facts)
        outer.append(self._progress_revealer)
        self.set_child(outer)
        self.update(model)

    # ------------------------------------------------------------------ #

    @property
    def installing(self) -> bool:
        return self.model_id in self.client.progress

    def update(self, model: dict[str, Any]) -> None:
        self.model = model
        name = model.get("name", self.model_id)
        self._name.set_label(name)
        parts = [model.get("vendor", ""), languages_summary(model)]
        self._subtitle.set_label(" · ".join(p for p in parts if p))
        self._description.set_label(describe(model))

        clear_box(self._facts)
        self._facts.append(_meter(_("Speed"), int(model.get("speed", 3))))
        self._facts.append(_meter(_("Accuracy"), int(model.get("accuracy", 3))))
        size = model.get("download", {}).get("size") or model.get("disk_size") or 0
        facts = []
        if size and not model.get("installed"):
            facts.append(format_size(size))
        elif model.get("installed") and (model.get("size_on_disk") or model.get("disk_size")):
            facts.append(_("{size} on disk").format(
                size=format_size(model.get("size_on_disk") or model.get("disk_size"))))
        if model.get("ram_mb"):
            # Translators: memory needed by a model, e.g. "900 MB RAM"
            facts.append(_("{size} RAM").format(size=format_ram(model["ram_mb"])))
        if facts:
            size_label = Gtk.Label(label=" · ".join(facts), xalign=1, hexpand=True,
                                   ellipsize=Pango.EllipsizeMode.END)
            size_label.add_css_class("caption")
            size_label.add_css_class("dim-label")
            self._facts.append(size_label)

        self._update_badges()
        self._update_actions()
        self._update_progress()

    def _update_badges(self) -> None:
        for widget in self._badges:
            self._title_line.remove(widget)
        model = self.model
        active = self.client.is_active(model)
        self._badges = []
        if model.get("recommended"):
            self._badges.append(badge(_("Recommended"), "accent"))
        if active:
            self._badges.append(badge(pgettext("model", "Active"), "success"))
        elif model.get("installed") and not self.installing:
            self._badges.append(badge(_("Installed"), "neutral"))
        if model.get("update_available") and not self.installing:
            self._badges.append(badge(_("Update Available"), "warning"))
        for widget in self._badges:
            wrap_box_append(self._title_line, widget)

    def _button(self, label: str, callback, *, suggested: bool = False,
                tooltip: str | None = None) -> Gtk.Button:
        button = Gtk.Button(label=label, use_underline=True, valign=Gtk.Align.CENTER)
        if suggested:
            button.add_css_class("suggested-action")
        if tooltip:
            button.set_tooltip_text(tooltip)
        button.connect("clicked", lambda *_a: callback())
        self._actions.append(button)
        return button

    def _icon_button(self, icon: str, tooltip: str, callback, css: str = "flat") -> Gtk.Button:
        button = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
        button.add_css_class(css)
        button.add_css_class("circular")
        button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
        button.connect("clicked", lambda *_a: callback())
        self._actions.append(button)
        return button

    def _update_actions(self) -> None:
        clear_box(self._actions)
        model = self.model
        name = model.get("name", self.model_id)
        if self.installing:
            self._icon_button("process-stop-symbolic", _("Cancel Download"), self._cancel)
            return
        installed = bool(model.get("installed"))
        active = self.client.is_active(model)
        if not installed:
            size = format_size(model.get("download", {}).get("size", 0))
            recommended = bool(model.get("recommended"))
            suggested = recommended and (self.onboarding or not self.client.installed_asr())
            self._button(_("_Download"), self._install, suggested=suggested,
                         tooltip=_("Download {name} ({size})").format(name=name, size=size))
            return
        if model.get("update_available"):
            self._button(_("_Update"), self._install, suggested=True,
                         tooltip=_("Download the new version of {name}").format(name=name))
        if not active:
            self._button(_("_Use"), self._use,
                         tooltip=_("Use {name} for dictation").format(name=name))
        elif self.onboarding:
            check = Gtk.Image(icon_name="emblem-ok-symbolic", valign=Gtk.Align.CENTER)
            check.add_css_class("success")
            check.update_property([Gtk.AccessibleProperty.LABEL], [_("Ready")])
            self._actions.append(check)
        if not self.onboarding:
            self._icon_button("user-trash-symbolic", _("Remove"), self._confirm_remove)

    def _update_progress(self) -> None:
        progress = self.client.progress.get(self.model_id)
        self._progress_revealer.set_reveal_child(progress is not None)
        if progress is None:
            return
        phase, done, total = progress
        self._phase.set_label(PHASE_LABELS.get(phase, PHASE_LABELS["download"])())
        if phase == "download" and total:
            fraction = done / total
            self._progress.set_fraction(fraction)
            self._percent.set_label(_("{done} of {total} · {percent}").format(
                done=format_size(done), total=format_size(total),
                percent=format_percent(fraction)))
        elif total:
            self._progress.set_fraction(done / total)
            self._percent.set_label(format_percent(done / total))
        else:
            self._progress.pulse()
            self._percent.set_label("")
        self.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                             [f"{self._phase.get_label()} {self._percent.get_label()}"])

    def on_progress(self, phase: str, message: str) -> None:
        """ModelProgress for this row (the client already updated ``progress``)."""
        if phase in ("done", "error", "cancelled"):
            self._update_badges()
            self._update_actions()
            self._update_progress()
            return
        if not self._progress_revealer.get_reveal_child():
            self._update_badges()
            self._update_actions()
        self._update_progress()

    # -- actions ----------------------------------------------------------- #

    def _install(self) -> None:
        name = self.model.get("name", self.model_id)
        # Show the progress bar right away; ModelProgress takes over.
        self.client.progress.setdefault(
            self.model_id, ("download", 0, int(self.model.get("download", {}).get("size", 0))))
        self.on_progress("download", "")

        def failed(err) -> None:
            self.client.progress.pop(self.model_id, None)
            self.on_progress("error", err.message)
            show_toast(self, _("Could not download {name}: {error}").format(
                name=name, error=err.message), urgent=True)

        self.client.install_model(self.model_id, on_error=failed)

    def _cancel(self) -> None:
        self.client.cancel_install(self.model_id)

    def _use(self) -> None:
        self.client.set_config(activation_patch(self.client, self.model),
                               on_error=lambda err: show_toast(self, err.message, urgent=True))

    def _confirm_remove(self) -> None:
        name = self.model.get("name", self.model_id)
        size = self.model.get("size_on_disk") or self.model.get("disk_size") or 0
        body = _("The model files ({size}) will be deleted from this computer. You can "
                 "download it again later.").format(size=format_size(size))
        if self.client.is_active(self.model):
            others = [m for m in self.client.installed_asr() if m.get("id") != self.model_id]
            if not others:
                body += "\n\n" + _("This is the only installed model: dictation will not "
                                   "work until you download another one.")
        dialog = Adw.AlertDialog(heading=_("Remove {name}?").format(name=name), body=body)
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("remove", _("_Remove"))
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, r: r == "remove" and self.client.remove_model(
            self.model_id))
        dialog.present(self.get_root())


def sorted_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Recommended first, then the catalog order."""
    return sorted(models, key=lambda m: 0 if m.get("recommended") else 1)


class ModelList(Gtk.ListBox):
    """Boxed list of ModelRow kept in sync with the client."""

    __gtype_name__ = "GreffierModelList"

    def __init__(self, client: DaemonClient, connections: Connections, *,
                 onboarding: bool = False) -> None:
        super().__init__(selection_mode=Gtk.SelectionMode.NONE)
        self.add_css_class("boxed-list")
        self.client = client
        self.onboarding = onboarding
        self.rows: dict[str, ModelRow] = {}
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=24,
                          margin_bottom=24, halign=Gtk.Align.CENTER)
        spin = spinner()
        spin.set_size_request(24, 24)
        loading.append(spin)
        text = Gtk.Label(label=_("Loading the model list…"))
        text.add_css_class("dim-label")
        loading.append(text)
        self.set_placeholder(loading)
        connections.connect(client, "models-updated", lambda *_a: self.rebuild())
        connections.connect(client, "model-progress", self._on_progress)
        connections.connect(client, "config-changed", lambda *_a: self.rebuild())
        self.rebuild()

    def rebuild(self) -> None:
        models = sorted_models(self.client.asr_models())
        ids = [m["id"] for m in models]
        if ids != list(self.rows):
            clear_box(self)
            self.rows = {}
            for model in models:
                row = ModelRow(self.client, model, onboarding=self.onboarding)
                self.rows[model["id"]] = row
                self.append(row)
        else:
            for model in models:
                self.rows[model["id"]].update(model)

    def _on_progress(self, _client, model_id: str, phase: str, _done, _total,
                     message: str) -> None:
        row = self.rows.get(model_id)
        if row is not None:
            row.on_progress(phase, message)


# --------------------------------------------------------------------------- #
# Preferences page
# --------------------------------------------------------------------------- #


class ModelsPage(Adw.PreferencesPage):
    __gtype_name__ = "GreffierModelsPage"

    def __init__(self, client: DaemonClient, binder: ConfigBinder,
                 connections: Connections) -> None:
        super().__init__(title=_("Models"), icon_name="drive-harddisk-symbolic",
                         name="models")
        self.client = client
        self.binder = binder
        self._connections = connections
        self._check_handler = 0
        self._check_timeout = 0

        # -- models -------------------------------------------------------- #
        group = Adw.PreferencesGroup(
            title=_("Speech Recognition"),
            description=_("Models run entirely on this computer, without sending anything "
                          "online."))
        self._check_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self._check_button.add_css_class("flat")
        self._check_content = Adw.ButtonContent(icon_name="view-refresh-symbolic",
                                                label=_("Check for _Updates"),
                                                use_underline=True)
        self._check_stack = Gtk.Stack()
        self._check_spinner = spinner()
        self._check_stack.add_named(self._check_content, "idle")
        busy = Gtk.Box(spacing=6)
        busy.append(self._check_spinner)
        busy.append(Gtk.Label(label=_("Checking…")))
        self._check_stack.add_named(busy, "busy")
        self._check_button.set_child(self._check_stack)
        self._check_button.connect("clicked", self._on_check_updates)
        group.set_header_suffix(self._check_button)
        self.model_list = ModelList(client, connections)
        group.add(self.model_list)
        self.add(group)

        # -- language ------------------------------------------------------- #
        lang_group = Adw.PreferencesGroup(title=_("Language"))
        self._language = Adw.ComboRow(title=_("Spoken Language"), use_subtitle=False)
        self._language.set_enable_search(True)
        self._language.set_expression(
            Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"))
        self._language.set_model(Gtk.StringList())
        binder.combo(self._language, "model", "language", [])
        lang_group.add(self._language)
        self.add(lang_group)

        # -- performance ------------------------------------------------------ #
        perf = Adw.PreferencesGroup(title=_("Performance"))
        threads = Adw.SpinRow.new_with_range(0, 64, 1)
        threads.set_title(_("Processor Threads"))
        threads.set_subtitle(_("0 chooses automatically"))
        binder.spin(threads, "model", "threads")
        perf.add(threads)

        provider = Adw.ComboRow(title=_("Hardware Acceleration"),
                                subtitle=_("NVIDIA graphics cards need the CUDA libraries"),
                                model=Gtk.StringList.new([_("None (Processor)"),
                                                          _("NVIDIA CUDA")]))
        binder.combo(provider, "model", "provider", ["cpu", "cuda"])
        perf.add(provider)

        preload = Adw.SwitchRow(title=_("Load at Startup"),
                                subtitle=_("Dictation starts instantly, but uses memory while "
                                           "idle"))
        binder.switch(preload, "model", "preload")
        perf.add(preload)

        unload = Adw.SpinRow.new_with_range(0, 1440, 5)
        unload.set_title(_("Free Memory After"))
        unload.set_subtitle(_("Minutes of inactivity; 0 keeps the model loaded"))
        binder.spin(unload, "model", "unload_after")
        perf.add(unload)
        self.add(perf)

        # -- updates ---------------------------------------------------------- #
        updates = Adw.PreferencesGroup(title=_("Updates"))
        check = Adw.SwitchRow(title=_("Check for Updates Automatically"),
                              subtitle=_("Look for new and improved models once a day"))
        binder.switch(check, "model", "check_updates")
        updates.add(check)
        catalog = Adw.EntryRow(title=_("Catalog Address"), show_apply_button=True)
        catalog.set_input_purpose(Gtk.InputPurpose.URL)
        binder.entry(catalog, "model", "catalog_url")
        updates.add(catalog)
        self.add(updates)

        connections.connect(client, "models-updated", lambda *_a: self._update_languages())
        binder.on_sync(self._update_languages)

    # ------------------------------------------------------------------ #

    def _update_languages(self) -> None:
        model = self.client.active_model_info()
        current = self.client.get("model", "language", "auto")
        if model is None:
            values = [current]
            self._language.set_sensitive(False)
            self._language.set_subtitle(_("Install a model first"))
        else:
            mode = model.get("language_mode", "auto")
            langs = model_languages(model)
            if mode == "auto":
                values = ["auto"]
                self._language.set_subtitle(
                    _("{name} recognises the language automatically").format(
                        name=model.get("name", "")))
            else:
                values = ([] if mode == "required" else ["auto"]) + langs
                self._language.set_subtitle(
                    _("Choose the language you dictate in") if mode == "required" else
                    _("“Automatic” lets {name} detect it").format(name=model.get("name", "")))
            self._language.set_sensitive(len(values) > 1)
        self.binder.set_combo_values(self._language, values,
                                     [language_name(v) for v in values])

    def _set_checking(self, busy: bool) -> None:
        self._check_stack.set_visible_child_name("busy" if busy else "idle")
        self._check_button.set_sensitive(not busy)
        if not busy:
            if self._check_handler:
                self.client.disconnect(self._check_handler)
                self._check_handler = 0
            if self._check_timeout:
                GLib.source_remove(self._check_timeout)
                self._check_timeout = 0

    def _on_check_updates(self, _button) -> None:
        self._set_checking(True)

        def done(*_args) -> None:
            self._set_checking(False)
            count = sum(1 for m in self.client.asr_models()
                        if m.get("installed") and m.get("update_available"))
            show_toast(self, ngettext("{n} update available", "{n} updates available",
                                      count).format(n=count)
                       if count else _("All installed models are up to date"))

        def failed(err) -> None:
            self._set_checking(False)
            show_toast(self, err.message, urgent=True)

        def timeout() -> bool:
            self._check_timeout = 0
            self._set_checking(False)
            return GLib.SOURCE_REMOVE

        self._check_handler = self.client.connect("models-updated", done)
        self._check_timeout = GLib.timeout_add_seconds(45, timeout)
        self.client.check_updates(on_error=failed)

    def do_unrealize(self) -> None:
        self._set_checking(False)
        Adw.PreferencesPage.do_unrealize(self)
