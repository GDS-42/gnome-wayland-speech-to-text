"""Widget smoke tests: main window, preferences, onboarding, custom widgets.

They need a display and are skipped otherwise. To run them headless::

    gtk4-broadwayd :5 & GDK_BACKEND=broadway BROADWAY_DISPLAY=:5 pytest tests/test_ui_widgets.py
"""

from __future__ import annotations

import shutil

import pytest

pytest.importorskip("gi")
if shutil.which("dbus-daemon") is None:  # pragma: no cover
    pytest.skip("dbus-daemon is not installed", allow_module_level=True)
try:
    import greffier.ui  # noqa: F401 - gi.require_version()
except (ImportError, ValueError) as exc:  # pragma: no cover
    pytest.skip(f"GTK 4 / libadwaita not available: {exc}", allow_module_level=True)

from gi.repository import Adw, Gdk, Gtk

Gtk.init_check()
if Gdk.Display.get_default() is None:  # pragma: no cover - CI without display
    pytest.skip("no display available", allow_module_level=True)
Adw.init()

from greffier.const import State  # noqa: E402
from greffier.ui.client import DaemonClient  # noqa: E402
from greffier.ui.history import HistoryRow  # noqa: E402
from greffier.ui.mic_button import MicButton  # noqa: E402
from greffier.ui.models_page import DotMeter, ModelRow, ModelsPage  # noqa: E402
from greffier.ui.preferences import GeneralPage  # noqa: E402
from greffier.ui.shortcut_dialog import ShortcutDialog  # noqa: E402
from greffier.ui.window import MainWindow  # noqa: E402
from tests.fake_daemon import FakeService, wait_until  # noqa: E402

pytestmark = pytest.mark.gui


def _connected_client(service: FakeService) -> DaemonClient:
    client = DaemonClient(service.connection())
    client.connect_to_service()
    assert wait_until(lambda: client.connected and client.models_loaded, 10)
    return client


def _descendants(widget: Gtk.Widget, cls: type) -> list:
    found, stack = [], [widget]
    while stack:
        current = stack.pop()
        if isinstance(current, cls):
            found.append(current)
        child = current.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return found


@pytest.fixture
def service():
    with FakeService("--fast") as svc:
        yield svc


def test_mic_button_states_and_level():
    window = Gtk.Window()
    mic = MicButton(disc_size=64)
    window.set_child(mic)
    window.present()
    for state in (State.IDLE, State.RECORDING, State.TRANSCRIBING, State.LOADING,
                  State.NO_MODEL, State.ERROR):
        mic.set_state(state)
        mic.set_level(0.7)
        wait_until(lambda: False, 0.05)
    mic.set_state(State.RECORDING)
    assert mic.has_css_class("recording")
    assert mic.get_tooltip_text() == "Stop Dictation"
    mic.set_disc_size(96)
    assert mic.get_size_request()[0] > 96
    window.destroy()


def test_dot_meter_measures():
    meter = DotMeter(4, "Speed")
    assert meter.measure(Gtk.Orientation.HORIZONTAL, -1)[0] == 5 * 7 + 4 * 3


def test_main_window_shows_state_history_and_preferences(service):
    client = _connected_client(service)
    window = MainWindow(None, client)
    window.present()
    view = window.main_view
    assert wait_until(lambda: window.stack.get_visible_child_name() == "main", 10)
    assert wait_until(lambda: len(_descendants(view.history.list, HistoryRow)) == 6, 5)
    assert wait_until(lambda: view.title.get_label() == "Ready to Dictate", 5)
    assert "Parakeet" in view.model_row.get_title()

    view.mic.emit("clicked")
    assert wait_until(lambda: view.title.get_label() == "Listening…", 5)
    assert view.cancel_revealer.get_reveal_child()
    view.cancel.emit("clicked")
    assert wait_until(lambda: client.state == State.IDLE, 5)

    window.show_preferences("models")
    dialog = window._prefs
    assert dialog is not None and dialog.get_visible_page_name() == "models"
    page = _descendants(dialog, ModelsPage)[0]
    assert len(page.model_list.rows) == 5
    assert isinstance(page.model_list.rows["parakeet-tdt-0.6b-v3-int8"], ModelRow)

    general = _descendants(dialog, GeneralPage)[0]
    sounds = next(r for r in _descendants(general, Adw.SwitchRow) if r.get_title() == "Sounds")
    sounds.set_active(False)
    assert wait_until(lambda: client.get("general", "sounds") is False, 5)
    dialog.close()
    window.destroy()


def test_onboarding_on_first_run(service):
    service.stop_daemon()
    service.start_daemon("--fast", "--scenario", "first-run")
    client = _connected_client(service)
    window = MainWindow(None, client)
    window.present()
    assert wait_until(lambda: window.stack.get_visible_child_name() == "onboarding", 10)
    onboarding = window._onboarding
    assert [p.name for p in onboarding.pages] == ["welcome", "model", "shortcut", "try", "done"]
    assert not onboarding._model_next.get_sensitive()  # nothing installed yet

    row = onboarding._models.rows["parakeet-tdt-0.6b-v3-int8"]
    row._install()
    assert wait_until(lambda: client.installed_asr(), 10)
    assert wait_until(lambda: onboarding._model_next.get_sensitive(), 5)

    onboarding.finish()
    assert wait_until(lambda: client.get("general", "first_run_done") is True, 5)
    assert wait_until(lambda: window.stack.get_visible_child_name() == "main", 5)
    window.destroy()


def test_shortcut_dialog_capture():
    dialog = ShortcutDialog("<Super><Alt>d")
    chosen = []
    dialog.connect("chosen", lambda _d, accel: chosen.append(accel))
    mods = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
    assert dialog._on_key_pressed(None, Gdk.KEY_space, 0, mods)
    assert dialog._set.get_sensitive()
    dialog._on_set()
    assert chosen == ["<Control><Alt>space"]
    dialog2 = ShortcutDialog("")
    dialog2.connect("chosen", lambda _d, accel: chosen.append(accel))
    dialog2._on_key_pressed(None, Gdk.KEY_BackSpace, 0, Gdk.ModifierType(0))
    assert chosen[-1] == ""
