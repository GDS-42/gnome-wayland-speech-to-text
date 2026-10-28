"""Tests for greffier.models.archive: safe extraction into a staging directory."""

from __future__ import annotations

import io
import os
import stat
import tarfile
import threading
from pathlib import Path

import pytest

from greffier.models import archive, download
from greffier.models.archive import ArchiveError, check_member, extract
from greffier.models.download import InstallCancelled
from tests.test_models_support import (
    ROOT,
    D,
    Entry,
    F,
    ProgressLog,
    make_spec,
    model_archive,
    noise,
    tar_bz2,
)

URL = "https://example.org/model.tar.bz2"


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "CHUNK_SIZE", 4096)
    monkeypatch.setattr(download, "PROGRESS_INTERVAL", 0.0)


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    path = tmp_path / "models" / ".staging-test"
    path.mkdir(parents=True)
    return path


def write_archive(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "download.verified"
    path.write_bytes(data)
    return path


def tree(root: Path) -> dict[str, int]:
    """Relative path -> permission bits of everything under ``root``."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            out[str(path.relative_to(root))] = stat.S_IMODE(path.lstat().st_mode)
    return out


def test_extract_strips_root_and_skips_excluded(tmp_path: Path, staging: Path) -> None:
    data, disk_size = model_archive()
    spec = make_spec(URL, data, disk_size=disk_size)
    log = ProgressLog()
    written = extract(write_archive(tmp_path, data), spec, staging, progress=log)
    assert written == disk_size
    assert tree(staging) == {"encoder.int8.onnx": 0o644, "tokens.txt": 0o644, "run.sh": 0o755}
    assert (staging / "encoder.int8.onnx").read_bytes() == b"encoder"
    assert log.phases() == ["extract"]
    assert log.calls[0] == ("extract", 0, disk_size)
    assert log.calls[-1] == ("extract", disk_size, disk_size)


def test_modes_are_normalised(tmp_path: Path, staging: Path) -> None:
    data = tar_bz2([
        D(f"{ROOT}/", mode=0o700),
        F(f"{ROOT}/setuid", mode=0o4777),
        F(f"{ROOT}/private", mode=0o600),
        F(f"{ROOT}/world-writable", mode=0o666),
        D(f"{ROOT}/sub", mode=0o777),
        F(f"{ROOT}/deep/er/file"),  # parents created implicitly
    ])
    spec = make_spec(URL, data, files={"model": "setuid"})
    extract(write_archive(tmp_path, data), spec, staging)
    assert tree(staging) == {
        "setuid": 0o755, "private": 0o644, "world-writable": 0o644,
        "sub": 0o755, "deep": 0o755, "deep/er": 0o755, "deep/er/file": 0o644,
    }


def test_archive_without_root(tmp_path: Path, staging: Path) -> None:
    data = tar_bz2([F("./model.onnx", b"m"), D("./data/"), F("data/x.bin", b"x")])
    spec = make_spec(URL, data, root="", files={"model": "model.onnx"})
    extract(write_archive(tmp_path, data), spec, staging)
    assert set(tree(staging)) == {"model.onnx", "data", "data/x.bin"}


def test_dot_slash_names_are_accepted(tmp_path: Path, staging: Path) -> None:
    data = tar_bz2([D("./"), D(f"./{ROOT}/"), F(f"./{ROOT}/encoder.int8.onnx"),
                    F(f"./{ROOT}/tokens.txt")])
    extract(write_archive(tmp_path, data), make_spec(URL, data), staging)
    assert set(tree(staging)) == {"encoder.int8.onnx", "tokens.txt"}


UNSAFE = {
    "parent-reference": Entry(f"{ROOT}/../evil", b"pwned"),
    "deep-parent-reference": Entry(f"{ROOT}/a/../../../evil", b"pwned"),
    "absolute-path": Entry("/tmp/evil", b"pwned"),
    "symlink-absolute": Entry(f"{ROOT}/evil", type=tarfile.SYMTYPE, linkname="/etc/passwd"),
    "symlink-relative": Entry(f"{ROOT}/evil", type=tarfile.SYMTYPE, linkname="tokens.txt"),
    "hardlink": Entry(f"{ROOT}/evil", type=tarfile.LNKTYPE, linkname=f"{ROOT}/tokens.txt"),
    "char-device": Entry(f"{ROOT}/evil", type=tarfile.CHRTYPE),
    "block-device": Entry(f"{ROOT}/evil", type=tarfile.BLKTYPE),
    "fifo": Entry(f"{ROOT}/evil", type=tarfile.FIFOTYPE),
    "outside-root": Entry("another-dir/evil", b"pwned"),
    "file-at-top-level": Entry("evil", b"pwned"),
    "file-named-like-root": Entry(ROOT, b"pwned"),
    "link-in-excluded-dir": Entry(f"{ROOT}/test_wavs/evil", type=tarfile.SYMTYPE,
                                  linkname="/etc/shadow"),
    "duplicate": Entry(f"{ROOT}/tokens.txt", b"second copy"),
}


@pytest.mark.parametrize("case", sorted(UNSAFE))
def test_unsafe_members_are_rejected(tmp_path: Path, staging: Path, case: str) -> None:
    bad = UNSAFE[case]
    data = tar_bz2([D(f"{ROOT}/"), F(f"{ROOT}/encoder.int8.onnx"), F(f"{ROOT}/tokens.txt"),
                    bad, F(f"{ROOT}/after.txt")])
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:bz2") as tar:
        assert bad.name in tar.getnames()  # the hostile name really is in the archive
    with pytest.raises(ArchiveError, match="unsafe entry"):
        extract(write_archive(tmp_path, data), make_spec(URL, data), staging)
    assert not (staging / "after.txt").exists()  # extraction stopped right there
    for path in (tmp_path / "evil", tmp_path / "models" / "evil", Path("/tmp/evil")):
        assert not path.exists()
    assert not any(p.is_symlink() for p in staging.rglob("*"))


def test_check_member_paths() -> None:
    def member(name: str, kind: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.type = kind
        return info

    exclude = ("test_wavs", "docs/big")
    assert check_member(member("r/a/b.onnx"), "r", exclude) == ("a", "b.onnx")
    assert check_member(member("r", tarfile.DIRTYPE), "r", exclude) is None
    assert check_member(member("r/test_wavs/x.wav"), "r", exclude) is None
    assert check_member(member("r/test_wavs", tarfile.DIRTYPE), "r", exclude) is None
    assert check_member(member("r/docs/big/f"), "r", exclude) is None
    assert check_member(member("r/docs/small"), "r", exclude) == ("docs", "small")
    assert check_member(member("r/test_wavs_more/x"), "r", exclude) == ("test_wavs_more", "x")
    assert check_member(member("a//b/./c"), "", ()) == ("a", "b", "c")
    with pytest.raises(ArchiveError):
        check_member(member("x/r/a"), "r", exclude)


def test_single_file_format(tmp_path: Path, staging: Path) -> None:
    data = noise(50_000)
    spec = make_spec("https://example.org/vad.onnx", data, fmt="file", kind="vad",
                     files={"model": "silero_vad.onnx"})
    source = write_archive(tmp_path, data)
    log = ProgressLog()
    assert extract(source, spec, staging, progress=log) == len(data)
    assert tree(staging) == {"silero_vad.onnx": 0o644}
    assert (staging / "silero_vad.onnx").read_bytes() == data
    assert source.exists()  # the caller deletes the download once installed
    assert log.calls[-1] == ("extract", len(data), len(data))


@pytest.mark.parametrize("damage", ["garbage", "truncated", "not-bz2"])
def test_damaged_archive(tmp_path: Path, staging: Path, damage: str) -> None:
    data, _size = model_archive(encoder=noise(100_000))
    broken = {
        "garbage": b"this is not an archive at all" * 10,
        "truncated": data[: len(data) // 2],
        "not-bz2": b"\x1f\x8b\x08\x00" + noise(200),
    }[damage]
    with pytest.raises(ArchiveError, match="damaged"):
        extract(write_archive(tmp_path, broken), make_spec(URL, broken), staging)


def test_archive_larger_than_announced(tmp_path: Path, staging: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archive, "SIZE_SLACK", 0)
    data, _size = model_archive(encoder=noise(10_000))
    with pytest.raises(ArchiveError, match="larger than announced"):
        extract(write_archive(tmp_path, data), make_spec(URL, data, disk_size=1000), staging)


def test_progress_stays_throttled_when_archive_exceeds_disk_size(
        tmp_path: Path, staging: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "PROGRESS_INTERVAL", 3600.0)
    data, size = model_archive(encoder=noise(300_000))
    log = ProgressLog()
    extract(write_archive(tmp_path, data), make_spec(URL, data, disk_size=1000), staging,
            progress=log)
    assert log.calls == [("extract", 0, 1000), ("extract", size, size)]


def test_cancel_during_extraction(tmp_path: Path, staging: Path) -> None:
    data, size = model_archive(encoder=noise(300_000))
    cancel = threading.Event()
    log = ProgressLog(lambda phase, done, total: cancel.set() if done > 0 else None)
    with pytest.raises(InstallCancelled):
        extract(write_archive(tmp_path, data), make_spec(URL, data, disk_size=size), staging,
                progress=log, cancel=cancel)
    assert log.calls[-1][1] < size
