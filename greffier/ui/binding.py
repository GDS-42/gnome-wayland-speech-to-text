"""Two-way binding between preference widgets and the service configuration.

Every user change is sent as a *minimal* patch (``{"section": {"key": value}}``)
with SetConfig; ``ConfigChanged`` (from this or another client, e.g.
``greffierctl``) updates the widgets live. Spin rows are debounced so that
holding an arrow does not flood the service. A rejected value shows a toast
and the widget goes back to the value the service holds.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gi.repository import Adw, GLib, GObject, Gtk

from greffier.ui.client import CallError, DaemonClient
from greffier.ui.util import show_toast

DEBOUNCE_MS = 450


class Connections:
    """Remembers signal handlers so that a closing dialog can drop them all."""

    def __init__(self) -> None:
        self._items: list[tuple[GObject.Object, int]] = []

    def connect(self, obj: GObject.Object, signal: str, callback: Callable) -> int:
        handler = obj.connect(signal, callback)
        self._items.append((obj, handler))
        return handler

    def disconnect_all(self) -> None:
        for obj, handler in self._items:
            if obj.handler_is_connected(handler):
                obj.disconnect(handler)
        self._items.clear()


@dataclass
class _Binding:
    widget: Gtk.Widget
    prop: str
    section: str
    key: str
    to_widget: Callable[[Any], Any]
    from_widget: Callable[[Any], Any]
    debounce: bool = False
    handler: int = 0
    source: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class ConfigBinder:
    def __init__(self, client: DaemonClient, owner: Gtk.Widget,
                 connections: Connections) -> None:
        self.client = client
        self.owner = owner  # widget used to find where toasts go
        self._bindings: list[_Binding] = []
        self._listeners: list[Callable[[], None]] = []
        connections.connect(client, "config-changed", lambda *_a: self.sync())

    # ------------------------------------------------------------------ #
    # Binding helpers
    # ------------------------------------------------------------------ #

    def switch(self, row: Adw.SwitchRow, section: str, key: str) -> Adw.SwitchRow:
        self._bind(row, "active", section, key, bool, bool)
        return row

    def expander(self, row: Adw.ExpanderRow, section: str, key: str) -> Adw.ExpanderRow:
        self._bind(row, "enable-expansion", section, key, bool, bool)
        return row

    def spin(self, row: Adw.SpinRow, section: str, key: str, kind: type = int) -> Adw.SpinRow:
        self._bind(row, "value", section, key, float, lambda v: kind(round(v, 2)),
                   debounce=True)
        return row

    def combo(self, row: Adw.ComboRow, section: str, key: str,
              values: list[str]) -> Adw.ComboRow:
        """``values[i]`` is the configuration value of item ``i`` of the row's model."""
        binding = self._bind(row, "selected", section, key, *self._combo_converters(values))
        binding.extra["values"] = list(values)
        return row

    @staticmethod
    def _combo_converters(values: list[str]) -> tuple[Callable, Callable]:
        def to_widget(value: Any) -> int:
            return values.index(value) if value in values else 0

        def from_widget(index: int) -> Any:
            return values[index] if 0 <= index < len(values) else None

        return to_widget, from_widget

    def set_combo_values(self, row: Adw.ComboRow, values: list[str], labels: list[str]) -> None:
        """Replace the items of a combo bound with combo() (lists that depend on state)."""
        for b in self._bindings:
            if b.widget is not row:
                continue
            if b.extra.get("values") == values:
                return
            b.to_widget, b.from_widget = self._combo_converters(list(values))
            b.extra["values"] = list(values)
            row.handler_block(b.handler)
            try:
                row.set_model(Gtk.StringList.new(labels))
            finally:
                row.handler_unblock(b.handler)
            self._apply(b)
            return

    def entry(self, row: Adw.EntryRow, section: str, key: str) -> Adw.EntryRow:
        binding = _Binding(row, "text", section, key, str, str)
        binding.handler = row.connect("apply", lambda *_a: self._on_widget(binding))
        self._bindings.append(binding)
        self._apply(binding)
        return row

    def on_sync(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` after every configuration update (dependent widgets)."""
        self._listeners.append(callback)
        callback()

    # ------------------------------------------------------------------ #

    def _bind(self, widget: Gtk.Widget, prop: str, section: str, key: str,
              to_widget: Callable, from_widget: Callable, debounce: bool = False) -> _Binding:
        binding = _Binding(widget, prop, section, key, to_widget, from_widget, debounce)
        binding.handler = widget.connect(f"notify::{prop}", lambda *_a: self._on_widget(binding))
        self._bindings.append(binding)
        self._apply(binding)
        return binding

    def sync(self) -> None:
        for binding in self._bindings:
            if not binding.source:
                self._apply(binding)
        for callback in self._listeners:
            callback()

    def _apply(self, binding: _Binding) -> None:
        value = self.client.get(binding.section, binding.key)
        if value is None:
            return
        target = binding.to_widget(value)
        if binding.widget.get_property(binding.prop) == target:
            return
        binding.widget.handler_block(binding.handler)
        try:
            binding.widget.set_property(binding.prop, target)
        finally:
            binding.widget.handler_unblock(binding.handler)

    def _on_widget(self, binding: _Binding) -> None:
        if binding.debounce:
            if binding.source:
                GLib.source_remove(binding.source)
            binding.source = GLib.timeout_add(DEBOUNCE_MS, self._flush, binding)
        else:
            self._send(binding)

    def _flush(self, binding: _Binding) -> bool:
        binding.source = 0
        self._send(binding)
        return GLib.SOURCE_REMOVE

    def _send(self, binding: _Binding) -> None:
        value = binding.from_widget(binding.widget.get_property(binding.prop))
        if value is None or value == self.client.get(binding.section, binding.key):
            return

        def failed(err: CallError) -> None:
            show_toast(self.owner, err.message, urgent=True)
            self._apply(binding)

        self.client.set_config({binding.section: {binding.key: value}}, on_error=failed)

    def flush_pending(self) -> None:
        """Send debounced changes now (the dialog is closing)."""
        for binding in self._bindings:
            if binding.source:
                GLib.source_remove(binding.source)
                binding.source = 0
                self._send(binding)
