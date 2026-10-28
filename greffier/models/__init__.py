"""Speech models: catalog, downloads, safe installation.

The public names below are imported lazily, so that code needing only the
data types (``greffier.models.types``) does not load the HTTP and archive
machinery.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from greffier.models.catalog import Catalog
    from greffier.models.manager import (
        InstallCancelled,
        ModelError,
        ModelManager,
        ModelStatus,
        ProgressFn,
    )
    from greffier.models.types import CatalogError, Download, InstalledModel, ModelSpec

__all__ = [
    "Catalog",
    "CatalogError",
    "Download",
    "InstallCancelled",
    "InstalledModel",
    "ModelError",
    "ModelManager",
    "ModelSpec",
    "ModelStatus",
    "ProgressFn",
]

_SOURCES = {
    "Catalog": "catalog",
    "CatalogError": "types",
    "Download": "types",
    "InstallCancelled": "manager",
    "InstalledModel": "types",
    "ModelError": "manager",
    "ModelManager": "manager",
    "ModelSpec": "types",
    "ModelStatus": "manager",
    "ProgressFn": "manager",
}


def __getattr__(name: str) -> Any:
    module = _SOURCES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
