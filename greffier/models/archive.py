"""Safe extraction of downloaded model archives into a staging directory.

Model archives come from third parties. Even though their SHA-256 is checked
against the catalog first, extraction trusts nothing in them:

* only regular files and directories are accepted; symbolic links, hard
  links, devices and FIFOs make the whole archive fail;
* absolute names and ``..`` components are rejected, and every member must sit
  under the catalog's ``root`` directory, which is stripped;
* members under one of the catalog's ``exclude`` prefixes are skipped (after
  the type checks above: an unsafe member is never tolerated);
* files are created with ``O_EXCL`` (duplicates are rejected) and modes are
  normalised to 0644/0755; ownership and timestamps are not restored;
* the extracted size is bounded by the catalog's ``disk_size``.

Any problem raises :class:`ArchiveError`; the caller then deletes the staging
directory, so a partially extracted archive is never used.
"""

from __future__ import annotations

import os
import tarfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath

from greffier.i18n import _
from greffier.models import download as _dl
from greffier.models.download import ModelError, ProgressFn, ProgressReporter, check_cancel
from greffier.models.types import ModelSpec

#: Extracting more than ``disk_size * SIZE_TOLERANCE + SIZE_SLACK`` bytes fails.
SIZE_TOLERANCE = 1.5
SIZE_SLACK = 64 * 1024 * 1024

_OPEN_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) \
    | getattr(os, "O_CLOEXEC", 0)


class ArchiveError(ModelError):
    """The downloaded archive is damaged, unsafe or does not match the catalog."""


def _unsafe(name: str, reason: str) -> ArchiveError:
    return ArchiveError(_("The model archive contains an unsafe entry: {name} ({reason}).")
                        .format(name=name, reason=reason))


def _damaged(exc: BaseException) -> ArchiveError:
    return ArchiveError(_("The model archive is damaged: {error}").format(error=exc))


def check_member(member: tarfile.TarInfo, root: str = "",
                 exclude: Sequence[str] = ()) -> tuple[str, ...] | None:
    """Validate ``member``; return its path relative to ``root`` (as parts).

    Returns ``None`` for members that are not installed (the root directory
    itself, excluded paths). Raises :class:`ArchiveError` for unsafe members.
    """
    name = member.name
    if member.issym() or member.islnk():
        raise _unsafe(name, _("link"))
    if not (member.isreg() or member.isdir()):
        raise _unsafe(name, _("special file"))
    if not name or "\x00" in name:
        raise _unsafe(repr(name), _("invalid name"))
    if name.startswith("/"):
        raise _unsafe(name, _("absolute path"))
    parts = PurePosixPath(name).parts  # drops "." and empty components
    if ".." in parts:
        raise _unsafe(name, _("parent directory reference"))
    if root:
        if not parts:
            if member.isdir():
                return None  # "./"
            raise _unsafe(name, _("outside the model directory"))
        if parts[0] != root:
            raise _unsafe(name, _("outside the model directory"))
        parts = parts[1:]
    if not parts:
        if member.isdir():
            return None  # the root directory itself
        raise _unsafe(name, _("outside the model directory"))
    for prefix in exclude:
        prefix_parts = PurePosixPath(prefix).parts
        if prefix_parts and parts[:len(prefix_parts)] == prefix_parts:
            return None
    return parts


def extract(source: Path, spec: ModelSpec, dest: Path, *, progress: ProgressFn | None = None,
            cancel: threading.Event | None = None) -> int:
    """Install the verified download ``source`` of ``spec`` into ``dest``.

    ``dest`` must be an empty directory (the staging directory). For the
    ``tar.bz2`` format the archive is extracted; for ``file`` the download is
    copied to ``files["model"]``. Reports phase ``extract`` (bytes written vs
    ``spec.disk_size``) and returns the number of bytes written.

    Raises :class:`ArchiveError` for damaged or unsafe archives,
    :class:`~greffier.models.download.InstallCancelled` and ``OSError`` for
    local I/O errors (e.g. disk full).
    """
    report = ProgressReporter(progress)
    if spec.download.format == "file":
        return _copy_single(source, spec, dest, report, cancel)
    if spec.download.format == "tar.bz2":
        return _extract_tar(source, spec, dest, report, cancel)
    raise ArchiveError(_("Unsupported model archive format: {format}")
                       .format(format=spec.download.format))


def _limit(spec: ModelSpec) -> int | None:
    if spec.disk_size <= 0:
        return None
    return int(spec.disk_size * SIZE_TOLERANCE) + SIZE_SLACK


def _write_stream(read: Callable[[int], bytes], target: Path, mode: int,
                  report: ProgressReporter, total: int, written: int,
                  cancel: threading.Event | None) -> int:
    """Copy from ``read`` (a ``read(n)`` callable) into a new file ``target``."""
    try:
        fd = os.open(target, _OPEN_FLAGS, 0o600)
    except FileExistsError:
        raise _unsafe(target.name, _("duplicate entry")) from None
    with os.fdopen(fd, "wb") as out:
        while True:
            check_cancel(cancel)
            try:
                chunk = read(_dl.CHUNK_SIZE)
            except (OSError, EOFError, tarfile.TarError) as exc:
                raise _damaged(exc) from exc
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
            report("extract", written, max(total, written))
        out.flush()
        os.fchmod(out.fileno(), mode)
        os.fsync(out.fileno())
    return written


def _copy_single(source: Path, spec: ModelSpec, dest: Path, report: ProgressReporter,
                 cancel: threading.Event | None) -> int:
    rel = spec.files.get("model")
    if not rel:
        raise ArchiveError(_("The model catalog entry is invalid (no 'model' file)."))
    target = dest / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    total = spec.disk_size or spec.download.size
    report("extract", 0, total, final=True)
    with open(source, "rb") as src:
        written = _write_stream(src.read, target, 0o644, report, total, 0, cancel)
    report("extract", max(total, written), max(total, written), final=True)
    return written


def _extract_tar(source: Path, spec: ModelSpec, dest: Path, report: ProgressReporter,
                 cancel: threading.Event | None) -> int:
    root, exclude = spec.download.root, spec.exclude
    dest_real = dest.resolve()
    total = spec.disk_size
    limit = _limit(spec)
    written = 0
    report("extract", 0, total, final=True)
    try:
        tar = tarfile.open(source, mode="r:bz2")  # noqa: SIM115 - closed by "with tar" below
    except (OSError, EOFError, tarfile.TarError) as exc:
        raise _damaged(exc) from exc
    with tar:
        while True:
            check_cancel(cancel)
            try:
                member = tar.next()
            except (OSError, EOFError, tarfile.TarError) as exc:
                raise _damaged(exc) from exc
            if member is None:
                break
            parts = check_member(member, root, exclude)
            if parts is None:
                continue
            target = dest.joinpath(*parts)
            # Defence in depth: the checks above already guarantee this.
            if not target.resolve().is_relative_to(dest_real):
                raise _unsafe(member.name, _("outside the model directory"))
            try:
                if member.isdir():
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            except (FileExistsError, NotADirectoryError):
                raise _unsafe(member.name, _("duplicate entry")) from None
            if limit is not None and written + member.size > limit:
                raise ArchiveError(_("The model archive is larger than announced in the "
                                     "catalog."))
            try:
                src = tar.extractfile(member)
            except (OSError, EOFError, tarfile.TarError) as exc:
                raise _damaged(exc) from exc
            if src is None:  # pragma: no cover - regular files always have data
                raise _damaged(ValueError(member.name))
            with src:
                mode = 0o755 if member.mode & 0o111 else 0o644
                written = _write_stream(src.read, target, mode, report, total, written, cancel)
    _normalise_dirs(dest)
    report("extract", max(total, written), max(total, written), final=True)
    return written


def _normalise_dirs(dest: Path) -> None:
    """Directories get 0755 whatever the umask was when they were created."""
    for dirpath, dirnames, _files in os.walk(dest):
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), 0o755)  # never links: rejected above
