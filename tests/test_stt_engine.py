"""Fast tests of greffier.stt.engine / families (no real model needed)."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

from greffier import paths
from greffier.models.types import InstalledModel, ModelSpec
from greffier.stt import engine as engine_mod
from greffier.stt.engine import (
    Engine,
    EngineError,
    as_samples,
    default_language,
    load_engine,
    normalise_language,
    resolve_language,
    resolve_threads,
)
from greffier.stt.families import (
    ADAPTERS,
    QWEN3_LANGUAGES,
    WHISPER_CREDITS,
    WHISPER_HALLUCINATIONS,
    WHISPER_LANGUAGES,
    normalise_phrase,
    strip_hallucinations,
)

ROOT = Path(__file__).resolve().parent.parent
CATALOG = json.loads(paths.package_data("catalog.json").read_text(encoding="utf-8"))
ENTRIES = {entry["id"]: entry for entry in CATALOG["models"]}


def spec(model_id: str, **changes) -> ModelSpec:
    entry = json.loads(json.dumps(ENTRIES[model_id]))
    entry.update(changes)
    return ModelSpec.from_dict(entry)


# --------------------------------------------------------------------------- #
# Threads and languages
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("cpus", "expected"), [(1, 1), (2, 1), (4, 2), (8, 4), (16, 8),
                                                 (64, 8)])
def test_resolve_threads_auto(monkeypatch: pytest.MonkeyPatch, cpus: int, expected: int) -> None:
    monkeypatch.setattr(engine_mod, "_cpu_count", lambda: cpus)
    assert resolve_threads(0) == expected


def test_resolve_threads_explicit() -> None:
    assert resolve_threads(3) == 3
    assert resolve_threads(12) == 12
    assert 1 <= resolve_threads() <= 8


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("auto", None), ("AUTO", None), ("fr", "fr"), ("FR", "fr"),
     ("fr_FR", "fr"), ("pt-BR", "pt"), ("yue", "yue"), (" de ", "de")],
)
def test_normalise_language(value: str | None, expected: str | None) -> None:
    assert normalise_language(value) == expected


@pytest.mark.parametrize(
    ("model_id", "language", "expected"),
    [
        # auto: the model detects the language, whatever is asked
        ("parakeet-tdt-0.6b-v3-int8", "fr", None),
        ("parakeet-tdt-0.6b-v3-int8", None, None),
        # required: supported → as is; missing/unsupported → French default
        ("canary-180m-flash-int8", "fr", "fr"),
        ("canary-180m-flash-int8", "en", "en"),
        ("canary-180m-flash-int8", None, "fr"),
        ("canary-180m-flash-int8", "auto", "fr"),
        ("canary-180m-flash-int8", "ja", "fr"),
        ("cohere-transcribe-14-lang-int8", "ko", "ko"),
        # optional: None/auto → detection; supported → forced; unsupported → detection
        ("qwen3-asr-0.6b-int8", None, None),
        ("qwen3-asr-0.6b-int8", "auto", None),
        ("qwen3-asr-0.6b-int8", "fr", "fr"),
        ("qwen3-asr-0.6b-int8", "br", None),
        ("whisper-turbo-int8", "fr_FR", "fr"),
    ],
)
def test_resolve_language(model_id: str, language: str | None, expected: str | None) -> None:
    assert resolve_language(spec(model_id), language) == expected


def test_resolve_language_with_explicit_support_list() -> None:
    whisper = spec("whisper-turbo-int8")
    assert resolve_language(whisper, "xx") == "xx"  # "*" claims everything…
    assert resolve_language(whisper, "xx", WHISPER_LANGUAGES) is None  # …the real list does not
    assert resolve_language(whisper, "fr", WHISPER_LANGUAGES) == "fr"


def test_default_language_without_french() -> None:
    assert default_language(spec("canary-180m-flash-int8")) == "fr"
    assert default_language(spec("canary-180m-flash-int8", languages=["de", "en"])) == "de"
    no_fr = spec("canary-180m-flash-int8", languages=["de", "en"])
    assert resolve_language(no_fr, None) == "de"


def test_language_tables() -> None:
    assert {"fr", "en", "de", "haw"} <= WHISPER_LANGUAGES
    assert "yue" not in WHISPER_LANGUAGES  # unknown to Whisper < large-v3: would abort
    assert set(QWEN3_LANGUAGES) == set(spec("qwen3-asr-0.6b-int8").languages)
    assert QWEN3_LANGUAGES["fr"] == "French"


# --------------------------------------------------------------------------- #
# Hallucinations and samples
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sous-titres réalisés par la communauté d'Amara.org", ""),
        ("sous-titres réalisés par la communauté d’Amara.org.", ""),
        ("Merci d'avoir regardé cette vidéo !", ""),
        ("Thanks for watching!", ""),
        ("you", ""),
        ("Merci.", "Merci."),  # a real dictation
        ("Thank you.", "Thank you."),
        ("Merci d'avoir regardé le dossier.", "Merci d'avoir regardé le dossier."),
        ("Bonjour à tous. Sous-titres réalisés par la communauté d'Amara.org",
         "Bonjour à tous."),
    ],
)
def test_whisper_hallucinations(text: str, expected: str) -> None:
    assert strip_hallucinations(text, WHISPER_HALLUCINATIONS, WHISPER_CREDITS) == expected


def test_normalise_phrase() -> None:
    assert normalise_phrase("  Merci  d’avoir REGARDÉ !") == "merci d avoir regardé"


def test_as_samples() -> None:
    assert as_samples(np.array([0, 16384, -32768], dtype=np.int16)).tolist() == [0, 0.5, -1]
    stereo = np.array([[0.2, 0.4], [0.6, 0.8]], dtype=np.float64)
    assert as_samples(stereo).tolist() == pytest.approx([0.3, 0.7])
    assert as_samples(np.array([[0.5], [0.25]])).tolist() == [0.5, 0.25]
    cleaned = as_samples(np.array([np.nan, np.inf, 0.1], dtype=np.float32))
    assert cleaned.dtype == np.float32 and np.isfinite(cleaned).all()
    assert as_samples([]).size == 0


# --------------------------------------------------------------------------- #
# Loader errors (no model is really loaded)
# --------------------------------------------------------------------------- #


def test_every_catalog_family_has_an_adapter_with_matching_roles() -> None:
    for entry in CATALOG["models"]:
        if entry["kind"] != "asr":
            continue
        adapter = ADAPTERS.get(entry["family"])
        assert adapter is not None, entry["family"]
        assert set(adapter.roles) <= set(entry["files"]), entry["id"]


def test_unknown_family(tmp_path: Path) -> None:
    model = InstalledModel(spec=spec("canary-180m-flash-int8", family="nope"), path=tmp_path)
    with pytest.raises(EngineError, match="nope"):
        load_engine(model)


def test_missing_files(tmp_path: Path) -> None:
    (tmp_path / "tokens.txt").write_text("a 0\n")
    model = InstalledModel(spec=spec("canary-180m-flash-int8"), path=tmp_path)
    with pytest.raises(EngineError, match="encoder.int8.onnx, decoder.int8.onnx"):
        load_engine(model)


def test_missing_role_in_spec(tmp_path: Path) -> None:
    broken = spec("canary-180m-flash-int8", files={"encoder": "e.onnx", "tokens": "t.txt"})
    with pytest.raises(EngineError, match="decoder"):
        load_engine(InstalledModel(spec=broken, path=tmp_path))


def test_corrupted_model(tmp_path: Path) -> None:
    for name in ("encoder.int8.onnx", "decoder.int8.onnx"):
        (tmp_path / name).write_bytes(b"not an onnx model" * 10)
    (tmp_path / "tokens.txt").write_text("<unk> 0\n")
    model = InstalledModel(spec=spec("canary-180m-flash-int8"), path=tmp_path)
    with pytest.raises(EngineError, match="Canary 180M Flash"):
        load_engine(model)


def test_corrupted_model_does_not_abort_after_gstreamer(tmp_path: Path) -> None:
    """The daemon's order: numpy, then Gst.init() (libunwind goes global), then sherpa-onnx.

    Without import_sherpa()'s RTLD_DEEPBIND, the C++ exception raised for the
    corrupted file aborts the process (SIGABRT). Run in a subprocess for that reason.
    """
    for name in ("encoder.int8.onnx", "decoder.int8.onnx"):
        (tmp_path / name).write_bytes(b"not an onnx model" * 10)
    (tmp_path / "tokens.txt").write_text("<unk> 0\n")
    script = textwrap.dedent("""
        import json, sys
        from pathlib import Path
        import numpy
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        Gst.init(None)
        from greffier import paths
        from greffier.models.types import InstalledModel, ModelSpec
        from greffier.stt import EngineError, load_engine
        entries = json.loads(paths.package_data("catalog.json").read_text())["models"]
        entry = next(e for e in entries if e["id"] == "canary-180m-flash-int8")
        model = InstalledModel(spec=ModelSpec.from_dict(entry), path=Path(sys.argv[1]))
        try:
            load_engine(model)
        except EngineError:
            print("EngineError")
    """)
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "EngineError"


def test_engine_protocol_is_structural() -> None:
    class Fake:
        model_id = "fake"

        def transcribe(self, samples: np.ndarray, language: str | None = None) -> str:
            return ""

        def close(self) -> None:
            pass

    assert isinstance(Fake(), Engine)
