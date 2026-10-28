"""System page of the preferences: GetDiagnostics as an actionable checklist."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from gi.repository import Adw, Gio, GLib, Gtk

from greffier.const import PROJECT_URL, SYSTEMD_UNIT, VERSION
from greffier.i18n import N_, _
from greffier.ui.binding import Connections
from greffier.ui.client import DaemonClient
from greffier.ui.util import button_row, copy_text, open_uri, show_toast, spinner, status_icon

EXTENSION_HELP_URL = f"{PROJECT_URL}#gnome-shell-extension"


def launch_desktop(desktop_id: str) -> bool:
    try:
        info = Gio.DesktopAppInfo.new(desktop_id)
    except TypeError:
        info = None
    if info is None:
        return False
    try:
        return bool(info.launch([], None))
    except GLib.Error:
        return False


PORTAL_CONSENT_HINT = N_(
    "The first time you dictate, GNOME asks for permission: turn on “Allow Remote "
    "Interaction”, then click “Share”.")


def check_hint(check: dict[str, Any], keyboard: str = "") -> str:
    """Extra explanation for a check (a "hint" key from the service wins).

    ``keyboard`` is the keyboard backend in use (GetStatus injection.keyboard):
    with the RemoteDesktop portal, GNOME shows a consent dialog whose "Allow
    Remote Interaction" switch is off by default.
    """
    if check.get("hint"):
        return str(check["hint"])
    if check.get("id") == "keyboard" and keyboard == "portal":
        return _(PORTAL_CONSENT_HINT)
    if check.get("ok"):
        return ""
    return {
        "session": _("Shortcuts and text insertion may only partly work outside GNOME on "
                     "Wayland."),
        "model": _("Download a speech model to start dictating."),
        "model-load": _("Try another model, or read the service log with “journalctl --user "
                        "-u {unit}”.").format(unit=SYSTEMD_UNIT),
        "vad": _("Long dictations and stopping on silence work better with it."),
        "shortcut": _("Choose another shortcut, or check GNOME Settings › Keyboard."),
        "extension": _("It shows a recording indicator in the top bar and inserts text "
                       "instantly."),
        "keyboard": _("Install the GNOME Shell extension, or turn on “Allow Remote "
                      "Interaction” when GNOME asks for permission."),
        "microphone": _("Plug in a microphone, or allow access in GNOME Settings › Privacy."),
    }.get(str(check.get("id", "")), "")


def format_report(report: dict[str, Any] | None, status: dict[str, Any] | None) -> str:
    """Plain-text report for bug reports ("Copy Report")."""
    lines = [f"Greffier {VERSION}"]
    if report:
        ext = report.get("extension") or {}
        lines += [
            f"Service: {report.get('version', '?')}",
            f"GNOME Shell: {report.get('gnome_shell') or '-'}",
            f"Session: {report.get('session_type') or '-'} {report.get('desktop', '')}".rstrip(),
            f"sherpa-onnx: {report.get('sherpa_onnx') or '-'}",
            f"CPU threads: {report.get('cpu_threads', '-')}",
            f"Microphones: {report.get('audio_devices', '-')}",
            "Extension: " + ", ".join(k for k in ("installed", "enabled", "active")
                                      if ext.get(k)) if ext else "Extension: -",
            "",
            "Checks:",
        ]
        marks = {"ok": "✓", "warning": "!", "error": "✗"}
        for check in report.get("checks") or []:
            mark = marks.get(check.get("level"), "?")
            lines.append(f"  {mark} [{check.get('id', '')}] {check.get('message', '')}")
        lines += ["", "Diagnostics:", json.dumps(report, indent=2, ensure_ascii=False)]
    else:
        lines.append("Service: not running")
    if status:
        lines += ["", "Status:", json.dumps(status, indent=2, ensure_ascii=False)]
    return "\n".join(lines)


class SystemPage(Adw.PreferencesPage):
    __gtype_name__ = "GreffierSystemPage"

    def __init__(self, client: DaemonClient, connections: Connections, *,
                 show_page: Callable[[str], None] | None = None,
                 on_run_setup: Callable[[], None] | None = None) -> None:
        super().__init__(title=_("System"), icon_name="computer-symbolic", name="system")
        self.client = client
        self._show_page = show_page or (lambda _name: None)
        self._on_run_setup = on_run_setup
        self.report: dict[str, Any] | None = None

        # -- checks ---------------------------------------------------------- #
        self._checks_group = Adw.PreferencesGroup(
            title=_("Checks"),
            description=_("What Greffier needs to work well on this computer."))
        self._refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                                   tooltip_text=_("Check Again"))
        self._refresh.add_css_class("flat")
        self._refresh.connect("clicked", lambda *_a: self.refresh())
        self._checks_group.set_header_suffix(self._refresh)
        self._checks = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._checks.add_css_class("boxed-list")
        busy = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=18, margin_bottom=18)
        busy.append(spinner())
        busy.append(Gtk.Label(label=_("Checking…")))
        self._checks.set_placeholder(busy)
        self._checks_group.add(self._checks)
        self.add(self._checks_group)

        # -- details --------------------------------------------------------- #
        self._details_group = Adw.PreferencesGroup(title=_("Details"))
        self._details: dict[str, Adw.ActionRow] = {}
        for key, title in (
            ("version", _("Version")),
            ("gnome_shell", _("GNOME Shell")),
            ("session", _("Session")),
            ("engine", _("Speech Engine")),
            ("threads", _("Processor Threads")),
            ("extension", _("GNOME Shell Extension")),
            ("portals", _("Portals")),
            ("tools", _("Command-Line Tools")),
        ):
            row = Adw.ActionRow(title=title)
            row.add_css_class("property")
            row.set_subtitle_selectable(True)
            self._details[key] = row
            self._details_group.add(row)
        self.add(self._details_group)

        # -- support --------------------------------------------------------- #
        support = Adw.PreferencesGroup(title=_("Troubleshooting"))
        support.add(button_row(_("Copy _Report"), "edit-copy-symbolic", self._copy_report))
        support.add(button_row(_("Run _Setup Assistant Again"), "view-refresh-symbolic",
                               self._run_setup))
        self.add(support)

        service = Adw.PreferencesGroup(title=_("Background Service"))
        self._service_row = Adw.ActionRow(title=_("Greffier Service"))
        self._service_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self._service_button.connect("clicked", self._on_service_clicked)
        self._service_row.add_suffix(self._service_button)
        service.add(self._service_row)
        self.add(service)

        connections.connect(client, "notify::connected", lambda *_a: self.refresh())
        connections.connect(client, "notify::starting", lambda *_a: self._update_service())
        self.refresh()

    # ------------------------------------------------------------------ #

    def refresh(self) -> None:
        self._update_service()
        if not self.client.connected:
            self.report = None
            self._render()
            return
        self._refresh.set_sensitive(False)

        def done(report: Any) -> None:
            self._refresh.set_sensitive(True)
            self.report = report if isinstance(report, dict) else None
            self._render()

        def failed(err) -> None:
            self._refresh.set_sensitive(True)
            show_toast(self, err.message, urgent=True)

        self.client.get_diagnostics(done, on_error=failed)

    def _clear_checks(self) -> None:
        child = self._checks.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._checks.remove(child)
            child = nxt

    def _render(self) -> None:
        self._clear_checks()
        report = self.report
        if report is None:
            row = Adw.ActionRow(title=_("The Greffier service is not running"),
                                subtitle=_("Dictation, shortcuts and models need it."))
            row.add_prefix(status_icon("error"))
            self._checks.append(row)
            self._details_group.set_visible(False)
            return
        self._details_group.set_visible(True)
        checks = sorted(report.get("checks") or [],
                        key=lambda c: {"error": 0, "warning": 1}.get(c.get("level"), 2))
        for check in checks:
            if isinstance(check, dict):
                self._checks.append(self._check_row(check))
        self._fill_details(report)

    def _check_row(self, check: dict[str, Any]) -> Adw.ActionRow:
        level = check.get("level") or ("ok" if check.get("ok") else "warning")
        row = Adw.ActionRow(title=GLib.markup_escape_text(str(check.get("message", ""))))
        row.set_title_lines(0)
        keyboard = ((self.client.status or {}).get("injection") or {}).get("keyboard", "")
        hint = check_hint(check, keyboard)
        if hint:
            row.set_subtitle(GLib.markup_escape_text(hint))
        row.add_prefix(status_icon(level))
        action = self._check_action(check)
        if action is not None and not check.get("ok"):
            label, callback = action
            button = Gtk.Button(label=label, use_underline=True, valign=Gtk.Align.CENTER)
            button.connect("clicked", lambda *_a: callback())
            row.add_suffix(button)
        return row

    def _check_action(self, check: dict[str, Any]):
        check_id = str(check.get("id", ""))
        ext = (self.report or {}).get("extension") or {}
        if check_id == "model":
            return _("_Choose…"), lambda: self._show_page("models")
        if check_id == "model-load":
            return _("_Models…"), lambda: self._show_page("models")
        if check_id == "vad":
            return _("_Install"), lambda: self.client.install_model("silero-vad")
        if check_id == "shortcut":
            return _("_Change…"), lambda: self._show_page("dictation")
        if check_id in ("extension", "keyboard"):
            if ext.get("installed") and not ext.get("active"):
                return _("_Open Extensions"), self._open_extensions
            return _("_Learn More"), lambda: open_uri(self, EXTENSION_HELP_URL)
        if check_id == "microphone":
            return _("_Sound Settings"), lambda: launch_desktop("gnome-sound-panel.desktop")
        return None

    def _open_extensions(self) -> None:
        if not launch_desktop("org.gnome.Extensions.desktop"):
            open_uri(self, EXTENSION_HELP_URL)

    def _fill_details(self, report: dict[str, Any]) -> None:
        yes, no = _("Yes"), _("No")
        service_version = report.get("version", "")
        version = VERSION if service_version in ("", VERSION) else _(
            "{gui} (service {service})").format(gui=VERSION, service=service_version)
        ext = report.get("extension") or {}
        if ext.get("active"):
            ext_text = _("Active")
        elif ext.get("enabled"):
            ext_text = _("Enabled, active after logging in again")
        elif ext.get("installed"):
            ext_text = _("Installed, not enabled")
        else:
            ext_text = _("Not installed")
        portals = report.get("portals") or {}
        tools = report.get("tools") or {}
        session = str(report.get("session_type") or "").capitalize() or _("Unknown")
        values = {
            "version": version,
            "gnome_shell": report.get("gnome_shell") or _("Not detected"),
            "session": session,
            "engine": f"sherpa-onnx {report.get('sherpa_onnx')}" if report.get("sherpa_onnx")
            else _("Not installed"),
            "threads": str(report.get("cpu_threads", "")),
            "extension": ext_text,
            "portals": " · ".join(
                f"{label}: {yes if portals.get(key) else no}" for key, label in (
                    ("global_shortcuts", _("Global Shortcuts")),
                    ("remote_desktop", _("Remote Desktop")))),
            "tools": " · ".join(f"{name}: {yes if ok else no}" for name, ok in tools.items())
            or _("None"),
        }
        for key, row in self._details.items():
            row.set_subtitle(GLib.markup_escape_text(str(values.get(key, ""))))

    # -- actions ------------------------------------------------------------- #

    def _copy_report(self) -> None:
        copy_text(self, format_report(self.report, self.client.status))
        show_toast(self, _("Report copied to clipboard"))

    def _run_setup(self) -> None:
        if self._on_run_setup is not None:
            self._on_run_setup()

    def _update_service(self) -> None:
        if self.client.starting:
            self._service_row.set_subtitle(_("Starting…"))
            self._service_button.set_label(_("Start"))
            self._service_button.set_sensitive(False)
        elif self.client.connected:
            version = self.client.version or (self.report or {}).get("version", "")
            self._service_row.set_subtitle(
                _("Running, version {version}").format(version=version) if version
                else _("Running"))
            self._service_button.set_label(_("_Stop"))
            self._service_button.set_use_underline(True)
            self._service_button.set_sensitive(True)
        else:
            self._service_row.set_subtitle(_("Not running"))
            self._service_button.set_label(_("_Start"))
            self._service_button.set_use_underline(True)
            self._service_button.set_sensitive(True)

    def _on_service_clicked(self, _button) -> None:
        if self.client.connected:
            dialog = Adw.AlertDialog(
                heading=_("Stop the Greffier Service?"),
                body=_("The dictation shortcut will not work until the service starts "
                       "again, for example at your next login."))
            dialog.add_response("cancel", _("_Cancel"))
            dialog.add_response("stop", _("_Stop"))
            dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_close_response("cancel")
            dialog.connect("response", lambda _d, r: r == "stop" and self.client.quit_service())
            dialog.present(self.get_root())
        else:
            self.client.start_service(
                lambda ok, msg: ok or show_toast(
                    self, _("Could not start the service: {error}").format(error=msg),
                    urgent=True))

