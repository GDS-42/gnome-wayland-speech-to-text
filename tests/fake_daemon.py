#!/usr/bin/env python3
"""Stand-alone fake of the Greffier background service (Daemon1 interface).

It owns ``io.github.greffier.Greffier.Daemon`` on the session bus and
implements every method, signal and property of
``greffier/data/daemon-interface.xml`` with realistic canned data: the six
catalog models (Parakeet installed and active, Canary installed with an update
available, a Qwen3 download in progress), French history entries, a ``Level``
signal while "recording" and the usual state transitions on Toggle. No
microphone, model or GNOME Shell is needed.

Run it on a private bus to develop or screenshot the GUI::

    dbus-run-session -- sh -c 'python tests/fake_daemon.py & sleep 1; python -m greffier.ui.app'

Scenarios (``--scenario``): default, first-run (no model, onboarding),
no-model, recording, transcribing, downloading. ``--freeze-progress 0.42``
keeps fake downloads at 42 % (screenshots), ``--fast`` shortens every delay
(tests). It prints ``READY`` once the bus name is owned.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gi.repository import Gio, GLib  # noqa: E402

from greffier import config as cfgmod  # noqa: E402
from greffier.const import DBUS_IFACE, DBUS_NAME, DBUS_PATH, VERSION, State  # noqa: E402

ERROR = "io.github.greffier.Greffier.Error."
INTERFACE_XML = ROOT / "greffier" / "data" / "daemon-interface.xml"
CATALOG = ROOT / "greffier" / "data" / "catalog.json"

SENTENCES = [
    "Ceci est un essai de dictée avec Greffier, tout se passe sur mon ordinateur.",
    "Merci pour votre message, je vous réponds dès demain matin.",
    "La réunion est reportée à jeudi prochain, à quatorze heures.",
    "Il faudra relire le contrat avant de le signer.",
]

HISTORY = [
    (2, 6.2, "Bonjour à tous, je vous confirme la réunion de demain à 10 heures dans la salle "
             "du conseil."),
    (14, 4.8, "N'oubliez pas d'apporter le compte rendu signé et les pièces justificatives."),
    (75, 9.5, "La séance est ouverte. Le greffier donne lecture du procès-verbal de la "
              "dernière audience, qui est adopté à l'unanimité."),
    (190, 3.1, "Penser à acheter du pain, des tomates et un bon fromage pour ce soir."),
    (60 * 26, 7.4, "Cher Monsieur, je vous remercie pour votre courrier du 12 octobre et "
                   "reviens vers vous très rapidement."),
    (60 * 24 * 4, 5.0, "Le dossier numéro 2024-117 est renvoyé à l'audience du 15 novembre."),
]

DEVICES = [
    {"id": "alsa_input.pci-0000_00_1f.3.analog-stereo", "name": "Built-in Audio Analog Stereo",
     "is_default": True},
    {"id": "alsa_input.usb-Blue_Microphones_Yeti-00.analog-stereo",
     "name": "Yeti Stereo Microphone", "is_default": False},
]


class FakeDaemon:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.loop = GLib.MainLoop()
        self.conn: Gio.DBusConnection | None = None
        node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML.read_text(encoding="utf-8"))
        self.iface = node.lookup_interface(DBUS_IFACE)
        self.speed = 0.1 if args.fast else 1.0

        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        self.specs: dict[str, dict[str, Any]] = {m["id"]: m for m in catalog["models"]}
        self.installed: dict[str, str] = {}  # id -> installed revision
        self.installs: dict[str, dict[str, Any]] = {}  # id -> progress state
        self.cfg = cfgmod.Config()
        self.cfg.general.first_run_done = True
        self.history: list[dict[str, Any]] = []
        self.state = State.IDLE
        self.detail = ""
        self.loaded = ""
        self.rec_started = 0.0
        self.rec_source = 0
        self.rec_max_source = 0
        self.sentence = 0
        self._setup_scenario(args.scenario)

    # ------------------------------------------------------------------ #
    # Canned data
    # ------------------------------------------------------------------ #

    def _setup_scenario(self, scenario: str) -> None:
        now = datetime.now().astimezone()
        if scenario in ("first-run", "no-model"):
            self.cfg.general.first_run_done = scenario != "first-run"
            if scenario == "first-run":
                self.installed = {}
            self._refresh_state()
            return
        self.installed = {
            "parakeet-tdt-0.6b-v3-int8": self.specs["parakeet-tdt-0.6b-v3-int8"]["revision"],
            "canary-180m-flash-int8": "2025-01-15",  # older: update available
            "silero-vad": self.specs["silero-vad"]["revision"],
        }
        for minutes, duration, text in HISTORY:
            self.history.append({
                "text": text,
                "time": (now - timedelta(minutes=minutes)).isoformat(timespec="seconds"),
                "duration": duration,
                "model": "parakeet-tdt-0.6b-v3-int8",
                "language": "fr",
            })
        self.loaded = self.cfg.model.active
        self._refresh_state()

    def start_scenario(self) -> None:
        scenario = self.args.scenario
        if scenario == "default":
            self._begin_install("qwen3-asr-0.6b-int8", start_fraction=0.18, rate=0.004)
        elif scenario == "downloading":
            self._begin_install("cohere-transcribe-14-lang-int8", start_fraction=0.42, rate=0.01)
        elif scenario == "first-run":
            if self.args.freeze_progress is not None:
                self._begin_install("parakeet-tdt-0.6b-v3-int8", start_fraction=0.0)
        elif scenario == "recording":
            self._start_recording(hold=True)
        elif scenario == "transcribing":
            self._set_state(State.TRANSCRIBING, "")
        elif self.loaded and self.cfg.model.preload:
            self._load_model()

    def _model_dict(self, model_id: str) -> dict[str, Any]:
        spec = dict(self.specs[model_id])
        installed = model_id in self.installed
        progress = self.installs.get(model_id)
        spec.update({
            "installed": installed,
            "installed_revision": self.installed.get(model_id, ""),
            "update_available": installed and self.installed[model_id] != spec["revision"],
            "size_on_disk": spec.get("disk_size", 0) if installed else 0,
            # Extra keys also sent by the real service.
            "active": installed and model_id == self.cfg.model.active,
            "loaded": model_id == self.loaded,
            "install": ({"phase": progress["phase"], "done": progress["done"],
                         "total": progress["total"]} if progress else None),
        })
        return spec

    def _active_installed(self) -> str:
        active = self.cfg.model.active
        if active in self.installed and self.specs.get(active, {}).get("kind") == "asr":
            return active
        return ""

    def _status(self) -> dict[str, Any]:
        active = self._active_installed()
        shortcut = self.cfg.shortcut
        ext = self.args.extension == "active"
        return {
            "state": str(self.state),
            "detail": self.detail,
            "model": active,
            "model_name": self.specs[active]["name"] if active else "",
            "model_loaded": bool(active) and self.loaded == active,
            "recording_seconds": round(GLib.get_monotonic_time() / 1e6 - self.rec_started, 1)
            if self.state == State.RECORDING else 0.0,
            "shortcut": {
                "accelerator": shortcut.accelerator,
                "backend": "none" if shortcut.backend == "none" else
                ("portal" if shortcut.mode == "push-to-talk" else "gnome"),
                "mode": shortcut.mode,
                "active": bool(shortcut.accelerator) and shortcut.backend != "none",
                "message": "",
            },
            "injection": {
                "method": self.cfg.injection.method,
                "keyboard": "extension" if ext else "portal",
                "clipboard": "extension" if ext else "wl-copy",
            },
        }

    def _diagnostics(self) -> dict[str, Any]:
        ext_state = self.args.extension
        ext = {"installed": ext_state != "none", "enabled": ext_state == "active",
               "active": ext_state == "active"}
        active = self._active_installed()
        gnome = self.args.gnome
        major = int(gnome.split(".")[0]) if gnome else 0
        checks = [
            {"id": "session", "ok": True, "level": "ok", "message": "GNOME on Wayland session"},
            {"id": "model", "ok": bool(active), "level": "ok" if active else "error",
             "message": f"Model: {self.specs[active]['name']}" if active else
             "No speech recognition model is installed."},
            {"id": "vad", "ok": "silero-vad" in self.installed,
             "level": "ok" if "silero-vad" in self.installed else "warning",
             "message": "Voice detection is installed" if "silero-vad" in self.installed else
             "Voice detection is not installed (long dictations and auto-stop are degraded)."},
            {"id": "shortcut", "ok": True, "level": "ok",
             "message": "Shortcut Super+Alt+D is active"},
            {"id": "extension", "ok": ext["active"], "level": "ok" if ext["active"] else "warning",
             "message": "The GNOME Shell extension is active" if ext["active"] else
             ("The GNOME Shell extension is installed but not active (log out and back in, "
              "then enable it in Extensions)." if ext["installed"] else
              "Install the Greffier GNOME Shell extension for the best experience.")},
            {"id": "keyboard", "ok": True, "level": "ok",
             "message": "Text is inserted with: " + ("extension" if ext["active"] else "portal")},
            {"id": "microphone", "ok": True, "level": "ok", "message": "2 microphones found"},
        ]
        return {
            "version": VERSION,
            "gnome_shell": gnome,
            "session_type": "wayland",
            "desktop": "GNOME",
            "sherpa_onnx": "1.12.14",
            "cpu_threads": 8,
            "extension": ext,
            "portals": {"global_shortcuts": major >= 48, "remote_desktop": True,
                        "registry": major >= 48},
            "tools": {"wl-copy": True, "ydotool": False},
            "audio_devices": len(DEVICES),
            "checks": checks,
        }

    # ------------------------------------------------------------------ #
    # D-Bus plumbing
    # ------------------------------------------------------------------ #

    def run(self) -> int:
        Gio.bus_own_name(Gio.BusType.SESSION, DBUS_NAME, Gio.BusNameOwnerFlags.NONE,
                         self._on_bus_acquired, self._on_name_acquired, self._on_name_lost)
        self.loop.run()
        return 0

    def _on_bus_acquired(self, conn: Gio.DBusConnection, _name: str) -> None:
        self.conn = conn
        conn.register_object(DBUS_PATH, self.iface, self._on_method_call,
                             self._on_get_property, None)

    def _on_name_acquired(self, _conn, _name: str) -> None:
        print("READY", flush=True)
        GLib.idle_add(lambda: self.start_scenario() or GLib.SOURCE_REMOVE)

    def _on_name_lost(self, _conn, _name: str) -> None:
        print("fake daemon: bus name lost", file=sys.stderr)
        self.loop.quit()

    def _emit(self, name: str, signature: str, args: tuple) -> None:
        if self.conn is None:
            return
        self.conn.emit_signal(None, DBUS_PATH, DBUS_IFACE, name,
                              GLib.Variant(signature, args) if signature else None)

    def _emit_property(self, name: str, value: str) -> None:
        if self.conn is None:
            return
        self.conn.emit_signal(None, DBUS_PATH, "org.freedesktop.DBus.Properties",
                              "PropertiesChanged",
                              GLib.Variant("(sa{sv}as)",
                                           (DBUS_IFACE, {name: GLib.Variant("s", value)}, [])))

    def _notice(self, level: str, message: str) -> None:
        self._emit("Notice", "(ss)", (level, message))

    def _on_get_property(self, _conn, _sender, _path, _iface, name: str):
        if name == "State":
            return GLib.Variant("s", str(self.state))
        if name == "ActiveModel":
            return GLib.Variant("s", self._active_installed())
        if name == "Version":
            return GLib.Variant("s", VERSION)
        return None

    def _on_method_call(self, _conn, _sender, _path, _iface, method: str,
                        params: GLib.Variant, invocation: Gio.DBusMethodInvocation) -> None:
        handler = getattr(self, f"m_{method}", None)
        if handler is None:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
            return
        try:
            result = handler(*params.unpack())
        except DaemonError as exc:
            invocation.return_dbus_error(ERROR + exc.code, exc.message)
            return
        out = self.iface.lookup_method(method).out_args
        if out:
            invocation.return_value(GLib.Variant("(s)", (json.dumps(result, ensure_ascii=False),)))
        else:
            invocation.return_value(None)

    # ------------------------------------------------------------------ #
    # State machine
    # ------------------------------------------------------------------ #

    def _set_state(self, state: State, detail: str = "") -> None:
        if (state, detail) == (self.state, self.detail):
            return
        changed = state != self.state
        self.state, self.detail = state, detail
        self._emit("StateChanged", "(ss)", (str(state), detail))
        if changed:
            self._emit_property("State", str(state))

    def _refresh_state(self) -> None:
        if self.state in (State.RECORDING, State.TRANSCRIBING):
            return
        if not self._active_installed():
            self._set_state(State.NO_MODEL, "No speech recognition model is installed.")
        elif self.loaded != self._active_installed() and self.state == State.LOADING:
            pass
        else:
            self._set_state(State.IDLE, "")

    def _load_model(self) -> None:
        active = self._active_installed()
        if not active:
            self._refresh_state()
            return
        self.loaded = ""
        self._set_state(State.LOADING, f"Loading {self.specs[active]['name']}…")

        def done() -> bool:
            self.loaded = self._active_installed()
            if self.state == State.LOADING:
                self._set_state(State.IDLE, "")
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(int(1500 * self.speed), done)

    def _start_recording(self, hold: bool = False) -> None:
        if not self._active_installed():
            raise DaemonError("NoModel", "No speech recognition model is installed.")
        if self.state == State.RECORDING:
            return
        if self.state == State.TRANSCRIBING:
            raise DaemonError("Busy", "Greffier is still transcribing.")
        self.rec_started = GLib.get_monotonic_time() / 1e6
        if hold:
            self.rec_started -= 7.0  # looks like a dictation in progress
        self._set_state(State.RECORDING, "")
        self.rec_source = GLib.timeout_add(66, self._on_level_tick)
        if not hold:
            self.rec_max_source = GLib.timeout_add_seconds(self.cfg.recording.max_duration,
                                                           self._on_max_duration)

    def _on_level_tick(self) -> bool:
        t = GLib.get_monotonic_time() / 1e6 - self.rec_started
        syllables = 0.5 + 0.5 * math.sin(t * 2 * math.pi * 3.7)
        words = 0.55 + 0.45 * math.sin(t * 2 * math.pi * 0.9 + 1.3)
        pause = 0.08 if (t % 3.4) > 3.0 else 1.0
        level = max(0.0, min(1.0, syllables * words * pause * random.uniform(0.6, 1.0)))
        self._emit("Level", "(d)", (level,))
        return GLib.SOURCE_CONTINUE

    def _stop_timers(self) -> None:
        for attr in ("rec_source", "rec_max_source"):
            source = getattr(self, attr)
            if source:
                GLib.source_remove(source)
                setattr(self, attr, 0)

    def _on_max_duration(self) -> bool:
        self.rec_max_source = 0
        self._stop_recording()
        return GLib.SOURCE_REMOVE

    def _stop_recording(self) -> None:
        if self.state != State.RECORDING:
            return
        duration = GLib.get_monotonic_time() / 1e6 - self.rec_started
        self._stop_timers()
        self._set_state(State.TRANSCRIBING, "")
        GLib.timeout_add(int(1200 * self.speed), self._finish_transcription, duration)

    def _finish_transcription(self, duration: float) -> bool:
        text = SENTENCES[self.sentence % len(SENTENCES)]
        self.sentence += 1
        model = self._active_installed()
        language = self.cfg.model.language if self.cfg.model.language != "auto" else "fr"
        if self.cfg.general.history_size:
            self.history.insert(0, {
                "text": text,
                "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                "duration": round(duration, 1), "model": model, "language": language,
            })
            del self.history[self.cfg.general.history_size:]
        meta = {"duration": round(duration, 1), "model": model, "language": language,
                "partial": False, "injected": True, "method": "paste"}
        self._emit("Transcribed", "(ss)", (text, json.dumps(meta, ensure_ascii=False)))
        self.state = State.IDLE  # leave TRANSCRIBING before recomputing
        self.detail = ""
        self._emit("StateChanged", "(ss)", (str(State.IDLE), ""))
        self._emit_property("State", str(State.IDLE))
        self._refresh_state()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------ #
    # Fake installs
    # ------------------------------------------------------------------ #

    def _begin_install(self, model_id: str, start_fraction: float = 0.0,
                       rate: float = 0.06) -> None:
        spec = self.specs[model_id]
        total = int(spec["download"]["size"])
        frozen = self.args.freeze_progress
        fraction = frozen if frozen is not None else start_fraction
        state = {"phase": "download", "done": int(total * fraction), "total": total,
                 "rate": rate, "source": 0}
        self.installs[model_id] = state
        self._emit("ModelProgress", "(sstts)", (model_id, "download", state["done"], total, ""))
        self._emit("ModelsChanged", "", ())
        state["source"] = GLib.timeout_add(int(200 * self.speed) or 20, self._install_tick,
                                           model_id)

    def _install_tick(self, model_id: str) -> bool:
        state = self.installs.get(model_id)
        if state is None:
            return GLib.SOURCE_REMOVE
        if self.args.freeze_progress is not None:
            self._emit("ModelProgress", "(sstts)",
                       (model_id, state["phase"], state["done"], state["total"], ""))
            return GLib.SOURCE_CONTINUE
        rate = 1.0 if self.args.fast else state["rate"]
        if state["phase"] == "download":
            state["done"] = min(state["total"], state["done"] + int(state["total"] * rate))
            if state["done"] >= state["total"]:
                state.update(phase="verify", done=0)
        elif state["phase"] == "verify":
            state["done"] = min(state["total"], state["done"] + int(state["total"] * 0.25))
            if state["done"] >= state["total"]:
                state.update(phase="extract", done=0, total=100)
        elif state["phase"] == "extract":
            state["done"] = min(100, state["done"] + 20)
            if state["done"] >= 100:
                self._end_install(model_id, "done")
                return GLib.SOURCE_REMOVE
        self._emit("ModelProgress", "(sstts)",
                   (model_id, state["phase"], state["done"], state["total"], ""))
        return GLib.SOURCE_CONTINUE

    def _end_install(self, model_id: str, outcome: str) -> None:
        state = self.installs.pop(model_id, None)
        if state and state.get("source"):
            GLib.source_remove(state["source"])
        spec = self.specs[model_id]
        total = int(spec["download"]["size"])
        self._emit("ModelProgress", "(sstts)",
                   (model_id, outcome, total if outcome == "done" else 0, total, ""))
        if outcome == "done":
            self.installed[model_id] = spec["revision"]
            self._notice("info", f"{spec['name']} is installed.")
            if spec["kind"] == "asr" and not self._active_installed():
                self._apply_patch({"model": {"active": model_id}})
            if model_id == self._active_installed():
                self._load_model()
        else:
            self._notice("info", f"Download of {spec['name']} cancelled.")
        self._emit("ModelsChanged", "", ())
        self._refresh_state()

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #

    def _apply_patch(self, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise DaemonError("InvalidArgument", "The settings patch must be a JSON object.")
        try:
            new = cfgmod.apply_patch(self.cfg, patch)
        except cfgmod.ConfigError as exc:
            raise DaemonError("InvalidArgument", str(exc)) from exc
        changed = cfgmod.diff_sections(self.cfg, new)
        old_active = self.cfg.model.active
        self.cfg = new
        if changed:
            self._emit("ConfigChanged", "(s)",
                       (json.dumps(cfgmod.to_dict(new), ensure_ascii=False),))
        if new.model.active != old_active:
            self._emit_property("ActiveModel", self._active_installed())
            if self._active_installed():
                self._load_model()
            else:
                self.loaded = ""
                self._refresh_state()
        return cfgmod.to_dict(new)

    # ------------------------------------------------------------------ #
    # Methods (m_<Name>)
    # ------------------------------------------------------------------ #

    def m_Toggle(self) -> None:  # noqa: N802 - D-Bus method names
        if self.state == State.RECORDING:
            self._stop_recording()
        else:
            self._start_recording()

    def m_Start(self) -> None:  # noqa: N802
        self._start_recording()

    def m_Stop(self) -> None:  # noqa: N802
        self._stop_recording()

    def m_Cancel(self) -> None:  # noqa: N802
        if self.state == State.RECORDING:
            self._stop_timers()
            self.state = State.IDLE
            self._emit("StateChanged", "(ss)", (str(State.IDLE), ""))
            self._emit_property("State", str(State.IDLE))
            self._refresh_state()
            self._notice("info", "Dictation cancelled.")

    def m_GetStatus(self) -> dict[str, Any]:  # noqa: N802
        return self._status()

    def m_GetConfig(self) -> dict[str, Any]:  # noqa: N802
        return cfgmod.to_dict(self.cfg)

    def m_SetConfig(self, patch: str) -> dict[str, Any]:  # noqa: N802
        try:
            data = json.loads(patch)
        except ValueError as exc:
            raise DaemonError("InvalidArgument", f"Invalid JSON: {exc}") from exc
        return self._apply_patch(data)

    def m_ListModels(self) -> list[dict[str, Any]]:  # noqa: N802
        return [self._model_dict(model_id) for model_id in self.specs]

    def m_InstallModel(self, model_id: str) -> None:  # noqa: N802
        if model_id not in self.specs:
            raise DaemonError("NotFound", f"Unknown model: {model_id}")
        if model_id in self.installs:
            raise DaemonError("Busy", f"{self.specs[model_id]['name']} is already being "
                                      "installed.")
        self._begin_install(model_id)

    def m_CancelInstall(self, model_id: str) -> None:  # noqa: N802
        if model_id not in self.installs:
            raise DaemonError("NotFound", f"{model_id} is not being installed.")
        self._end_install(model_id, "cancelled")

    def m_RemoveModel(self, model_id: str) -> None:  # noqa: N802
        if model_id in self.installs:
            raise DaemonError("Busy", "Cancel the download first.")
        if model_id not in self.installed:
            raise DaemonError("NotFound", f"{model_id} is not installed.")
        del self.installed[model_id]
        if self.loaded == model_id:
            self.loaded = ""
        self._emit("ModelsChanged", "", ())
        if model_id == self.cfg.model.active:
            others = [m for m in self.installed if self.specs[m]["kind"] == "asr"]
            if others:
                self._apply_patch({"model": {"active": others[0]}})
            else:
                self._emit_property("ActiveModel", "")
        self._refresh_state()

    def m_CheckUpdates(self) -> None:  # noqa: N802
        def done() -> bool:
            self._emit("ModelsChanged", "", ())
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(int(1500 * self.speed), done)

    def m_GetHistory(self) -> list[dict[str, Any]]:  # noqa: N802
        return list(self.history)

    def m_ClearHistory(self) -> None:  # noqa: N802
        self.history.clear()

    def m_ListAudioDevices(self) -> list[dict[str, Any]]:  # noqa: N802
        return list(DEVICES)

    def m_GetDiagnostics(self) -> dict[str, Any]:  # noqa: N802
        return self._diagnostics()

    def m_Quit(self) -> None:  # noqa: N802
        GLib.timeout_add(50, lambda: self.loop.quit() or GLib.SOURCE_REMOVE)


class DaemonError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Test helpers: a private bus (no service activation) + this daemon
# --------------------------------------------------------------------------- #

BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:tmpdir={tmp}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def _die_with_parent() -> None:
    """Child processes exit if the test process dies (Linux), so nothing leaks."""
    try:
        import ctypes
        import signal

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):  # pragma: no cover - not Linux/glibc
        pass


class FakeService:
    """Private dbus-daemon plus a fake Greffier service, for tests.

    ``with FakeService("--fast") as svc:`` starts both; ``svc.connection()``
    returns a client connection to the private bus; ``stop_daemon()`` and
    ``start_daemon()`` simulate the service going away and coming back.
    """

    def __init__(self, *args: str) -> None:
        self.args = list(args)
        self.address = ""
        self._tmp: Any = None
        self._bus: Any = None
        self._daemon: Any = None

    def __enter__(self) -> FakeService:
        self.start_bus()
        self.start_daemon()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def start_bus(self) -> None:
        import shutil
        import subprocess
        import tempfile

        exe = shutil.which("dbus-daemon")
        if exe is None:
            raise RuntimeError("dbus-daemon is not installed")
        self._tmp = tempfile.TemporaryDirectory(prefix="greffier-bus-")
        conf = Path(self._tmp.name) / "bus.conf"
        conf.write_text(BUS_CONFIG.format(tmp=self._tmp.name), encoding="utf-8")
        self._bus = subprocess.Popen([exe, f"--config-file={conf}", "--nofork",
                                      "--print-address=1"], stdout=subprocess.PIPE, text=True,
                                     preexec_fn=_die_with_parent)
        self.address = self._bus.stdout.readline().strip()

    def start_daemon(self, *args: str, timeout: float = 15.0) -> None:
        import os
        import selectors
        import subprocess
        import time

        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS=self.address)
        self._daemon = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                         *(args or self.args)],
                                        env=env, stdout=subprocess.PIPE, text=True,
                                        preexec_fn=_die_with_parent)
        selector = selectors.DefaultSelector()
        selector.register(self._daemon.stdout, selectors.EVENT_READ)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if selector.select(timeout=0.1) and "READY" in self._daemon.stdout.readline():
                return
            if self._daemon.poll() is not None:
                break
        raise RuntimeError("the fake daemon did not start")

    def stop_daemon(self) -> None:
        if self._daemon is not None:
            self._daemon.terminate()
            self._daemon.wait(5)
            self._daemon = None

    def stop(self) -> None:
        self.stop_daemon()
        if self._bus is not None:
            self._bus.terminate()
            self._bus.wait(5)
            self._bus = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None

    def connection(self) -> Gio.DBusConnection:
        flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                 | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
        return Gio.DBusConnection.new_for_address_sync(self.address, flags, None, None)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    """Iterate the default GLib main context until ``predicate()`` is true."""
    import time

    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        for _ in range(200):
            if not context.iteration(False):
                break
        time.sleep(0.005)
    return bool(predicate())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", default="default",
                        choices=["default", "first-run", "no-model", "recording",
                                 "transcribing", "downloading"])
    parser.add_argument("--fast", action="store_true", help="shorten every delay (tests)")
    parser.add_argument("--freeze-progress", type=float, default=None, metavar="FRACTION",
                        help="keep fake downloads at this fraction (screenshots)")
    parser.add_argument("--extension", default="none", choices=["none", "installed", "active"],
                        help="state of the GNOME Shell extension in diagnostics")
    parser.add_argument("--gnome", default="48.2", help="GNOME Shell version to report")
    args = parser.parse_args(argv)
    return FakeDaemon(args).run()


if __name__ == "__main__":
    sys.exit(main())
