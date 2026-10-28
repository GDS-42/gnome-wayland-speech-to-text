"""Installed models: status, install / update / repair, removal.

Layout on disk (``models_dir``, normally ``~/.local/share/greffier/models``)::

    <id>/                       an installed model
        greffier-model.json     manifest: the spec it was installed from
        ...                     the model files (spec.files)
    .staging-<id>-<random>/     an installation in progress
    .old-<id>-<random>/         the previous copy, during the final swap

Installing downloads the archive (resumable, SHA-256 checked, see
:mod:`~greffier.models.download`), extracts it safely into a staging
directory (:mod:`~greffier.models.archive`), checks that every file of the
spec is present, writes the manifest, and only then swaps the staging
directory with the installed copy. Whatever fails, the previously installed
version stays usable and the staging directory is removed.

Threading: :meth:`ModelManager.install` blocks and is meant to run in a worker
thread (one per download) while the main thread calls :meth:`statuses`,
:meth:`get_installed` or :meth:`remove`; shared state is guarded by a lock.
Only one ``ModelManager`` should manage a given ``models_dir`` (the daemon's):
leftovers of interrupted installations are cleaned up when it is created.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from greffier.const import VERSION
from greffier.i18n import _
from greffier.models import archive, download
from greffier.models.archive import ArchiveError
from greffier.models.catalog import Catalog, revision_key, valid_model_id
from greffier.models.download import InstallCancelled, ModelError, ProgressFn, check_cancel
from greffier.models.types import CatalogError, InstalledModel, ModelSpec

__all__ = [
    "MANIFEST",
    "ArchiveError",
    "InstallCancelled",
    "ModelError",
    "ModelManager",
    "ModelStatus",
    "ProgressFn",
]

log = logging.getLogger(__name__)

MANIFEST = "greffier-model.json"
STAGING_PREFIX = ".staging-"
OLD_PREFIX = ".old-"


@dataclass
class ModelStatus:
    """A catalog model (or an installed model the catalog no longer lists)."""

    spec: ModelSpec  # catalog entry; the installed spec for orphaned models
    installed: InstalledModel | None
    update_available: bool
    orphaned: bool = False  # installed, but no longer in the catalog
    _size_fn: Callable[[], int] | None = field(default=None, repr=False, compare=False)

    @property
    def size_on_disk(self) -> int:
        """Bytes used by the installed copy (0 if not installed); computed lazily."""
        if self.installed is None:
            return 0
        if self._size_fn is not None:
            return self._size_fn()
        return _dir_size(self.installed.path)

    def to_dict(self) -> dict[str, Any]:
        """``spec.to_dict()`` plus the installation state (for D-Bus / JSON)."""
        data = self.spec.to_dict()
        data.update(
            installed=self.installed is not None,
            installed_revision=self.installed.spec.revision if self.installed else "",
            update_available=self.update_available,
            orphaned=self.orphaned,
            size_on_disk=self.size_on_disk,
        )
        return data


class ModelManager:
    """Install, update, list and remove models under ``models_dir``."""

    def __init__(self, catalog: Catalog, models_dir: Path, downloads_dir: Path) -> None:
        self.catalog = catalog  # replaceable after a catalog refresh
        self.models_dir = Path(models_dir)
        self.downloads_dir = Path(downloads_dir)
        self._lock = threading.RLock()
        self._installing: set[str] = set()
        self._download_locks: dict[str, threading.Lock] = {}  # sha256 -> lock
        self._sizes: dict[tuple[str, str], int] = {}
        self._warned: set[tuple[str, str]] = set()
        self._cleanup()

    # -- queries ----------------------------------------------------------- #

    def statuses(self) -> list[ModelStatus]:
        """Catalog models in catalog order, then orphaned installed models by id."""
        catalog = self.catalog
        result: list[ModelStatus] = []
        for spec in catalog.models():
            installed = self.get_installed(spec.id)
            update = installed is not None and (
                revision_key(spec.revision) > revision_key(installed.spec.revision))
            result.append(self._status(spec, installed, update, orphaned=False))
        for installed in self.installed_models():
            if catalog.get(installed.id) is None:
                result.append(self._status(installed.spec, installed, False, orphaned=True))
        return result

    def get_installed(self, model_id: str) -> InstalledModel | None:
        """The installed copy of ``model_id``, or None.

        None also when the manifest is missing or corrupt or a file of the spec
        is missing (logged once); installing again repairs such a copy.
        """
        if not valid_model_id(model_id):
            return None
        with self._lock:
            return self._read_installed(self.models_dir / model_id, model_id)

    def installed_models(self) -> list[InstalledModel]:
        """Every installed model, catalog or not, sorted by id."""
        try:
            names = sorted(e.name for e in os.scandir(self.models_dir)
                           if not e.name.startswith(".") and e.is_dir())
        except OSError:
            return []
        return [m for m in map(self.get_installed, names) if m is not None]

    def vad_model(self) -> InstalledModel | None:
        """The installed voice-activity-detection model (``kind == "vad"``), if any."""
        for spec in self.catalog.models("vad"):
            installed = self.get_installed(spec.id)
            if installed is not None:
                return installed
        return next((m for m in self.installed_models() if m.spec.kind == "vad"), None)

    def is_installing(self, model_id: str) -> bool:
        """True while :meth:`install` runs for ``model_id``."""
        with self._lock:
            return model_id in self._installing

    # -- install ----------------------------------------------------------- #

    def install(self, model_id: str, progress: ProgressFn | None,
                cancel: threading.Event | None) -> InstalledModel:
        """Download, verify and install (or update / repair) ``model_id``.

        Blocks; ``progress`` is called from the calling thread with the phases
        ``download``, ``verify`` and ``extract``. Raises :class:`InstallCancelled`
        when ``cancel`` is set (the partial download is kept for a later
        attempt) and :class:`ModelError` on failure; in both cases the
        previously installed copy, if any, is left untouched.
        """
        spec = self.catalog.get(model_id)
        if spec is None:
            raise ModelError(_("Unknown model: {id}").format(id=model_id))
        with self._lock:
            if model_id in self._installing:
                raise ModelError(_("{name} is already being installed.").format(name=spec.name))
            self._installing.add(model_id)
        try:
            # Catalog entries may share an archive: one download per checksum.
            with self._lock:
                download_lock = self._download_locks.setdefault(
                    spec.download.sha256, threading.Lock())
            while not download_lock.acquire(timeout=0.2):
                check_cancel(cancel)
            try:
                return self._install(spec, progress, cancel)
            finally:
                download_lock.release()
        except OSError as exc:  # anything not already translated (ModelError is not one)
            raise self._install_error(spec, exc) from exc
        finally:
            with self._lock:
                self._installing.discard(model_id)

    def _install(self, spec: ModelSpec, progress: ProgressFn | None,
                 cancel: threading.Event | None) -> InstalledModel:
        log.info("installing model %s (revision %s)", spec.id, spec.revision)
        try:
            self.models_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise self._install_error(spec, exc) from exc
        source = download.fetch(spec.download, self.downloads_dir, progress=progress,
                                cancel=cancel, reserve=[(self.models_dir, spec.disk_size)])
        check_cancel(cancel)
        try:
            staging = Path(tempfile.mkdtemp(prefix=f"{STAGING_PREFIX}{spec.id}-",
                                            dir=self.models_dir))
        except OSError as exc:
            raise self._install_error(spec, exc) from exc
        installed_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        try:
            archive.extract(source, spec, staging, progress=progress, cancel=cancel)
            for rel in spec.files.values():
                if not os.path.lexists(staging / rel):
                    raise ArchiveError(_("The model archive does not contain the file {file}.")
                                       .format(file=rel))
            check_cancel(cancel)
            _write_manifest(staging, spec, installed_at)
            os.chmod(staging, 0o755)  # mkdtemp creates it 0700
            self._swap(staging, spec.id)
        except ArchiveError:
            _rmtree(staging)
            download.discard(spec.download, self.downloads_dir)  # useless: never retry it
            raise
        except OSError as exc:
            _rmtree(staging)
            raise self._install_error(spec, exc) from exc
        except BaseException:
            _rmtree(staging)
            raise
        download.discard(spec.download, self.downloads_dir)
        log.info("model %s installed in %s", spec.id, self.models_dir / spec.id)
        return InstalledModel(spec, self.models_dir / spec.id, installed_at)

    def _swap(self, staging: Path, model_id: str) -> None:
        """Replace ``<models_dir>/<model_id>`` with ``staging``, keeping it on failure."""
        final = self.models_dir / model_id
        old: Path | None = None
        with self._lock:
            if os.path.lexists(final):
                old = self.models_dir / f"{OLD_PREFIX}{model_id}-{secrets.token_hex(4)}"
                os.rename(final, old)
            try:
                os.rename(staging, final)
            except BaseException:
                if old is not None:
                    os.rename(old, final)
                raise
            self._forget_size(model_id)
            _fsync_dir(self.models_dir)
        if old is not None:
            _rmtree(old)

    @staticmethod
    def _install_error(spec: ModelSpec, exc: OSError) -> ModelError:
        log.error("cannot install %s: %s", spec.id, exc)
        return ModelError(_("Could not install {name}: {error}")
                          .format(name=spec.name, error=exc.strerror or exc))

    # -- remove ------------------------------------------------------------ #

    def remove(self, model_id: str) -> None:
        """Delete the installed copy of ``model_id`` (no-op if not installed).

        Raises :class:`ModelError` while that model is being installed.
        """
        if not valid_model_id(model_id):
            raise ModelError(_("Invalid model identifier: {id}").format(id=model_id))
        name = self._display_name(model_id)
        with self._lock:
            if model_id in self._installing:
                raise ModelError(_("{name} cannot be removed while it is being installed.")
                                 .format(name=name))
            path = self.models_dir / model_id
            if not os.path.lexists(path):
                log.info("model %s is not installed", model_id)
                return
            trash = self.models_dir / f"{OLD_PREFIX}{model_id}-{secrets.token_hex(4)}"
            try:
                os.rename(path, trash)
            except OSError as exc:
                raise ModelError(_("Could not remove {name}: {error}")
                                 .format(name=name, error=exc.strerror or exc)) from exc
            if not trash.is_symlink():  # never reach through a link
                with contextlib.suppress(OSError):
                    (trash / MANIFEST).unlink()  # not restorable by _cleanup()
            self._forget_size(model_id)
        log.info("removing model %s", model_id)
        _rmtree(trash)

    # -- internals --------------------------------------------------------- #

    def _status(self, spec: ModelSpec, installed: InstalledModel | None, update: bool, *,
                orphaned: bool) -> ModelStatus:
        size_fn = (lambda: self._size_on_disk(installed)) if installed is not None else None
        return ModelStatus(spec, installed, update, orphaned, size_fn)

    def _size_on_disk(self, installed: InstalledModel) -> int:
        key = (installed.id, installed.installed_at)
        with self._lock:
            cached = self._sizes.get(key)
        if cached is None:
            cached = _dir_size(installed.path)
            with self._lock:
                self._sizes[key] = cached
        return cached

    def _forget_size(self, model_id: str) -> None:
        self._sizes = {k: v for k, v in self._sizes.items() if k[0] != model_id}

    def _display_name(self, model_id: str) -> str:
        spec = self.catalog.get(model_id)
        if spec is not None:
            return spec.name
        installed = self.get_installed(model_id)
        return installed.spec.name if installed is not None else model_id

    def _warn_once(self, path: Path, reason: str) -> None:
        key = (str(path), reason)
        if key not in self._warned:
            self._warned.add(key)
            log.warning("ignoring installed model %s: %s", path, reason)

    def _read_installed(self, path: Path, expected_id: str | None) -> InstalledModel | None:
        """Parse the manifest in ``path`` and check the model files exist."""
        try:
            raw = (path / MANIFEST).read_bytes()
        except FileNotFoundError:
            if path.is_dir():
                self._warn_once(path, "no manifest")
            return None
        except OSError as exc:
            self._warn_once(path, f"unreadable manifest ({exc})")
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
            spec = ModelSpec.from_dict(data["spec"])
            installed_at = str(data.get("installed_at", ""))
        except (ValueError, KeyError, TypeError, AttributeError, CatalogError) as exc:
            self._warn_once(path, f"corrupt manifest ({exc})")
            return None
        if expected_id is not None and spec.id != expected_id:
            self._warn_once(path, f"manifest is for {spec.id!r}")
            return None
        missing = [rel for rel in spec.files.values() if not os.path.lexists(path / rel)]
        if missing:
            self._warn_once(path, f"missing {', '.join(missing)}")
            return None
        return InstalledModel(spec, path, installed_at)

    def _cleanup(self) -> None:
        """Remove leftovers of interrupted installs and stale partial downloads."""
        try:
            entries = sorted(os.scandir(self.models_dir), key=lambda e: e.name)
        except FileNotFoundError:
            entries = []
        except OSError as exc:
            log.warning("cannot list %s: %s", self.models_dir, exc)
            entries = []
        for entry in entries:
            path = Path(entry.path)
            if entry.name.startswith(STAGING_PREFIX):
                log.info("removing unfinished installation %s", entry.name)
                _rmtree(path)
            elif entry.name.startswith(OLD_PREFIX):
                restorable = not entry.is_symlink() and (path / MANIFEST).is_file()
                old = self._read_installed(path, None) if restorable else None
                final = self.models_dir / old.id if old is not None else None
                if final is not None and valid_model_id(old.id) and not os.path.lexists(final):
                    # Interrupted between the two renames of _swap(): put it back.
                    log.warning("restoring model %s after an interrupted update", old.id)
                    try:
                        os.rename(path, final)
                        continue
                    except OSError as exc:
                        log.warning("cannot restore %s: %s", path, exc)
                log.info("removing previous copy %s", entry.name)
                _rmtree(path)
        if len(self.catalog):
            keep = {spec.download.sha256 for spec in self.catalog.models()}
            download.clean_downloads(self.downloads_dir, keep)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _write_manifest(directory: Path, spec: ModelSpec, installed_at: str) -> None:
    path = directory / MANIFEST
    data = {"spec": spec.to_dict(), "installed_at": installed_at, "app_version": VERSION}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(path, 0o644)


def _rmtree(path: Path) -> None:
    """Delete ``path`` (a link is unlinked, never followed); log failures."""
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path)
    except OSError as exc:
        log.warning("cannot delete %s: %s", path, exc)


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _dir_size(path: Path) -> int:
    """Apparent size of the regular files under ``path`` (links not followed)."""
    total = 0
    stack = [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return total
