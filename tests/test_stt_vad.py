"""VoiceActivity with the real Silero model (640 KB, fast)."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np
import pytest

from greffier.audio import decode_file
from greffier.const import SAMPLE_RATE
from greffier.stt import EngineError, VoiceActivity
from greffier.stt.vad import TAIL_PADDING

SR = SAMPLE_RATE


@pytest.fixture(scope="module")
def vad_model() -> Path:
    path = Path(os.environ.get("GREFFIER_TEST_DL", "/tmp/claude-0/dl")) / "silero_vad.onnx"
    if not path.is_file():
        pytest.skip("Silero VAD model not available")
    return path


@pytest.fixture(scope="module")
def speech() -> np.ndarray:
    path = (Path(os.environ.get("GREFFIER_TEST_DL", "/tmp/claude-0/dl"))
            / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8" / "test_wavs" / "fr.wav")
    if not path.is_file():
        pytest.skip("French sample not available")
    return decode_file(path)  # ~5 s of continuous French speech


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def feed(vad: VoiceActivity, audio: np.ndarray, chunk: int) -> list:
    segments = []
    for i in range(0, len(audio), chunk):
        segments += vad.accept_segments(audio[i:i + chunk])
    return segments + vad.flush_segments()


def test_missing_model(tmp_path: Path) -> None:
    with pytest.raises(EngineError):
        VoiceActivity(tmp_path / "nope.onnx")


def test_silence_has_no_speech(vad_model: Path) -> None:
    vad = VoiceActivity(vad_model)
    assert vad.accept(silence(2.0)) == []
    assert vad.flush() == []
    assert not vad.speech_started
    assert vad.trailing_silence == pytest.approx(2.0)


def test_noise_has_no_speech(vad_model: Path) -> None:
    noise = (np.random.default_rng(0).standard_normal(3 * SR) * 0.02).astype(np.float32)
    vad = VoiceActivity(vad_model)
    assert vad.accept(noise) + vad.flush() == []


def test_segments_do_not_depend_on_chunking(vad_model: Path, speech: np.ndarray) -> None:
    audio = np.concatenate([silence(1), speech, silence(2), speech, silence(1)])
    reference = None
    for chunk in (len(audio), 512, 777, 160, 4096):
        vad = VoiceActivity(vad_model)
        spans = [(s.start, s.end) for s in feed(vad, audio, chunk)]
        if reference is None:
            reference = spans
        assert spans == reference, chunk
    assert reference is not None and len(reference) == 2
    (s1, e1), (s2, e2) = reference
    assert 0.6 * SR < s1 < 1.4 * SR  # starts a little before the speech (lead padding)
    assert e1 - s1 == pytest.approx(len(speech), rel=0.1)
    assert 7.5 * SR < s2 < 8.4 * SR


def test_speech_flags_and_trailing_silence(vad_model: Path, speech: np.ndarray) -> None:
    vad = VoiceActivity(vad_model, min_silence=0.5)
    vad.accept(silence(0.5))
    assert not vad.speech_started
    vad.accept(speech[: 2 * SR])
    assert vad.speech_started and vad.speaking
    assert vad.trailing_silence == 0.0
    finished = vad.accept(speech[2 * SR:]) + vad.accept(silence(1.0))
    assert len(finished) == 1
    assert not vad.speaking
    first = vad.trailing_silence
    assert 0.5 <= first <= 1.2
    vad.accept(silence(1.0))
    assert vad.trailing_silence == pytest.approx(first + 1.0, abs=0.05)


def test_flush_returns_segment_in_progress(vad_model: Path, speech: np.ndarray) -> None:
    vad = VoiceActivity(vad_model)
    assert vad.accept(np.concatenate([silence(0.5), speech[:-100]])) == []
    tail = vad.flush()
    assert len(tail) == 1 and len(tail[0]) > 3 * SR


def test_accept_pads_segment_tails(vad_model: Path, speech: np.ndarray) -> None:
    audio = np.concatenate([silence(0.5), speech, silence(1.0)])
    raw = VoiceActivity(vad_model).accept_segments(audio)
    padded = VoiceActivity(vad_model).accept(audio)
    assert len(raw) == len(padded) == 1
    assert len(padded[0]) - len(raw[0].samples) == int(TAIL_PADDING * SR)
    np.testing.assert_array_equal(padded[0][: len(raw[0].samples)], raw[0].samples)


def test_reset(vad_model: Path, speech: np.ndarray) -> None:
    vad = VoiceActivity(vad_model)
    first = feed(vad, np.concatenate([silence(1), speech]), SR)
    vad.reset()
    assert not vad.speech_started and vad.trailing_silence == 0.0
    second = feed(vad, np.concatenate([silence(1), speech]), SR)
    assert [(s.start, s.end) for s in first] == [(s.start, s.end) for s in second]


def test_usable_from_a_worker_thread(vad_model: Path, speech: np.ndarray) -> None:
    results: list = []

    def work() -> None:
        vad = VoiceActivity(vad_model)
        results.extend(vad.accept(np.concatenate([silence(0.5), speech, silence(1)])))

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(30)
    assert len(results) == 1
