"""Streaming voice activity detection: Silero VAD through sherpa-onnx.

``VoiceActivity`` accepts audio in chunks of any length (the detector itself
must be fed one 512-sample window at a time; remainders are buffered) and
returns the speech segments that are finished. It drives auto-stop on silence
(``speech_started`` / ``trailing_silence``), live insertion (``accept()``), and
the splitting of long recordings (``accept_segments()``, with offsets).

An instance is not bound to a thread: it can be fed from a GStreamer streaming
thread or a worker thread; calls are serialised with an internal lock and the
properties can be read from any thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from greffier.const import SAMPLE_RATE
from greffier.i18n import _
from greffier.stt.engine import EngineError, as_samples, import_sherpa

sherpa_onnx = import_sherpa()

WINDOW = 512  # samples per Silero window at 16 kHz (32 ms)
TAIL_PADDING = 0.2  # seconds of audio appended after each segment by accept()/flush()
HISTORY = 2.0  # seconds of recent audio kept to pad segment tails


@dataclass(frozen=True)
class SpeechSegment:
    """A finished speech segment. ``start`` counts samples since creation/reset."""

    start: int
    samples: np.ndarray

    @property
    def end(self) -> int:
        return self.start + len(self.samples)

    @property
    def duration(self) -> float:
        return len(self.samples) / SAMPLE_RATE


class VoiceActivity:
    """Silero VAD over a 16 kHz mono float32 stream.

    ``min_silence``: seconds of silence that close a segment; ``max_speech``:
    soft limit after which the detector looks harder for a pause to cut at.
    """

    def __init__(self, model_path: Path, *, min_silence: float = 0.5, max_speech: float = 25.0,
                 threshold: float = 0.5, min_speech: float = 0.25) -> None:
        path = Path(model_path)
        if not path.is_file():
            raise EngineError(_("The voice detection model is missing: {path}").format(path=path))
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(path)
        config.silero_vad.threshold = threshold
        config.silero_vad.min_silence_duration = min_silence
        config.silero_vad.min_speech_duration = min_speech
        config.silero_vad.max_speech_duration = max_speech
        config.silero_vad.window_size = WINDOW
        config.sample_rate = SAMPLE_RATE
        config.num_threads = 1
        try:
            self._vad = sherpa_onnx.VoiceActivityDetector(
                config, buffer_size_in_seconds=max(60.0, 2 * max_speech + 10.0))
        except RuntimeError as exc:
            raise EngineError(
                _("Could not load the voice detection model: {error}").format(error=exc)
            ) from exc
        self.model_path = path
        self.min_silence = min_silence
        self.max_speech = max_speech
        self._lock = threading.Lock()
        self._clear()

    # -- state ------------------------------------------------------------------ #

    def _clear(self) -> None:
        self._pending = np.zeros(0, dtype=np.float32)  # < WINDOW samples not yet fed
        self._received = 0  # samples accepted since creation/reset
        self._history = np.zeros(0, dtype=np.float32)  # recent audio, for tail padding
        self._history_start = 0  # absolute offset of _history[0]
        self._speaking = False
        self._speech_started = False
        self._last_end: int | None = None  # end of the last finished segment

    @property
    def speech_started(self) -> bool:
        """True once speech has been detected since creation/reset."""
        return self._speech_started

    @property
    def speaking(self) -> bool:
        """True while the detector is inside a speech segment."""
        return self._speaking

    @property
    def trailing_silence(self) -> float:
        """Seconds since the end of the last speech (0 while speaking).

        Before any speech, the whole duration received so far. The detector
        confirms the end of speech only after ``min_silence``; the value then
        jumps from 0 to about ``min_silence`` and grows from there.
        """
        if self._speaking:
            return 0.0
        return max(0, self._received - (self._last_end or 0)) / SAMPLE_RATE

    def reset(self) -> None:
        """Forget everything; offsets restart at 0."""
        with self._lock:
            self._vad.reset()
            self._clear()

    # -- feeding -------------------------------------------------------------- #

    def accept(self, samples: np.ndarray) -> list[np.ndarray]:
        """Feed audio; return the speech segments finished by it (tail-padded)."""
        return [padded for _segment, padded in self._process(samples, flush=False)]

    def flush(self) -> list[np.ndarray]:
        """End of stream: return the segment in progress, if any (tail-padded)."""
        return [padded for _segment, padded in self._process(None, flush=True)]

    def accept_segments(self, samples: np.ndarray) -> list[SpeechSegment]:
        """Like accept(), but unpadded segments with their sample offsets."""
        return [segment for segment, _padded in self._process(samples, flush=False)]

    def flush_segments(self) -> list[SpeechSegment]:
        """Like flush(), but unpadded segments with their sample offsets."""
        return [segment for segment, _padded in self._process(None, flush=True)]

    def _process(self, samples: np.ndarray | None,
                 flush: bool) -> list[tuple[SpeechSegment, np.ndarray]]:
        with self._lock:
            out: list[tuple[SpeechSegment, np.ndarray]] = []
            audio = as_samples(samples) if samples is not None else np.zeros(0, np.float32)
            if audio.size:
                self._received += audio.size
                self._history = np.concatenate([self._history, audio])
                buf = np.concatenate([self._pending, audio]) if self._pending.size else audio
            else:
                buf = self._pending
            full = len(buf) - len(buf) % WINDOW
            for offset in range(0, full, WINDOW):
                self._vad.accept_waveform(buf[offset:offset + WINDOW])
                self._speaking = bool(self._vad.is_speech_detected())
                self._speech_started = self._speech_started or self._speaking
                out.extend(self._drain())
            self._pending = buf[full:].copy()
            if flush:
                if self._pending.size:  # complete the last window with silence
                    last = np.zeros(WINDOW, dtype=np.float32)
                    last[: self._pending.size] = self._pending
                    self._vad.accept_waveform(last)
                    self._pending = np.zeros(0, dtype=np.float32)
                self._vad.flush()
                out.extend(self._drain())
                self._speaking = False
            self._trim_history()
            return out

    def _drain(self) -> list[tuple[SpeechSegment, np.ndarray]]:
        out = []
        while not self._vad.empty():
            front = self._vad.front
            segment = SpeechSegment(int(front.start), np.asarray(front.samples, dtype=np.float32))
            self._vad.pop()
            if segment.samples.size == 0:
                continue
            self._last_end = segment.end
            out.append((segment, self._pad_tail(segment)))
        return out

    def _pad_tail(self, segment: SpeechSegment) -> np.ndarray:
        """Append up to TAIL_PADDING s of the audio that followed the segment."""
        lo = segment.end - self._history_start
        hi = min(lo + int(TAIL_PADDING * SAMPLE_RATE), len(self._history))
        if lo < 0 or hi <= lo:
            return segment.samples
        return np.concatenate([segment.samples, self._history[lo:hi]])

    def _trim_history(self) -> None:
        keep = int(HISTORY * SAMPLE_RATE)
        extra = len(self._history) - keep
        if extra > 0:
            self._history = self._history[extra:].copy()
            self._history_start += extra
