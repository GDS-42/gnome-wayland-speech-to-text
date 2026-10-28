"""Recorder and device listing, with GStreamer test sources instead of a microphone."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import gi
import numpy as np
import pytest

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

from greffier.audio import AudioDevice, AudioError, Recorder, capture, decode_file, list_devices
from greffier.audio.capture import ENV_SOURCE, ensure_gst, rms_to_level
from greffier.const import SAMPLE_RATE

LIVE_TONE = "audiotestsrc is-live=true wave=sine freq=440 volume=0.05"


def wait_for(predicate, timeout: float = 5.0) -> bool:
    """Run the GLib main context until ``predicate()`` is true."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.01)
    return False


def count(kind: str) -> int:
    return len(os.listdir(f"/proc/self/{kind}"))


@pytest.fixture
def tone_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SOURCE, LIVE_TONE)


# --------------------------------------------------------------------------- #
# Level meter
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("rms", "expected"),
    [(0.0, 0.0), (10 ** (-80 / 20), 0.0), (10 ** (-60 / 20), 0.0), (10 ** (-35 / 20), 0.5),
     (10 ** (-10 / 20), 1.0), (1.0, 1.0), (float("nan"), 0.0)],
)
def test_rms_to_level(rms: float, expected: float) -> None:
    assert rms_to_level(rms) == pytest.approx(expected, abs=1e-6)


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #


def test_record_live_source(tone_source: None) -> None:
    chunks: list[np.ndarray] = []
    levels: list[float] = []
    threads: set[int] = set()

    def on_chunk(samples: np.ndarray) -> None:
        threads.add(threading.get_ident())
        chunks.append(samples)

    recorder = Recorder(on_chunk=on_chunk, on_level=levels.append)
    assert not recorder.active
    recorder.start()
    assert recorder.active
    time.sleep(0.6)
    audio = recorder.stop()
    assert not recorder.active
    assert audio.dtype == np.float32 and audio.ndim == 1
    assert 0.4 * SAMPLE_RATE < len(audio) < 1.0 * SAMPLE_RATE
    assert recorder.duration == pytest.approx(len(audio) / SAMPLE_RATE)
    np.testing.assert_array_equal(np.concatenate(chunks), audio)  # nothing lost or added
    assert threading.get_ident() not in threads  # callbacks run on a streaming thread
    # 0.05 × sine → RMS ≈ -29 dBFS → level ≈ 0.62, about 15 times per second
    assert 4 <= len(levels) <= 15
    assert all(level == pytest.approx(0.62, abs=0.05) for level in levels)
    # sine at 440 Hz, really resampled to 16 kHz
    spectrum = np.abs(np.fft.rfft(audio[:8000]))
    assert np.argmax(spectrum) * SAMPLE_RATE / 8000 == pytest.approx(440, abs=4)


def test_file_source_with_decodebin(monkeypatch: pytest.MonkeyPatch, french_wav: Path) -> None:
    monkeypatch.setenv(ENV_SOURCE, f"filesrc location={french_wav} ! decodebin")
    recorder = Recorder()
    recorder.start()
    assert wait_for(lambda: recorder.duration > 4.9)
    audio = recorder.stop()  # the whole file, resampled from 22.05 kHz
    assert len(audio) == pytest.approx(4.969 * SAMPLE_RATE, rel=0.01)
    np.testing.assert_allclose(audio, decode_file(french_wav), atol=1e-6)


def test_stop_drains_the_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1600-sample buffers: a take of ~0.5 s must keep every buffer produced.
    monkeypatch.setenv(ENV_SOURCE, LIVE_TONE + " samplesperbuffer=1600 ! audio/x-raw,rate=16000")
    recorder = Recorder()
    recorder.start()
    started = time.monotonic()
    time.sleep(0.5)
    audio = recorder.stop()
    elapsed = time.monotonic() - started
    assert len(audio) % 1600 == 0
    assert len(audio) / SAMPLE_RATE == pytest.approx(elapsed, abs=0.15)


def test_cancel_discards(tone_source: None) -> None:
    recorder = Recorder()
    recorder.start()
    time.sleep(0.2)
    recorder.cancel()
    assert not recorder.active
    assert recorder.stop().size == 0  # nothing left, and stop() after cancel is harmless


def test_restart_same_recorder(tone_source: None) -> None:
    recorder = Recorder()
    for _ in range(2):
        recorder.start()
        time.sleep(0.15)
        assert 0 < len(recorder.stop()) < 0.6 * SAMPLE_RATE


def test_no_leak_after_many_cycles(tone_source: None) -> None:
    def cycle() -> None:
        recorder = Recorder(on_chunk=lambda _s: None, on_level=lambda _l: None)
        recorder.start()
        time.sleep(0.03)
        assert recorder.stop().size > 0

    cycle()
    cycle()
    threads, fds = count("task"), count("fd")
    for _ in range(20):
        cycle()
    assert count("task") <= threads + 2
    assert count("fd") <= fds + 2


def test_error_callback_on_main_thread(tone_source: None) -> None:
    calls: list[tuple[str, int]] = []
    recorder = Recorder(on_error=lambda message: calls.append((message,
                                                               threading.get_ident())))
    recorder.start()
    try:
        pipeline = recorder._pipeline
        assert pipeline is not None
        error = GLib.Error.new_literal(Gst.ResourceError.quark(), "device unplugged",
                                       int(Gst.ResourceError.READ))
        pipeline.post_message(Gst.Message.new_error(pipeline, error, "test"))
        assert wait_for(lambda: bool(calls))
    finally:
        started = time.monotonic()
        recorder.stop()
    assert calls == [("device unplugged", threading.get_ident())]
    assert recorder.error == "device unplugged"
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize(
    "description",
    ["nosuchelement", "filesrc location=/nonexistent/a.wav ! decodebin",
     "filesrc location=/etc/hostname ! decodebin"],
)
def test_broken_sources_raise(monkeypatch: pytest.MonkeyPatch, description: str) -> None:
    monkeypatch.setenv(ENV_SOURCE, description)
    recorder = Recorder()
    with pytest.raises(AudioError):
        recorder.start()
    assert not recorder.active


def test_default_source_fails_cleanly_without_audio_server(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """With a real microphone this records; without one it must raise, not record silence."""
    monkeypatch.delenv(ENV_SOURCE, raising=False)
    recorder = Recorder(device_id="no-such-device")  # falls back to the default source
    started = time.monotonic()
    try:
        recorder.start()
    except AudioError as exc:
        assert str(exc)
    else:
        recorder.cancel()
    assert time.monotonic() - started < 5


# --------------------------------------------------------------------------- #
# Devices
# --------------------------------------------------------------------------- #


def test_list_devices_is_quick() -> None:
    started = time.monotonic()
    devices = list_devices()
    assert time.monotonic() - started < 3
    assert all(isinstance(d, AudioDevice) and d.id and d.name for d in devices)
    assert len({d.id for d in devices}) == len(devices)


class FakeDevice:
    """Stands in for Gst.Device in capture._describe()."""

    def __init__(self, name: str, props: str, internal: str | None = None) -> None:
        ensure_gst()
        self._name = name
        self._props = Gst.Structure.from_string(f"props, {props}")[0] if props else None
        self._internal = internal

    def get_display_name(self) -> str:
        return self._name

    def get_properties(self):
        return self._props

    def get_property(self, name: str):
        if name == "internal-name" and self._internal is not None:
            return self._internal
        raise TypeError(name)


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        (FakeDevice("Micro", 'node.name=(string)"alsa_input.usb-mic", is-default=(boolean)true'),
         AudioDevice("alsa_input.usb-mic", "Micro", True)),
        (FakeDevice("Micro", "device.class=(string)sound", internal="alsa_input.pci"),
         AudioDevice("alsa_input.pci", "Micro", False)),
        (FakeDevice("Monitor", "device.class=(string)monitor", internal="x.monitor"), None),
        (FakeDevice("Monitor", 'node.name=(string)"alsa_output.pci.monitor"'), None),
        (FakeDevice("Sink", 'media.class=(string)"Audio/Sink", node.name=(string)out'), None),
        (FakeDevice("Only a name", ""), AudioDevice("Only a name", "Only a name", False)),
    ],
)
def test_describe_device(device: FakeDevice, expected: AudioDevice | None) -> None:
    assert capture._describe(device) == expected  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# File decoding
# --------------------------------------------------------------------------- #


def test_decode_file(french_wav: Path) -> None:
    audio = decode_file(french_wav)
    assert audio.dtype == np.float32
    assert len(audio) == pytest.approx(4.969 * SAMPLE_RATE, rel=0.005)
    assert 0.01 < float(np.sqrt(np.mean(audio ** 2))) < 0.5


def test_decode_missing_or_invalid(tmp_path: Path) -> None:
    with pytest.raises(AudioError):
        decode_file(tmp_path / "missing.wav")
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio at all" * 100)
    with pytest.raises(AudioError):
        decode_file(bad)
