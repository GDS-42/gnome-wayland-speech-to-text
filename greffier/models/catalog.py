"""The model catalog: built-in copy plus an optional, newer remote copy.

``greffier/data/catalog.json`` ships with the application. A copy of the same
file published on the main branch is fetched periodically
(:meth:`Catalog.fetch_remote`) and cached; :meth:`Catalog.load` returns
whichever of the two has the higher ``revision`` (``"YYYY-MM-DD.N"``, compared
naturally), so new models and fixed archives reach users without a release.

A catalog document looks like::

    {"schema": 1, "revision": "2026-10-28.1", "min_app_version": "0.1.0",
     "models": [{...ModelSpec...}, ...]}

A cached catalog with an unknown ``schema`` or requiring a newer application
is ignored as a whole; an individual invalid entry is skipped with a warning
so that one bad entry never hides the others.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from greffier import paths
from greffier.const import VERSION
from greffier.models import download as _http
from greffier.models.types import CatalogError, ModelSpec

log = logging.getLogger(__name__)

SCHEMA = 1
MAX_CATALOG_BYTES = 2 * 1024 * 1024
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_NATURAL_RE = re.compile(r"[0-9]+|[^0-9]+")


def revision_key(revision: str) -> tuple[tuple[int, int | str], ...]:
    """Sort key comparing digit runs as integers ("2026-10-28.10" > "2026-10-28.9").

    Also used for version strings ("0.10.0" > "0.9.1").
    """
    return tuple((0, int(part)) if part[0].isascii() and part[0].isdigit() else (1, part)
                 for part in _NATURAL_RE.findall(revision or ""))


def valid_model_id(model_id: object) -> bool:
    """True for ids usable as a directory name (letters, digits, ``._+-``)."""
    return isinstance(model_id, str) and _ID_RE.fullmatch(model_id) is not None


def parse_entry(data: Any, *, https_only: bool = False) -> ModelSpec:
    """Build and validate one catalog entry; raise :class:`CatalogError`.

    Stricter than :meth:`ModelSpec.validate`: ids must be plain file names and
    checksums lowercase hexadecimal, because both end up in paths on disk.
    With ``https_only`` the loopback ``http://`` URLs that ``ModelSpec``
    tolerates for test servers are refused too (catalogs used by the app).
    """
    if not isinstance(data, dict):
        raise CatalogError("model entry is not an object")
    spec = ModelSpec.from_dict(data)
    if https_only and not spec.download.url.startswith("https://"):
        raise CatalogError(f"model {spec.id!r}: download url must use https")
    if not valid_model_id(spec.id):
        raise CatalogError(f"model {spec.id!r}: id must be a plain name ([A-Za-z0-9._+-])")
    if not _SHA256_RE.fullmatch(spec.download.sha256):
        raise CatalogError(f"model {spec.id!r}: sha256 must be hexadecimal")
    if spec.download.size <= 0 or spec.disk_size < 0:
        raise CatalogError(f"model {spec.id!r}: invalid size")
    if not spec.revision:
        raise CatalogError(f"model {spec.id!r}: empty revision")
    if spec.download.format == "file" and "model" not in spec.files:
        raise CatalogError(f"model {spec.id!r}: single-file downloads need a 'model' file")
    return spec


class Catalog:
    """An immutable list of :class:`ModelSpec`, in catalog order."""

    def __init__(self, models: Iterable[ModelSpec] = (), revision: str = "", *,
                 source: str = "", min_app_version: str = "") -> None:
        self._models = list(models)
        self._by_id = {m.id: m for m in self._models}
        self.revision = revision
        self.source = source  # "builtin" or the file/URL it came from (logs)
        self.min_app_version = min_app_version

    def __repr__(self) -> str:
        return f"<Catalog {self.revision!r} from {self.source or '?'}: {len(self._models)} models>"

    def __len__(self) -> int:
        return len(self._models)

    def get(self, model_id: str) -> ModelSpec | None:
        """The entry with this id, or None."""
        return self._by_id.get(model_id)

    def models(self, kind: str | None = None) -> list[ModelSpec]:
        """Entries in catalog order, optionally only those of ``kind``."""
        return [m for m in self._models if kind is None or m.kind == kind]

    # -- parsing ----------------------------------------------------------- #

    @classmethod
    def from_dict(cls, data: Any, *, source: str = "", https_only: bool = False) -> Catalog:
        """Parse a catalog document.

        Raises :class:`CatalogError` when the document as a whole is unusable
        (wrong schema, newer application required, no valid model); invalid
        entries are skipped with a warning (see :func:`parse_entry`).
        """
        where = source or "catalog"
        if not isinstance(data, dict):
            raise CatalogError(f"{where}: not a JSON object")
        schema = data.get("schema")
        if schema != SCHEMA or isinstance(schema, bool):
            raise CatalogError(f"{where}: unsupported schema {schema!r}")
        revision = data.get("revision")
        if not isinstance(revision, str) or not revision:
            raise CatalogError(f"{where}: missing revision")
        min_version = data.get("min_app_version", "")
        if not isinstance(min_version, str):
            raise CatalogError(f"{where}: invalid min_app_version")
        if min_version and revision_key(min_version) > revision_key(VERSION):
            raise CatalogError(f"{where}: requires Greffier {min_version} (this is {VERSION})")
        entries = data.get("models")
        if not isinstance(entries, list):
            raise CatalogError(f"{where}: 'models' must be a list")

        models: list[ModelSpec] = []
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            try:
                spec = parse_entry(entry, https_only=https_only)
            except CatalogError as exc:
                log.warning("%s: skipping invalid entry #%d: %s", where, index, exc)
                continue
            if spec.id in seen:
                log.warning("%s: skipping duplicate entry %r", where, spec.id)
                continue
            seen.add(spec.id)
            models.append(spec)
        if not models:
            raise CatalogError(f"{where}: no valid model")
        return cls(models, revision, source=source, min_app_version=min_version)

    @classmethod
    def from_bytes(cls, raw: bytes, *, source: str = "", https_only: bool = False) -> Catalog:
        """Parse a JSON document; raise :class:`CatalogError`."""
        if len(raw) > MAX_CATALOG_BYTES:
            raise CatalogError(f"{source or 'catalog'}: larger than {MAX_CATALOG_BYTES} bytes")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CatalogError(f"{source or 'catalog'}: invalid JSON ({exc})") from exc
        return cls.from_dict(data, source=source, https_only=https_only)

    @classmethod
    def from_file(cls, path: Path, *, source: str = "", https_only: bool = False) -> Catalog:
        """Read and parse ``path``; raise :class:`CatalogError` or ``OSError``."""
        with open(path, "rb") as fh:
            raw = fh.read(MAX_CATALOG_BYTES + 1)
        return cls.from_bytes(raw, source=source or str(path), https_only=https_only)

    # -- loading ----------------------------------------------------------- #

    @classmethod
    def load(cls, cache_file: Path | None = None) -> Catalog:
        """Built-in catalog, or the cached remote one if it has a higher revision.

        Download URLs must use HTTPS. A missing or invalid cache is ignored
        (and logged). Raises
        :class:`CatalogError` only if neither catalog is usable.
        """
        builtin: Catalog | None = None
        try:
            builtin = cls.from_file(paths.package_data("catalog.json"), source="builtin",
                                    https_only=True)
        except (OSError, CatalogError) as exc:
            log.error("built-in model catalog is unusable: %s", exc)

        cached: Catalog | None = None
        # End-to-end tests serve archives from a loopback HTTP server; nothing
        # else can relax the HTTPS requirement.
        strict = os.environ.get("GREFFIER_TEST_ALLOW_LOOPBACK") != "1"
        if cache_file is not None:
            try:
                cached = cls.from_file(Path(cache_file), https_only=strict)
            except FileNotFoundError:
                pass
            except (OSError, CatalogError) as exc:
                log.warning("ignoring cached model catalog: %s", exc)

        if builtin is None and cached is None:
            raise CatalogError("no usable model catalog")
        if cached is not None and (
                builtin is None or revision_key(cached.revision) > revision_key(builtin.revision)):
            best = cached
        else:
            best = builtin
        assert best is not None
        log.debug("using model catalog %s", best)
        return best

    @classmethod
    def fetch_remote(cls, url: str, cache_file: Path, timeout: float = 15.0) -> Catalog:
        """Download the remote catalog, cache it and return :meth:`load`'s result.

        The document is validated before it replaces the cache, and the cache
        is written atomically, so a failed or bogus fetch never damages it.
        Raises :class:`CatalogError` (invalid document, refused URL) or
        ``OSError`` (network or disk error).
        """
        if not _http.is_allowed_url(url):
            raise CatalogError(f"refusing to fetch the catalog from {url!r}: HTTPS is required")
        try:
            with _http.open_url(url, headers={"Accept": "application/json"},
                                timeout=timeout) as response:
                raw = response.read(MAX_CATALOG_BYTES + 1)
        except http.client.HTTPException as exc:  # not an OSError
            raise OSError(f"cannot fetch {url}: {exc!r}") from exc
        Catalog.from_bytes(raw, source=url, https_only=True)  # before touching the cache
        _atomic_write(Path(cache_file), raw)
        log.info("model catalog refreshed from %s", url)
        return cls.load(cache_file)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
