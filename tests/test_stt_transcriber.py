"""Transcriber splitting logic, with a fake engine (fast)."""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from greffier.audio import decode_file
from greffier.const import SAMPLE_RATE
from greffier.stt import Transcriber
from greffier.stt.transcriber import PADDING, merge_spans, split_on_low_energy

SR = SAMPLE_RATE
DL = Path(os.environ.get("GREFFIER_TEST_DL", "/tmp/claude-0/dl"))


class FakeEngine:
    """Records the length of each chunk and answers with canned texts."""

    model_id = "fake"

    def __init__(self, answers: list[str] | None = None) -> None:
        self.calls: list[float] = []
        self.languages: list[str | None] = []
        self.answers = answers

    def transcribe(self, samples: np.ndarray, language: str | None = None) -> str:
        self.calls.append(len(samples) / SR)
        self.languages.append(language)
        if self.answers is not None:
            return self.answers[len(self.calls) - 1]
        return f"part{len(self.calls)}"

    def close(self) -> None:
        pass


@pytest.fixture(scope="module")
def vad_model() -> Path:
    path = DL / "silero_vad.onnx"
    if not path.is_file():
        pytest.skip("Silero VAD model not available")
    return path


@pytest.fixture(scope="module")
def speech() -> np.ndarray:
    path = DL / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8" / "test_wavs" / "fr.wav"
    if not path.is_file():
        pytest.skip("French sample not available")
    return decode_file(path)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), dtype=np.float32)


def tone(seconds: float, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def test_merge_spans_respects_limit_and_padding() -> None:
    pad = int(PADDING * SR)
    spans = [(1 * SR, 3 * SR), (4 * SR, 6 * SR), (7 * SR, 9 * SR), (15 * SR, 16 * SR)]
    chunks = merge_spans(spans, 6 * SR, pad, 20 * SR)
    assert chunks == [(1 * SR - pad, 6 * SR + pad), (7 * SR - pad, 9 * SR + pad),
                      (15 * SR - pad, 16 * SR + pad)]
    assert all(hi - lo <= 6 * SR for lo, hi in chunks)


def test_merge_spans_unlimited_and_clamped() -> None:
    pad = int(PADDING * SR)
    assert merge_spans([(100, 2 * SR), (5 * SR, 6 * SR)], 0, pad, 6 * SR) == [(0, 6 * SR)]


def test_merge_spans_never_overlap() -> None:
    pad = int(PADDING * SR)
    chunks = merge_spans([(0, 4 * SR), (4 * SR + 1000, 8 * SR)], 5 * SR, pad, 9 * SR)
    assert len(chunks) == 2
    assert chunks[0][1] == chunks[1][0]  # they meet in the middle of the short pause


def test_split_on_low_energy_cuts_in_the_pause() -> None:
    audio = np.concatenate([tone(6.5), silence(0.3), tone(6.0)])
    pieces = split_on_low_energy(audio, 8 * SR)
    assert len(pieces) == 2
    assert 6.5 * SR <= len(pieces[0]) <= 6.8 * SR
    assert sum(map(len, pieces)) == len(audio)


def test_split_on_low_energy_unlimited() -> None:
    audio = tone(40)
    assert len(split_on_low_energy(audio, 0)) == 1
    pieces = split_on_low_energy(audio, 10 * SR)
    assert all(len(p) <= 10 * SR for p in pieces)
    assert sum(map(len, pieces)) == len(audio)


# --------------------------------------------------------------------------- #
# Without VAD
# --------------------------------------------------------------------------- #


def test_too_short_is_empty() -> None:
    engine = FakeEngine()
    assert Transcriber(engine, None, 30).transcribe(tone(0.25), "fr") == ""
    assert engine.calls == []


def test_digital_silence_is_empty_without_vad() -> None:
    engine = FakeEngine()
    assert Transcriber(engine, None, 30).transcribe(silence(3), "fr") == ""
    assert engine.calls == []


def test_whole_audio_without_vad() -> None:
    engine = FakeEngine()
    assert Transcriber(engine, None, 30).transcribe(tone(5), "fr") == "part1"
    assert engine.calls == [pytest.approx(5.0)]
    assert engine.languages == ["fr"]


def test_long_audio_without_vad_is_split() -> None:
    engine = FakeEngine()
    audio = np.concatenate([tone(6.5), silence(0.3), tone(6.0)])
    assert Transcriber(engine, None, 8).transcribe(audio, None) == "part1 part2"
    assert all(c <= 8 for c in engine.calls)


def test_missing_vad_model_falls_back(tmp_path: Path) -> None:
    transcriber = Transcriber(FakeEngine(), tmp_path / "missing.onnx", 30)
    assert not transcriber.has_vad
    assert transcriber.transcribe(tone(2), "fr") == "part1"


# --------------------------------------------------------------------------- #
# With VAD
# --------------------------------------------------------------------------- #


def test_no_speech_means_no_engine_call(vad_model: Path) -> None:
    engine = FakeEngine()
    transcriber = Transcriber(engine, vad_model, 30)
    noise = (np.random.default_rng(1).standard_normal(3 * SR) * 0.01).astype(np.float32)
    assert transcriber.transcribe(silence(3), "fr") == ""
    assert transcriber.transcribe(noise, "fr") == ""
    assert engine.calls == []


def test_speech_is_trimmed_and_padded(vad_model: Path, speech: np.ndarray) -> None:
    engine = FakeEngine()
    audio = np.concatenate([silence(2), speech, silence(2)])
    assert Transcriber(engine, vad_model, 30).transcribe(audio, "fr") == "part1"
    assert len(engine.calls) == 1
    # Leading/trailing silence removed, ~0.2 s of context kept around the speech.
    assert len(speech) / SR < engine.calls[0] < len(speech) / SR + 1.0


def test_segments_are_merged_up_to_the_limit(vad_model: Path, speech: np.ndarray) -> None:
    audio = np.concatenate([silence(1), speech, silence(1), speech, silence(1), speech])
    merged = FakeEngine()
    Transcriber(merged, vad_model, 30).transcribe(audio, "fr")
    assert len(merged.calls) == 1  # 3 × 5 s + pauses fit in 30 s
    limited = FakeEngine()
    text = Transcriber(limited, vad_model, 8).transcribe(audio, "fr")
    assert len(limited.calls) == 3 and all(c <= 8 for c in limited.calls)
    assert text == "part1 part2 part3"


def test_unlimited_segment_length(vad_model: Path, speech: np.ndarray) -> None:
    engine = FakeEngine()
    audio = np.concatenate([speech, silence(3), speech, silence(3), speech])
    Transcriber(engine, vad_model, 0).transcribe(audio, None)
    assert len(engine.calls) == 1


def test_empty_answers_are_skipped(vad_model: Path, speech: np.ndarray) -> None:
    engine = FakeEngine(answers=["Bonjour.", "  ", "Au revoir."])
    audio = np.concatenate([speech, silence(1), speech, silence(1), speech])
    assert Transcriber(engine, vad_model, 8).transcribe(audio, "fr") == "Bonjour. Au revoir."


def test_very_long_continuous_speech_respects_limit(vad_model: Path, speech: np.ndarray) -> None:
    engine = FakeEngine()
    audio = np.tile(speech, 8)  # 40 s without real pauses
    Transcriber(engine, vad_model, 12).transcribe(audio, "fr")
    assert engine.calls and all(c <= 12 for c in engine.calls)
    assert sum(engine.calls) > 30
