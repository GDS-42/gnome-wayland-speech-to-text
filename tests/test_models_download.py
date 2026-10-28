"""Tests for greffier.models.download: resumable, verified downloads."""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from pathlib import Path

import pytest

from greffier.models import download
from greffier.models.download import (
    InstallCancelled,
    ModelError,
    check_free_space,
    clean_downloads,
    fetch,
    human_size,
    part_path,
    verified_path,
)
from greffier.models.types import Download
from tests.test_models_support import FileServer, ProgressLog, noise

DATA = noise(200_000)


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(download, "CHUNK_SIZE", 4096)
    monkeypatch.setattr(download, "PROGRESS_INTERVAL", 0.0)  # see every chunk


@pytest.fixture
def server():
    with FileServer() as srv:
        yield srv


@pytest.fixture
def ddir(tmp_path: Path) -> Path:
    return tmp_path / "downloads"


def dl_for(url: str, data: bytes = DATA) -> Download:
    return Download(url=url, sha256=hashlib.sha256(data).hexdigest(), size=len(data),
                    format="tar.bz2")


def files_in(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


def free_space(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    usage = shutil._ntuple_diskusage(10**12, 10**12 - free, free)  # type: ignore[attr-defined]
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)


# --------------------------------------------------------------------------- #
# Happy path and progress
# --------------------------------------------------------------------------- #


def test_download_happy_path(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/m.tar.bz2", DATA))
    log = ProgressLog()
    path = fetch(dl, ddir, progress=log)
    assert path == verified_path(dl, ddir)
    assert path.read_bytes() == DATA
    assert files_in(ddir) == [f"{dl.sha256}.verified"]  # no .part, no sidecar
    assert log.phases() == ["download", "verify"]
    downloads = [c for c in log.calls if c[0] == "download"]
    assert downloads[0] == ("download", 0, len(DATA))
    assert downloads[-1] == ("download", len(DATA), len(DATA))
    assert log.calls[-1] == ("verify", len(DATA), len(DATA))
    (request,) = server.gets("/m.tar.bz2")
    assert request.range is None
    assert request.user_agent == download.USER_AGENT


def test_progress_is_throttled(server: FileServer, ddir: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "CHUNK_SIZE", 512)
    monkeypatch.setattr(download, "PROGRESS_INTERVAL", 0.1)
    data = noise(3_000_000, seed=3)
    dl = dl_for(server.add("/big", data), data)
    log = ProgressLog()
    started = time.monotonic()
    fetch(dl, ddir, progress=log)
    elapsed = time.monotonic() - started
    downloads = [c for c in log.calls if c[0] == "download"]
    assert len(downloads) <= 10 * elapsed + 3  # first call, ≤10/s, final call
    assert downloads[-1] == ("download", len(data), len(data))


def test_only_phase_starts_and_final_calls_pass_the_throttle(
        server: FileServer, ddir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "PROGRESS_INTERVAL", 3600.0)
    dl = dl_for(server.add("/m", DATA, drop_after=50_000))  # with a retry in between
    log = ProgressLog()
    fetch(dl, ddir, progress=log)
    n = len(DATA)
    assert log.calls == [("download", 0, n), ("download", n, n), ("verify", n, n)]


# --------------------------------------------------------------------------- #
# Resume and retries
# --------------------------------------------------------------------------- #


def test_resume_after_dropped_connection(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/m", DATA, drop_after=50_000))
    assert fetch(dl, ddir).read_bytes() == DATA
    first, second = server.gets("/m")
    assert first.range is None
    assert second.range == "bytes=50000-"


def test_resume_follows_redirects_with_range(server: FileServer, ddir: Path) -> None:
    server.add("/asset", DATA, drop_after=50_000)
    dl = dl_for(server.add("/release", redirect=server.url("/asset")))
    assert fetch(dl, ddir).read_bytes() == DATA
    assert [r.range for r in server.gets("/asset")] == [None, "bytes=50000-"]


def test_redirect_to_plain_http_is_refused(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/release", redirect="http://example.org/m.tar.bz2"))
    with pytest.raises(ModelError, match="insecure"):
        fetch(dl, ddir)


@pytest.mark.parametrize("status", [503, 500, 429])
def test_retry_on_transient_http_errors(server: FileServer, ddir: Path, status: int) -> None:
    dl = dl_for(server.add("/m", DATA, fail=[status, status]))
    assert fetch(dl, ddir).read_bytes() == DATA
    assert len(server.gets("/m")) == 3


def test_gives_up_after_three_retries(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/m", DATA, fail=[503] * 10))
    with pytest.raises(ModelError, match="after several attempts") as info:
        fetch(dl, ddir)
    assert not isinstance(info.value, InstallCancelled)
    assert len(server.gets("/m")) == 4
    assert part_path(dl, ddir).exists()  # still resumable later


@pytest.mark.parametrize("status", [404, 403, 410])
def test_permanent_http_error_is_not_retried(server: FileServer, ddir: Path,
                                             status: int) -> None:
    dl = dl_for(server.add("/m", DATA, fail=[status]))
    with pytest.raises(ModelError, match=str(status)):
        fetch(dl, ddir)
    assert len(server.gets("/m")) == 1


def test_read_timeout_is_retried(server: FileServer, ddir: Path,
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "TIMEOUT", 0.3)
    dl = dl_for(server.add("/m", DATA, stall_after=30_000, stall=1.5))
    assert fetch(dl, ddir).read_bytes() == DATA
    ranges = [r.range for r in server.gets("/m")]
    assert ranges[0] is None
    assert ranges[1] is not None and ranges[1].startswith("bytes=")


def test_server_ignoring_range_restarts_cleanly(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/m", DATA, drop_after=70_000, ignore_range=True))
    assert fetch(dl, ddir).read_bytes() == DATA
    first, second = server.gets("/m")
    assert second.range == "bytes=70000-"  # asked to resume, got a full 200 answer


def test_part_with_mismatching_sidecar_is_restarted(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/m", DATA))
    ddir.mkdir()
    part_path(dl, ddir).write_bytes(b"garbage" * 100)
    (ddir / f"{dl.sha256}.json").write_text(
        json.dumps({"url": dl.url, "size": dl.size + 1, "sha256": dl.sha256}))
    assert fetch(dl, ddir).read_bytes() == DATA
    assert server.gets("/m")[0].range is None


def test_part_without_sidecar_is_restarted(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/m", DATA))
    ddir.mkdir()
    part_path(dl, ddir).write_bytes(DATA[:1000])
    assert fetch(dl, ddir).read_bytes() == DATA
    assert server.gets("/m")[0].range is None


def test_verified_download_is_reused(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/m", DATA))
    fetch(dl, ddir)
    log = ProgressLog()
    assert fetch(dl, ddir, progress=log).read_bytes() == DATA
    assert len(server.gets("/m")) == 1
    assert log.phases() == ["verify"]


def test_corrupt_verified_download_is_fetched_again(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/m", DATA))
    fetch(dl, ddir).write_bytes(noise(len(DATA), seed=9))
    assert fetch(dl, ddir).read_bytes() == DATA
    assert len(server.gets("/m")) == 2


# --------------------------------------------------------------------------- #
# Integrity
# --------------------------------------------------------------------------- #


def test_sha256_mismatch_deletes_the_file(server: FileServer, ddir: Path) -> None:
    url = server.add("/m", noise(len(DATA), seed=1))  # same size, other content
    with pytest.raises(ModelError, match="checksum"):
        fetch(dl_for(url), ddir)
    assert files_in(ddir) == []


@pytest.mark.parametrize("served", [DATA + b"tail", DATA[:-10]])
def test_size_mismatch_deletes_the_file(server: FileServer, ddir: Path, served: bytes) -> None:
    url = server.add("/m", served)
    with pytest.raises(ModelError, match="expected size"):
        fetch(dl_for(url), ddir)
    assert files_in(ddir) == []
    assert len(server.gets("/m")) == 1  # not retried


def cancel_at(cancel: threading.Event, offset: int) -> ProgressLog:
    """A progress callback that cancels the download once ``offset`` bytes are in."""
    return ProgressLog(lambda phase, done, total:
                       cancel.set() if phase == "download" and done >= offset else None)


def test_size_mismatch_on_resume(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/m", DATA))
    cancel = threading.Event()
    with pytest.raises(InstallCancelled):
        fetch(dl, ddir, progress=cancel_at(cancel, 50_000), cancel=cancel)
    server.resources["/m"].data = DATA + b"x"  # the file changed on the server
    with pytest.raises(ModelError, match="expected size"):
        fetch(dl, ddir)
    assert server.gets("/m")[-1].range is not None
    assert files_in(ddir) == []


def test_body_longer_than_catalog_size_is_rejected(server: FileServer, ddir: Path) -> None:
    url = server.add("/m", DATA + b"0123456789", content_length=-1)  # no Content-Length
    with pytest.raises(ModelError, match="expected size"):
        fetch(dl_for(url), ddir)
    assert files_in(ddir) == []


def test_malformed_checksum_never_builds_a_path(tmp_path: Path, ddir: Path) -> None:
    dl = Download(url="https://example.org/x", sha256=("../" * 22)[:64], size=10,
                  format="file")
    with pytest.raises(ModelError):
        fetch(dl, ddir)
    assert files_in(tmp_path) == []


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #


def test_cancel_keeps_part_and_resume_verifies_it(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/m", DATA))
    cancel = threading.Event()
    with pytest.raises(InstallCancelled):
        fetch(dl, ddir, progress=cancel_at(cancel, 100_000), cancel=cancel)
    part = part_path(dl, ddir)
    kept = part.stat().st_size
    assert 100_000 <= kept < len(DATA)
    assert (ddir / f"{dl.sha256}.json").exists()

    log = ProgressLog()
    assert fetch(dl, ddir, progress=log, cancel=threading.Event()).read_bytes() == DATA
    assert log.phases() == ["verify", "download", "verify"]
    assert ("verify", kept, kept) in log.calls
    assert server.gets("/m")[-1].range == f"bytes={kept}-"


def test_cancel_before_start(server: FileServer, ddir: Path) -> None:
    dl = dl_for(server.add("/m", DATA))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InstallCancelled):
        fetch(dl, ddir, cancel=cancel)
    assert server.gets("/m") == []


def test_cancel_interrupts_retry_backoff(server: FileServer, ddir: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "RETRY_DELAYS", (30.0, 30.0, 30.0))
    dl = dl_for(server.add("/m", DATA, fail=[503] * 5))
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(InstallCancelled):
        fetch(dl, ddir, cancel=cancel)
    assert time.monotonic() - started < 5


# --------------------------------------------------------------------------- #
# Disk space
# --------------------------------------------------------------------------- #


def test_not_enough_disk_space(server: FileServer, ddir: Path, tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    dl = dl_for(server.add("/m", DATA))
    free_space(monkeypatch, 1000)
    with pytest.raises(ModelError, match="Not enough disk space") as info:
        fetch(dl, ddir, reserve=[(tmp_path / "models", 1_000_000_000)])
    # (200 kB download + 1 GB installed) + 5 %
    assert "1.1 GB needed" in str(info.value)
    assert "1.0 kB available" in str(info.value)
    assert server.gets("/m") == []


def test_disk_space_only_counts_the_remaining_download(server: FileServer, ddir: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    dl = dl_for(server.add("/m", DATA))
    cancel = threading.Event()
    with pytest.raises(InstallCancelled):
        fetch(dl, ddir, progress=cancel_at(cancel, 100_000), cancel=cancel)
    remaining = len(DATA) - part_path(dl, ddir).stat().st_size
    free_space(monkeypatch, int(remaining * 1.02))  # less than the 5 % margin
    with pytest.raises(ModelError, match="Not enough disk space"):
        fetch(dl, ddir)
    free_space(monkeypatch, int(remaining * 1.06))
    assert fetch(dl, ddir).read_bytes() == DATA


def test_free_space_is_summed_per_filesystem(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    free_space(monkeypatch, 1000)
    check_free_space([(tmp_path / "a", 600)])
    check_free_space([(tmp_path / "a", 600), (tmp_path / "b", 0)])
    with pytest.raises(ModelError):
        check_free_space([(tmp_path / "a", 600), (tmp_path / "b" / "c", 600)])


@pytest.mark.parametrize(("size", "text"), [
    (0, "0 bytes"),
    (999, "999 bytes"),
    (1000, "1.0 kB"),
    (643_854, "643.9 kB"),
    (999_960, "1.0 MB"),
    (153_692_328, "153.7 MB"),
    (1_200_000_000, "1.2 GB"),
    (3 * 10**12, "3.0 TB"),
    (5 * 10**15, "5000.0 TB"),
])
def test_human_size(size: int, text: str) -> None:
    assert human_size(size) == text


def test_clean_downloads_keeps_listed_checksums(tmp_path: Path) -> None:
    keep, drop = "a" * 64, "b" * 64
    names = [f"{keep}.part", f"{keep}.json", f"{drop}.part", f"{drop}.json",
             f"{drop}.verified", f"{drop}.json.tmp", "unrelated.txt"]
    for name in names:
        (tmp_path / name).write_text("x")
    clean_downloads(tmp_path, {keep})
    assert files_in(tmp_path) == sorted([f"{keep}.part", f"{keep}.json", "unrelated.txt"])
