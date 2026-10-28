"""Whole-recording transcription: silence gate, VAD splitting, engine calls.

``Transcriber.transcribe()`` is what the daemon's worker thread calls with a
finished take (or a live segment):

1. audio shorter than ``MIN_AUDIO`` → "" (a key press, not a dictation);
2. with the VAD: no speech at all → "" (models such as Whisper invent text
   on silence); otherwise the speech segments are merged into chunks of at
   most ``max_segment_s`` (pauses between merged segments are kept, ~0.2 s of
   padding around each chunk) and transcribed one by one;
3. without the VAD: digital silence → ""; audio up to ``max_segment_s`` is
   transcribed whole, longer audio is cut at the quietest point before each
   limit.

``max_segment_s <= 0`` means "no limit".
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np

from greffier.const import SAMPLE_RATE
from greffier.stt.engine import Engine, EngineError, as_samples
from greffier.stt.vad import VoiceActivity

log = logging.getLogger(__name__)

MIN_AUDIO = 0.3  # seconds
PADDING = 0.2  # seconds of context kept around each chunk
SILENCE_PEAK = 1e-4  # about -80 dBFS: below this, the input is muted, not quiet
CUT_FRAME = 0.03  # seconds; resolution of the low-energy cut search
CUT_SEARCH = 5.0  # seconds before the limit searched for a quiet cut point
VAD_MIN_SILENCE = 0.3  # shorter than for auto-stop: more places to split long takes
VAD_MAX_SPEECH = 25.0


def split_on_low_energy(audio: np.ndarray, limit: int) -> list[np.ndarray]:
    """Cut ``audio`` into pieces of at most ``limit`` samples at quiet points.

    Each cut is placed at the centre of the quietest ``CUT_FRAME`` within the
    last ``CUT_SEARCH`` seconds (at most half the limit) before the limit.
    """
    if limit <= 0 or len(audio) <= limit:
        return [audio]
    frame = max(1, int(CUT_FRAME * SAMPLE_RATE))
    search = min(int(CUT_SEARCH * SAMPLE_RATE), limit // 2)
    pieces: list[np.ndarray] = []
    pos = 0
    while len(audio) - pos > limit:
        lo, hi = pos + limit - search, pos + limit
        window = audio[lo:hi]
        count = len(window) // frame
        if count:
            energy = np.square(window[: count * frame].reshape(count, frame)).mean(axis=1)
            cut = lo + int(np.argmin(energy)) * frame + frame // 2
        else:  # pragma: no cover - limit shorter than a frame
            cut = hi
        cut = min(max(cut, pos + 1), hi)
        pieces.append(audio[pos:cut])
        pos = cut
    pieces.append(audio[pos:])
    return pieces


def merge_spans(spans: list[tuple[int, int]], limit: int, padding: int,
                total: int) -> list[tuple[int, int]]:
    """Group consecutive speech spans into padded chunks of at most ``limit`` samples.

    ``spans`` are sorted ``(start, end)`` sample offsets; ``limit <= 0`` means
    unlimited. Padding never makes two chunks overlap (they meet halfway).
    """
    groups: list[list[int]] = []
    for start, end in spans:
        if groups and (limit <= 0 or (end + padding) - (groups[-1][0] - padding) <= limit):
            groups[-1][1] = max(groups[-1][1], end)
        else:
            groups.append([start, end])
    chunks: list[tuple[int, int]] = []
    for i, (start, end) in enumerate(groups):
        lo = max(0, start - padding)
        hi = min(total, end + padding)
        if i > 0:
            lo = max(lo, (groups[i - 1][1] + start) // 2)
        if i + 1 < len(groups):
            hi = min(hi, (end + groups[i + 1][0]) // 2)
        chunks.append((lo, hi))
    return chunks


class Transcriber:
    """Transcribe recordings of any length with an engine and the optional VAD."""

    def __init__(self, engine: Engine, vad_model: Path | None, max_segment_s: float) -> None:
        self.engine = engine
        self.max_segment_s = float(max_segment_s or 0.0)
        self._lock = threading.Lock()
        self._vad: VoiceActivity | None = None
        if vad_model is not None:
            max_speech = VAD_MAX_SPEECH
            if self.max_segment_s > 0:
                max_speech = max(1.0, min(VAD_MAX_SPEECH, self.max_segment_s - 2 * PADDING))
            try:
                self._vad = VoiceActivity(vad_model, min_silence=VAD_MIN_SILENCE,
                                          max_speech=max_speech)
            except EngineError as exc:
                log.warning("VAD unavailable, splitting on energy only: %s", exc)

    @property
    def has_vad(self) -> bool:
        return self._vad is not None

    def transcribe(self, samples: np.ndarray, language: str | None) -> str:
        """Text of a whole take (float32 mono 16 kHz), "" when nothing was said."""
        texts = []
        for chunk in self.split(samples):
            text = self.engine.transcribe(chunk, language).strip()
            if text:
                texts.append(text)
        return " ".join(texts)

    def split(self, samples: np.ndarray) -> list[np.ndarray]:
        """Chunks to transcribe ([] when there is nothing worth transcribing)."""
        audio = as_samples(samples)
        if len(audio) < MIN_AUDIO * SAMPLE_RATE:
            return []
        limit = int(self.max_segment_s * SAMPLE_RATE) if self.max_segment_s > 0 else 0
        if self._vad is None:
            if float(np.max(np.abs(audio))) < SILENCE_PEAK:
                return []
            return split_on_low_energy(audio, limit)
        spans = self._speech_spans(audio)
        if not spans:
            return []
        chunks: list[np.ndarray] = []
        for lo, hi in merge_spans(spans, limit, int(PADDING * SAMPLE_RATE), len(audio)):
            chunks.extend(split_on_low_energy(audio[lo:hi], limit))
        return chunks

    def _speech_spans(self, audio: np.ndarray) -> list[tuple[int, int]]:
        assert self._vad is not None
        with self._lock:
            self._vad.reset()
            segments = self._vad.accept_segments(audio) + self._vad.flush_segments()
            self._vad.reset()
        return [(s.start, min(s.end, len(audio))) for s in segments if s.end > s.start]
