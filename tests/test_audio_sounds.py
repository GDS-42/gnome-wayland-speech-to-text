"""Feedback sounds: shipped files and the non-blocking player."""

from __future__ import annotations

import importlib.util
import time
import wave
from pathlib import Path

import pytest

from greffier import paths
from greffier.audio import SoundPlayer
from greffier.audio.sounds import SOUNDS

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", SOUNDS)
def test_sound_files(name: str) -> None:
    path = paths.package_data(f"sounds/{name}.wav")
    assert path.stat().st_size < 40_000
    with wave.open(str(path)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (44100, 1, 2)
        assert wav.getnframes() / wav.getframerate() <= 0.2
        frames = wav.readframes(wav.getnframes())
    first, last = (int.from_bytes(frames[i:i + 2], "little", signed=True) for i in (0, -2))
    assert first == 0 and last == 0  # no click at either end


def test_shipped_sounds_match_generator(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("make_sounds", ROOT / "tools" / "make_sounds.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--out", str(tmp_path)]) == 0
    for name in SOUNDS:
        assert (tmp_path / f"{name}.wav").read_bytes() == \
            paths.package_data(f"sounds/{name}.wav").read_bytes(), name


def test_play_is_non_blocking_and_never_raises() -> None:
    player = SoundPlayer()
    started = time.monotonic()
    for name in (*SOUNDS, "unknown"):
        player.play(name)
    assert time.monotonic() - started < 1.0
    player.close()
    player.close()  # idempotent


def test_missing_sound_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(paths, "package_data", lambda name: tmp_path / name)
    SoundPlayer().play("start")  # logged, not raised
