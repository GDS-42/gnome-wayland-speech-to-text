"""Tests for greffier.models.manager: install, update, status, removal."""

from __future__ import annotations

import errno
import filecmp
import json
import logging
import os
import shutil
import tarfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from greffier.const import VERSION
from greffier.models import archive, download, manager
from greffier.models.catalog import Catalog, parse_entry
from greffier.models.manager import (
    MANIFEST,
    InstallCancelled,
    ModelError,
    ModelManager,
    ModelStatus,
)
from greffier.models.types import InstalledModel, ModelSpec
from tests.conftest import TEST_DL
from tests.test_models_support import (
    ROOT,
    D,
    Entry,
    F,
    FileServer,
    ProgressLog,
    Resource,
    catalog_of,
    make_spec,
    model_archive,
    noise,
    tar_bz2,
)

MODEL_ID = "test-model"


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(download, "RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(download, "CHUNK_SIZE", 4096)
    monkeypatch.setattr(download, "PROGRESS_INTERVAL", 0.0)


@pytest.fixture
def server():
    with FileServer() as srv:
        yield srv


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "models"


@pytest.fixture
def downloads_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "downloads"


@pytest.fixture
def make_manager(models_dir: Path, downloads_dir: Path):
    def make(*specs: ModelSpec) -> ModelManager:
        return ModelManager(catalog_of(*specs), models_dir, downloads_dir)
    return make


def publish(server: FileServer, path: str, data: bytes, **options: Any) -> ModelSpec:
    """Serve ``data`` at ``path`` and return a matching catalog entry."""
    return make_spec(server.add(path, data), data, **options)


def publish_model(server: FileServer, path: str = "/v1.tar.bz2", encoder: bytes = b"v1",
                  revision: str = "2026-01-01", **options: Any) -> ModelSpec:
    data, disk_size = model_archive(encoder=encoder)
    return publish(server, path, data, revision=revision, disk_size=disk_size, **options)


def ls(path: Path) -> list[str]:
    return sorted(os.listdir(path)) if path.exists() else []


def install(mgr: ModelManager, model_id: str = MODEL_ID, **kw: Any) -> Any:
    return mgr.install(model_id, kw.get("progress", ProgressLog()),
                       kw.get("cancel", threading.Event()))


# --------------------------------------------------------------------------- #
# Install
# --------------------------------------------------------------------------- #


def test_install_happy_path(server: FileServer, make_manager, models_dir: Path,
                            downloads_dir: Path) -> None:
    spec = publish_model(server)
    mgr = make_manager(spec)
    (before,) = mgr.statuses()
    assert before.installed is None
    assert before.to_dict()["installed"] is False
    assert before.to_dict()["installed_revision"] == ""
    assert before.size_on_disk == 0

    log = ProgressLog()
    installed = install(mgr, progress=log)

    assert installed.path == models_dir / MODEL_ID
    assert installed.file("encoder").read_bytes() == b"v1"
    assert installed.file("tokens").is_file()
    assert not (installed.path / "test_wavs").exists()
    assert log.phases() == ["download", "verify", "extract"]
    assert ls(models_dir) == [MODEL_ID]  # no staging directory left
    assert ls(downloads_dir) == []  # the archive is deleted once installed

    manifest = json.loads((installed.path / MANIFEST).read_text())
    assert manifest["spec"] == spec.to_dict()
    assert manifest["app_version"] == VERSION
    assert datetime.fromisoformat(manifest["installed_at"]).utcoffset().total_seconds() == 0
    assert manifest["installed_at"] == installed.installed_at

    again = mgr.get_installed(MODEL_ID)
    assert again is not None and again.spec == spec and again.path == installed.path
    (status,) = mgr.statuses()
    assert status.installed is not None and not status.update_available and not status.orphaned
    assert status.size_on_disk >= spec.disk_size
    data = status.to_dict()
    assert {k: data[k] for k in spec.to_dict()} == spec.to_dict()
    assert data["installed"] is True
    assert data["installed_revision"] == spec.revision
    assert data["update_available"] is False
    assert data["orphaned"] is False
    assert data["size_on_disk"] == status.size_on_disk
    json.dumps(data)  # serialisable as is


def test_install_single_file_vad_model(server: FileServer, make_manager,
                                       downloads_dir: Path) -> None:
    data = noise(20_000)
    vad = publish(server, "/silero_vad.onnx", data, model_id="vad-test", kind="vad",
                  fmt="file", files={"model": "silero_vad.onnx"})
    asr = publish_model(server)
    mgr = make_manager(asr, vad)
    assert mgr.vad_model() is None
    install(mgr, MODEL_ID)
    assert mgr.vad_model() is None  # an ASR model is not a VAD
    install(mgr, "vad-test")
    found = mgr.vad_model()
    assert found is not None and found.id == "vad-test"
    assert found.file("model").read_bytes() == data
    assert ls(downloads_dir) == []


def test_install_resumes_after_interruption(server: FileServer, make_manager) -> None:
    data, disk_size = model_archive(encoder=noise(150_000))
    spec = make_spec(server.add("/m", data, drop_after=60_000), data, disk_size=disk_size)
    install(make_manager(spec))
    assert [r.range for r in server.gets("/m")] == [None, "bytes=60000-"]


def test_install_retries_on_503(server: FileServer, make_manager) -> None:
    data, disk_size = model_archive()
    spec = make_spec(server.add("/m", data, fail=[503, 503, 503]), data, disk_size=disk_size)
    assert install(make_manager(spec)).file("tokens").is_file()
    assert len(server.gets("/m")) == 4


def test_cancel_then_resume(server: FileServer, make_manager, models_dir: Path,
                            downloads_dir: Path) -> None:
    data, disk_size = model_archive(encoder=noise(150_000))
    spec = make_spec(server.add("/m", data), data, disk_size=disk_size)
    mgr = make_manager(spec)
    cancel = threading.Event()
    log = ProgressLog(lambda p, d, t: cancel.set() if p == "download" and d >= 60_000 else None)
    with pytest.raises(InstallCancelled):
        install(mgr, progress=log, cancel=cancel)
    assert mgr.get_installed(MODEL_ID) is None
    assert not mgr.is_installing(MODEL_ID)
    assert ls(models_dir) == []
    assert f"{spec.download.sha256}.part" in ls(downloads_dir)

    log = ProgressLog()
    install(mgr, progress=log)
    assert log.phases() == ["verify", "download", "verify", "extract"]
    assert server.gets("/m")[-1].range is not None
    assert ls(downloads_dir) == []


def test_cancel_during_extraction_keeps_verified_download(server: FileServer, make_manager,
                                                          models_dir: Path,
                                                          downloads_dir: Path) -> None:
    spec = publish_model(server, encoder=noise(100_000))
    mgr = make_manager(spec)
    cancel = threading.Event()
    with pytest.raises(InstallCancelled):
        install(mgr, progress=ProgressLog(lambda p, d, t: cancel.set() if p == "extract" else None),
                cancel=cancel)
    assert ls(models_dir) == []  # staging removed
    assert ls(downloads_dir) == [f"{spec.download.sha256}.verified"]
    install(mgr)
    assert len(server.gets("/v1.tar.bz2")) == 1  # not downloaded twice


def test_unknown_model(make_manager) -> None:
    with pytest.raises(ModelError, match="Unknown model"):
        install(make_manager(), "nope")


def test_not_enough_disk_space(server: FileServer, make_manager, models_dir: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    spec = publish_model(server)
    usage = shutil._ntuple_diskusage(10**9, 10**9 - 10, 10)  # type: ignore[attr-defined]
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage)
    with pytest.raises(ModelError, match="Not enough disk space"):
        install(make_manager(spec))
    assert server.requests == []
    assert ls(models_dir) == []


# --------------------------------------------------------------------------- #
# Updates
# --------------------------------------------------------------------------- #


def test_update_replaces_old_revision(server: FileServer, make_manager,
                                      models_dir: Path) -> None:
    v1 = publish_model(server, "/v1.tar.bz2", b"v1", "2026-01-01")
    mgr = make_manager(v1)
    install(mgr)
    v2 = publish_model(server, "/v2.tar.bz2", b"v2", "2026-02-01")
    mgr.catalog = catalog_of(v2)
    (status,) = mgr.statuses()
    assert status.update_available
    assert status.to_dict()["installed_revision"] == "2026-01-01"

    seen_during_update: list[str] = []

    def watch(phase: str, done: int, total: int) -> None:
        current = mgr.get_installed(MODEL_ID)
        seen_during_update.append(current.spec.revision if current else "missing")

    installed = install(mgr, progress=ProgressLog(watch))
    assert set(seen_during_update) == {"2026-01-01"}  # old copy usable until the swap
    assert installed.file("encoder").read_bytes() == b"v2"
    assert mgr.get_installed(MODEL_ID).spec.revision == "2026-02-01"
    assert not mgr.statuses()[0].update_available
    assert ls(models_dir) == [MODEL_ID]  # no .old-* or .staging-* left


def test_older_catalog_revision_is_not_an_update(server: FileServer, make_manager) -> None:
    v2 = publish_model(server, "/v2.tar.bz2", b"v2", "2026-02-01")
    mgr = make_manager(v2)
    install(mgr)
    mgr.catalog = catalog_of(publish_model(server, "/v1.tar.bz2", b"v1", "2026-01-01"))
    assert not mgr.statuses()[0].update_available


def _corrupt(server: FileServer, spec: ModelSpec) -> None:
    server.resources["/v2.tar.bz2"].data = noise(spec.download.size, seed=5)


def _missing_file(server: FileServer, spec: ModelSpec) -> ModelSpec:
    data = tar_bz2([D(f"{ROOT}/"), F(f"{ROOT}/encoder.int8.onnx", b"v2")])  # no tokens.txt
    return make_spec(server.add("/v2.tar.bz2", data), data, revision="2026-02-01")


def _unsafe(server: FileServer, spec: ModelSpec) -> ModelSpec:
    data = tar_bz2([D(f"{ROOT}/"), F(f"{ROOT}/encoder.int8.onnx", b"v2"),
                    F(f"{ROOT}/tokens.txt"),
                    Entry(f"{ROOT}/evil", type=tarfile.SYMTYPE, linkname="/etc/passwd")])
    return make_spec(server.add("/v2.tar.bz2", data), data, revision="2026-02-01")


@pytest.mark.parametrize("failure", ["checksum", "missing-file", "unsafe", "disk-full",
                                     "swap", "cancel"])
def test_failed_update_keeps_old_version(server: FileServer, make_manager, models_dir: Path,
                                         downloads_dir: Path, failure: str,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    v1 = publish_model(server, "/v1.tar.bz2", b"v1", "2026-01-01")
    mgr = make_manager(v1)
    install(mgr)
    v2 = publish_model(server, "/v2.tar.bz2", b"v2", "2026-02-01")
    cancel = threading.Event()
    progress = ProgressLog()
    expected: type[Exception] = ModelError
    if failure == "checksum":
        _corrupt(server, v2)
    elif failure == "missing-file":
        v2 = _missing_file(server, v2)
    elif failure == "unsafe":
        v2 = _unsafe(server, v2)
    elif failure == "disk-full":
        def full(*args: Any, **kwargs: Any) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")
        monkeypatch.setattr(archive, "_extract_tar", full)
    elif failure == "swap":
        real_rename = os.rename

        def rename(src: Any, dst: Any) -> None:
            if ".staging-" in str(src):
                raise OSError(errno.EIO, "I/O error")
            real_rename(src, dst)
        monkeypatch.setattr(os, "rename", rename)
    elif failure == "cancel":
        expected = InstallCancelled
        progress = ProgressLog(lambda p, d, t: cancel.set() if p == "extract" else None)
    mgr.catalog = catalog_of(v2)

    with pytest.raises(expected) as info:
        install(mgr, cancel=cancel, progress=progress)

    if failure == "disk-full":
        assert "No space left on device" in str(info.value)
    installed = mgr.get_installed(MODEL_ID)
    assert installed is not None
    assert installed.spec.revision == "2026-01-01"
    assert installed.file("encoder").read_bytes() == b"v1"
    assert ls(models_dir) == [MODEL_ID]
    if failure in ("checksum", "missing-file", "unsafe"):
        assert ls(downloads_dir) == []  # a bad archive is not kept
    assert mgr.statuses()[0].update_available
    assert not mgr.is_installing(MODEL_ID)


def test_repair_reinstalls_same_revision(server: FileServer, make_manager,
                                         models_dir: Path) -> None:
    spec = publish_model(server)
    mgr = make_manager(spec)
    install(mgr)
    (models_dir / MODEL_ID / "tokens.txt").unlink()
    assert mgr.get_installed(MODEL_ID) is None  # broken copy is not "installed"
    install(mgr)
    assert mgr.get_installed(MODEL_ID) is not None
    assert ls(models_dir) == [MODEL_ID]


# --------------------------------------------------------------------------- #
# Status details
# --------------------------------------------------------------------------- #


def test_orphaned_models(server: FileServer, make_manager) -> None:
    old = publish_model(server)
    mgr = make_manager(old)
    install(mgr)
    other = publish_model(server, "/other.tar.bz2", model_id="other-model")
    mgr.catalog = catalog_of(other)
    statuses = mgr.statuses()
    assert [(s.spec.id, s.orphaned) for s in statuses] == [("other-model", False),
                                                             (MODEL_ID, True)]
    orphan = statuses[1]
    assert orphan.installed is not None and orphan.spec == old
    assert not orphan.update_available
    assert orphan.to_dict()["orphaned"] is True
    with pytest.raises(ModelError, match="Unknown model"):
        install(mgr, MODEL_ID)
    mgr.remove(MODEL_ID)
    assert [s.spec.id for s in mgr.statuses()] == ["other-model"]


def test_size_on_disk_is_lazy_and_cached(server: FileServer, make_manager,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    spec = publish_model(server)
    mgr = make_manager(spec)
    install(mgr)
    calls: list[Path] = []
    real = manager._dir_size
    monkeypatch.setattr(manager, "_dir_size", lambda path: calls.append(path) or real(path))
    statuses = mgr.statuses()
    assert calls == []  # not computed until asked
    size = statuses[0].size_on_disk
    assert size == mgr.statuses()[0].to_dict()["size_on_disk"]
    assert len(calls) == 1
    install(mgr)  # reinstall invalidates the cache
    assert mgr.statuses()[0].size_on_disk == size
    assert len(calls) == 2


def test_status_without_manager_computes_size(tmp_path: Path) -> None:
    spec = make_spec("https://example.org/m.tar.bz2", b"x")
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "a").write_bytes(b"12345")
    status = ModelStatus(spec, InstalledModel(spec, tmp_path / "m"), False)
    assert status.size_on_disk == 5


@pytest.mark.parametrize("damage", ["garbage", "not-an-object", "wrong-id", "no-manifest",
                                    "bad-spec"])
def test_corrupt_manifest_means_not_installed(server: FileServer, make_manager,
                                              models_dir: Path, damage: str,
                                              caplog: pytest.LogCaptureFixture) -> None:
    spec = publish_model(server)
    mgr = make_manager(spec)
    install(mgr)
    manifest = models_dir / MODEL_ID / MANIFEST
    if damage == "garbage":
        manifest.write_bytes(b"\x00{{{")
    elif damage == "not-an-object":
        manifest.write_text("[]")
    elif damage == "wrong-id":
        data = json.loads(manifest.read_text())
        data["spec"]["id"] = "another-id"
        manifest.write_text(json.dumps(data))
    elif damage == "no-manifest":
        manifest.unlink()
    elif damage == "bad-spec":
        data = json.loads(manifest.read_text())
        data["spec"]["files"] = {"encoder": "/etc/passwd"}
        manifest.write_text(json.dumps(data))
    with caplog.at_level(logging.WARNING):
        assert mgr.get_installed(MODEL_ID) is None
        assert mgr.statuses()[0].installed is None
        assert mgr.get_installed(MODEL_ID) is None
    assert caplog.text.count("ignoring installed model") == 1  # logged once
    install(mgr)  # installing again repairs it
    assert mgr.get_installed(MODEL_ID) is not None


def test_get_installed_rejects_unsafe_ids(make_manager, models_dir: Path) -> None:
    mgr = make_manager()
    for bad in ("", "..", "../x", ".staging-x", "a/b"):
        assert mgr.get_installed(bad) is None


# --------------------------------------------------------------------------- #
# Removal and concurrency
# --------------------------------------------------------------------------- #


def test_remove(server: FileServer, make_manager, models_dir: Path) -> None:
    spec = publish_model(server)
    mgr = make_manager(spec)
    install(mgr)
    mgr.remove(MODEL_ID)
    assert mgr.get_installed(MODEL_ID) is None
    assert ls(models_dir) == []
    mgr.remove(MODEL_ID)  # idempotent
    for bad in ("../data", "..", ".staging-x", "", "a/b"):
        with pytest.raises(ModelError, match="Invalid model identifier"):
            mgr.remove(bad)


def test_remove_symlinked_model_only_removes_the_link(make_manager, models_dir: Path,
                                                      tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.onnx").write_text("precious")
    models_dir.mkdir(parents=True)
    (models_dir / "linked").symlink_to(target)
    make_manager().remove("linked")
    assert not os.path.lexists(models_dir / "linked")
    assert (target / "keep.onnx").read_text() == "precious"


def test_remove_while_installing(server: FileServer, make_manager, models_dir: Path) -> None:
    encoder = noise(200_000)
    data, disk_size = model_archive(encoder=encoder)
    gate = threading.Event()
    server.resources["/m"] = Resource(data=data, gate=gate, gate_after=50_000)
    spec = make_spec(server.url("/m"), data, disk_size=disk_size)
    other = publish_model(server, "/other.tar.bz2", model_id="other-model")
    mgr = make_manager(spec, other)
    install(mgr, "other-model")

    started = threading.Event()
    result: dict[str, Any] = {}

    def progress(phase: str, done: int, total: int) -> None:
        if phase == "download" and done >= 40_000:
            started.set()

    def worker() -> None:
        try:
            result["installed"] = mgr.install(MODEL_ID, progress, threading.Event())
        except BaseException as exc:  # pragma: no cover - reported below
            result["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert started.wait(10)
        assert mgr.is_installing(MODEL_ID)
        with pytest.raises(ModelError, match="being installed"):
            mgr.remove(MODEL_ID)
        with pytest.raises(ModelError, match="already being installed"):
            install(mgr, MODEL_ID)
        # The main thread keeps working while the download is in progress.
        assert [s.installed is not None for s in mgr.statuses()] == [False, True]
        mgr.remove("other-model")
    finally:
        gate.set()
        thread.join(10)
    assert "error" not in result, result.get("error")
    assert result["installed"].file("encoder").read_bytes() == encoder
    assert not mgr.is_installing(MODEL_ID)
    mgr.remove(MODEL_ID)
    assert ls(models_dir) == []


@pytest.mark.parametrize("shared_archive", [False, True])
def test_concurrent_installs_of_different_models(server: FileServer, make_manager,
                                                 shared_archive: bool) -> None:
    # With shared_archive, the three entries point to the same file (same SHA-256).
    specs = [publish_model(server, f"/m{i}.tar.bz2", model_id=f"model-{i}",
                           encoder=b"same" if shared_archive else noise(50_000, seed=i))
             for i in range(3)]
    mgr = make_manager(*specs)
    errors: list[BaseException] = []

    def run(model_id: str) -> None:
        try:
            install(mgr, model_id)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(s.id,)) for s in specs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert errors == []
    assert all(s.installed is not None for s in mgr.statuses())


# --------------------------------------------------------------------------- #
# Start-up cleanup
# --------------------------------------------------------------------------- #


def test_cleanup_of_stale_directories(server: FileServer, make_manager, models_dir: Path,
                                      downloads_dir: Path) -> None:
    spec = publish_model(server)
    install(make_manager(spec))
    staging = models_dir / ".staging-test-model-abcd1234"
    (staging / "sub").mkdir(parents=True)
    (staging / "sub" / "file").write_text("half extracted")
    removed = models_dir / ".old-gone-model-0000beef"  # a removal that was interrupted
    removed.mkdir()
    (removed / "x").write_text("x")
    downloads_dir.mkdir(parents=True, exist_ok=True)
    stale, current = "f" * 64, spec.download.sha256
    for name in (f"{stale}.part", f"{stale}.json", f"{current}.part", f"{current}.json"):
        (downloads_dir / name).write_text("partial")

    mgr = make_manager(spec)
    assert ls(models_dir) == [MODEL_ID]
    assert ls(downloads_dir) == [f"{current}.json", f"{current}.part"]  # still resumable
    assert mgr.get_installed(MODEL_ID) is not None


def test_cleanup_restores_copy_interrupted_during_swap(server: FileServer, make_manager,
                                                       models_dir: Path) -> None:
    spec = publish_model(server)
    install(make_manager(spec))
    # Crash between "rename current to .old" and "rename staging to current".
    os.rename(models_dir / MODEL_ID, models_dir / ".old-test-model-12345678")
    (models_dir / ".staging-test-model-xyz").mkdir()
    mgr = make_manager(spec)
    assert ls(models_dir) == [MODEL_ID]
    assert mgr.get_installed(MODEL_ID) is not None


def test_cleanup_drops_old_copy_when_current_exists(server: FileServer, make_manager,
                                                    models_dir: Path) -> None:
    spec = publish_model(server)
    install(make_manager(spec))
    shutil.copytree(models_dir / MODEL_ID, models_dir / ".old-test-model-12345678")
    make_manager(spec)
    assert ls(models_dir) == [MODEL_ID]


# --------------------------------------------------------------------------- #
# Real archive (slow)
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_real_canary_archive(models_dir: Path, downloads_dir: Path,
                             monkeypatch: pytest.MonkeyPatch) -> None:
    archive_file = TEST_DL / "sherpa-onnx-nemo-canary-180m-flash-en-es-de-fr-int8.tar.bz2"
    if not archive_file.is_file():
        pytest.skip(f"real archive not available: {archive_file}")
    monkeypatch.setattr(download, "CHUNK_SIZE", 1 << 20)
    monkeypatch.setattr(download, "PROGRESS_INTERVAL", 0.1)
    builtin = Catalog.load().get("canary-180m-flash-int8")
    assert builtin is not None
    with FileServer() as srv:
        # Cut the connection once at 60 MB to exercise resuming on the real file.
        srv.resources["/canary.tar.bz2"] = Resource(file=archive_file, drop_after=60_000_000)
        entry = builtin.to_dict()
        entry["download"]["url"] = srv.url("/canary.tar.bz2")
        spec = parse_entry(entry)
        mgr = ModelManager(catalog_of(spec), models_dir, downloads_dir)
        log = ProgressLog()
        installed = mgr.install(spec.id, log, threading.Event())
        assert [r.range for r in srv.gets("/canary.tar.bz2")] == [None, "bytes=60000000-"]

    assert log.phases() == ["download", "verify", "extract"]
    assert len(log.calls) < 400  # throttled
    assert log.calls[-1] == ("extract", spec.disk_size, spec.disk_size)
    reference = TEST_DL / spec.download.root
    for role, rel in spec.files.items():
        path = installed.file(role)
        assert path.is_file(), role
        assert oct(path.stat().st_mode & 0o777) == oct(0o644)
        if reference.is_dir():
            assert filecmp.cmp(path, reference / rel, shallow=False), role
    assert sorted(os.listdir(installed.path)) == sorted([*spec.files.values(), MANIFEST])
    status = mgr.statuses()[0]
    manifest_size = (installed.path / MANIFEST).stat().st_size
    assert status.size_on_disk - manifest_size == spec.disk_size
    assert ls(downloads_dir) == []
    assert ls(models_dir) == [spec.id]
