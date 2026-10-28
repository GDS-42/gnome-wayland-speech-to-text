"""Real models through the public API (slow; skipped when the archives are absent).

Run with ``python -m pytest tests/test_stt_models.py -m slow -s`` to see the
load times and real-time factors (RTF = processing time / audio duration).
Cohere Transcribe needs ~3.5 GB of RAM: it only runs with GREFFIER_TEST_HEAVY=1.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator

import numpy as np
import pytest

from greffier import paths
from greffier.audio import decode_file
from greffier.const import SAMPLE_RATE
from greffier.models.types import InstalledModel, ModelSpec
from greffier.stt import EngineError, Transcriber, load_engine
from greffier.stt.families import SherpaEngine
from tests.conftest import TEST_DL, real_model_dir

pytestmark = pytest.mark.slow

ENTRIES = {e["id"]: e for e in json.loads(paths.package_data("catalog.json").read_text())["models"]}
PARAKEET_FR = "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/test_wavs/fr.wav"
QWEN_FR = "sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25/test_wavs/fr1.wav"


def installed(model_id: str) -> InstalledModel:
    entry = ENTRIES[model_id]
    return InstalledModel(spec=ModelSpec.from_dict(entry),
                          path=real_model_dir(entry["download"]["root"]))


def wav(relative: str) -> np.ndarray:
    path = TEST_DL / relative
    if not path.is_file():
        pytest.skip(f"sample not available: {path}")
    return decode_file(path)


def engine_fixture(model_id: str, threads: int = 0) -> Iterator[SherpaEngine]:
    model = installed(model_id)
    engine = load_engine(model, threads=threads)
    assert isinstance(engine, SherpaEngine)
    print(f"\n[bench] {model_id}: load {engine.load_time:.2f} s + warm-up "
          f"{engine.warm_up_time:.2f} s, {engine.threads} threads")
    yield engine
    engine.close()


def timed(engine: SherpaEngine, audio: np.ndarray, language: str | None) -> str:
    started = time.monotonic()
    text = engine.transcribe(audio, language)
    elapsed = time.monotonic() - started
    duration = len(audio) / SAMPLE_RATE
    print(f"[bench] {engine.model_id} [{language}]: {duration:.2f} s in {elapsed:.2f} s, "
          f"RTF {elapsed / duration:.3f}: {text!r}")
    return text


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


class TestParakeet:
    @pytest.fixture(scope="class")
    @classmethod
    def engine(cls) -> Iterator[SherpaEngine]:
        yield from engine_fixture("parakeet-tdt-0.6b-v3-int8")

    def test_french(self, engine: SherpaEngine) -> None:
        text = timed(engine, wav(PARAKEET_FR), "fr")
        assert "votre pays" in text.lower()
        assert text[0].isupper() and text.endswith(".")  # punctuation and capitals

    def test_language_is_ignored(self, engine: SherpaEngine) -> None:
        assert "votre pays" in engine.transcribe(wav(PARAKEET_FR), "en").lower()

    def test_transcriber(self, engine: SherpaEngine) -> None:
        vad = TEST_DL / "silero_vad.onnx"
        transcriber = Transcriber(engine, vad if vad.is_file() else None,
                                  engine.spec.max_segment_s)
        speech = wav(PARAKEET_FR)
        audio = np.concatenate([silence(1.5), speech, silence(1.0), speech, silence(1.0)])
        text = transcriber.transcribe(audio, "fr")
        assert text.lower().count("votre pays") == 2
        assert transcriber.transcribe(silence(3), "fr") == ""

    def test_closed_engine(self) -> None:
        engine = load_engine(installed("parakeet-tdt-0.6b-v3-int8"), warm_up=False)
        engine.close()
        with pytest.raises(EngineError):
            engine.transcribe(silence(1), None)


class TestCanary:
    @pytest.fixture(scope="class")
    @classmethod
    def engine(cls) -> Iterator[SherpaEngine]:
        yield from engine_fixture("canary-180m-flash-int8")

    def test_french(self, engine: SherpaEngine) -> None:
        assert "votre pays" in timed(engine, wav(PARAKEET_FR), "fr").lower()

    def test_language_switch(self, engine: SherpaEngine) -> None:
        speech = wav(PARAKEET_FR)
        english = engine.transcribe(speech, "en")  # forced English: no French words left
        assert "votre" not in english.lower()
        assert "votre pays" in engine.transcribe(speech, "fr").lower()  # and back
        assert "votre pays" in engine.transcribe(speech, None).lower()  # default: French


class TestWhisper:
    @pytest.fixture(scope="class")
    @classmethod
    def engine(cls) -> Iterator[SherpaEngine]:
        yield from engine_fixture("whisper-turbo-int8")

    def test_french_and_detection(self, engine: SherpaEngine) -> None:
        speech = wav(PARAKEET_FR)
        assert "votre pays" in timed(engine, speech, "fr").lower()
        assert "votre pays" in timed(engine, speech, None).lower()

    def test_unknown_language_does_not_abort(self, engine: SherpaEngine) -> None:
        # sherpa-onnx exits the process on an unknown Whisper language
        assert "votre pays" in engine.transcribe(wav(PARAKEET_FR), "xx").lower()

    def test_silence_hallucination_filtered(self, engine: SherpaEngine) -> None:
        assert engine.transcribe(silence(1.0), None) == ""  # Whisper says "you"


class TestQwen3:
    @pytest.fixture(scope="class")
    @classmethod
    def engine(cls) -> Iterator[SherpaEngine]:
        yield from engine_fixture("qwen3-asr-0.6b-int8")

    def test_french_hint_and_detection(self, engine: SherpaEngine) -> None:
        speech = wav(QWEN_FR)
        assert "Paris" in timed(engine, speech, "fr")
        assert "Paris" in timed(engine, speech, None)

    def test_long_segment_is_not_truncated(self, engine: SherpaEngine) -> None:
        # 30 s: needs a KV cache longer than sherpa's default max_total_len=512
        text = engine.transcribe(np.tile(wav(PARAKEET_FR), 6), "fr")
        assert text.lower().count("votre pays") >= 5


@pytest.mark.skipif(os.environ.get("GREFFIER_TEST_HEAVY") != "1",
                    reason="needs ~3.5 GB of RAM; set GREFFIER_TEST_HEAVY=1")
class TestCohere:
    @pytest.fixture(scope="class")
    @classmethod
    def engine(cls) -> Iterator[SherpaEngine]:
        yield from engine_fixture("cohere-transcribe-14-lang-int8")

    def test_french(self, engine: SherpaEngine) -> None:
        speech = wav(PARAKEET_FR)
        assert "votre pays" in timed(engine, speech, "fr").lower()
        assert "votre pays" in engine.transcribe(speech, None).lower()  # default: French
