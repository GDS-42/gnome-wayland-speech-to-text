"""In-memory fakes for the controller's dependencies."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from gi.repository import GLib

from greffier import paths
from greffier.daemon.controller import Deps
from greffier.models.types import InstalledModel, ModelSpec


def catalog_specs() -> list[ModelSpec]:
    data = json.loads(paths.package_data("catalog.json").read_text())
    return [ModelSpec.from_dict(m) for m in data["models"]]


class FakeCatalog:
    revision = "test"

    def __init__(self, specs: list[ModelSpec]) -> None:
        self._specs = {s.id: s for s in specs}

    def get(self, model_id: str) -> ModelSpec | None:
        return self._specs.get(model_id)

    def models(self, kind: str | None = None) -> list[ModelSpec]:
        return [s for s in self._specs.values() if kind is None or s.kind == kind]


@dataclass
class FakeStatus:
    spec: ModelSpec
    installed: InstalledModel | None
    update_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = self.spec.to_dict()
        d.update(installed=self.installed is not None, update_available=self.update_available,
                 installed_revision=self.installed.spec.revision if self.installed else None,
                 orphaned=False, size_on_disk=0)
        return d


class FakeModels:
    def __init__(self, installed: list[str], root: Path) -> None:
        self.catalog = FakeCatalog(catalog_specs())
        self.root = root
        self.installed: dict[str, InstalledModel] = {}
        self.install_delay = 0.0
        self.fail_install: str | None = None
        for model_id in installed:
            self._add(model_id)

    def _add(self, model_id: str) -> InstalledModel:
        spec = self.catalog.get(model_id)
        assert spec is not None
        model = InstalledModel(spec=spec, path=self.root / model_id)
        self.installed[model_id] = model
        return model

    def statuses(self) -> list[FakeStatus]:
        return [FakeStatus(s, self.installed.get(s.id)) for s in self.catalog.models()]

    def get_installed(self, model_id: str) -> InstalledModel | None:
        return self.installed.get(model_id)

    def vad_model(self) -> InstalledModel | None:
        return self.installed.get("silero-vad")

    def install(self, model_id: str, progress, cancel: threading.Event) -> InstalledModel:
        total = 100
        for done in range(0, total + 1, 25):
            if cancel.is_set():
                raise FakeCancelled()
            progress("download", done, total)
            time.sleep(self.install_delay)
        if self.fail_install == model_id:
            raise RuntimeError("disk full")
        return self._add(model_id)

    def remove(self, model_id: str) -> None:
        del self.installed[model_id]


class FakeCancelled(Exception):
    pass


class FakeRecorder:
    instances: list[FakeRecorder] = []
    fail_start = False

    def __init__(self, device_id: str = "", on_chunk=None, on_level=None, on_error=None) -> None:
        self.device_id = device_id
        self.on_chunk, self.on_level, self.on_error = on_chunk, on_level, on_error
        self.active = False
        self.cancelled = False
        FakeRecorder.instances.append(self)

    def start(self) -> None:
        if FakeRecorder.fail_start:
            raise RuntimeError("no microphone")
        self.active = True
        if self.on_level:
            self.on_level(0.5)

    def stop(self) -> np.ndarray:
        self.active = False
        return np.full(16000, 0.1, dtype=np.float32)

    def cancel(self) -> None:
        self.active = False
        self.cancelled = True

    def feed(self, seconds: float) -> None:
        if self.on_chunk:
            self.on_chunk(np.zeros(int(seconds * 16000), dtype=np.float32))


class FakeEngine:
    def __init__(self, model: InstalledModel) -> None:
        self.model_id = model.id
        self.closed = False

    def transcribe(self, samples, language=None) -> str:
        return "bonjour"

    def close(self) -> None:
        self.closed = True


class FakeTranscriber:
    text = "bonjour tout le monde"
    calls: list[tuple[int, str | None]] = []

    def __init__(self, engine, vad_model, max_segment_s) -> None:
        self.engine, self.vad_model = engine, vad_model

    def transcribe(self, samples, language) -> str:
        FakeTranscriber.calls.append((len(samples), language))
        return FakeTranscriber.text


class FakeVad:
    """Reports one finished segment per accepted chunk and growing silence."""

    def __init__(self, path) -> None:
        self.speech_started = False
        self.trailing_silence = 0.0
        self.flushed = False

    def accept(self, samples):
        self.speech_started = True
        self.trailing_silence += len(samples) / 16000
        return [samples]

    def flush(self):
        self.flushed = True
        return []


@dataclass
class Result:
    ok: bool = True
    method: str = "paste"
    keyboard: str = "extension"
    clipboard: str = "extension"
    message: str = ""


class FakeInjector:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.result = Result()
        self.config = None

    def inject(self, text: str, callback) -> None:
        self.texts.append(text)
        GLib.idle_add(lambda: callback(self.result) or False)

    def update_config(self, cfg) -> None:
        self.config = cfg

    def note_shortcut_activity(self) -> None:
        self.shortcut_notes = getattr(self, "shortcut_notes", 0) + 1

    def describe(self) -> dict:
        return {"method": "paste", "keyboard": "extension", "clipboard": "extension"}

    def diagnostics(self) -> dict:
        return {"extension": {"installed": True, "enabled": True, "active": True},
                "portals": {}, "tools": {}}

    def close(self) -> None:
        pass


@dataclass
class HotkeyStatus:
    backend: str = "gnome"
    active: bool = True
    accelerator: str = "<Super><Alt>d"
    mode: str = "toggle"
    message: str = ""


class FakeHotkeys:
    def __init__(self, pressed, released, command) -> None:
        self.pressed, self.released, self.command = pressed, released, command
        self.status = HotkeyStatus()
        self.applied: list[Any] = []

    def apply(self, cfg, callback=None) -> None:
        self.applied.append(cfg)
        self.status = HotkeyStatus(mode=cfg.mode, accelerator=cfg.accelerator)
        if callback:
            callback(self.status)

    def close(self) -> None:
        pass


@dataclass
class FakeSounds:
    played: list[str] = field(default_factory=list)

    def play(self, name: str) -> None:
        self.played.append(name)


def make_deps(tmp: Path, installed: list[str]) -> Deps:
    FakeRecorder.instances.clear()
    FakeRecorder.fail_start = False
    FakeTranscriber.calls.clear()
    FakeTranscriber.text = "bonjour tout le monde"
    return Deps(
        models=FakeModels(installed, tmp / "models"),
        recorder_factory=FakeRecorder,
        list_devices=lambda: [],
        engine_loader=lambda model, threads=0, provider="cpu": FakeEngine(model),
        transcriber_factory=FakeTranscriber,
        vad_factory=FakeVad,
        postprocess=lambda text, cfg, lang: text.strip(),
        injector=FakeInjector(),
        hotkeys_factory=FakeHotkeys,
        sounds=FakeSounds(),
        fetch_catalog=lambda url, cache: FakeCatalog(catalog_specs()),
        set_autostart=lambda enabled: None,
        environment=lambda: {"gnome_shell": "49.1"},
        install_cancelled=FakeCancelled,
    )


def run_until(predicate, timeout: float = 5.0) -> bool:
    """Iterate the default main context until predicate() is true."""
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        while ctx.pending():
            ctx.iteration(False)
        time.sleep(0.005)
    return predicate()
