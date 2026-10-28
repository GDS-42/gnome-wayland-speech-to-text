"""One adapter per sherpa-onnx model family.

Each adapter knows the roles of its files (``ModelSpec.files``), how to build
the ``OfflineRecognizer`` and how to hand the language to the model:

* ``nemo_transducer`` (Parakeet) — language detected by the model (mode auto);
* ``nemo_canary`` — ``src_lang``/``tgt_lang`` (required), switched at run time
  with ``set_config()``;
* ``whisper`` — ``language`` (optional, "" = detection) switched with
  ``set_config()``, plus a filter for Whisper's well-known hallucinations;
* ``cohere_transcribe`` — per stream ``set_option("language", "fr")`` (required);
* ``qwen3_asr`` — per stream ``set_option("language", "French")`` (optional).
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from typing import ClassVar

import numpy as np

from greffier.const import SAMPLE_RATE
from greffier.i18n import _
from greffier.models.types import InstalledModel
from greffier.stt.engine import (
    EngineError,
    as_samples,
    default_language,
    import_sherpa,
    resolve_language,
)

sherpa_onnx = import_sherpa()

log = logging.getLogger(__name__)

WARM_UP_SECONDS = 0.5


# --------------------------------------------------------------------------- #
# Hallucination filters
# --------------------------------------------------------------------------- #


def normalise_phrase(text: str) -> str:
    """Case-, accent-form- and punctuation-insensitive form used for comparisons."""
    text = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def _phrases(*items: str) -> frozenset[str]:
    return frozenset(normalise_phrase(item) for item in items)


# Whisper was trained on subtitled videos: on silence or noise it "transcribes"
# the credits and outros it saw most. Only outputs made entirely of one of
# these are dropped; real dictations ("Merci.", "Thank you.") are kept.
WHISPER_CREDITS = _phrases(
    "Sous-titres réalisés par la communauté d'Amara.org",
    "Sous-titres réalisés para la communauté d'Amara.org",
    "Sous-titres fait par la communauté d'Amara.org",
    "Sous-titres par la communauté d'Amara.org",
    "Sous-titrage ST' 501",
    "Sous-titrage Société Radio-Canada",
    "Sous-titrage FR 2021",
    "Subtitles by the Amara.org community",
    "Untertitel der Amara.org-Community",
    "Untertitel im Auftrag des ZDF, 2021",
    "Subtítulos realizados por la comunidad de Amara.org",
    "Sottotitoli creati dalla comunità Amara.org",
    "Ondertiteld door de Amara.org gemeenschap",
)
WHISPER_HALLUCINATIONS = WHISPER_CREDITS | _phrases(
    "Merci d'avoir regardé cette vidéo !",
    "Merci d'avoir regardé !",
    "Merci d'avoir regardé la vidéo.",
    "Abonnez-vous à la chaîne !",
    "N'oubliez pas de vous abonner à la chaîne !",
    "Thank you for watching!",
    "Thanks for watching!",
    "Thank you so much for watching!",
    "Please subscribe to my channel.",
    "Don't forget to like and subscribe!",
    "Vielen Dank fürs Zuschauen!",
    "Gracias por ver el video.",
    "you",
)
# Qwen3-ASR occasionally leaks a chat-template role name on near-silence.
QWEN3_ARTEFACTS = _phrases("system", "user", "assistant")

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def strip_hallucinations(text: str, phrases: frozenset[str],
                         trailing: frozenset[str] = frozenset()) -> str:
    """"" if the whole ``text`` is a known phrase; also drop trailing ``trailing`` sentences."""
    if normalise_phrase(text) in phrases:
        return ""
    if trailing:
        sentences = _SENTENCE_END.split(text.strip())
        while sentences and normalise_phrase(sentences[-1]) in trailing:
            sentences.pop()
        text = " ".join(sentences)
    return text


# --------------------------------------------------------------------------- #
# Languages
# --------------------------------------------------------------------------- #

# Languages of the multilingual Whisper tokenizer. An unknown code makes
# sherpa-onnx terminate the whole process, so codes are always checked
# against this list first. "yue" is left out: only large-v3 models know it.
WHISPER_LANGUAGES = frozenset({
    "en", "zh", "de", "es", "ru", "ko", "fr", "ja", "pt", "tr", "pl", "ca", "nl", "ar", "sv",
    "it", "id", "hi", "fi", "vi", "he", "uk", "el", "ms", "cs", "ro", "da", "hu", "ta", "no",
    "th", "ur", "hr", "bg", "lt", "la", "mi", "ml", "cy", "sk", "te", "fa", "lv", "bn", "sr",
    "az", "sl", "kn", "et", "mk", "br", "eu", "is", "hy", "ne", "mn", "bs", "kk", "sq", "sw",
    "gl", "mr", "pa", "si", "km", "sn", "yo", "so", "af", "oc", "ka", "be", "tg", "sd", "gu",
    "am", "yi", "lo", "uz", "fo", "ht", "ps", "tk", "nn", "mt", "sa", "lb", "my", "bo", "tl",
    "mg", "as", "tt", "haw", "ln", "ha", "ba", "jw", "su"
})

# Qwen3-ASR takes English language names as hints.
QWEN3_LANGUAGES = {
    "ar": "Arabic", "cs": "Czech", "da": "Danish", "de": "German", "el": "Greek",
    "en": "English", "es": "Spanish", "fa": "Persian", "fi": "Finnish", "fil": "Filipino",
    "fr": "French", "hi": "Hindi", "hu": "Hungarian", "id": "Indonesian", "it": "Italian",
    "ja": "Japanese", "ko": "Korean", "mk": "Macedonian", "ms": "Malay", "nl": "Dutch",
    "pl": "Polish", "pt": "Portuguese", "ro": "Romanian", "ru": "Russian", "sv": "Swedish",
    "th": "Thai", "tr": "Turkish", "vi": "Vietnamese", "yue": "Cantonese", "zh": "Chinese",
}


# --------------------------------------------------------------------------- #
# Adapters
# --------------------------------------------------------------------------- #


class SherpaEngine:
    """Common part of the adapters: file checks, locking, decoding, clean-up."""

    family: ClassVar[str] = ""
    roles: ClassVar[tuple[str, ...]] = ()
    hallucinations: ClassVar[frozenset[str]] = frozenset()
    trailing_hallucinations: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, model: InstalledModel, *, threads: int, provider: str = "cpu") -> None:
        self.model = model
        self.spec = model.spec
        self.model_id = model.id
        self.threads = threads
        self.provider = provider
        self.load_time = 0.0  # seconds spent building the recognizer
        self.warm_up_time = 0.0
        self._lock = threading.Lock()
        files = self._check_files()
        started = time.monotonic()
        try:
            self._recognizer: sherpa_onnx.OfflineRecognizer | None = self._create(files)
        except (RuntimeError, AssertionError, ValueError, TypeError, OSError) as exc:
            raise EngineError(
                _("Could not load the model “{name}”: {error}").format(
                    name=self.spec.name, error=exc)
            ) from exc
        self.load_time = time.monotonic() - started
        log.info("%s loaded in %.2f s (%d threads, %s)", self.model_id, self.load_time,
                 threads, provider)

    # -- to override ---------------------------------------------------------- #

    def _create(self, files: dict[str, str]) -> sherpa_onnx.OfflineRecognizer:
        raise NotImplementedError

    def _language(self, language: str | None) -> str | None:
        """Language to use for this request (None = let the model detect it)."""
        return resolve_language(self.spec, language)

    def _configure(self, recognizer: sherpa_onnx.OfflineRecognizer,
                   language: str | None) -> dict[str, str]:
        """Prepare ``recognizer`` for ``language``; return per-stream options."""
        return {}

    # -- public API ------------------------------------------------------------ #

    def transcribe(self, samples: np.ndarray, language: str | None = None) -> str:
        audio = as_samples(samples)
        if audio.size == 0:
            return ""
        return self._clean(self._decode(audio, language))

    def warm_up(self) -> None:
        """Decode a short silence once; the result is discarded."""
        started = time.monotonic()
        self._decode(np.zeros(int(WARM_UP_SECONDS * SAMPLE_RATE), dtype=np.float32), None)
        self.warm_up_time = time.monotonic() - started
        log.debug("%s warmed up in %.2f s", self.model_id, self.warm_up_time)

    def close(self) -> None:
        with self._lock:
            self._recognizer = None  # sherpa-onnx frees the model with the object

    # -- internals -------------------------------------------------------------- #

    def _check_files(self) -> dict[str, str]:
        files: dict[str, str] = {}
        missing: list[str] = []
        for role in self.roles:
            try:
                path = self.model.file(role)
            except KeyError:
                raise EngineError(
                    _("The model “{name}” is misconfigured: its catalog entry has no "
                      "“{role}” file.").format(name=self.spec.name, role=role)
                ) from None
            if not path.exists():
                missing.append(path.name)
            files[role] = str(path)
        if missing:
            raise EngineError(
                _("The model “{name}” is incomplete ({files} missing). Reinstall it from "
                  "the model list.").format(name=self.spec.name, files=", ".join(missing))
            )
        return files

    def _decode(self, audio: np.ndarray, language: str | None) -> str:
        with self._lock:
            recognizer = self._recognizer
            if recognizer is None:
                raise EngineError(_("The speech recognition model is not loaded."))
            lang = self._language(language)
            try:
                options = self._configure(recognizer, lang)
                stream = recognizer.create_stream()
                for key, value in options.items():
                    stream.set_option(key, value)
                stream.accept_waveform(SAMPLE_RATE, audio)
                recognizer.decode_stream(stream)
                return stream.result.text or ""
            except (RuntimeError, ValueError, TypeError) as exc:
                raise EngineError(
                    _("Speech recognition failed: {error}").format(error=exc)
                ) from exc

    def _clean(self, text: str) -> str:
        text = " ".join(text.split())
        if self.hallucinations or self.trailing_hallucinations:
            text = strip_hallucinations(text, self.hallucinations, self.trailing_hallucinations)
        return text.strip()


class NemoTransducerEngine(SherpaEngine):
    """NVIDIA Parakeet TDT: language detected by the model."""

    family = "nemo_transducer"
    roles = ("encoder", "decoder", "joiner", "tokens")

    def _create(self, files: dict[str, str]) -> sherpa_onnx.OfflineRecognizer:
        return sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=files["encoder"], decoder=files["decoder"], joiner=files["joiner"],
            tokens=files["tokens"], num_threads=self.threads, provider=self.provider,
            model_type="nemo_transducer",
        )


class NemoCanaryEngine(SherpaEngine):
    """NVIDIA Canary: source language required (transcription, not translation)."""

    family = "nemo_canary"
    roles = ("encoder", "decoder", "tokens")

    def _create(self, files: dict[str, str]) -> sherpa_onnx.OfflineRecognizer:
        self._current = default_language(self.spec)
        recognizer = sherpa_onnx.OfflineRecognizer.from_nemo_canary(
            encoder=files["encoder"], decoder=files["decoder"], tokens=files["tokens"],
            src_lang=self._current, tgt_lang=self._current, num_threads=self.threads,
            provider=self.provider,
        )
        use_pnc = bool(self.spec.options.get("use_pnc", True))
        if recognizer.config.model_config.canary.use_pnc != use_pnc:
            recognizer.config.model_config.canary.use_pnc = use_pnc
            recognizer.recognizer.set_config(recognizer.config)
        return recognizer

    def _configure(self, recognizer: sherpa_onnx.OfflineRecognizer,
                   language: str | None) -> dict[str, str]:
        lang = language or default_language(self.spec)
        if lang != self._current:
            config = recognizer.config
            config.model_config.canary.src_lang = lang
            config.model_config.canary.tgt_lang = lang
            recognizer.recognizer.set_config(config)
            self._current = lang
        return {}


class WhisperEngine(SherpaEngine):
    """OpenAI Whisper: optional language ("" = detection), hallucination filter."""

    family = "whisper"
    roles = ("encoder", "decoder", "tokens")
    hallucinations = WHISPER_HALLUCINATIONS
    trailing_hallucinations = WHISPER_CREDITS

    def _create(self, files: dict[str, str]) -> sherpa_onnx.OfflineRecognizer:
        if "*" in self.spec.languages:
            self._supported = WHISPER_LANGUAGES
        else:
            self._supported = WHISPER_LANGUAGES & frozenset(self.spec.languages)
        self._current = ""
        return sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=files["encoder"], decoder=files["decoder"], tokens=files["tokens"],
            language="", task="transcribe", num_threads=self.threads, provider=self.provider,
            tail_paddings=int(self.spec.options.get("tail_paddings", -1)),
        )

    def _language(self, language: str | None) -> str | None:
        return resolve_language(self.spec, language, self._supported)

    def _configure(self, recognizer: sherpa_onnx.OfflineRecognizer,
                   language: str | None) -> dict[str, str]:
        wanted = language or ""
        if wanted and wanted not in self._supported:  # defensive: would kill the process
            wanted = ""
        if wanted != self._current:
            config = recognizer.config
            config.model_config.whisper.language = wanted
            recognizer.recognizer.set_config(config)
            self._current = wanted
        return {}


class CohereTranscribeEngine(SherpaEngine):
    """Cohere Transcribe: language required, set per stream."""

    family = "cohere_transcribe"
    roles = ("encoder", "decoder", "tokens")

    def _create(self, files: dict[str, str]) -> sherpa_onnx.OfflineRecognizer:
        options = self.spec.options
        return sherpa_onnx.OfflineRecognizer.from_cohere_transcribe(
            encoder=files["encoder"], decoder=files["decoder"], tokens=files["tokens"],
            num_threads=self.threads, provider=self.provider,
            language=default_language(self.spec),
            use_punct=bool(options.get("use_punct", True)),
            use_itn=bool(options.get("use_itn", True)),
        )

    def _configure(self, recognizer: sherpa_onnx.OfflineRecognizer,
                   language: str | None) -> dict[str, str]:
        return {"language": language or default_language(self.spec)}


class Qwen3AsrEngine(SherpaEngine):
    """Qwen3-ASR: optional language hint given as an English name."""

    family = "qwen3_asr"
    roles = ("conv_frontend", "encoder", "decoder", "tokenizer")
    hallucinations = QWEN3_ARTEFACTS

    def _create(self, files: dict[str, str]) -> sherpa_onnx.OfflineRecognizer:
        options = self.spec.options
        known = frozenset(QWEN3_LANGUAGES)
        self._supported = known if "*" in self.spec.languages else (
            known & frozenset(self.spec.languages))
        return sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            conv_frontend=files["conv_frontend"], encoder=files["encoder"],
            decoder=files["decoder"], tokenizer=files["tokenizer"],
            num_threads=self.threads, provider=self.provider,
            max_new_tokens=int(options.get("max_new_tokens", 256)),
            max_total_len=int(options.get("max_total_len", 1024)),
        )

    def _language(self, language: str | None) -> str | None:
        return resolve_language(self.spec, language, self._supported)

    def _configure(self, recognizer: sherpa_onnx.OfflineRecognizer,
                   language: str | None) -> dict[str, str]:
        return {"language": QWEN3_LANGUAGES[language]} if language else {}


ADAPTERS: dict[str, type[SherpaEngine]] = {
    cls.family: cls
    for cls in (NemoTransducerEngine, NemoCanaryEngine, WhisperEngine,
                CohereTranscribeEngine, Qwen3AsrEngine)
}
