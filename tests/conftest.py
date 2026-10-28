"""Shared pytest fixtures.

Every test runs with GREFFIER_HOME pointing to a fresh temporary directory, so
nothing touches the real ~/.config or ~/.local/share.

Slow tests use real model archives extracted under GREFFIER_TEST_DL (default
/tmp/claude-0/dl, the maintainer's scratch area); they are skipped when the
directory is missing, e.g. in CI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

TEST_DL = Path(os.environ.get("GREFFIER_TEST_DL", "/tmp/claude-0/dl"))


@pytest.fixture(autouse=True)
def greffier_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "greffier-home"
    monkeypatch.setenv("GREFFIER_HOME", str(home))
    monkeypatch.delenv("GREFFIER_MODELS_DIR", raising=False)
    monkeypatch.setenv("GREFFIER_LANG", "en")
    return home


def real_model_dir(root: str) -> Path:
    """Directory of an extracted sherpa-onnx archive, or skip the test."""
    path = TEST_DL / root
    if not path.is_dir():
        pytest.skip(f"real model not available: {path}")
    return path


@pytest.fixture
def french_wav() -> Path:
    path = TEST_DL / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8" / "test_wavs" / "fr.wav"
    if not path.is_file():
        pytest.skip("French sample not available")
    return path
