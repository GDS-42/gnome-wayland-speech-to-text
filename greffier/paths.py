"""XDG locations. ``GREFFIER_HOME`` relocates everything (tests, portable setups)."""

import os
from pathlib import Path


def _xdg(var: str, fallback: str, leaf: str) -> Path:
    override = os.environ.get("GREFFIER_HOME")
    if override:
        return Path(override) / leaf
    base = os.environ.get(var) or str(Path.home() / fallback)
    return Path(base) / "greffier"


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config", "config")


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share", "data")


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache", "cache")


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state", "state")


def config_file() -> Path:
    return config_dir() / "config.toml"


def models_dir() -> Path:
    """Installed models, one sub-directory per model id."""
    override = os.environ.get("GREFFIER_MODELS_DIR")
    return Path(override) if override else data_dir() / "models"


def downloads_dir() -> Path:
    """Partial downloads (resumable)."""
    return cache_dir() / "downloads"


def catalog_cache_file() -> Path:
    """Last remote catalog fetched successfully."""
    return cache_dir() / "catalog.json"


def history_file() -> Path:
    return state_dir() / "history.jsonl"


def package_data(name: str) -> Path:
    """File shipped inside the Python package (greffier/data)."""
    return Path(__file__).resolve().parent / "data" / name
