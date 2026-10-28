"""The dictation controller: one state machine tying all components together.

Everything here runs on the GLib main thread except the jobs handed to
``Worker`` (engine loading / transcription) and the download threads. The
engine objects are *owned by the worker thread* (``_w_*`` attributes): the main
thread only keeps a summary of what is loaded, which keeps loading, unloading
and transcribing correctly ordered without locks.

Components are created through a ``Deps`` object so that tests can replace the
microphone, the engine, the injector, etc. with fakes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from gi.repository import GLib

from greffier import config as cfgmod
from greffier import paths
from greffier.const import (
    DBUS_IFACE,
    DBUS_NAME,
    DBUS_PATH,
    SAMPLE_RATE,
    VERSION,
    State,
)
from greffier.daemon.history import History
from greffier.daemon.worker import Worker, call_in_main
from greffier.i18n import _

log = logging.getLogger(__name__)

TOGGLE_COMMAND = [
    "gdbus", "call", "--session", "--dest", DBUS_NAME, "--object-path", DBUS_PATH,
    "--method", f"{DBUS_IFACE}.Toggle",
]
ERROR_DISPLAY_SECONDS = 4
CATALOG_FIRST_CHECK_SECONDS = 45
CATALOG_CHECK_INTERVAL_SECONDS = 24 * 3600


class DaemonError(Exception):
    """Error reported to D-Bus callers as io.github.greffier.Greffier.Error.<code>."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Deps:
    """Factories and services used by the controller (replaceable in tests)."""

    models: Any  # greffier.models.manager.ModelManager
    recorder_factory: Callable[..., Any]  # -> greffier.audio.Recorder
    list_devices: Callable[[], list]
    engine_loader: Callable[..., Any]  # -> greffier.stt.Engine
    transcriber_factory: Callable[[Any, Path | None, float], Any]
    vad_factory: Callable[[Path], Any]  # -> greffier.stt.VoiceActivity
    postprocess: Callable[[str, Any, str], str]
    injector: Any  # greffier.desktop.injection.Injector
    hotkeys_factory: Callable[..., Any]  # (on_pressed, on_released, toggle_command)
    sounds: Any  # greffier.audio.SoundPlayer
    fetch_catalog: Callable[[str, Path], Any]  # -> Catalog (blocking, thread)
    set_autostart: Callable[[bool], None]
    environment: Callable[[], dict]  # extra diagnostics (shell version, …)
    install_cancelled: type[BaseException] = Exception


class Controller:
    def __init__(
        self,
        deps: Deps,
        emit: Callable[[str, str, tuple], None],
        notify: Callable[..., None],
        config_path: Path | None = None,
    ) -> None:
        self.deps = deps
        self._emit = emit  # (signal name, GVariant signature, args)
        self._notify = notify  # (id, title, body, *, open_gui=True)
        self._config_path = config_path
        self.cfg = cfgmod.load(config_path)

        self._state: State | None = None
        self._detail = ""
        self._active_property = ""
        self._error_source = 0

        # Engine, as seen from the main thread.
        self._loaded_model = ""
        self._loading_model = ""
        self._load_generation = 0
        self._last_load_error = ""
        self._pending_transcriptions = 0
        self._unload_source = 0

        # Engine, owned by the worker thread.
        self._worker = Worker("greffier-engine")
        self._w_engine: Any = None
        self._w_transcriber: Any = None
        self._w_model = ""

        # Recording session.
        self._recorder: Any = None
        self._session = 0
        self._rec_started = 0.0
        self._rec_timeout = 0
        self._vad: Any = None
        self._vad_lock = threading.Lock()
        self._auto_stop_posted = False
        self._live: dict[int, dict[str, Any]] = {}
        self._live_queue: list[np.ndarray] = []  # guarded by _vad_lock
        self._cancelled_sessions: set[int] = set()

        # Model installs.
        self._installs: dict[str, threading.Event] = {}
        self._progress: dict[str, tuple[str, int, int]] = {}
        self._catalog_source = 0
        self._catalog_busy = False

        g = self.cfg.general
        self.history = History(paths.history_file(), g.history_size, g.persist_history)
        self.hotkeys = deps.hotkeys_factory(
            self._on_hotkey_pressed, self._on_hotkey_released, TOGGLE_COMMAND
        )

    # ===================================================================== #
    # Lifecycle
    # ===================================================================== #

    def start(self) -> None:
        self._apply_hotkeys()
        self._ensure_active_model(announce=False)
        if self.cfg.model.preload:
            self._load_engine()
        self._refresh_state()
        self._ensure_vad_installed()
        if self.cfg.model.check_updates:
            self._catalog_source = GLib.timeout_add_seconds(
                CATALOG_FIRST_CHECK_SECONDS, self._on_catalog_timer
            )

    def shutdown(self) -> None:
        for source in (self._error_source, self._unload_source, self._rec_timeout,
                       self._catalog_source):
            if source:
                GLib.source_remove(source)
        self._error_source = self._unload_source = self._rec_timeout = 0
        self._catalog_source = 0
        if self._recorder is not None:
            try:
                self._recorder.cancel()
            except Exception:
                log.exception("cannot cancel recording")
            self._recorder = None
        for event in self._installs.values():
            event.set()
        for closable in (self.hotkeys, self.deps.injector):
            try:
                closable.close()
            except Exception:
                log.exception("error while closing %r", closable)
        self._worker.submit(self._w_unload)
        self._worker.shutdown()

    # ===================================================================== #
    # State
    # ===================================================================== #

    @property
    def state(self) -> State:
        return self._state or State.IDLE

    def _compute_state(self) -> tuple[State, str]:
        if self._recorder is not None:
            return State.RECORDING, ""
        if self._pending_transcriptions:
            return State.TRANSCRIBING, ""
        model = self._active_installed()
        if model is None:
            return State.NO_MODEL, _("No speech recognition model is installed.")
        if self._loading_model:
            return State.LOADING, _("Loading {name}…").format(name=model.spec.name)
        return State.IDLE, ""

    def _refresh_state(self) -> None:
        if self._error_source:
            return  # an error is being displayed; it refreshes when it expires
        state, detail = self._compute_state()
        self._publish_state(state, detail)

    def _publish_state(self, state: State, detail: str) -> None:
        if (state, detail) != (self._state, self._detail):
            self._state, self._detail = state, detail
            self._emit("StateChanged", "(ss)", (str(state), detail))
            self._emit_properties({"State": GLib.Variant("s", str(state))})
        active = self.cfg.model.active if self._active_installed() else ""
        if active != self._active_property:
            self._active_property = active
            self._emit_properties({"ActiveModel": GLib.Variant("s", active)})

    def _emit_properties(self, changed: dict[str, GLib.Variant]) -> None:
        self._emit("PropertiesChanged", "(sa{sv}as)", (DBUS_IFACE, changed, []))

    def _fail(self, message: str, *, notify: bool = True, sound: bool = True) -> None:
        """Show an error for a few seconds, then return to the normal state.

        While the microphone is open the RECORDING state is kept (the user must
        still see that they are being recorded); the error is only announced.
        """
        log.warning("%s", message)
        if self._recorder is None:
            if self._error_source:
                GLib.source_remove(self._error_source)
            self._error_source = GLib.timeout_add_seconds(ERROR_DISPLAY_SECONDS,
                                                          self._end_error)
            self._publish_state(State.ERROR, message)
        self._emit("Notice", "(ss)", ("error", message))
        if sound:
            self._sound("error")
        if notify and self.cfg.general.notifications:
            self._notify("error", _("Dictation problem"), message)

    def _end_error(self) -> bool:
        self._error_source = 0
        self._refresh_state()
        return GLib.SOURCE_REMOVE

    def _notice(self, level: str, message: str) -> None:
        self._emit("Notice", "(ss)", (level, message))

    def _sound(self, name: str) -> None:
        if self.cfg.general.sounds:
            try:
                self.deps.sounds.play(name)
            except Exception:
                log.exception("cannot play sound %s", name)

    # ===================================================================== #
    # Models and engine
    # ===================================================================== #

    def _active_installed(self) -> Any:
        try:
            model = self.deps.models.get_installed(self.cfg.model.active)
        except Exception:
            log.exception("cannot read installed model")
            return None
        return model if model is not None and model.spec.kind == "asr" else None

    def _installed_asr(self) -> list[Any]:
        found = []
        for status in self.deps.models.statuses():
            if status.installed is not None and status.installed.spec.kind == "asr":
                found.append(status.installed)
        return found

    def _ensure_active_model(self, announce: bool = True) -> None:
        """If the configured model is missing, fall back to another installed one."""
        if self._active_installed() is not None:
            return
        candidates = self._installed_asr()
        if not candidates:
            return
        chosen = candidates[0]
        log.info("active model %r unavailable, switching to %r", self.cfg.model.active, chosen.id)
        self._set_config({"model": {"active": chosen.id}}, react=False)
        if announce:
            self._notice("info", _("Now using {name}.").format(name=chosen.spec.name))

    def _vad_path(self) -> Path | None:
        try:
            vad = self.deps.models.vad_model()
        except Exception:
            log.exception("cannot look up the VAD model")
            return None
        return vad.file("model") if vad is not None else None

    def _load_engine(self, force: bool = False) -> None:
        model = self._active_installed()
        if model is None:
            return
        if not force and model.id in (self._loaded_model, self._loading_model):
            return
        self._cancel_unload()
        self._load_generation += 1
        generation = self._load_generation
        self._loading_model = model.id
        threads, provider = self.cfg.model.threads, self.cfg.model.provider
        vad_path = self._vad_path()
        self._refresh_state()

        def job() -> tuple[str, float]:
            self._w_unload()
            started = time.monotonic()
            engine = self.deps.engine_loader(model, threads=threads, provider=provider)
            self._w_engine, self._w_model = engine, model.id
            self._w_transcriber = self.deps.transcriber_factory(
                engine, vad_path, model.spec.max_segment_s
            )
            return model.id, time.monotonic() - started

        def done(result: tuple[str, float]) -> None:
            if generation != self._load_generation:
                return
            self._loading_model = ""
            self._loaded_model = result[0]
            self._last_load_error = ""
            log.info("model %s loaded in %.1f s", result[0], result[1])
            self._refresh_state()
            self._schedule_unload()

        def failed(exc: BaseException) -> None:
            if generation != self._load_generation:
                return
            self._loading_model = ""
            self._loaded_model = ""
            self._last_load_error = str(exc)
            if self._recorder is not None:
                self.cancel(quiet=True)  # nothing could transcribe this take
            self._fail(_("Could not load the model: {error}").format(error=exc))

        self._worker.submit(job, done, failed)

    def _w_unload(self) -> None:
        """Worker thread: release the engine."""
        engine, self._w_engine = self._w_engine, None
        self._w_transcriber, self._w_model = None, ""
        if engine is not None:
            try:
                engine.close()
            except Exception:
                log.exception("error while closing the engine")

    def _unload_engine(self) -> None:
        self._load_generation += 1
        self._loading_model = ""
        self._loaded_model = ""
        self._worker.submit(self._w_unload)
        self._refresh_state()

    def _rebuild_transcriber(self) -> None:
        """Pick up a newly installed VAD without reloading the model."""
        model = self._active_installed()
        if model is None or not self._loaded_model:
            return
        vad_path = self._vad_path()

        def job() -> None:
            if self._w_engine is not None:
                self._w_transcriber = self.deps.transcriber_factory(
                    self._w_engine, vad_path, model.spec.max_segment_s
                )

        self._worker.submit(job)

    def _schedule_unload(self) -> None:
        self._cancel_unload()
        minutes = self.cfg.model.unload_after
        if minutes > 0 and self._loaded_model:
            self._unload_source = GLib.timeout_add_seconds(minutes * 60, self._on_unload_timer)

    def _cancel_unload(self) -> None:
        if self._unload_source:
            GLib.source_remove(self._unload_source)
            self._unload_source = 0

    def _on_unload_timer(self) -> bool:
        self._unload_source = 0
        if self._recorder is None and not self._pending_transcriptions:
            log.info("unloading idle model to free memory")
            self._unload_engine()
        return GLib.SOURCE_REMOVE

    # ===================================================================== #
    # Dictation
    # ===================================================================== #

    def toggle(self) -> None:
        if self._recorder is not None:
            self.stop()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        if self._recorder is not None:
            return
        model = self._active_installed()
        if model is None:
            self._ensure_active_model()
            model = self._active_installed()
        if model is None:
            message = _("No speech recognition model is installed. Open Greffier to choose one.")
            self._fail(message)
            raise DaemonError("NoModel", message)
        if model.id not in (self._loaded_model, self._loading_model):
            self._load_engine()  # loads while the user speaks

        rec_cfg = self.cfg.recording
        vad = None
        if rec_cfg.auto_stop or rec_cfg.live:
            vad_path = self._vad_path()
            if vad_path is None:
                self._notice("warning", _("The voice detection model is missing: automatic "
                                          "stop and live insertion are disabled."))
                self._ensure_vad_installed()
            else:
                try:
                    vad = self.deps.vad_factory(vad_path)
                except Exception as exc:
                    log.warning("cannot create the VAD: %s", exc)

        self._session += 1
        session = self._session
        try:
            recorder = self.deps.recorder_factory(
                device_id=rec_cfg.device,
                on_chunk=(lambda samples: self._on_chunk(session, samples)) if vad else None,
                on_level=self._on_level,
                on_error=lambda message: self._on_audio_error(session, message),
            )
            recorder.start()
        except Exception as exc:
            message = _("Cannot open the microphone: {error}").format(error=exc)
            self._fail(message)
            raise DaemonError("Failed", message) from exc

        self._recorder = recorder
        self._vad = vad
        self._auto_stop_posted = False
        self._rec_started = time.monotonic()
        self._live_queue = []
        if rec_cfg.live and vad is not None:  # without a VAD: transcribe the whole take
            self._live[session] = {"parts": [], "pending": 0, "stopped": False,
                                   "duration": 0.0, "model": model.id}
        self._rec_timeout = GLib.timeout_add_seconds(rec_cfg.max_duration, self._on_max_duration)
        self._cancel_unload()
        if self._error_source:  # a new action supersedes a displayed error
            GLib.source_remove(self._error_source)
            self._error_source = 0
        self._sound("start")
        self._refresh_state()

    def stop(self) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        session = self._session
        self._recorder = None
        if self._rec_timeout:
            GLib.source_remove(self._rec_timeout)
            self._rec_timeout = 0
        try:
            audio = recorder.stop()
        except Exception:
            log.exception("error while stopping the recorder")
            audio = np.zeros(0, dtype=np.float32)
        duration = len(audio) / SAMPLE_RATE
        self._sound("stop")
        with self._vad_lock:
            vad, self._vad = self._vad, None
            # Segments the streaming thread queued but the main loop has not
            # drained yet come first, then what the VAD still holds.
            tail, self._live_queue = self._live_queue, []
            if vad is not None and session in self._live:
                tail.extend(vad.flush())

        live = self._live.get(session)
        if live is not None:
            live["stopped"] = True
            live["duration"] = duration
            for segment in tail:
                self._submit_transcription(segment, session, partial=True, duration=duration)
            self._maybe_finish_live(session)
        else:
            self._submit_transcription(audio, session, partial=False, duration=duration)
        self._refresh_state()

    def cancel(self, quiet: bool = False) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        self._recorder = None
        self._cancelled_sessions.add(self._session)
        self._live.pop(self._session, None)
        if self._rec_timeout:
            GLib.source_remove(self._rec_timeout)
            self._rec_timeout = 0
        with self._vad_lock:
            self._vad = None
            self._live_queue = []
        try:
            recorder.cancel()
        except Exception:
            log.exception("error while cancelling the recorder")
        if not quiet:
            self._sound("cancel")
            self._notice("info", _("Dictation cancelled."))
        self._refresh_state()
        self._schedule_unload()

    def _on_max_duration(self) -> bool:
        self._rec_timeout = 0
        if self._recorder is not None:
            self._notice("warning", _("Maximum dictation length reached."))
            self.stop()
        return GLib.SOURCE_REMOVE

    # -- callbacks from the GStreamer streaming thread ------------------------ #

    def _on_level(self, level: float) -> None:
        call_in_main(self._emit, "Level", "(d)", (float(level),))

    def _on_chunk(self, session: int, samples: np.ndarray) -> None:
        rec_cfg = self.cfg.recording
        with self._vad_lock:
            vad = self._vad
            if vad is None or session != self._session:
                return
            segments = vad.accept(samples)
            started, silence = vad.speech_started, vad.trailing_silence
            live = bool(segments) and rec_cfg.live
            if live:
                self._live_queue.extend(segments)
        if live:
            call_in_main(self._on_live_segments, session)
        if (rec_cfg.auto_stop and started and silence >= rec_cfg.silence_timeout
                and not self._auto_stop_posted):
            self._auto_stop_posted = True
            call_in_main(self._on_auto_stop, session)

    def _on_audio_error(self, session: int, message: str) -> None:
        if session == self._session and self._recorder is not None:
            self.cancel()
            self._fail(_("Microphone error: {error}").format(error=message))

    # -- main thread ---------------------------------------------------------- #

    def _on_auto_stop(self, session: int) -> None:
        if session == self._session and self._recorder is not None:
            log.info("silence detected, stopping")
            self.stop()

    def _on_live_segments(self, session: int) -> None:
        if session != self._session or session not in self._live:
            return
        with self._vad_lock:
            segments, self._live_queue = self._live_queue, []
        for segment in segments:
            self._submit_transcription(segment, session, partial=True, duration=0.0)

    def _submit_transcription(self, audio: np.ndarray, session: int, *, partial: bool,
                              duration: float) -> None:
        language = self.cfg.model.language
        live = self._live.get(session) if partial else None
        if live is not None:
            live["pending"] += 1
        self._pending_transcriptions += 1
        self._cancel_unload()

        def job() -> tuple[str, float, str]:
            transcriber = self._w_transcriber
            if transcriber is None:
                raise RuntimeError(self._last_load_error or _("The model is not loaded."))
            started = time.monotonic()
            text = transcriber.transcribe(audio, None if language == "auto" else language)
            return text, time.monotonic() - started, self._w_model

        def done(result: tuple[str, float, str]) -> None:
            self._pending_transcriptions -= 1
            if live is not None:
                live["pending"] -= 1
            self._on_transcribed(session, result, partial=partial, duration=duration)
            if live is not None:
                self._maybe_finish_live(session)
            self._refresh_state()
            self._schedule_unload()

        def failed(exc: BaseException) -> None:
            self._pending_transcriptions -= 1
            if live is not None:
                live["pending"] -= 1
                self._maybe_finish_live(session)
            self._fail(_("Transcription failed: {error}").format(error=exc))

        self._worker.submit(job, done, failed)
        self._refresh_state()

    def _on_transcribed(self, session: int, result: tuple[str, float, str], *, partial: bool,
                        duration: float) -> None:
        raw, elapsed, model_id = result
        if session in self._cancelled_sessions:
            return
        language = self.cfg.model.language
        text = self.deps.postprocess(raw, self.cfg.text, language) if raw.strip() else ""
        log.debug("transcribed %.1f s in %.2f s: %r", duration, elapsed, text)
        if not text.strip():
            if not partial:
                self._notice("info", _("Nothing was heard."))
            return
        if partial and session in self._live:
            self._live[session]["parts"].append(text)

        meta = {
            "duration": round(duration, 2),
            "elapsed": round(elapsed, 3),
            "model": model_id,
            "language": language,
            "partial": partial,
            "injected": False,
            "method": "",
        }
        if not partial:
            self._add_history(text, duration, model_id, language)
        # The injector appends the separating space itself (injection.trailing_space).
        self.deps.injector.inject(text, lambda res: self._on_injected(res, text, meta))

    def _on_injected(self, result: Any, text: str, meta: dict[str, Any]) -> None:
        meta["injected"] = bool(result.ok) and result.method != "clipboard"
        meta["method"] = result.method
        self._emit("Transcribed", "(ss)", (text, json.dumps(meta, ensure_ascii=False)))
        if not result.ok:
            self._fail(result.message or _("The text could not be inserted."))
            return
        if result.method == "clipboard" and self.cfg.general.notifications:
            self._notify("copied", _("Text copied to the clipboard"),
                         result.message or _("Paste it with Ctrl+V."), open_gui=False)
        elif self.cfg.general.notify_transcription and self.cfg.general.notifications:
            self._notify("transcription", _("Dictation"), text, open_gui=False)

    def _maybe_finish_live(self, session: int) -> None:
        live = self._live.get(session)
        if live is None or not live["stopped"] or live["pending"] > 0:
            return
        del self._live[session]
        if live["parts"]:
            self._add_history(" ".join(live["parts"]), live["duration"], live["model"],
                              self.cfg.model.language)

    def _add_history(self, text: str, duration: float, model_id: str, language: str) -> None:
        self.history.add({
            "text": text,
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "duration": round(duration, 2),
            "model": model_id,
            "language": language,
        })

    # -- shortcuts -------------------------------------------------------------- #

    def _apply_hotkeys(self) -> None:
        try:
            self.hotkeys.apply(self.cfg.shortcut, self._on_hotkeys_applied)
        except Exception as exc:
            log.exception("cannot set up the shortcut")
            self._notice("error", _("Cannot set up the keyboard shortcut: {error}").format(
                error=exc))

    def _on_hotkeys_applied(self, status: Any) -> None:
        if status.message:
            self._notice("info" if status.active else "warning", status.message)

    def _on_hotkey_pressed(self) -> None:
        try:
            if self.cfg.shortcut.mode == "push-to-talk":
                self.start_recording()
            else:
                self.toggle()
        except DaemonError:
            pass  # already reported to the user

    def _on_hotkey_released(self) -> None:
        if self.cfg.shortcut.mode == "push-to-talk":
            self.stop()

    # ===================================================================== #
    # Model management
    # ===================================================================== #

    def list_models(self) -> list[dict[str, Any]]:
        out = []
        for status in self.deps.models.statuses():
            item = status.to_dict()
            item["active"] = item["id"] == self.cfg.model.active and status.installed is not None
            item["loaded"] = item["id"] == self._loaded_model
            progress = self._progress.get(item["id"]) if item["id"] in self._installs else None
            item["install"] = (
                {"phase": progress[0], "done": progress[1], "total": progress[2]}
                if progress else None
            )
            out.append(item)
        return out

    def install_model(self, model_id: str, *, quiet: bool = False) -> None:
        spec = self.deps.models.catalog.get(model_id)
        if spec is None:
            raise DaemonError("NotFound", _("Unknown model: {id}").format(id=model_id))
        if model_id in self._installs:
            raise DaemonError("Busy", _("{name} is already being installed.").format(
                name=spec.name))
        cancel = threading.Event()
        self._installs[model_id] = cancel
        self._progress[model_id] = ("download", 0, spec.download.size)
        cancelled_type = self.deps.install_cancelled

        def progress(phase: str, done: int, total: int) -> None:
            call_in_main(self._on_install_progress, model_id, phase, done, total)

        def run() -> None:
            try:
                installed = self.deps.models.install(model_id, progress, cancel)
            except cancelled_type:
                call_in_main(self._on_install_end, model_id, "cancelled", None, "", quiet)
            except Exception as exc:
                log.exception("installing %s failed", model_id)
                call_in_main(self._on_install_end, model_id, "error", None, str(exc), quiet)
            else:
                call_in_main(self._on_install_end, model_id, "done", installed, "", quiet)

        threading.Thread(target=run, name=f"install-{model_id}", daemon=True).start()
        self._emit("ModelProgress", "(sstts)", (model_id, "download", 0, spec.download.size, ""))
        self._emit("ModelsChanged", "()", ())
        if spec.kind == "asr":
            self._ensure_vad_installed(require_asr=False)

    def _on_install_progress(self, model_id: str, phase: str, done: int, total: int) -> None:
        if model_id not in self._installs:
            return
        self._progress[model_id] = (phase, done, total)
        self._emit("ModelProgress", "(sstts)", (model_id, phase, done, total, ""))

    def _on_install_end(self, model_id: str, outcome: str, installed: Any, error: str,
                        quiet: bool) -> None:
        self._installs.pop(model_id, None)
        last = self._progress.pop(model_id, ("download", 0, 0))
        spec = self.deps.models.catalog.get(model_id)
        name = spec.name if spec else model_id
        total = last[2]
        self._emit("ModelProgress", "(sstts)",
                   (model_id, outcome, total if outcome == "done" else last[1], total, error))
        self._emit("ModelsChanged", "()", ())

        if outcome == "cancelled":
            self._notice("info", _("Download of {name} cancelled.").format(name=name))
        elif outcome == "error":
            message = _("Could not install {name}: {error}").format(name=name, error=error)
            if quiet:
                log.warning("%s", message)
            else:
                self._fail(message)
        elif installed is not None and installed.spec.kind == "vad":
            self._rebuild_transcriber()
        elif installed is not None:
            if not quiet:
                self._notice("info", _("{name} is installed.").format(name=name))
                if self.cfg.general.notifications:
                    self._notify("installed", _("Model ready"),
                                 _("{name} is installed and ready to use.").format(name=name))
            if model_id == self.cfg.model.active:
                self._load_engine(force=True)  # new install or update of the active model
            elif self._active_installed() is None:
                self._set_config({"model": {"active": model_id}}, react=False)
                self._load_engine()
        self._refresh_state()

    def cancel_install(self, model_id: str) -> None:
        event = self._installs.get(model_id)
        if event is None:
            raise DaemonError("NotFound", _("{id} is not being installed.").format(id=model_id))
        event.set()

    def remove_model(self, model_id: str) -> None:
        if model_id in self._installs:
            raise DaemonError("Busy", _("Cancel the download first."))
        if self.deps.models.get_installed(model_id) is None:
            raise DaemonError("NotFound", _("{id} is not installed.").format(id=model_id))
        if model_id in (self._loaded_model, self._loading_model):
            self._unload_engine()
        try:
            self.deps.models.remove(model_id)
        except Exception as exc:
            raise DaemonError("Failed", str(exc)) from exc
        self._emit("ModelsChanged", "()", ())
        if model_id == self.cfg.model.active:
            self._ensure_active_model()
            if self.cfg.model.preload:
                self._load_engine()
        self._refresh_state()

    def _ensure_vad_installed(self, require_asr: bool = True) -> None:
        """The Silero VAD is tiny and improves everything: install it silently."""
        try:
            if self.deps.models.vad_model() is not None:
                return
            if require_asr and not self._installed_asr():
                return
            vad_specs = self.deps.models.catalog.models("vad")
        except Exception:
            log.exception("cannot check the VAD model")
            return
        for spec in vad_specs[:1]:
            if spec.id not in self._installs:
                with contextlib.suppress(DaemonError):
                    self.install_model(spec.id, quiet=True)

    # -- catalog updates ------------------------------------------------------- #

    def _on_catalog_timer(self) -> bool:
        self._catalog_source = GLib.timeout_add_seconds(
            CATALOG_CHECK_INTERVAL_SECONDS, self._on_catalog_timer
        )
        if self.cfg.model.check_updates:
            self.check_updates(interactive=False)
        return GLib.SOURCE_REMOVE

    def check_updates(self, interactive: bool = True) -> None:
        if self._catalog_busy:
            return
        self._catalog_busy = True
        url = self.cfg.model.catalog_url

        def run() -> None:
            try:
                catalog = self.deps.fetch_catalog(url, paths.catalog_cache_file())
            except Exception as exc:
                call_in_main(self._on_catalog_fetched, None, str(exc), interactive)
            else:
                call_in_main(self._on_catalog_fetched, catalog, "", interactive)

        threading.Thread(target=run, name="catalog", daemon=True).start()

    def _on_catalog_fetched(self, catalog: Any, error: str, interactive: bool) -> None:
        self._catalog_busy = False
        if catalog is None:
            log.info("catalog refresh failed: %s", error)
            if interactive:
                self._notice("warning", _("Could not check for updates: {error}").format(
                    error=error))
            return
        self.deps.models.catalog = catalog
        self._emit("ModelsChanged", "()", ())
        updates = [s for s in self.deps.models.statuses() if s.update_available]
        if updates:
            names = ", ".join(s.spec.name for s in updates)
            message = _("Update available: {names}").format(names=names)
            self._notice("info", message)
            if self._should_announce(catalog.revision) and self.cfg.general.notifications:
                self._notify("updates", _("Model updates available"), message)
        elif interactive:
            self._notice("info", _("Your models are up to date."))

    @staticmethod
    def _should_announce(revision: str) -> bool:
        """Notify about a given catalog revision only once."""
        marker = paths.state_dir() / "announced-catalog"
        try:
            if marker.read_text().strip() == revision:
                return False
        except OSError:
            pass
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(revision)
        except OSError:
            pass
        return True

    # ===================================================================== #
    # Configuration
    # ===================================================================== #

    def get_config(self) -> dict[str, Any]:
        return cfgmod.to_dict(self.cfg)

    def set_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        try:
            self._set_config(patch, react=True)
        except cfgmod.ConfigError as exc:
            raise DaemonError("InvalidArgument", str(exc)) from exc
        return cfgmod.to_dict(self.cfg)

    def _set_config(self, patch: dict[str, Any], react: bool) -> None:
        new = cfgmod.apply_patch(self.cfg, patch)
        changed = cfgmod.diff_sections(self.cfg, new)
        if not changed:
            return
        try:
            cfgmod.save(new, self._config_path)
        except (OSError, UnicodeError) as exc:
            # Keep the previous settings: memory and disk must not disagree.
            log.error("cannot save the configuration: %s", exc)
            raise DaemonError("Failed", _("Cannot save the settings: {error}").format(
                error=exc)) from exc
        old, self.cfg = self.cfg, new
        self._emit("ConfigChanged", "(s)", (json.dumps(cfgmod.to_dict(new), ensure_ascii=False),))
        if react:
            self._react_to_config(old, new, changed)
        self._refresh_state()

    def _react_to_config(self, old: cfgmod.Config, new: cfgmod.Config, changed: set[str]) -> None:
        if "shortcut" in changed:
            self._apply_hotkeys()
        if "injection" in changed:
            self.deps.injector.update_config(new.injection)
        if "general" in changed:
            g = new.general
            if g.autostart != old.general.autostart:
                try:
                    self.deps.set_autostart(g.autostart)
                except Exception as exc:
                    self._notice("error", _("Cannot change the start-up setting: {error}").format(
                        error=exc))
            if (g.history_size, g.persist_history) != (old.general.history_size,
                                                       old.general.persist_history):
                self.history.configure(g.history_size, g.persist_history)
        if "model" in changed:
            m, o = new.model, old.model
            if (m.active, m.threads, m.provider) != (o.active, o.threads, o.provider):
                if self._active_installed() is None:
                    self._unload_engine()
                elif m.preload or self._loaded_model or self._loading_model:
                    self._load_engine(force=True)
            elif m.preload and not o.preload:
                self._load_engine()
            if m.unload_after != o.unload_after:
                self._schedule_unload()
            if m.check_updates and not o.check_updates and not self._catalog_source:
                self._catalog_source = GLib.timeout_add_seconds(
                    CATALOG_FIRST_CHECK_SECONDS, self._on_catalog_timer)

    # ===================================================================== #
    # Queries
    # ===================================================================== #

    def status(self) -> dict[str, Any]:
        model = self._active_installed()
        hk = self.hotkeys.status
        try:
            injection = self.deps.injector.describe()
        except Exception:
            log.exception("injector.describe failed")
            injection = {"method": self.cfg.injection.method, "keyboard": "none",
                         "clipboard": "none"}
        return {
            "state": str(self.state),
            "detail": self._detail,
            "model": model.id if model else "",
            "model_name": model.spec.name if model else "",
            "model_loaded": bool(model and self._loaded_model == model.id),
            "recording_seconds": round(time.monotonic() - self._rec_started, 1)
            if self._recorder is not None else 0.0,
            "shortcut": {
                "accelerator": hk.accelerator,
                "backend": hk.backend,
                "mode": hk.mode,
                "active": hk.active,
                "message": hk.message,
            },
            "injection": injection,
        }

    def list_audio_devices(self) -> list[dict[str, Any]]:
        return [
            {"id": d.id, "name": d.name, "is_default": d.is_default}
            for d in self.deps.list_devices()
        ]

    def diagnostics(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "version": VERSION,
            "session_type": os.environ.get("XDG_SESSION_TYPE", ""),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
            "cpu_threads": os.cpu_count() or 1,
            "gnome_shell": "",
            "sherpa_onnx": _sherpa_version(),
        }
        try:
            report.update(self.deps.environment())
        except Exception:
            log.exception("environment diagnostics failed")
        try:
            report.update(self.deps.injector.diagnostics())
        except Exception:
            log.exception("injector diagnostics failed")
        report.setdefault("extension", {"installed": False, "enabled": False, "active": False})
        report.setdefault("portals", {})
        report.setdefault("tools", {})
        try:
            devices = self.list_audio_devices()
        except Exception:
            devices = []
        report["audio_devices"] = len(devices)
        report["checks"] = self._checks(report, devices)
        return report

    def _checks(self, report: dict[str, Any], devices: list) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []

        def add(check_id: str, ok: bool, message: str, level_if_bad: str = "warning") -> None:
            checks.append({"id": check_id, "ok": ok, "level": "ok" if ok else level_if_bad,
                           "message": message})

        wayland = report["session_type"] == "wayland"
        gnome = "GNOME" in report["desktop"].upper()
        add("session", wayland and gnome,
            _("GNOME on Wayland session") if wayland and gnome else
            _("Greffier is designed for GNOME on Wayland; other sessions may only partly work."))
        model = self._active_installed()
        add("model", model is not None,
            _("Model: {name}").format(name=model.spec.name) if model else
            _("No speech recognition model is installed."), "error")
        if model is not None and self._last_load_error:
            add("model-load", False, _("The model failed to load: {error}").format(
                error=self._last_load_error), "error")
        vad = self._vad_path()
        add("vad", vad is not None,
            _("Voice detection is installed") if vad else
            _("Voice detection is not installed (long dictations and auto-stop are degraded)."))
        hk = self.hotkeys.status
        add("shortcut", hk.active,
            hk.message or (_("Shortcut {accel} is active").format(accel=hk.accelerator)
                           if hk.active else _("The keyboard shortcut is not active.")),
            "error" if self.cfg.shortcut.backend != "none" else "warning")
        ext = report.get("extension", {})
        add("extension", bool(ext.get("active")),
            _("The GNOME Shell extension is active") if ext.get("active") else
            (_("The GNOME Shell extension is installed but not active (log out and back in, "
               "then enable it in Extensions).") if ext.get("installed") else
             _("Install the Greffier GNOME Shell extension for the best experience.")))
        injection = self.status()["injection"]
        add("keyboard", injection.get("keyboard", "none") != "none",
            _("Text is inserted with: {backend}").format(backend=injection.get("keyboard"))
            if injection.get("keyboard", "none") != "none" else
            _("No way to simulate the paste shortcut: text will only be copied to the clipboard."),
            "error")
        add("microphone", bool(devices),
            ngettext_devices(len(devices)) if devices else
            _("No microphone found (check GNOME Settings › Privacy › Microphone)."))
        return checks

    def history_items(self) -> list[dict[str, Any]]:
        return self.history.items()

    def clear_history(self) -> None:
        self.history.clear()



def ngettext_devices(count: int) -> str:
    from greffier.i18n import ngettext

    return ngettext("{n} microphone found", "{n} microphones found", count).format(n=count)


def _sherpa_version() -> str:
    try:
        from importlib.metadata import version

        return version("sherpa-onnx")
    except Exception:
        return ""
