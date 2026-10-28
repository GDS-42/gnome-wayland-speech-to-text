"""Controller state machine, driven through fakes on a real GLib main loop."""

from __future__ import annotations

import json

import pytest

from greffier import config as cfgmod
from greffier.const import State
from greffier.daemon.controller import Controller, DaemonError
from tests.daemon_fakes import FakeRecorder, FakeTranscriber, make_deps, run_until

PARAKEET = "parakeet-tdt-0.6b-v3-int8"
CANARY = "canary-180m-flash-int8"


class Bus:
    """Collects emitted signals."""

    def __init__(self) -> None:
        self.signals: list[tuple[str, tuple]] = []
        self.notifications: list[tuple[str, str, str]] = []

    def emit(self, name: str, signature: str, args: tuple) -> None:
        self.signals.append((name, args))

    def notify(self, ident: str, title: str, body: str, open_gui: bool = True) -> None:
        self.notifications.append((ident, title, body))

    def named(self, name: str) -> list[tuple]:
        return [args for n, args in self.signals if n == name]

    def states(self) -> list[str]:
        return [args[0] for args in self.named("StateChanged")]


@pytest.fixture
def make(tmp_path):
    controllers = []

    def factory(installed=(PARAKEET, "silero-vad"), **cfg_patch):
        deps = make_deps(tmp_path, list(installed))
        config_path = tmp_path / "config.toml"
        if cfg_patch:
            cfgmod.save(cfgmod.apply_patch(cfgmod.Config(), cfg_patch), config_path)
        bus = Bus()
        ctl = Controller(deps, bus.emit, bus.notify, config_path=config_path)
        ctl.start()
        controllers.append(ctl)
        return ctl, deps, bus

    yield factory
    for ctl in controllers:
        ctl.shutdown()


def loaded(ctl: Controller) -> bool:
    return ctl.status()["model_loaded"]


def test_preload_then_idle(make):
    ctl, deps, bus = make()
    assert run_until(lambda: loaded(ctl))
    assert ctl.state == State.IDLE
    assert "loading" in bus.states() and bus.states()[-1] == "idle"


def test_dictation_round_trip(make):
    ctl, deps, bus = make()
    run_until(lambda: loaded(ctl))
    ctl.toggle()
    assert ctl.state == State.RECORDING
    assert deps.sounds.played[-1] == "start"
    ctl.toggle()
    assert run_until(lambda: bus.named("Transcribed"))
    text, meta = bus.named("Transcribed")[0]
    assert text == "bonjour tout le monde"
    meta = json.loads(meta)
    assert meta["injected"] is True and meta["partial"] is False
    assert deps.injector.texts == ["bonjour tout le monde"]  # the injector adds the space
    assert ctl.history_items()[0]["text"] == "bonjour tout le monde"
    assert run_until(lambda: ctl.state == State.IDLE)
    assert "stop" in deps.sounds.played


def test_recording_starts_while_model_loads(make):
    ctl, deps, bus = make(model={"preload": False})
    assert not loaded(ctl)
    ctl.start_recording()  # triggers loading in parallel
    assert ctl.state == State.RECORDING
    ctl.stop()
    assert run_until(lambda: bus.named("Transcribed"))


def test_no_model(make):
    ctl, deps, bus = make(installed=())
    assert ctl.state == State.NO_MODEL
    with pytest.raises(DaemonError) as err:
        ctl.toggle()
    assert err.value.code == "NoModel"
    assert bus.notifications and ctl.state == State.ERROR


def test_falls_back_to_an_installed_model(make):
    ctl, deps, bus = make(installed=(CANARY,))
    assert ctl.cfg.model.active == CANARY
    assert run_until(lambda: loaded(ctl))


def test_microphone_failure_is_reported(make):
    ctl, deps, bus = make()
    FakeRecorder.fail_start = True
    with pytest.raises(DaemonError):
        ctl.start_recording()
    assert ctl.state == State.ERROR
    assert "error" in deps.sounds.played
    assert any(level == "error" for level, _msg in bus.named("Notice"))


def test_cancel_discards(make):
    ctl, deps, bus = make()
    ctl.start_recording()
    ctl.cancel()
    assert FakeRecorder.instances[-1].cancelled
    assert run_until(lambda: ctl.state == State.IDLE)
    assert not bus.named("Transcribed") and not deps.injector.texts


def test_empty_transcription_is_not_injected(make):
    ctl, deps, bus = make()
    run_until(lambda: loaded(ctl))
    FakeTranscriber.text = "   "
    ctl.toggle()
    ctl.toggle()
    assert run_until(lambda: any("Nothing" in m for _l, m in bus.named("Notice")))
    assert deps.injector.texts == []


def test_push_to_talk(make):
    ctl, deps, bus = make(shortcut={"mode": "push-to-talk"})
    run_until(lambda: loaded(ctl))
    ctl.hotkeys.pressed()
    assert ctl.state == State.RECORDING
    ctl.hotkeys.released()
    assert run_until(lambda: bus.named("Transcribed"))


def test_toggle_mode_hotkey(make):
    ctl, deps, bus = make()
    ctl.hotkeys.pressed()
    assert ctl.state == State.RECORDING
    ctl.hotkeys.released()  # ignored in toggle mode
    assert ctl.state == State.RECORDING
    ctl.hotkeys.pressed()
    assert ctl.state != State.RECORDING


def test_auto_stop_on_silence(make):
    ctl, deps, bus = make(recording={"auto_stop": True, "silence_timeout": 1.0})
    run_until(lambda: loaded(ctl))
    ctl.start_recording()
    FakeRecorder.instances[-1].feed(1.5)  # fake VAD: speech then 1.5 s of silence
    assert run_until(lambda: bus.named("Transcribed"))


def test_live_mode_injects_segments(make):
    ctl, deps, bus = make(recording={"live": True})
    run_until(lambda: loaded(ctl))
    ctl.start_recording()
    rec = FakeRecorder.instances[-1]
    rec.feed(0.5)
    rec.feed(0.5)
    assert run_until(lambda: len(deps.injector.texts) == 2)
    ctl.stop()
    assert run_until(lambda: ctl.history_items())
    assert ctl.history_items()[0]["text"] == "bonjour tout le monde bonjour tout le monde"
    assert all(json.loads(meta)["partial"] for _t, meta in bus.named("Transcribed"))


def test_clipboard_fallback_notifies(make):
    ctl, deps, bus = make()
    run_until(lambda: loaded(ctl))
    deps.injector.result.method = "clipboard"
    deps.injector.result.message = "Paste with Ctrl+V"
    ctl.toggle()
    ctl.toggle()
    assert run_until(lambda: bus.notifications)
    assert bus.notifications[-1][0] == "copied"


def test_set_config_validates_and_reacts(make):
    ctl, deps, bus = make()
    run_until(lambda: loaded(ctl))
    with pytest.raises(DaemonError) as err:
        ctl.set_config({"shortcut": {"mode": "nope"}})
    assert err.value.code == "InvalidArgument"
    cfg = ctl.set_config({"shortcut": {"accelerator": "<Ctrl><Alt>space"}})
    assert cfg["shortcut"]["accelerator"] == "<Ctrl><Alt>space"
    assert deps_hotkeys_last(ctl).accelerator == "<Ctrl><Alt>space"
    assert bus.named("ConfigChanged")
    ctl.set_config({"injection": {"method": "clipboard"}})
    assert deps.injector.config.method == "clipboard"
    # persisted
    assert cfgmod.load(ctl._config_path).shortcut.accelerator == "<Ctrl><Alt>space"


def deps_hotkeys_last(ctl):
    return ctl.hotkeys.applied[-1]


def test_switching_model_reloads(make):
    ctl, deps, bus = make(installed=(PARAKEET, CANARY, "silero-vad"))
    run_until(lambda: loaded(ctl))
    ctl.set_config({"model": {"active": CANARY}})
    assert run_until(lambda: ctl.status()["model"] == CANARY and loaded(ctl))


def test_install_flow_and_auto_activation(make):
    ctl, deps, bus = make(installed=())
    ctl.install_model(CANARY)
    with pytest.raises(DaemonError):
        ctl.install_model(CANARY)  # busy
    assert run_until(lambda: any(p[1] == "done" for p in bus.named("ModelProgress")
                                 if p[0] == CANARY))
    # the VAD is installed automatically next to the first ASR model
    assert run_until(lambda: "silero-vad" in deps.models.installed)
    assert ctl.cfg.model.active == CANARY
    assert run_until(lambda: loaded(ctl))
    assert ctl.state == State.IDLE


def test_install_error_and_cancel(make):
    ctl, deps, bus = make()
    deps.models.fail_install = CANARY
    ctl.install_model(CANARY)
    assert run_until(lambda: any(p[1] == "error" for p in bus.named("ModelProgress")))
    deps.models.install_delay = 0.05
    ctl.install_model("whisper-turbo-int8")
    ctl.cancel_install("whisper-turbo-int8")
    assert run_until(lambda: any(p[1] == "cancelled" for p in bus.named("ModelProgress")))


def test_remove_active_model(make):
    ctl, deps, bus = make(installed=(PARAKEET, CANARY, "silero-vad"))
    run_until(lambda: loaded(ctl))
    ctl.remove_model(PARAKEET)
    assert ctl.cfg.model.active == CANARY
    assert run_until(lambda: ctl.status()["model"] == CANARY and loaded(ctl))
    with pytest.raises(DaemonError):
        ctl.remove_model(PARAKEET)


def test_list_models_marks_active(make):
    ctl, deps, bus = make()
    models = {m["id"]: m for m in ctl.list_models()}
    assert models[PARAKEET]["active"] and models[PARAKEET]["installed"]
    assert not models[CANARY]["active"]


def test_unload_after_idle(make, monkeypatch):
    ctl, deps, bus = make()
    run_until(lambda: loaded(ctl))
    ctl._unload_source = 0
    ctl._on_unload_timer()
    assert not loaded(ctl)
    assert ctl.state == State.IDLE  # unloading does not change what the user sees


def test_diagnostics_shape(make):
    ctl, deps, bus = make()
    report = ctl.diagnostics()
    assert report["gnome_shell"] == "49.1"
    ids = {c["id"] for c in report["checks"]}
    assert {"session", "model", "vad", "shortcut", "extension", "keyboard", "microphone"} <= ids
    assert all(c["level"] in ("ok", "warning", "error") for c in report["checks"])


def test_history_persistence_toggle(make):
    ctl, deps, bus = make(general={"persist_history": True})
    run_until(lambda: loaded(ctl))
    ctl.toggle()
    ctl.toggle()
    assert run_until(lambda: ctl.history_items())
    from greffier import paths

    assert paths.history_file().exists()
    ctl.set_config({"general": {"persist_history": False}})
    assert not paths.history_file().exists()
