"""Microphone capture with GStreamer (PipeWire / PulseAudio).

The recording pipeline is::

    <source> ! audioconvert ! audioresample
             ! audio/x-raw,format=F32LE,rate=16000,channels=1 ! appsink

``<source>`` is, in order of preference:

1. the ``gst-launch`` description held by ``GREFFIER_AUDIO_SOURCE`` (tests and
   demos replace the microphone with ``audiotestsrc is-live=true`` or
   ``filesrc location=a.wav ! decodebin``);
2. the element created by ``Gst.DeviceMonitor`` for the configured device;
3. ``autoaudiosrc`` (the system default source).

Threading: ``start()``, ``stop()`` and ``cancel()`` are called on the GLib main
thread. GStreamer streaming threads only append to a lock-protected buffer and
invoke the ``on_chunk`` / ``on_level`` callbacks; ``on_error`` is dispatched on
the main thread through a bus signal watch.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gi
import numpy as np

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import GLib, Gst, GstApp  # noqa: F401 - GstApp types appsink

from greffier.const import SAMPLE_RATE
from greffier.i18n import _

log = logging.getLogger(__name__)

ENV_SOURCE = "GREFFIER_AUDIO_SOURCE"
CAPS = f"audio/x-raw,format=F32LE,rate={SAMPLE_RATE},channels=1,layout=interleaved"

START_TIMEOUT = 3.0  # seconds allowed to reach PLAYING
EOS_TIMEOUT = 0.3  # seconds stop() waits for the tail of the take
PROBE_TIMEOUT = 2.0  # seconds allowed to enumerate devices
LEVEL_INTERVAL = 1 / 15  # ~15 Hz level updates
LEVEL_FLOOR_DB = -60.0  # maps to level 0
LEVEL_CEIL_DB = -10.0  # maps to level 1

# autoaudiosrc silently falls back to these when no real source works (no
# audio server, no microphone): recording would then produce pure silence.
_FAKE_SOURCES = frozenset({"audiotestsrc", "fakesrc"})
# Preferred device providers when several expose the same node: PulseAudio
# (pipewire-pulse on Fedora) is the most battle-tested capture path.
_PROVIDER_RANK = {"GstPulseDevice": 0, "GstPipeWireDevice": 1}

_init_lock = threading.Lock()


def use_gcc_unwinder() -> None:
    """Make libgcc_s the process-wide C++ unwinder before GStreamer is loaded.

    libgstreamer links libunwind, and gi loads it into the *global* symbol
    scope at ``Gst.init()``. C++ libraries whose ``_Unwind_*`` symbols are
    bound after that (onnxruntime and sherpa-onnx are bound lazily) then mix
    libunwind with the libgcc_s unwinder already used by libstdc++, and the
    first C++ exception - e.g. a corrupted model file - aborts the process
    instead of raising RuntimeError. Promoting libgcc_s to the global scope
    first makes every later binding resolve to it. Harmless when libgcc_s is
    missing or GStreamer is already loaded (then it simply cannot help).
    """
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libgcc_s.so.1", mode=os.RTLD_GLOBAL)


use_gcc_unwinder()  # importing gi.repository.Gst does not load libgstreamer yet


class AudioError(RuntimeError):
    """The microphone cannot be opened or a recording failed."""


@dataclass(frozen=True)
class AudioDevice:
    """A capture device as shown to the user."""

    id: str  # stable identifier stored in RecordingConfig.device
    name: str  # human-readable name ("Built-in Audio Analog Stereo")
    is_default: bool = False


def ensure_gst() -> None:
    """Initialise GStreamer once (thread-safe, idempotent)."""
    with _init_lock:
        if not Gst.is_initialized():
            use_gcc_unwinder()
            ok, _argv = Gst.init_check(None)
            if not ok:
                raise AudioError(_("GStreamer could not be initialised."))


def _make(factory: str, name: str | None = None) -> Gst.Element:
    element = Gst.ElementFactory.make(factory, name)
    if element is None:
        raise AudioError(
            _("The GStreamer element “{element}” is missing. Install the GStreamer base "
              "and good plugins.").format(element=factory)
        )
    return element


def rms_to_level(rms: float) -> float:
    """Map an RMS amplitude to a 0‥1 meter value (-60 dBFS → 0, -10 dBFS → 1)."""
    if rms <= 0.0 or not math.isfinite(rms):
        return 0.0
    db = 20.0 * math.log10(rms)
    level = (db - LEVEL_FLOOR_DB) / (LEVEL_CEIL_DB - LEVEL_FLOOR_DB)
    return min(1.0, max(0.0, level))


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #


def _prop(props: Gst.Structure | None, name: str) -> Any:
    if props is None or not props.has_field(name):
        return None
    try:
        return props.get_value(name)
    except TypeError:  # field type without Python mapping
        return None


def _describe(device: Gst.Device) -> AudioDevice | None:
    """AudioDevice for a Gst.Device, or None for monitors of output devices."""
    props = device.get_properties()
    name = device.get_display_name() or ""
    node_name = _prop(props, "node.name")
    try:
        internal = device.get_property("internal-name")  # GstPulseDevice only
    except TypeError:
        internal = None
    media_class = _prop(props, "media.class")
    if _prop(props, "device.class") == "monitor":
        return None
    if isinstance(media_class, str) and "Sink" in media_class:
        return None  # PipeWire exposes sink monitors this way
    for candidate in (node_name, internal):
        if isinstance(candidate, str) and candidate.endswith(".monitor"):
            return None
    device_id = ""
    for candidate in (node_name, internal, _prop(props, "device.name"),
                      _prop(props, "object.path"), name):
        if isinstance(candidate, str) and candidate:
            device_id = candidate
            break
    if not device_id:
        return None
    is_default = bool(_prop(props, "is-default") or False)
    return AudioDevice(id=device_id, name=name or device_id, is_default=is_default)


def _probe() -> list[tuple[AudioDevice, Gst.Device]]:
    """Enumerate capture devices (may block if an audio server misbehaves)."""
    ensure_gst()
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Audio/Source", None)
    found: dict[str, tuple[int, AudioDevice, Gst.Device]] = {}
    for device in monitor.get_devices() or []:
        info = _describe(device)
        if info is None:
            continue
        rank = _PROVIDER_RANK.get(type(device).__gtype__.name, len(_PROVIDER_RANK))
        known = found.get(info.id)
        if known is None or rank < known[0]:
            # Keep the default flag if any provider reports it.
            default = info.is_default or (known is not None and known[1].is_default)
            found[info.id] = (rank, AudioDevice(info.id, info.name, default), device)
        elif info.is_default and not known[1].is_default:
            found[info.id] = (known[0], AudioDevice(known[1].id, known[1].name, True), known[2])
    items = [(dev, gst_dev) for _rank, dev, gst_dev in found.values()]
    items.sort(key=lambda item: (not item[0].is_default, item[0].name.casefold()))
    return items


def _probe_with_timeout(timeout: float) -> list[tuple[AudioDevice, Gst.Device]]:
    """Run _probe() in a helper thread so a stuck audio server cannot freeze the caller."""
    result: list[tuple[AudioDevice, Gst.Device]] = []
    done = threading.Event()

    def worker() -> None:
        try:
            result.extend(_probe())
        except Exception:
            log.exception("cannot enumerate audio devices")
        finally:
            done.set()

    threading.Thread(target=worker, name="greffier-audio-probe", daemon=True).start()
    if not done.wait(timeout):
        log.warning("audio device enumeration timed out after %.1f s", timeout)
        return []
    return list(result)


def list_devices() -> list[AudioDevice]:
    """Capture devices (default first), without monitors of output devices.

    Returns an empty list quickly when no audio server is reachable.
    """
    return [device for device, _gst in _probe_with_timeout(PROBE_TIMEOUT)]


# --------------------------------------------------------------------------- #
# File decoding (tests, diagnostics)
# --------------------------------------------------------------------------- #


def decode_file(path: Path | str, timeout: float = 60.0) -> np.ndarray:
    """Decode any audio file GStreamer can read to 16 kHz mono float32.

    Blocking; meant for tests, diagnostics and command-line tools.
    Raises AudioError if the file cannot be decoded.
    """
    ensure_gst()
    path = Path(path)
    if not path.is_file():
        raise AudioError(_("Audio file not found: {path}").format(path=path))
    pipeline = Gst.Pipeline.new("greffier-decode")
    src = _make("filesrc")
    src.set_property("location", str(path))
    decode = _make("decodebin")
    convert = _make("audioconvert")
    resample = _make("audioresample")
    caps = _make("capsfilter")
    caps.set_property("caps", Gst.Caps.from_string(CAPS))
    sink = _make("appsink")
    sink.set_property("sync", False)
    sink.set_property("emit-signals", False)
    for element in (src, decode, convert, resample, caps, sink):
        pipeline.add(element)
    src.link(decode)
    convert.link(resample)
    resample.link(caps)
    caps.link(sink)

    def on_pad_added(_decode: Gst.Element, pad: Gst.Pad) -> None:
        current = pad.get_current_caps() or pad.query_caps(None)
        if current.get_size() and current.get_structure(0).get_name().startswith("audio/"):
            sinkpad = convert.get_static_pad("sink")
            if not sinkpad.is_linked():
                pad.link(sinkpad)

    handler = decode.connect("pad-added", on_pad_added)
    chunks: list[np.ndarray] = []
    bus = pipeline.get_bus()
    deadline = time.monotonic() + timeout
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise AudioError(_bus_error(bus) or _("Cannot decode {path}.").format(path=path))
        while True:
            sample = sink.try_pull_sample(50 * Gst.MSECOND)
            if sample is not None:
                chunks.append(_sample_to_array(sample))
                continue
            message = bus.pop_filtered(Gst.MessageType.ERROR)
            if message is not None:
                raise AudioError(message.parse_error()[0].message)
            if sink.is_eos():
                break
            if time.monotonic() > deadline:
                raise AudioError(_("Decoding {path} timed out.").format(path=path))
    finally:
        decode.disconnect(handler)
        pipeline.set_state(Gst.State.NULL)
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def _sample_to_array(sample: Gst.Sample) -> np.ndarray:
    buf = sample.get_buffer()
    size = buf.get_size()
    size -= size % 4
    if size <= 0:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(buf.extract_dup(0, size), dtype=np.float32).copy()


def _bin_children(gst_bin: Gst.Bin) -> list[Gst.Element]:
    """Direct children of a bin (Gst.Iterator is not iterable in every PyGObject)."""
    children: list[Gst.Element] = []
    iterator = gst_bin.iterate_elements()
    while True:
        result, element = iterator.next()
        if result == Gst.IteratorResult.OK:
            children.append(element)
        elif result == Gst.IteratorResult.RESYNC:
            children.clear()
            iterator.resync()
        else:
            return children


def _bus_error(bus: Gst.Bus) -> str:
    message = bus.pop_filtered(Gst.MessageType.ERROR)
    if message is None:
        return ""
    err, debug = message.parse_error()
    log.debug("GStreamer error: %s (%s)", err.message, debug)
    return err.message


# --------------------------------------------------------------------------- #
# Recorder
# --------------------------------------------------------------------------- #


class Recorder:
    """One microphone take at a time, as 16 kHz mono float32.

    ``on_chunk(samples)`` and ``on_level(level)`` run on a GStreamer streaming
    thread and must return quickly; ``on_error(message)`` runs on the GLib main
    thread when the pipeline reports an error (device unplugged, server
    restarted…). A Recorder can be started again after ``stop()``/``cancel()``.
    """

    def __init__(
        self,
        device_id: str = "",
        on_chunk: Callable[[np.ndarray], None] | None = None,
        on_level: Callable[[float], None] | None = None,
        *,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.device_id = device_id
        self._on_chunk = on_chunk
        self._on_level = on_level
        self._on_error = on_error
        self.source_description = ""  # what was actually opened (logs, diagnostics)
        self.error = ""  # last error reported by the pipeline

        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._samples = 0
        self._collecting = False
        self._eos = threading.Event()

        self._pipeline: Gst.Pipeline | None = None
        self._bus: Gst.Bus | None = None
        self._handlers: list[tuple[Any, int]] = []
        self._watching = False

        # Level meter state, touched only by the streaming thread.
        self._level_sumsq = 0.0
        self._level_count = 0
        self._level_due = 0.0

    # -- public API ---------------------------------------------------------- #

    @property
    def active(self) -> bool:
        """True between a successful start() and stop()/cancel()."""
        return self._pipeline is not None

    @property
    def duration(self) -> float:
        """Seconds of audio captured in the current (or last) take."""
        with self._lock:
            return self._samples / SAMPLE_RATE

    def start(self) -> None:
        """Open the microphone. Raises AudioError on failure (main thread)."""
        if self._pipeline is not None:
            log.warning("Recorder.start() called while already recording")
            return
        ensure_gst()
        with self._lock:
            self._chunks = []
            self._samples = 0
            self._collecting = True
        self._eos.clear()
        self.error = ""
        self._level_sumsq, self._level_count, self._level_due = 0.0, 0, 0.0

        pipeline, sink, auto_source = self._build()
        self._pipeline = pipeline
        self._bus = pipeline.get_bus()
        self._connect(sink, "new-sample", self._on_new_sample)
        self._connect(sink, "eos", self._on_sink_eos)
        try:
            self._set_state(Gst.State.READY)
            if auto_source is not None:
                self._check_auto_source(auto_source)
            self._set_state(Gst.State.PLAYING)
        except AudioError:
            self._teardown()
            raise
        self._bus.add_signal_watch()
        self._watching = True
        self._connect(self._bus, "message::error", self._on_bus_error)
        self._connect(self._bus, "message::warning", self._on_bus_warning)
        log.info("recording from %s", self.source_description)

    def stop(self) -> np.ndarray:
        """Close the microphone and return the whole take (float32, 16 kHz mono).

        The pipeline is drained with an EOS (at most EOS_TIMEOUT) so that the
        audio still in flight is not lost.
        """
        pipeline = self._pipeline
        if pipeline is not None:
            if not self._eos.is_set():
                pipeline.send_event(Gst.Event.new_eos())
                if not self._eos.wait(EOS_TIMEOUT):
                    log.debug("no EOS after %.0f ms, closing anyway", EOS_TIMEOUT * 1000)
            self._teardown()
        return self._take()

    def cancel(self) -> None:
        """Close the microphone and discard the take."""
        self._teardown()
        self._take()

    # -- pipeline construction ----------------------------------------------- #

    def _build(self) -> tuple[Gst.Pipeline, Gst.Element, Gst.Element | None]:
        """Return (pipeline, appsink, autoaudiosrc or None)."""
        auto_source = None
        description = os.environ.get(ENV_SOURCE, "").strip()
        if description:
            # parse_launch (rather than parse_bin_from_description) keeps the
            # delayed linking of elements with "sometimes" pads such as
            # decodebin working: in a parsed bin, decodebin is linked to a
            # target-less ghost pad and fails with "missing plug-in".
            try:
                parsed = Gst.parse_launch(f"{description} ! audioconvert name=greffier-convert")
            except GLib.Error as exc:
                raise AudioError(
                    _("Invalid audio source “{source}”: {error}").format(
                        source=description, error=exc.message)
                ) from exc
            if isinstance(parsed, Gst.Pipeline):
                pipeline = parsed
            else:  # pragma: no cover - parse_launch returns a pipeline for "a ! b"
                pipeline = Gst.Pipeline.new("greffier-recorder")
                pipeline.add(parsed)
            convert = pipeline.get_by_name("greffier-convert")
            self.source_description = description
        else:
            pipeline = Gst.Pipeline.new("greffier-recorder")
            source = self._device_source()
            if source is None:
                source = auto_source = _make("autoaudiosrc", "greffier-source")
                self.source_description = "autoaudiosrc"
            convert = _make("audioconvert", "greffier-convert")
            pipeline.add(source)
            pipeline.add(convert)
            if not source.link(convert):
                raise AudioError(_("Cannot connect to the audio source."))

        resample = _make("audioresample")
        capsfilter = _make("capsfilter")
        capsfilter.set_property("caps", Gst.Caps.from_string(CAPS))
        sink = _make("appsink", "greffier-sink")
        sink.set_property("emit-signals", True)
        sink.set_property("sync", False)
        sink.set_property("drop", False)
        sink.set_property("max-buffers", 0)
        for element in (resample, capsfilter, sink):
            pipeline.add(element)
        if not (convert.link(resample) and resample.link(capsfilter) and capsfilter.link(sink)):
            raise AudioError(_("Cannot build the recording pipeline."))
        return pipeline, sink, auto_source

    def _device_source(self) -> Gst.Element | None:
        """Element for the configured device, or None to use the default source."""
        if not self.device_id:
            return None
        for info, device in _probe_with_timeout(PROBE_TIMEOUT):
            if info.id == self.device_id:
                element = device.create_element("greffier-source")
                if element is not None:
                    self.source_description = f"{info.name} ({info.id})"
                    return element
        log.warning("audio device %r not found, using the default source", self.device_id)
        return None

    def _set_state(self, state: Gst.State) -> None:
        assert self._pipeline is not None and self._bus is not None
        ret = self._pipeline.set_state(state)
        if ret == Gst.StateChangeReturn.ASYNC:
            ret, _current, _pending = self._pipeline.get_state(int(START_TIMEOUT * Gst.SECOND))
            if ret == Gst.StateChangeReturn.ASYNC:
                raise AudioError(_("The microphone did not start in time."))
        if ret == Gst.StateChangeReturn.FAILURE:
            detail = _bus_error(self._bus)
            raise AudioError(detail or _("The microphone cannot be opened."))

    def _check_auto_source(self, auto_source: Gst.Element) -> None:
        children = [child.get_factory().get_name() for child in _bin_children(auto_source)
                    if child.get_factory() is not None]
        if children and all(name in _FAKE_SOURCES for name in children):
            raise AudioError(
                _("No microphone was found. Check that a microphone is connected and that "
                  "PipeWire or PulseAudio is running.")
            )
        if children:
            self.source_description = f"autoaudiosrc ({', '.join(children)})"

    # -- teardown ------------------------------------------------------------ #

    def _connect(self, obj: Any, signal: str, handler: Callable[..., Any]) -> None:
        self._handlers.append((obj, obj.connect(signal, handler)))

    def _teardown(self) -> None:
        pipeline, self._pipeline = self._pipeline, None
        with self._lock:
            self._collecting = False
        if self._watching and self._bus is not None:
            self._bus.remove_signal_watch()
            self._watching = False
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
        # Disconnecting breaks the GObject → closure → self reference cycles.
        for obj, handler_id in self._handlers:
            with contextlib.suppress(TypeError, ValueError):  # already gone
                obj.disconnect(handler_id)
        self._handlers.clear()
        if self._bus is not None:
            self._bus.set_flushing(True)  # drop queued messages (they hold references)
            self._bus = None

    def _take(self) -> np.ndarray:
        with self._lock:
            chunks, self._chunks = self._chunks, []
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)

    # -- GStreamer callbacks -------------------------------------------------- #

    def _on_new_sample(self, sink: GstApp.AppSink) -> Gst.FlowReturn:
        """Streaming thread: store the buffer, feed the callbacks."""
        sample = sink.pull_sample()
        if sample is None:
            return Gst.FlowReturn.OK
        chunk = _sample_to_array(sample)
        if chunk.size == 0:
            return Gst.FlowReturn.OK
        with self._lock:
            if not self._collecting:
                return Gst.FlowReturn.OK
            self._chunks.append(chunk)
            self._samples += chunk.size
        if self._on_chunk is not None:
            try:
                self._on_chunk(chunk)
            except Exception:
                log.exception("on_chunk callback failed")
        if self._on_level is not None:
            self._update_level(chunk)
        return Gst.FlowReturn.OK

    def _update_level(self, chunk: np.ndarray) -> None:
        self._level_sumsq += float(np.dot(chunk, chunk))
        self._level_count += chunk.size
        now = time.monotonic()
        if now < self._level_due:
            return
        # Deadline schedule: averages LEVEL_INTERVAL whatever the buffer size;
        # after a stall (or at the first buffer) the schedule restarts from now.
        late = now - self._level_due
        self._level_due = (self._level_due if late < LEVEL_INTERVAL else now) + LEVEL_INTERVAL
        rms = math.sqrt(self._level_sumsq / self._level_count)
        self._level_sumsq, self._level_count = 0.0, 0
        try:
            self._on_level(rms_to_level(rms))  # type: ignore[misc]
        except Exception:
            log.exception("on_level callback failed")

    def _on_sink_eos(self, _sink: GstApp.AppSink) -> None:
        self._eos.set()

    def _on_bus_error(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        """Main thread (bus signal watch)."""
        err, debug = message.parse_error()
        log.error("audio pipeline error: %s (%s)", err.message, debug)
        self.error = err.message
        self._eos.set()  # nothing more will flow; stop() must not wait
        if self._on_error is not None:
            try:
                self._on_error(err.message)
            except Exception:
                log.exception("on_error callback failed")

    def _on_bus_warning(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        warning, debug = message.parse_warning()
        log.warning("audio pipeline warning: %s (%s)", warning.message, debug)
