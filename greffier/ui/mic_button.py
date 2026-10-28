"""The large circular microphone button of the main window.

A ``Gtk.Button`` subclass (so focus, keyboard activation, the accessible
button role and click handling come for free) drawn with ``Gtk.Snapshot``:

* the disc colour is the CSS ``color`` of the widget (accent when idle, red
  while recording, see style.css), so dark mode, high contrast and the GNOME
  accent colour are honoured without hard-coded colours;
* while recording, two soft rings follow the microphone level;
* while transcribing or loading, an indeterminate arc turns around the disc.

Animations run from a frame clock tick callback only when something moves and
stop when ``gtk-enable-animations`` is off (the rings then follow the level
directly and the arc is drawn still).
"""

from __future__ import annotations

import math

from gi.repository import Adw, GLib, GObject, Graphene, Gsk, Gtk

from greffier.const import State
from greffier.i18n import _
from greffier.ui.util import animations_enabled

_ICONS = {
    "recording": "media-playback-stop-symbolic",
}
_RING_ALPHA = (0.26, 0.12)


class MicButton(Gtk.Button):
    __gtype_name__ = "GreffierMicButton"

    def __init__(self, disc_size: int = 112) -> None:
        super().__init__()
        self.add_css_class("mic-button")
        # Keep the keyboard focus where it is when clicked: the dictated text is
        # inserted into the focused widget (e.g. the text view of the onboarding).
        self.set_focus_on_click(False)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self._icon = Gtk.Image(icon_name="audio-input-microphone-symbolic")
        self._icon.set_can_target(False)
        self.set_child(self._icon)

        self._state = State.IDLE
        self._level = 0.0  # displayed (smoothed) level
        self._target = 0.0  # last level received
        self._last_level_time = 0
        self._spin_start = 0
        self._tick_id = 0
        self._press = 1.0  # scale of the disc, animated on press
        self._disc_size = 0
        self.set_disc_size(disc_size)

        target = Adw.CallbackAnimationTarget.new(self._on_press_value)
        self._press_anim = Adw.SpringAnimation.new(
            self, 1.0, 1.0, Adw.SpringParams.new(0.6, 1.0, 400.0), target)
        self._press_anim.set_clamp(False)
        self._update_accessibility()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @GObject.Property(type=int, default=112, minimum=32, maximum=512)
    def disc_size(self) -> int:
        """Diameter of the disc in pixels (the widget reserves room for the rings)."""
        return self._disc_size

    @disc_size.setter
    def disc_size(self, value: int) -> None:
        self.set_disc_size(value)

    def set_disc_size(self, value: int) -> None:
        if value == self._disc_size:
            return
        self._disc_size = value
        side = int(value * 1.56)
        self.set_size_request(side, side)
        self._icon.set_pixel_size(max(16, int(value * 0.36)))
        self.queue_resize()

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        for name in ("recording", "busy", "idle"):
            self.remove_css_class(name)
        if state == State.RECORDING:
            self.add_css_class("recording")
        elif state in (State.TRANSCRIBING, State.LOADING):
            self.add_css_class("busy")
            self._spin_start = 0
        else:
            self.add_css_class("idle")
        if state != State.RECORDING:
            self._target = 0.0
        self._icon.set_from_icon_name(_ICONS.get(state, "audio-input-microphone-symbolic"))
        self._update_accessibility()
        self._ensure_ticking()
        self.queue_draw()

    def set_level(self, level: float) -> None:
        """Feed a microphone level in [0, 1] (Level signal, ~15 Hz)."""
        if self._state != State.RECORDING:
            return
        self._target = max(0.0, min(1.0, level))
        self._last_level_time = GLib.get_monotonic_time()
        if animations_enabled(self):
            self._ensure_ticking()
        else:
            self._level = self._target
            self.queue_draw()

    # ------------------------------------------------------------------ #
    # Accessibility
    # ------------------------------------------------------------------ #

    def _update_accessibility(self) -> None:
        labels = {
            State.RECORDING: (_("Stop Dictation"), _("Listening. Activate to stop and insert "
                                                      "the text.")),
            State.TRANSCRIBING: (_("Transcribing"), _("Your speech is being converted to "
                                                       "text.")),
            State.LOADING: (_("Start Dictation"), _("The speech model is loading.")),
        }
        label, description = labels.get(
            self._state, (_("Start Dictation"),
                          _("Records your voice and types the text into the focused window.")))
        self.set_tooltip_text(label)
        self.update_property([Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
                             [label, description])

    # ------------------------------------------------------------------ #
    # Animation
    # ------------------------------------------------------------------ #

    def _needs_ticks(self) -> bool:
        if not self.get_mapped() or not animations_enabled(self):
            return False
        if self._state in (State.TRANSCRIBING, State.LOADING):
            return True
        return self._state == State.RECORDING or self._level > 0.002

    def _ensure_ticking(self) -> None:
        if self._tick_id == 0 and self._needs_ticks():
            self._tick_id = self.add_tick_callback(self._on_tick)

    def do_map(self) -> None:
        Gtk.Button.do_map(self)
        self._ensure_ticking()

    def do_unmap(self) -> None:
        if self._tick_id:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = 0
        Gtk.Button.do_unmap(self)

    def _on_tick(self, _widget, clock) -> bool:
        now = clock.get_frame_time()
        if self._state == State.RECORDING and now - self._last_level_time > 250_000:
            self._target *= 0.85  # no recent level: let the rings settle
        # fast attack, slow release, like a VU meter
        rate = 0.45 if self._target > self._level else 0.12
        self._level += (self._target - self._level) * rate
        self.queue_draw()
        if not self._needs_ticks():
            self._level = self._target if self._state == State.RECORDING else 0.0
            self._tick_id = 0
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _on_press_value(self, value: float) -> None:
        self._press = value
        self.queue_draw()

    def do_state_flags_changed(self, previous: Gtk.StateFlags) -> None:
        Gtk.Button.do_state_flags_changed(self, previous)
        was = bool(previous & Gtk.StateFlags.ACTIVE)
        now = bool(self.get_state_flags() & Gtk.StateFlags.ACTIVE)
        if was != now:
            self._press_anim.set_value_from(self._press)
            self._press_anim.set_value_to(0.94 if now else 1.0)
            self._press_anim.play()

    # ------------------------------------------------------------------ #
    # Drawing
    # ------------------------------------------------------------------ #

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        width, height = self.get_width(), self.get_height()
        cx, cy = width / 2, height / 2
        radius = self._disc_size / 2 * self._press
        color = self.get_color()

        if self._state == State.RECORDING:
            room = min(width, height) / 2 - self._disc_size / 2 - 2
            level = math.sqrt(self._level)  # perceptual: quiet speech still moves
            for i, alpha in enumerate(_RING_ALPHA):
                extra = room * (0.22 + 0.78 * level) * (0.62 if i == 0 else 1.0)
                _fill_circle(snapshot, cx, cy, radius + extra, _with_alpha(color, alpha))
        elif self.is_sensitive() and self._state != State.TRANSCRIBING:
            hover = bool(self.get_state_flags() & Gtk.StateFlags.PRELIGHT)
            _fill_circle(snapshot, cx, cy,
                         radius + self._disc_size * (0.09 if hover else 0.075),
                         _with_alpha(color, 0.2 if hover else 0.11))

        if self.has_visible_focus():
            _stroke_circle(snapshot, cx, cy, radius + 5, 3, _with_alpha(color, 0.55))

        _fill_circle(snapshot, cx, cy, radius, color)

        if self._state in (State.TRANSCRIBING, State.LOADING):
            self._snapshot_arc(snapshot, cx, cy, radius + self._disc_size * 0.09, color)

        self.snapshot_child(self._icon, snapshot)

    def _snapshot_arc(self, snapshot: Gtk.Snapshot, cx: float, cy: float, r: float,
                      color) -> None:
        if animations_enabled(self):
            t = GLib.get_monotonic_time() / 1_000_000
            start = (t * 1.1) % 1.0
            length = 0.22 + 0.14 * math.sin(t * 2.6)
        else:
            start, length = 0.0, 0.3
        width = max(3.0, self._disc_size * 0.045)
        _stroke_circle(snapshot, cx, cy, r, width, _with_alpha(color, 0.2))
        a0 = (start - 0.25) * 2 * math.pi
        a1 = a0 + length * 2 * math.pi
        builder = Gsk.PathBuilder.new()
        builder.move_to(cx + r * math.cos(a0), cy + r * math.sin(a0))
        builder.svg_arc_to(r, r, 0, length > 0.5, True,
                           cx + r * math.cos(a1), cy + r * math.sin(a1))
        stroke = Gsk.Stroke.new(width)
        stroke.set_line_cap(Gsk.LineCap.ROUND)
        snapshot.append_stroke(builder.to_path(), stroke, color)


def _with_alpha(color, alpha: float):
    copy = color.copy()
    copy.alpha = color.alpha * alpha
    return copy


def _fill_circle(snapshot: Gtk.Snapshot, cx: float, cy: float, r: float, color) -> None:
    if r <= 0:
        return
    rect = Graphene.Rect().init(cx - r, cy - r, 2 * r, 2 * r)
    rounded = Gsk.RoundedRect()
    rounded.init_from_rect(rect, r)
    snapshot.push_rounded_clip(rounded)
    snapshot.append_color(color, rect)
    snapshot.pop()


def _stroke_circle(snapshot: Gtk.Snapshot, cx: float, cy: float, r: float, width: float,
                   color) -> None:
    builder = Gsk.PathBuilder.new()
    builder.add_circle(Graphene.Point().init(cx, cy), r)
    snapshot.append_stroke(builder.to_path(), Gsk.Stroke.new(width), color)
