"""Resumable, verified HTTP downloads of model files.

A download goes to ``<downloads_dir>/<sha256>.part`` next to a small JSON
sidecar (``<sha256>.json``: url, size, sha256) that tells a later attempt
whether the partial file can be resumed. Once complete, the SHA-256 computed
while streaming is compared with the catalog and the file is renamed to
``<sha256>.verified``; the installer deletes it after extraction.

Only the standard library is used. Every request carries the Greffier
User-Agent, uses HTTPS (plain HTTP is accepted for loopback test servers only)
and never follows a redirect to a non-HTTPS URL. The checksum, not the
transport, is what makes a download trustworthy: anything that does not match
the catalog is deleted.

This module also hosts the exceptions and progress plumbing shared by the
installer (re-exported by :mod:`greffier.models.manager`), so that the lower
layers do not import the manager.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from greffier.const import PROJECT_URL, VERSION
from greffier.i18n import _, pgettext
from greffier.models.types import Download

log = logging.getLogger(__name__)

ProgressFn = Callable[[str, int, int], None]
"""``(phase, done, total)``; phases are ``download``, ``verify`` and ``extract``."""

USER_AGENT = f"Greffier/{VERSION} (+{PROJECT_URL})"
CHUNK_SIZE = 1 << 20  # 1 MiB
TIMEOUT = 30.0  # seconds, applies to the connection and to every read
RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0, 9.0)  # one entry per retry
PROGRESS_INTERVAL = 0.1  # at most 10 progress callbacks per second
SPACE_MARGIN = 0.05  # free space required on top of the strict minimum

PART_SUFFIX = ".part"
META_SUFFIX = ".json"
DONE_SUFFIX = ".verified"
_DOWNLOAD_FILE_RE = re.compile(r"([0-9a-f]{64})\.(part|json|json\.tmp|verified)")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ModelError(RuntimeError):
    """A model could not be installed, updated or removed.

    The message is translated and meant to be shown to the user as is.
    """


class InstallCancelled(ModelError):
    """The user cancelled an installation; a partial download is kept for later."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or _("Installation cancelled."))


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def human_size(size: float) -> str:
    """Format a byte count with decimal units, like GNOME does ("1.2 GB")."""
    if size < 1000:
        return _("{size} bytes").format(size=int(size))
    units = (_("{size} kB"), _("{size} MB"), _("{size} GB"), _("{size} TB"))
    value = float(size)
    for template in units:
        value /= 1000
        if value < 999.95 or template is units[-1]:
            number = f"{value:.1f}".replace(".", pgettext("decimal separator", "."))
            return template.format(size=number)
    raise AssertionError("unreachable")  # pragma: no cover


class ProgressReporter:
    """Throttle a :data:`ProgressFn` to one call per ``PROGRESS_INTERVAL``.

    The first call of each phase and calls made with ``final=True`` are always
    forwarded (a final call identical to the last forwarded one is dropped).
    """

    def __init__(self, fn: ProgressFn | None) -> None:
        self._fn = fn
        self._phase = ""
        self._last = 0.0
        self._sent: tuple[str, int, int] | None = None

    def __call__(self, phase: str, done: int, total: int, *, final: bool = False) -> None:
        if self._fn is None:
            return
        now = time.monotonic()
        call = (phase, done, total)
        if final:
            if call == self._sent:
                return
        elif phase == self._phase and now - self._last < PROGRESS_INTERVAL:
            return
        self._phase, self._last, self._sent = phase, now, call
        self._fn(phase, done, total)


def check_cancel(cancel: threading.Event | None) -> None:
    """Raise :class:`InstallCancelled` if ``cancel`` is set."""
    if cancel is not None and cancel.is_set():
        raise InstallCancelled()


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def is_allowed_url(url: str) -> bool:
    """HTTPS URLs, plus plain HTTP to the loopback interface (test servers)."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme == "https":
        return bool(host)
    return parts.scheme == "http" and host in ("127.0.0.1", "localhost", "::1")


class _SecureRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never to a non-HTTPS URL (no downgrade)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        newurl = urllib.parse.urljoin(req.full_url, newurl)
        downgrade = (urllib.parse.urlsplit(req.full_url).scheme == "https"
                     and urllib.parse.urlsplit(newurl).scheme != "https")
        if downgrade or not is_allowed_url(newurl):
            raise urllib.error.HTTPError(
                req.full_url, code, f"refusing to follow redirect to {newurl}", headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and req.get_method() == "HEAD":
            new.method = "HEAD"
        return new


_OPENER = urllib.request.build_opener(_SecureRedirectHandler)


def open_url(url: str, *, headers: dict[str, str] | None = None, method: str = "GET",
             timeout: float | None = None) -> http.client.HTTPResponse:
    """Open ``url`` with the Greffier User-Agent and the redirect policy above.

    Raises ``ValueError`` for a URL that is not allowed and the usual
    ``urllib.error.URLError``/``HTTPError``/``OSError`` for network errors.
    """
    if not is_allowed_url(url):
        raise ValueError(f"refusing to download from {url!r}: HTTPS is required")
    all_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    all_headers.update(headers or {})
    request = urllib.request.Request(url, headers=all_headers, method=method)
    return _OPENER.open(request, timeout=TIMEOUT if timeout is None else timeout)


class _NetworkError(Exception):
    """Internal: a request failed; ``transient`` errors are retried."""

    def __init__(self, reason: str, *, transient: bool, status: int = 0) -> None:
        super().__init__(reason)
        self.reason = reason
        self.transient = transient
        self.status = status


def _network_error(exc: BaseException) -> _NetworkError:
    """Classify an exception raised by urllib/http.client/socket."""
    if isinstance(exc, urllib.error.HTTPError):
        transient = exc.code in (408, 425, 429) or exc.code >= 500
        if 300 <= exc.code < 400:
            reason = _("the server redirected to an insecure address")
        else:
            reason = _("the server answered with error {status}").format(status=exc.code)
        return _NetworkError(reason, transient=transient, status=exc.code)
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
        exc = exc.reason
    if isinstance(exc, ssl.SSLCertVerificationError | ssl.CertificateError):
        return _NetworkError(_("the secure connection could not be verified ({detail})")
                             .format(detail=getattr(exc, "verify_message", "") or exc),
                             transient=False)
    if isinstance(exc, TimeoutError):
        return _NetworkError(_("the server did not answer in time"), transient=True)
    if isinstance(exc, ConnectionError | http.client.HTTPException):
        return _NetworkError(_("the connection was interrupted"), transient=True)
    if isinstance(exc, OSError):  # DNS failure, network unreachable, TLS error...
        return _NetworkError(_("cannot reach the server ({detail})")
                             .format(detail=exc.strerror or exc), transient=True)
    return _NetworkError(str(exc), transient=False)


def _parse_content_range(value: str | None) -> tuple[int, int | None]:
    """``bytes START-END/TOTAL`` -> (START, TOTAL or None)."""
    match = re.fullmatch(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*", value or "")
    if not match:
        raise _NetworkError(f"invalid Content-Range {value!r}", transient=True)
    total = None if match.group(3) == "*" else int(match.group(3))
    return int(match.group(1)), total


# --------------------------------------------------------------------------- #
# Disk space
# --------------------------------------------------------------------------- #


def _existing_dir(path: Path) -> Path:
    path = path.absolute()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def check_free_space(needs: Iterable[tuple[Path, int]]) -> None:
    """Raise :class:`ModelError` unless every filesystem has room for ``needs``.

    ``needs`` lists ``(directory, bytes)``; directories that do not exist yet
    are measured on their closest existing parent and directories on the same
    filesystem are added up. A ``SPACE_MARGIN`` safety margin is applied.
    """
    per_device: dict[int, list[Any]] = {}
    for directory, size in needs:
        if size <= 0:
            continue
        anchor = _existing_dir(Path(directory))
        entry = per_device.setdefault(anchor.stat().st_dev, [Path(directory), 0, anchor])
        entry[1] += size
    for directory, size, anchor in per_device.values():
        needed = int(size * (1 + SPACE_MARGIN))
        free = shutil.disk_usage(anchor).free
        if free < needed:
            raise ModelError(
                _("Not enough disk space in {path}: {needed} needed, {free} available.")
                .format(path=directory, needed=human_size(needed), free=human_size(free)))


# --------------------------------------------------------------------------- #
# Files of a download
# --------------------------------------------------------------------------- #


def _check_sha(dl: Download) -> None:
    # The checksum names files on disk: never let a malformed one build a path.
    if not _SHA256_RE.fullmatch(dl.sha256) or dl.size <= 0:
        raise ModelError(_("The model catalog entry is invalid (checksum or size)."))


def part_path(dl: Download, downloads_dir: Path) -> Path:
    """Partial download of ``dl``."""
    return downloads_dir / f"{dl.sha256}{PART_SUFFIX}"


def verified_path(dl: Download, downloads_dir: Path) -> Path:
    """Complete download of ``dl`` whose SHA-256 has been checked."""
    return downloads_dir / f"{dl.sha256}{DONE_SUFFIX}"


def _meta_path(dl: Download, downloads_dir: Path) -> Path:
    return downloads_dir / f"{dl.sha256}{META_SUFFIX}"


def discard(dl: Download, downloads_dir: Path) -> None:
    """Delete every file belonging to ``dl`` (partial, verified, sidecar)."""
    for path in (part_path(dl, downloads_dir), verified_path(dl, downloads_dir),
                 _meta_path(dl, downloads_dir)):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("cannot delete %s: %s", path, exc)


def clean_downloads(downloads_dir: Path, keep: Iterable[str]) -> None:
    """Remove downloads whose SHA-256 is not in ``keep`` (no longer resumable)."""
    keep = set(keep)
    try:
        entries = list(os.scandir(downloads_dir))
    except OSError:
        return
    for entry in entries:
        match = _DOWNLOAD_FILE_RE.fullmatch(entry.name)
        if match and match.group(1) not in keep:
            log.info("removing stale download %s", entry.name)
            with contextlib.suppress(OSError):
                os.unlink(entry.path)


def _read_meta(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable download sidecar %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _write_meta(dl: Download, downloads_dir: Path) -> None:
    path = _meta_path(dl, downloads_dir)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"url": dl.url, "size": dl.size, "sha256": dl.sha256}),
                   encoding="utf-8")
    os.replace(tmp, path)


def _hash_file(path: Path, hasher: Any, report: ProgressReporter,
               cancel: threading.Event | None) -> int:
    """Feed ``path`` into ``hasher`` (phase ``verify``); return the bytes read."""
    total = path.stat().st_size
    done = 0
    report("verify", 0, total, final=True)
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK_SIZE):
            check_cancel(cancel)
            hasher.update(chunk)
            done += len(chunk)
            report("verify", done, total)
    report("verify", done, total, final=True)
    return done


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


class _State:
    """Progress of a download: bytes in the part file and their running hash."""

    def __init__(self) -> None:
        self.offset = 0
        self.hasher = hashlib.sha256()

    def reset(self) -> None:
        self.offset = 0
        self.hasher = hashlib.sha256()


def _size_mismatch(dl: Download, downloads_dir: Path, actual: int) -> ModelError:
    log.error("size mismatch for %s: %d bytes instead of %d", dl.url, actual, dl.size)
    discard(dl, downloads_dir)
    return ModelError(
        _("The downloaded file does not have the expected size ({actual} bytes instead "
          "of {expected}). It has been deleted; please try again later.")
        .format(actual=actual, expected=dl.size))


def _transfer(dl: Download, downloads_dir: Path, state: _State, report: ProgressReporter,
              cancel: threading.Event | None) -> None:
    """One HTTP request, resuming at ``state.offset``; raises _NetworkError."""
    part = part_path(dl, downloads_dir)
    headers = {"Range": f"bytes={state.offset}-"} if state.offset else {}
    try:
        response = open_url(dl.url, headers=headers)
    except ValueError as exc:
        raise _NetworkError(str(exc), transient=False) from exc
    except (OSError, http.client.HTTPException) as exc:
        raise _network_error(exc) from exc

    with response:
        if response.status == 206 and state.offset:
            start, total = _parse_content_range(response.headers.get("Content-Range"))
            if total is not None and total != dl.size:
                raise _size_mismatch(dl, downloads_dir, total)
            if start != state.offset:
                log.warning("server resumed %s at %d instead of %d; restarting",
                            dl.url, start, state.offset)
                state.reset()
                raise _NetworkError("unexpected Content-Range", transient=True)
            mode = "ab"
        elif response.status == 200:
            if state.offset:
                log.info("server ignored the Range request for %s; restarting", dl.url)
                state.reset()
            length = response.headers.get("Content-Length")
            if length is not None and length.strip().isdigit() and int(length) != dl.size:
                raise _size_mismatch(dl, downloads_dir, int(length))
            mode = "wb"
        else:
            raise _NetworkError(f"unexpected HTTP status {response.status}", transient=False,
                                status=response.status)

        with open(part, mode) as out:
            if out.tell() != state.offset:  # the part file changed under our feet
                state.reset()
                raise _NetworkError("partial download changed on disk", transient=True)
            while True:
                check_cancel(cancel)
                try:
                    # read1: whatever one socket read brings (up to CHUNK_SIZE), so
                    # cancellation stays responsive on slow connections.
                    chunk = response.read1(CHUNK_SIZE)
                except (OSError, http.client.HTTPException) as exc:
                    raise _network_error(exc) from exc
                if not chunk:
                    break
                if state.offset + len(chunk) > dl.size:
                    out.close()
                    raise _size_mismatch(dl, downloads_dir, state.offset + len(chunk))
                out.write(chunk)
                state.hasher.update(chunk)
                state.offset += len(chunk)
                report("download", state.offset, dl.size)
    if state.offset < dl.size:
        raise _NetworkError(_("the connection was interrupted"), transient=True)


def fetch(dl: Download, downloads_dir: Path, *, progress: ProgressFn | None = None,
          cancel: threading.Event | None = None,
          reserve: Sequence[tuple[Path, int]] = ()) -> Path:
    """Download ``dl`` into ``downloads_dir`` and return the verified file.

    Resumes a previous partial download when its sidecar matches, retries
    transient network errors (resuming each time) and checks the size and the
    SHA-256 against the catalog. ``reserve`` lists extra space needed by the
    caller (e.g. ``(models_dir, spec.disk_size)``) for the free-space check.

    Raises :class:`InstallCancelled` when ``cancel`` is set (the partial file
    is kept) and :class:`ModelError` on any other failure (a corrupt file is
    deleted).
    """
    _check_sha(dl)
    check_cancel(cancel)
    try:
        return _fetch(dl, downloads_dir, ProgressReporter(progress), cancel, reserve)
    except OSError as exc:  # local I/O only: network errors are classified below
        log.error("cannot store the download of %s: %s", dl.url, exc)
        raise ModelError(_("Cannot write the download to disk: {error}")
                         .format(error=exc.strerror or exc)) from exc


def _fetch(dl: Download, downloads_dir: Path, report: ProgressReporter,
           cancel: threading.Event | None, reserve: Sequence[tuple[Path, int]]) -> Path:
    downloads_dir.mkdir(parents=True, exist_ok=True)
    done = verified_path(dl, downloads_dir)
    if done.is_file() and done.stat().st_size == dl.size:
        check_free_space(reserve)
        hasher = hashlib.sha256()
        _hash_file(done, hasher, report, cancel)
        if hasher.hexdigest() == dl.sha256:
            log.info("reusing verified download %s", done.name)
            return done
        log.warning("verified download %s is corrupt; downloading again", done.name)
    done.unlink(missing_ok=True)

    part = part_path(dl, downloads_dir)
    meta = _read_meta(_meta_path(dl, downloads_dir))
    resumable = 0
    if part.is_file():
        resumable = part.stat().st_size
        if not (meta and meta.get("sha256") == dl.sha256 and meta.get("size") == dl.size
                and 0 < resumable <= dl.size):
            log.info("discarding unusable partial download %s", part.name)
            resumable = 0
        elif meta.get("url") != dl.url:
            log.info("resuming %s from a different URL (same checksum)", part.name)

    check_free_space([(downloads_dir, dl.size - resumable), *reserve])

    state = _State()
    if resumable:
        state.offset = _hash_file(part, state.hasher, report, cancel)
    else:
        _write_meta(dl, downloads_dir)
        part.write_bytes(b"")
    _download_loop(dl, downloads_dir, state, report, cancel)

    if state.hasher.hexdigest() != dl.sha256:
        log.error("SHA-256 mismatch for %s: got %s", dl.url, state.hasher.hexdigest())
        discard(dl, downloads_dir)
        raise ModelError(_("The downloaded file is corrupted (checksum mismatch). "
                           "It has been deleted; please try again."))
    report("verify", dl.size, dl.size, final=True)
    os.replace(part, done)
    _meta_path(dl, downloads_dir).unlink(missing_ok=True)
    return done


def _download_loop(dl: Download, downloads_dir: Path, state: _State, report: ProgressReporter,
                   cancel: threading.Event | None) -> None:
    """Request until the whole file is there, retrying transient failures."""
    failures = 0
    report("download", state.offset, dl.size, final=True)
    while state.offset < dl.size:
        check_cancel(cancel)
        before = state.offset
        try:
            _transfer(dl, downloads_dir, state, report, cancel)
        except _NetworkError as exc:
            if exc.status == 416:  # our offset is past the server's end: start over
                state.reset()
                part_path(dl, downloads_dir).write_bytes(b"")
            if state.offset > before:
                failures = 0  # progress was made: this is a new streak
            if not exc.transient and exc.status != 416:
                log.error("download of %s failed: %s", dl.url, exc.reason)
                raise ModelError(_("Download failed: {error}").format(error=exc.reason)) from None
            if failures >= len(RETRY_DELAYS):
                log.error("download of %s failed %d times, giving up: %s", dl.url,
                          failures + 1, exc.reason)
                raise ModelError(
                    _("Download failed after several attempts: {error}")
                    .format(error=exc.reason)) from None
            delay = RETRY_DELAYS[failures]
            failures += 1
            log.warning("download of %s interrupted at %d/%d bytes (%s); retry %d/%d in %.0f s",
                        dl.url, state.offset, dl.size, exc.reason, failures,
                        len(RETRY_DELAYS), delay)
            if cancel is not None and cancel.wait(delay):
                raise InstallCancelled() from None
            if cancel is None and delay:
                time.sleep(delay)
    report("download", state.offset, dl.size, final=True)
