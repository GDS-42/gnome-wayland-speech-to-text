"""Keyboard shortcut capture dialog, modelled on GNOME Settings.

Press a combination to preview it, then "Set". Escape cancels, Backspace
disables the shortcut. System shortcuts are inhibited while the dialog is
open (Wayland) so that combinations already used by GNOME Shell can be typed.
"""

from __future__ import annotations

from gi.repository import Adw, Gdk, GObject, Gtk

from greffier.i18n import _
from greffier.ui.util import accel_label, shortcut_label

_MODIFIER_KEYS = {
    Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, Gdk.KEY_Control_R,
    Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R, Gdk.KEY_Super_L,
    Gdk.KEY_Super_R, Gdk.KEY_Hyper_L, Gdk.KEY_Hyper_R, Gdk.KEY_ISO_Level3_Shift,
    Gdk.KEY_ISO_Level5_Shift, Gdk.KEY_Caps_Lock, Gdk.KEY_Num_Lock,
}
# Keys that make sense without a modifier for a global shortcut.
_STANDALONE_KEYS = {getattr(Gdk, f"KEY_F{i}") for i in range(1, 25)} | {
    Gdk.KEY_Pause, Gdk.KEY_Scroll_Lock, Gdk.KEY_Print, Gdk.KEY_Menu, Gdk.KEY_Insert,
}


def accelerator_for_event(keyval: int, state: Gdk.ModifierType) -> str | None:
    """Accelerator for a key press, or None if it is not a usable shortcut."""
    mods = state & Gtk.accelerator_get_default_mod_mask()
    keyval = Gdk.keyval_to_lower(keyval)
    if keyval == Gdk.KEY_ISO_Left_Tab:
        keyval = Gdk.KEY_Tab
    if keyval in _MODIFIER_KEYS:
        return None
    if not mods and keyval not in _STANDALONE_KEYS and not _is_media_key(keyval):
        return None
    if mods == Gdk.ModifierType.SHIFT_MASK and Gdk.keyval_to_unicode(keyval):
        return None  # Shift+letter would break typing capitals
    return Gtk.accelerator_name(keyval, mods)


def _is_media_key(keyval: int) -> bool:
    name = Gdk.keyval_name(keyval) or ""
    return name.startswith("XF86")


class ShortcutDialog(Adw.Dialog):
    """Emits ``chosen(accelerator)``; an empty accelerator disables the shortcut."""

    __gtype_name__ = "GreffierShortcutDialog"
    __gsignals__ = {"chosen": (GObject.SignalFlags.RUN_FIRST, None, (str,))}

    def __init__(self, current: str = "", push_to_talk: bool = False) -> None:
        super().__init__(title=_("Set Shortcut"), content_width=440, content_height=360)
        self._accel = ""
        self._inhibited = None

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        self._cancel = Gtk.Button(label=_("_Cancel"), use_underline=True)
        self._cancel.connect("clicked", lambda *_a: self.close())
        self._set = Gtk.Button(label=_("_Set"), use_underline=True, sensitive=False)
        self._set.add_css_class("suggested-action")
        self._set.connect("clicked", self._on_set)
        header.pack_start(self._cancel)
        header.pack_end(self._set)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER,
                      margin_top=12, margin_bottom=24, margin_start=24, margin_end=24)
        image = Gtk.Image(icon_name="input-keyboard-symbolic", pixel_size=72)
        image.add_css_class("dim-label")
        image.set_margin_bottom(6)
        title = Gtk.Label(label=_("Press the new shortcut"), wrap=True,
                          justify=Gtk.Justification.CENTER)
        title.add_css_class("title-2")
        what = (_("Hold it while you speak.") if push_to_talk else
                _("It starts and stops dictation in any application."))
        self._hint = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                               label=what + "\n" + _("Press Esc to cancel or Backspace to "
                                                     "disable the shortcut."))
        self._hint.add_css_class("dim-label")
        self._preview = shortcut_label(current)
        self._preview.set_halign(Gtk.Align.CENTER)
        self._preview.add_css_class("large-keycaps")
        self._preview.set_margin_top(6)
        self._error = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, visible=False)
        self._error.add_css_class("error")
        for widget in (image, title, self._hint, self._preview, self._error):
            box.append(widget)
        toolbar.set_content(box)
        self.set_child(toolbar)
        self.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                             [_("Press the key combination to use, Escape to cancel or "
                                "Backspace to disable the shortcut.")])

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)
        self.connect("map", self._on_map)
        self.connect("unmap", self._on_unmap)
        self.set_can_focus(True)
        self.set_focusable(True)

    # ------------------------------------------------------------------ #

    def _on_key_pressed(self, _controller, keyval: int, _keycode: int,
                        state: Gdk.ModifierType) -> bool:
        mods = state & Gtk.accelerator_get_default_mod_mask()
        if not mods and keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if not mods and keyval == Gdk.KEY_BackSpace:
            self.emit("chosen", "")
            self.close()
            return True
        if not mods and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self._accel:
            self._on_set()
            return True
        if Gdk.keyval_to_lower(keyval) in _MODIFIER_KEYS:
            return True
        accel = accelerator_for_event(keyval, state)
        if accel is None:
            self._error.set_label(_("Use at least one modifier key such as Super, Ctrl or "
                                    "Alt."))
            self._error.set_visible(True)
            return True
        self._accel = accel
        self._error.set_visible(False)
        self._preview.set_accelerator(accel)
        self._set.set_sensitive(True)
        self.update_property([Gtk.AccessibleProperty.DESCRIPTION],
                             [_("New shortcut: {shortcut}").format(shortcut=accel_label(accel))])
        return True

    def _on_set(self, *_args) -> None:
        if self._accel:
            self.emit("chosen", self._accel)
        self.close()

    def _on_map(self, *_args) -> None:
        native = self.get_native()
        surface = native.get_surface() if native is not None else None
        if isinstance(surface, Gdk.Toplevel):
            try:
                surface.inhibit_system_shortcuts(None)
                self._inhibited = surface
            except (TypeError, AttributeError):  # pragma: no cover - backend specific
                self._inhibited = None
        self.grab_focus()

    def _on_unmap(self, *_args) -> None:
        if self._inhibited is not None:
            self._inhibited.restore_system_shortcuts()
            self._inhibited = None
