# Greffier — Architecture

Greffier is a local, private voice-dictation tool for GNOME on Wayland
(Fedora first). Press a shortcut, speak, and the text appears in the focused
application. Speech recognition runs entirely on the machine with
[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx); the default model is
NVIDIA Parakeet TDT 0.6B v3 (int8), which handles French with punctuation and
capitalisation at roughly 10× real time on a 4-core CPU.

Decisions are recorded in [`docs/adr/`](adr/). This page is the map.

## 1. Processes

```
 ┌───────────────┐  gdbus call …Toggle   ┌──────────────────────────────────────────┐
 │ GNOME custom  │ ────────────────────▶ │ greffier-daemon  (systemd user service,  │
 │ shortcut      │                       │ D-Bus activatable)                       │
 └───────────────┘                       │                                          │
 ┌───────────────┐  Activated/Deactivated│  HotkeyManager ─┐                        │
 │ GlobalShortcut│ ────────────────────▶ │                 ▼                        │
 │ portal (48+)  │                       │  Controller (state machine, main loop)   │
 └───────────────┘                       │   │ Recorder (GStreamer/PipeWire)        │
                                         │   │ Transcriber (VAD + sherpa engine)    │
 ┌───────────────┐   Daemon1 (D-Bus)     │   │ postprocess (voice commands, typo)   │
 │ greffier (GUI)│ ◀───────────────────▶ │   │ Injector (clipboard + paste/type)    │
 │ GTK4/Adwaita  │                       │   │ ModelManager (catalog, downloads)    │
 └───────────────┘                       │   └ History, notifications, sounds       │
 ┌───────────────┐   Daemon1 (D-Bus)     └───────┬──────────────────────────────────┘
 │ greffierctl   │ ◀───────────────────▶         │ Shell1 (D-Bus): clipboard, paste, type
 └───────────────┘                               ▼
                                         ┌──────────────────────────────────────────┐
                                         │ GNOME Shell extension (optional)         │
                                         │ top-bar indicator, recording OSD,        │
                                         │ focus-free clipboard + virtual keyboard  │
                                         └──────────────────────────────────────────┘
```

* **greffier-daemon** owns everything stateful: configuration (single writer),
  models, the microphone, the engine and the history. It runs a GLib main loop
  (no GTK) and is a `Gio.Application` with id `io.github.greffier.Greffier.Daemon`
  so that notifications and portals attribute it to Greffier.
* **greffier** (GUI) and **greffierctl** (CLI) are thin D-Bus clients. Closing the
  window never stops dictation; the GUI can be started/stopped at will.
* The **GNOME Shell extension** is optional. With it, injection is invisible and
  instant and the user gets visual feedback that cannot steal focus. Without it,
  Greffier falls back to xdg-desktop-portal RemoteDesktop, `wl-copy` and `ydotool`.

The D-Bus contracts are the source of truth:
[`greffier/data/daemon-interface.xml`](../greffier/data/daemon-interface.xml) and
[`greffier/data/shell-interface.xml`](../greffier/data/shell-interface.xml).

## 2. Dictation pipeline

```
NO_MODEL ──install/select──▶ LOADING ──ok──▶ IDLE ──Start──▶ RECORDING ──Stop──▶ TRANSCRIBING
    ▲                          │ fail            ▲   ◀─Cancel──┘    │ auto-stop       │
    └──────────── ERROR ◀──────┘                 └──────── inject + history ◀─────────┘
```

1. `Start`: open the microphone (GStreamer `autoaudiosrc → audioconvert →
   audioresample → appsink`, 16 kHz mono float32), play the start sound, emit
   `Level` at ~15 Hz. If the model is not loaded yet, recording starts anyway
   and loading continues in parallel.
2. While recording, the VAD (Silero) tracks speech: it drives auto-stop on
   silence and, in *live* mode, cuts segments that are transcribed and inserted
   while the user keeps talking.
3. `Stop`: the audio goes to a single worker thread. Recordings longer than the
   model's `max_segment_s` are split on pauses by the VAD.
4. The text is post-processed (voice commands, replacements, optional French
   typography) and handed to the `Injector` on the main thread, which appends
   the separating space (`injection.trailing_space`) and serialises injections.

Threading rule: **GLib main thread for all state and D-Bus; one worker thread
for model loading/transcription; one worker thread per download; GStreamer
streaming threads only push audio into thread-safe buffers.** Results come back
with `GLib.idle_add`.

## 3. Text injection on GNOME Wayland

Wayland forbids clients from typing into other windows, so Greffier composes
two backends: a *clipboard* backend and a *keyboard* backend.

| Method (`injection.method`) | What happens | Works with accents / any layout |
|---|---|---|
| `auto` (default) = `paste` | set CLIPBOARD+PRIMARY, press the paste shortcut, restore the previous text clipboard | yes |
| `type` | synthesise key presses from keysyms | only characters present in the current layout |
| `clipboard` | only copy, notify the user | yes (manual paste) |

Backends, in `auto` preference order:

* keyboard: **extension** (Clutter virtual keyboard) → **portal**
  (`org.freedesktop.portal.RemoteDesktop`, one-time consent, persisted with a
  restore token) → **ydotool** (needs `ydotoold`) → none.
* clipboard: **extension** (`St.Clipboard`) → **wl-copy** → none.

The paste shortcut defaults to `auto`: Ctrl+Shift+V when the extension reports
a terminal has focus, Ctrl+V otherwise.

## 4. Shortcuts

* `toggle` mode (default): a GNOME *custom keyboard shortcut* named "Greffier"
  runs `gdbus call … Toggle`. It is visible and editable in GNOME Settings →
  Keyboard, survives crashes and D-Bus-activates the daemon.
* `push-to-talk` mode: the GlobalShortcuts portal (GNOME 48+) delivers press
  *and* release events. The daemon registers itself with
  `org.freedesktop.host.portal.Registry` and runs as `app-<id>.service` so that
  the portal can identify it.

## 5. Models

* `greffier/data/catalog.json` is the built-in catalog. A remote copy
  (`model.catalog_url`, the same file on the main branch) is fetched daily; the
  one with the highest `revision` wins, so new models and fixed archives reach
  users without an application release.
* Each entry carries the archive URL, size, **SHA-256**, the archive root and
  the role → file mapping for its engine *family*.
* Install = resumable download to `~/.cache/greffier/downloads`, SHA-256
  verification, safe extraction (no absolute paths, no `..`, no links) into a
  staging directory, then an atomic rename to `~/.local/share/greffier/models/<id>`
  with a `greffier-model.json` manifest. An update is an install of a newer
  `revision`; the old copy is replaced only once the new one is complete.
* The Silero VAD (640 KB) is a support model installed automatically.

| Family | sherpa-onnx factory | Language handling |
|---|---|---|
| `nemo_transducer` (Parakeet) | `from_transducer(model_type="nemo_transducer")` | automatic |
| `nemo_canary` | `from_nemo_canary(src_lang, tgt_lang)` | required |
| `whisper` | `from_whisper(language)` | optional (`""` = detect) |
| `cohere_transcribe` | `from_cohere_transcribe` + `stream.set_option("language")` | required |
| `qwen3_asr` | `from_qwen3_asr` + `stream.set_option("language", "French")` | optional |

## 6. Component contracts (Python)

All modules live in the `greffier` package. Signatures below are binding for
anyone implementing or calling them.

### `greffier.config`
`Config` dataclasses, `load()`, `save()`, `apply_patch(cfg, patch) -> Config`
(raises `ConfigError`), `to_dict()`, `from_dict()`, `diff_sections(a, b)`.

### `greffier.models`
* `types.py`: `ModelSpec`, `Download`, `InstalledModel`, `CatalogError`.
* `catalog.py`: `Catalog.load(cache_file: Path | None = None) -> Catalog`,
  `.get(id) -> ModelSpec | None`, `.models(kind: str | None = None) -> list[ModelSpec]`,
  `.revision`, `Catalog.fetch_remote(url, cache_file, timeout=15.0) -> Catalog`
  (raises `CatalogError`/`OSError`).
* `manager.py`:
  ```python
  ProgressFn = Callable[[str, int, int], None]   # (phase, done, total) — called from the worker thread
  class ModelError(RuntimeError); class InstallCancelled(ModelError)
  @dataclass class ModelStatus:  spec, installed: InstalledModel | None, update_available: bool
      def to_dict(self) -> dict   # spec.to_dict() + {"installed", "installed_revision", "update_available", "size_on_disk"}
  class ModelManager:
      def __init__(self, catalog: Catalog, models_dir: Path, downloads_dir: Path)
      catalog: Catalog                      # replaceable after a refresh
      def statuses(self) -> list[ModelStatus]
      def get_installed(self, model_id: str) -> InstalledModel | None
      def install(self, model_id: str, progress: ProgressFn, cancel: threading.Event) -> InstalledModel
      def remove(self, model_id: str) -> None
  ```
  `install()` blocks (run it in a thread) and emits phases `download`, `verify`,
  `extract`; it is safe to call for an installed model (update / repair).

### `greffier.audio`
```python
class AudioError(RuntimeError)
@dataclass(frozen=True) class AudioDevice: id: str; name: str; is_default: bool
def list_devices() -> list[AudioDevice]
class Recorder:
    def __init__(self, device_id: str = "", on_chunk: Callable[[np.ndarray], None] | None = None,
                 on_level: Callable[[float], None] | None = None)
    def start(self) -> None          # raises AudioError; main thread
    def stop(self) -> np.ndarray     # whole take, float32 mono 16 kHz
    def cancel(self) -> None
    active: bool; duration: float
class SoundPlayer: def play(self, name: str) -> None   # "start" | "stop" | "cancel" | "error"
```
`on_chunk`/`on_level` run on a GStreamer thread. `GREFFIER_AUDIO_SOURCE` may hold
a `gst-launch` source description (e.g. `filesrc location=a.wav ! decodebin`)
to replace the microphone in tests.

### `greffier.stt`
```python
class EngineError(RuntimeError)
class Engine(Protocol):
    model_id: str
    def transcribe(self, samples: np.ndarray, language: str | None = None) -> str
    def close(self) -> None
def load_engine(model: InstalledModel, *, threads: int = 0, provider: str = "cpu") -> Engine
class VoiceActivity:     # streaming Silero VAD
    def __init__(self, model_path: Path, *, min_silence: float = 0.5, max_speech: float = 25.0)
    def accept(self, samples: np.ndarray) -> list[np.ndarray]   # finished speech segments
    def flush(self) -> list[np.ndarray]
    speech_started: bool; trailing_silence: float  # seconds since the last speech frame
class Transcriber:
    def __init__(self, engine: Engine, vad_model: Path | None, max_segment_s: float)
    def transcribe(self, samples: np.ndarray, language: str | None) -> str
```
`language` is an ISO 639-1 code or `None`/`"auto"`; adapters translate it for
their family and ignore it when the model detects the language itself.

### `greffier.text`
`postprocess(text: str, cfg: TextConfig, language: str = "fr") -> str`.

### `greffier.desktop`
Everything here runs on the GLib main thread and reports through callbacks
invoked on that thread.
```python
@dataclass class InjectResult: ok: bool; method: str; keyboard: str; clipboard: str; message: str = ""
class Injector:
    def __init__(self, bus: Gio.DBusConnection, cfg: InjectionConfig)
    def update_config(self, cfg: InjectionConfig) -> None
    def inject(self, text: str, callback: Callable[[InjectResult], None]) -> None
    def describe(self) -> dict      # {"method", "keyboard", "clipboard"} that would be used now
    def diagnostics(self) -> dict   # {"extension": {...}, "portals": {...}, "tools": {...}}
    def close(self) -> None
@dataclass class HotkeyStatus: backend: str; active: bool; accelerator: str; mode: str; message: str = ""
class HotkeyManager:
    def __init__(self, bus, on_pressed: Callable[[], None], on_released: Callable[[], None],
                 toggle_command: list[str])
    def apply(self, cfg: ShortcutConfig, callback: Callable[[HotkeyStatus], None] | None = None) -> None
    status: HotkeyStatus
    def remove(self) -> None         # delete the GNOME custom shortcut (uninstall)
    def close(self) -> None
def autostart.is_enabled() -> bool; def autostart.set_enabled(enabled: bool) -> None
def portal.register_host_app(bus, app_id) -> bool   # once, before any other portal call
```

## 7. Files on disk

| Path | Content |
|---|---|
| `~/.config/greffier/config.toml` | settings (written by the daemon) |
| `~/.local/share/greffier/models/<id>/` | installed models + `greffier-model.json` |
| `~/.cache/greffier/downloads/` | partial downloads (`.part`, resumable) |
| `~/.cache/greffier/catalog.json` | last remote catalog |
| `~/.local/state/greffier/history.jsonl` | history, only if `general.persist_history` |
| `~/.config/systemd/user/app-io.github.greffier.Greffier.Daemon.service` | autostart unit |
| `~/.local/share/dbus-1/services/io.github.greffier.Greffier.Daemon.service` | D-Bus activation of the service |
| `~/.local/share/dbus-1/services/io.github.greffier.Greffier.service` | D-Bus activation of the window (`DBusActivatable=true`) |

`GREFFIER_HOME` relocates config/data/cache/state (tests, portable use).

## 8. Internationalisation

Source strings are English, wrapped in `_()` from `greffier.i18n`. Translations
are plain `.po` files in `greffier/data/locale/` parsed at runtime (no `msgfmt`
step). French is complete and is the reference translation.

## 9. Renaming the project

Change `APP_ID`/`APP_NAME` in `greffier/const.py`, then the ids in
`data/` (desktop files, metainfo, systemd and D-Bus service files, icons),
`gnome-extension/*/metadata.json` + `extension.js` constants, and
`greffier/data/*-interface.xml`.
