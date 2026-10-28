"""Speech-recognition engines: the common interface and the loader.

An engine wraps one sherpa-onnx ``OfflineRecognizer`` for one installed model.
``load_engine()`` picks the adapter of the model's *family* (see
:mod:`greffier.stt.families`), loads the files, and warms the model up so the
first real dictation does not pay for lazy initialisation.

Engines are used from a single worker thread (see docs/ARCHITECTURE.md §2);
they nevertheless serialise calls with a lock.
"""

from __future__ import annotations

import logging
import os
import sys
from types import ModuleType
from typing import Any, Protocol, runtime_checkable

import numpy as np

from greffier.i18n import _
from greffier.models.types import InstalledModel, ModelSpec

log = logging.getLogger(__name__)

MAX_AUTO_THREADS = 8


class EngineError(RuntimeError):
    """The model cannot be loaded or the transcription failed (message is translated)."""


@runtime_checkable
class Engine(Protocol):
    model_id: str

    def transcribe(self, samples: np.ndarray, language: str | None = None) -> str:
        """Text of ``samples`` (float32 mono 16 kHz); ``language`` is ISO 639-1 or None."""
        ...

    def close(self) -> None:
        """Free the model. Further transcribe() calls raise EngineError."""
        ...


def as_samples(samples: Any) -> np.ndarray:
    """Contiguous 1-D float32 array from mono (or interleaved/2-D) audio."""
    audio = np.asarray(samples)
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / 32768.0
    audio = audio.astype(np.float32, copy=False)
    if audio.ndim == 2:
        audio = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
    elif audio.ndim != 1:
        audio = audio.reshape(-1)
    if audio.size and not np.isfinite(audio).all():
        audio = np.nan_to_num(audio, nan=0.0, posinf=1.0, neginf=-1.0)
    return np.ascontiguousarray(audio, dtype=np.float32)


def import_sherpa() -> ModuleType:
    """Import ``sherpa_onnx`` with RTLD_DEEPBIND so its C++ exceptions stay catchable.

    libgstreamer links libunwind, and ``Gst.init()`` puts it in the global
    symbol scope. If sherpa-onnx is loaded after that, its ``_Unwind_Resume``
    binds to libunwind while libstdc++ (loaded earlier, e.g. by numpy) throws
    with libgcc_s: the first C++ exception - a corrupted model file for
    instance - then aborts the whole daemon instead of raising RuntimeError.
    DEEPBIND makes sherpa-onnx and onnxruntime resolve symbols from their own
    dependencies (libgcc_s) first, whatever was imported before.
    """
    module = sys.modules.get("sherpa_onnx")
    if module is not None:
        return module
    flags = sys.getdlopenflags()
    deepbind = getattr(os, "RTLD_DEEPBIND", 0)  # glibc only
    if deepbind:
        sys.setdlopenflags(flags | deepbind)
    try:
        import sherpa_onnx
    finally:
        sys.setdlopenflags(flags)
    return sherpa_onnx


def _cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))  # honours taskset/cgroup CPU pinning
    except (AttributeError, OSError):  # pragma: no cover - non-Linux
        return os.cpu_count() or 1


def resolve_threads(threads: int = 0) -> int:
    """Number of inference threads; ``0`` means automatic.

    Automatic = half of the usable logical CPUs, between 1 and 8. Most desktop
    CPUs have two hardware threads per core and ONNX Runtime gains little from
    SMT siblings; leaving the other half free keeps the desktop responsive while
    a dictation is transcribed, and beyond ~8 threads these models stop scaling.
    """
    if threads > 0:
        return threads
    return max(1, min(MAX_AUTO_THREADS, _cpu_count() // 2))


def normalise_language(language: str | None) -> str | None:
    """ISO 639-1/639-3 code in lower case, or None for automatic detection."""
    if not language:
        return None
    code = language.strip().lower().replace("_", "-").split("-")[0]
    if code in ("", "auto", "detect", "*"):
        return None
    return code


def default_language(spec: ModelSpec) -> str:
    """Language used when a model requires one and none (or an unsupported one) is given."""
    if spec.supports("fr"):
        return "fr"
    for code in spec.languages:
        if code != "*":
            return code
    return "en"


def resolve_language(spec: ModelSpec, language: str | None,
                     supported: frozenset[str] | None = None) -> str | None:
    """Language to pass to the model, according to ``spec.language_mode``.

    * ``auto``: the model detects the language itself → None;
    * ``required``: the requested language if supported, else :func:`default_language`;
    * ``optional``: None/"auto" → None (detection), a supported code → that code,
      an unsupported code → None (detection is better than a wrong language).

    ``supported`` overrides ``spec.supports()`` (e.g. Whisper's real language list).
    """
    if spec.language_mode == "auto":
        return None
    code = normalise_language(language)

    def ok(value: str) -> bool:
        return value in supported if supported is not None else spec.supports(value)

    if spec.language_mode == "required":
        if code and ok(code):
            return code
        fallback = default_language(spec)
        if code:
            log.warning("%s does not support language %r, using %r", spec.id, code, fallback)
        return fallback
    if code and ok(code):
        return code
    if code:
        log.warning("%s does not support language %r, detecting it", spec.id, code)
    return None


def load_engine(model: InstalledModel, *, threads: int = 0, provider: str = "cpu",
                warm_up: bool = True) -> Engine:
    """Load ``model`` with the adapter of its family. Raises EngineError.

    ``warm_up`` runs ~0.5 s of silence through the model once (result discarded)
    so that the first real dictation is not slowed down by lazy initialisation.
    """
    from greffier.stt.families import ADAPTERS  # local import: families imports this module

    adapter = ADAPTERS.get(model.spec.family)
    if adapter is None:
        raise EngineError(
            _("The model “{name}” uses an unsupported engine family “{family}”. "
              "Update Greffier to use it.").format(name=model.spec.name, family=model.spec.family)
        )
    engine = adapter(model, threads=resolve_threads(threads), provider=provider)
    if warm_up:
        try:
            engine.warm_up()
        except BaseException:
            engine.close()
            raise
    return engine
