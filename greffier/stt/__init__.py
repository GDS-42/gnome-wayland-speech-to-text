"""Speech recognition: sherpa-onnx engines, voice activity detection, transcription."""

from greffier.stt.engine import Engine, EngineError, load_engine, resolve_threads
from greffier.stt.transcriber import Transcriber
from greffier.stt.vad import SpeechSegment, VoiceActivity

__all__ = [
    "Engine",
    "EngineError",
    "SpeechSegment",
    "Transcriber",
    "VoiceActivity",
    "load_engine",
    "resolve_threads",
]
