"""Pure helpers of the GUI: formatting, languages, shortcuts (no display needed)."""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
try:
    import greffier.ui  # noqa: F401 - gi.require_version()
except (ImportError, ValueError) as exc:  # pragma: no cover - no GTK 4 / libadwaita
    pytest.skip(f"GTK 4 / libadwaita not available: {exc}", allow_module_level=True)

from gi.repository import Gdk, GLib

from greffier.ui import util
from greffier.ui.diagnostics import check_hint, format_report
from greffier.ui.models_page import activation_patch, sorted_models
from greffier.ui.shortcut_dialog import accelerator_for_event


def test_modules_import():
    import greffier.ui.app
    import greffier.ui.client
    import greffier.ui.history
    import greffier.ui.mic_button
    import greffier.ui.onboarding
    import greffier.ui.preferences
    import greffier.ui.window  # noqa: F401

    assert callable(greffier.ui.app.main)


def test_format_size_rounds_and_drops_zero_decimals():
    def size(n):  # GLib separates the unit with a no-break space
        return util.format_size(n).replace("\xa0", " ")

    assert size(487_170_055) == "487 MB"
    assert size(1_699_791_751) == "1.7 GB"
    assert size(900 * 1_000_000) == "900 MB"
    assert util.format_ram(3500).replace("\xa0", " ") == "3.5 GB"


def test_format_percent_and_clock():
    assert util.format_percent(0.42) == "42%"
    assert util.format_percent(2) == "100%"
    assert util.format_clock(7.9) == "0:07"
    assert util.format_clock(3725) == "1:02:05"


def test_relative_time():
    now = GLib.DateTime.new_local(2026, 10, 28, 15, 0, 0)
    assert util.format_relative_time(now.add_seconds(-20), now) == "Just now"
    assert util.format_relative_time(now.add_minutes(-1), now) == "1 minute ago"
    assert util.format_relative_time(now.add_minutes(-14), now) == "14 minutes ago"
    assert util.format_relative_time(now.add_hours(-3), now) in ("12:00", "12:00 PM")
    assert util.format_relative_time(now.add_days(-1), now).startswith("Yesterday at ")
    assert util.format_relative_time(now.add_days(-10), now) == "October 18"
    assert util.format_relative_time(None) == ""


def test_parse_time_accepts_offsets_and_naive_strings():
    assert util.parse_time("2026-10-28T14:32:00+01:00") is not None
    assert util.parse_time("2026-10-28T14:32:00") is not None
    assert util.parse_time("not a date") is None
    assert util.parse_time("") is None


def test_languages():
    assert util.language_name("fr") == "Français"
    assert util.language_name("auto") == "Automatic"
    parakeet = {"languages": ["fr", "en", "de", "es", "it", "nl"], "language_mode": "auto"}
    assert util.languages_summary(parakeet) == "6 languages, detected automatically"
    canary = {"languages": ["de", "en", "es", "fr"], "language_mode": "required"}
    assert util.languages_summary(canary) == "Deutsch, English, Español, Français"
    whisper = {"languages": ["*"], "language_mode": "optional"}
    assert util.languages_summary(whisper) == "Multilingual"
    assert "fr" in util.model_languages(whisper)


def test_describe_picks_interface_language():
    model = {"description": {"fr": "Bonjour", "en": "Hello"}}
    assert util.describe(model) == "Hello"  # GREFFIER_LANG=en (conftest)
    assert util.describe({"description": {"fr": "Bonjour"}}) == "Bonjour"


def test_shortcut_helpers():
    assert util.accel_is_valid("<Super><Alt>d")
    assert not util.accel_is_valid("")
    assert "D" in util.accel_label("<Super><Alt>d")
    assert util.accel_label("") == "Disabled"
    assert util.split_template("Press {shortcut} anywhere", "{shortcut}") == ("Press",
                                                                              "anywhere")
    assert util.split_template("{shortcut} pour dicter", "{shortcut}") == ("", "pour dicter")


def test_accelerator_for_event():
    super_alt = Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.ALT_MASK
    accel = accelerator_for_event(Gdk.KEY_D, super_alt)
    assert accel is not None and accel.endswith("d") and "<Super>" in accel
    assert accelerator_for_event(Gdk.KEY_a, Gdk.ModifierType(0)) is None  # no modifier
    assert accelerator_for_event(Gdk.KEY_a, Gdk.ModifierType.SHIFT_MASK) is None
    assert accelerator_for_event(Gdk.KEY_F9, Gdk.ModifierType(0)) == "F9"
    assert accelerator_for_event(Gdk.KEY_Control_L, Gdk.ModifierType.CONTROL_MASK) is None


class _Client:
    def __init__(self, language: str) -> None:
        self.config = {"model": {"language": language}}

    def get(self, section, key, default=None):
        return self.config.get(section, {}).get(key, default)


def test_activation_patch_fixes_unsupported_language():
    canary = {"id": "canary", "languages": ["de", "en", "es", "fr"],
              "language_mode": "required"}
    assert activation_patch(_Client("fr"), canary) == {"model": {"active": "canary"}}
    assert activation_patch(_Client("auto"), canary)["model"]["language"] == "en"
    cohere = {"id": "cohere", "languages": ["ja", "zh"], "language_mode": "required"}
    assert activation_patch(_Client("fr"), cohere)["model"]["language"] == "ja"
    whisper = {"id": "whisper", "languages": ["*"], "language_mode": "optional"}
    assert activation_patch(_Client("auto"), whisper) == {"model": {"active": "whisper"}}


def test_sorted_models_puts_recommended_first():
    models = [{"id": "a"}, {"id": "b", "recommended": True}, {"id": "c"}]
    assert [m["id"] for m in sorted_models(models)] == ["b", "a", "c"]


def test_check_hints_and_report():
    assert check_hint({"id": "extension", "ok": False})
    assert check_hint({"id": "extension", "ok": True}) == ""
    assert check_hint({"id": "x", "ok": False, "hint": "Do this"}) == "Do this"
    assert "Allow Remote Interaction" in check_hint({"id": "keyboard", "ok": True}, "portal")
    report = format_report({"version": "0.1.0", "checks": [
        {"id": "model", "ok": True, "level": "ok", "message": "Model: X"}]}, {"state": "idle"})
    assert "✓ [model] Model: X" in report and '"state": "idle"' in report
    assert "not running" in format_report(None, None)


def test_dbus_errors_become_user_messages():
    from gi.repository import Gio

    from greffier.ui.client import error_from_glib

    def remote(name: str, message: str) -> GLib.Error:
        return GLib.Error.new_literal(Gio.io_error_quark(), f"GDBus.Error:{name}: {message}",
                                      Gio.IOErrorEnum.DBUS_ERROR)

    err = error_from_glib(remote("io.github.greffier.Greffier.Error.Busy",
                                 "Cancel the download first."))
    assert (err.code, err.message) == ("Busy", "Cancel the download first.")
    err = error_from_glib(remote("org.freedesktop.DBus.Error.ServiceUnknown", "x"))
    assert err.code == "NotRunning"
    err = error_from_glib(remote("org.freedesktop.DBus.Error.NoReply", "x"))
    assert err.code == "Timeout"
