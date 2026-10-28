#!/usr/bin/env python3
"""Generate Greffier's feedback sounds (greffier/data/sounds/*.wav).

Pure standard library (``wave`` + ``math``), deterministic output: running it
again produces byte-identical files, so the WAVs can be committed and
regenerated at will. Format: 44.1 kHz, 16-bit, mono, at most 200 ms each.

* ``start``  — soft rising two-note chime (E5 → A5): "listening".
* ``stop``   — the same chime falling (A5 → E5): "done".
* ``cancel`` — muted low double tap: "discarded".
* ``error``  — short, low and soft buzz: "something went wrong".

Usage::

    python tools/make_sounds.py [--out DIR]
"""

from __future__ import annotations

import argparse
import math
import sys
import wave
from array import array
from collections.abc import Callable, Iterable
from pathlib import Path

RATE = 44100
PEAK = 0.45  # default normalised peak (about -7 dBFS): audible but never harsh
EDGE_FADE = 0.012  # seconds of raised-cosine fade at the very end (no click)

E4, G3 = 329.63, 196.00
E5, A5 = 659.25, 880.00


def raised_cosine(x: float) -> float:
    """Smooth 0 → 1 ramp for x in [0, 1]."""
    x = min(1.0, max(0.0, x))
    return 0.5 - 0.5 * math.cos(math.pi * x)


def chime(freq: float, t: float, attack: float = 0.006, decay: float = 0.07) -> float:
    """A soft bell-like note: a few gentle partials, quick attack, exponential decay."""
    if t < 0:
        return 0.0
    env = raised_cosine(t / attack) * math.exp(-t / decay)
    partials = (
        (1.0, 1.0),
        (2.0, 0.22),  # octave, adds warmth
        (3.0, 0.06),  # a hint of brightness
    )
    return env * sum(amp * math.sin(2 * math.pi * freq * k * t) for k, amp in partials)


def tap(freq: float, t: float) -> float:
    """A muted knock: low sine with a very short decay."""
    if t < 0:
        return 0.0
    env = raised_cosine(t / 0.003) * math.exp(-t / 0.018)
    return env * (math.sin(2 * math.pi * freq * t) + 0.12 * math.sin(4 * math.pi * freq * t))


def buzz(t: float, length: float) -> float:
    """Low soft buzz: few odd harmonics, light tremolo, smooth attack/release."""
    if t < 0 or t > length:
        return 0.0
    env = raised_cosine(t / 0.015) * raised_cosine((length - t) / 0.06)
    tremolo = 1.0 - 0.25 * (0.5 + 0.5 * math.cos(2 * math.pi * 28.0 * t))
    tone = (math.sin(2 * math.pi * G3 * t)
            + 0.30 * math.sin(2 * math.pi * 3 * G3 * t)
            + 0.10 * math.sin(2 * math.pi * 5 * G3 * t))
    return env * tremolo * tone


# name -> (length in seconds, peak, generator). The sustained buzz gets a lower
# peak so that its loudness (RMS) matches the decaying chimes.
SOUNDS: dict[str, tuple[float, float, Callable[[float], float]]] = {
    "start": (0.180, PEAK, lambda t: chime(E5, t) + 0.9 * chime(A5, t - 0.070)),
    "stop": (0.180, PEAK, lambda t: chime(A5, t) + 0.9 * chime(E5, t - 0.070)),
    "cancel": (0.150, PEAK, lambda t: tap(E4, t) + 0.7 * tap(E4 * 0.94, t - 0.075)),
    "error": (0.190, 0.24, lambda t: buzz(t, 0.190)),
}


def render(length: float, peak_level: float, fn: Callable[[float], float]) -> list[float]:
    n = int(round(length * RATE))
    samples = [fn(i / RATE) for i in range(n)]
    fade = int(EDGE_FADE * RATE)
    for i in range(fade):  # guarantee a silent last sample
        samples[n - 1 - i] *= raised_cosine(i / fade)
    peak = max(abs(s) for s in samples) or 1.0
    return [s * peak_level / peak for s in samples]


def to_pcm16(samples: Iterable[float]) -> bytes:
    pcm = array("h", (max(-32767, min(32767, round(s * 32767))) for s in samples))
    if sys.byteorder == "big":
        pcm.byteswap()  # WAV is little-endian
    return pcm.tobytes()


def write_wav(path: Path, samples: list[float]) -> None:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(to_pcm16(samples))


def main(argv: list[str] | None = None) -> int:
    default_out = Path(__file__).resolve().parent.parent / "greffier" / "data" / "sounds"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=default_out, help="output directory")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, (length, peak, fn) in SOUNDS.items():
        path = args.out / f"{name}.wav"
        write_wav(path, render(length, peak, fn))
        print(f"{path} ({path.stat().st_size} bytes, {length * 1000:.0f} ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
