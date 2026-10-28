"""Tests for greffier.models.catalog: loading, merging by revision, remote fetch."""

from __future__ import annotations

import json
import logging
import os
import socket
from pathlib import Path
from typing import Any

import pytest

from greffier import paths
from greffier.models import catalog as catalog_mod
from greffier.models import download
from greffier.models.catalog import Catalog, parse_entry, revision_key, valid_model_id
from greffier.models.types import CatalogError
from tests.test_models_support import FileServer, entry_dict

BUILTIN = json.loads(paths.package_data("catalog.json").read_text(encoding="utf-8"))


def remote_entry(model_id: str = "remote-model", **options: Any) -> dict[str, Any]:
    return entry_dict("https://example.org/model.tar.bz2", b"payload", model_id=model_id,
                      **options)


def catalog_doc(revision: str = "2099-01-01.1", models: list[Any] | None = None,
                **extra: Any) -> dict[str, Any]:
    doc = {"schema": 1, "revision": revision, "min_app_version": "0.1.0",
           "models": [remote_entry()] if models is None else models}
    doc.update(extra)
    return doc


def write_doc(path: Path, doc: Any) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


@pytest.fixture
def server():
    with FileServer() as srv:
        yield srv


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "catalog.json"


# --------------------------------------------------------------------------- #
# Built-in catalog and revisions
# --------------------------------------------------------------------------- #


def test_builtin_catalog_is_valid_and_ordered() -> None:
    catalog = Catalog.load()
    assert catalog.revision == BUILTIN["revision"]
    assert catalog.source == "builtin"
    assert [m.id for m in catalog.models()] == [e["id"] for e in BUILTIN["models"]]
    assert [m.id for m in catalog.models("vad")] == ["silero-vad"]
    assert all(m.kind == "asr" for m in catalog.models("asr"))
    assert len(catalog.models("asr")) == len(catalog) - 1
    assert catalog.get("silero-vad").download.format == "file"
    assert catalog.get("does-not-exist") is None


@pytest.mark.parametrize(("lower", "higher"), [
    ("2026-10-28.9", "2026-10-28.10"),
    ("2026-10-28", "2026-10-28.1"),
    ("2026-09-30.7", "2026-10-01.1"),
    ("2025-12-31.99", "2026-01-01.1"),
    ("0.9.1", "0.10.0"),
    ("v4", "v5"),
])
def test_revision_key_is_natural(lower: str, higher: str) -> None:
    assert revision_key(lower) < revision_key(higher)
    assert revision_key("2026-01-01.01") == revision_key("2026-01-01.1")


def test_valid_model_id() -> None:
    assert valid_model_id("parakeet-tdt-0.6b-v3-int8")
    for bad in ("", ".hidden", "../x", "a/b", "a\\b", "a b", None, 3, "x" * 200):
        assert not valid_model_id(bad)


# --------------------------------------------------------------------------- #
# Merging built-in and cached catalogs
# --------------------------------------------------------------------------- #


def test_cache_with_higher_revision_wins(cache: Path) -> None:
    cache.parent.mkdir(parents=True)
    write_doc(cache, catalog_doc("2099-01-01.1"))
    catalog = Catalog.load(cache)
    assert catalog.revision == "2099-01-01.1"
    assert catalog.get("remote-model") is not None
    assert catalog.get("parakeet-tdt-0.6b-v3-int8") is None  # whole catalogs, not entries


@pytest.mark.parametrize("revision", ["2000-01-01.1", BUILTIN["revision"]])
def test_cache_with_lower_or_equal_revision_loses(cache: Path, revision: str) -> None:
    cache.parent.mkdir(parents=True)
    write_doc(cache, catalog_doc(revision))
    catalog = Catalog.load(cache)
    assert catalog.source == "builtin"
    assert catalog.get("remote-model") is None


def test_revisions_compare_numerically(cache: Path, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    builtin = write_doc(tmp_path / "builtin.json",
                        catalog_doc("2026-10-28.9", [remote_entry("old-model")]))
    monkeypatch.setattr(catalog_mod.paths, "package_data", lambda name: builtin)
    cache.parent.mkdir(parents=True)
    write_doc(cache, catalog_doc("2026-10-28.10", [remote_entry("new-model")]))
    assert Catalog.load(cache).get("new-model") is not None  # "…10" > "…9"


def test_missing_cache_is_fine(cache: Path) -> None:
    assert Catalog.load(cache).source == "builtin"


@pytest.mark.parametrize("doc", [
    catalog_doc(schema=2),
    catalog_doc(schema="1"),
    catalog_doc(min_app_version="99.0"),
    catalog_doc(min_app_version="0.10.0"),
    catalog_doc(revision=""),
    catalog_doc(models={"a": 1}),
    catalog_doc(models=[]),
    [1, 2, 3],
])
def test_unusable_cache_is_ignored(cache: Path, doc: Any,
                                   caplog: pytest.LogCaptureFixture) -> None:
    cache.parent.mkdir(parents=True)
    write_doc(cache, doc)
    with caplog.at_level(logging.WARNING):
        catalog = Catalog.load(cache)
    assert catalog.source == "builtin"
    assert "ignoring cached model catalog" in caplog.text


@pytest.mark.parametrize("raw", [b"{not json", b"\xff\xfe garbage", b""])
def test_corrupt_cache_is_ignored(cache: Path, raw: bytes) -> None:
    cache.parent.mkdir(parents=True)
    cache.write_bytes(raw)
    assert Catalog.load(cache).source == "builtin"


def test_invalid_entries_are_skipped(cache: Path, caplog: pytest.LogCaptureFixture) -> None:
    no_files = remote_entry("no-files")
    del no_files["files"]
    entries = [
        remote_entry("good"),
        entry_dict("http://example.org/m.tar.bz2", b"x", model_id="plain-http"),
        remote_entry("bad-sha", sha256="z" * 64),
        remote_entry("short-sha", sha256="ab" * 10),
        no_files,
        remote_entry("../escape"),
        remote_entry(".hidden"),
        remote_entry("unsafe-file", files={"encoder": "../../etc/passwd"}),
        remote_entry("bad-kind", kind="tts"),
        remote_entry("good"),  # duplicate
        "not an object",
        remote_entry("good-2", revision="2026-02-02"),
    ]
    cache.parent.mkdir(parents=True)
    write_doc(cache, catalog_doc(models=entries))
    with caplog.at_level(logging.WARNING):
        catalog = Catalog.load(cache)
    assert [m.id for m in catalog.models()] == ["good", "good-2"]
    assert caplog.text.count("skipping") == len(entries) - 2


def test_app_catalogs_refuse_loopback_urls(cache: Path, server: FileServer) -> None:
    # ModelSpec tolerates http://127.0.0.1 for test servers; the app's catalogs do not.
    loopback = entry_dict(server.url("/m.tar.bz2"), b"x", model_id="loopback")
    assert parse_entry(loopback).id == "loopback"
    with pytest.raises(CatalogError, match="https"):
        parse_entry(loopback, https_only=True)
    cache.parent.mkdir(parents=True)
    write_doc(cache, catalog_doc(models=[loopback, remote_entry()]))
    assert [m.id for m in Catalog.load(cache).models()] == ["remote-model"]
    url = server.add("/catalog.json", json.dumps(catalog_doc(models=[loopback])).encode())
    with pytest.raises(CatalogError):
        Catalog.fetch_remote(url, cache)


def test_parse_entry_rejects_single_file_without_model_role() -> None:
    entry = entry_dict("https://example.org/vad.onnx", b"x", fmt="file",
                       files={"weights": "vad.onnx"})
    with pytest.raises(CatalogError):
        parse_entry(entry)


def test_unusable_builtin_falls_back_to_cache(cache: Path, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_mod.paths, "package_data", lambda name: tmp_path / "missing")
    with pytest.raises(CatalogError):
        Catalog.load(cache)
    cache.parent.mkdir(parents=True)
    write_doc(cache, catalog_doc("2000-01-01.1"))
    assert Catalog.load(cache).revision == "2000-01-01.1"


# --------------------------------------------------------------------------- #
# Remote catalog
# --------------------------------------------------------------------------- #


def test_fetch_remote_writes_cache_and_returns_merged(server: FileServer, cache: Path) -> None:
    raw = json.dumps(catalog_doc("2099-01-01.1")).encode()
    url = server.add("/catalog.json", raw)
    catalog = Catalog.fetch_remote(url, cache)
    assert catalog.revision == "2099-01-01.1"
    assert cache.read_bytes() == raw
    assert server.requests[0].user_agent == download.USER_AGENT
    assert download.USER_AGENT.startswith("Greffier/")
    assert Catalog.load(cache).revision == "2099-01-01.1"


def test_fetch_remote_older_revision_keeps_builtin(server: FileServer, cache: Path) -> None:
    url = server.add("/catalog.json", json.dumps(catalog_doc("2000-01-01.1")).encode())
    assert Catalog.fetch_remote(url, cache).source == "builtin"
    assert cache.exists()


@pytest.mark.parametrize("raw", [
    b"<html>captive portal</html>",
    json.dumps(catalog_doc(min_app_version="99.0")).encode(),
    json.dumps(catalog_doc(schema=2)).encode(),
    json.dumps(catalog_doc(models=["junk"])).encode(),
    b" " * (2 * 1024 * 1024 + 1),
])
def test_fetch_remote_rejects_bad_documents(server: FileServer, cache: Path, raw: bytes) -> None:
    cache.parent.mkdir(parents=True)
    previous = json.dumps(catalog_doc("2098-01-01.1")).encode()
    cache.write_bytes(previous)
    url = server.add("/catalog.json", raw)
    with pytest.raises(CatalogError):
        Catalog.fetch_remote(url, cache)
    assert cache.read_bytes() == previous
    assert os.listdir(cache.parent) == ["catalog.json"]


@pytest.mark.parametrize("status", [404, 503])
def test_fetch_remote_http_errors(server: FileServer, cache: Path, status: int) -> None:
    url = server.add("/catalog.json", b"{}", fail=[status])
    with pytest.raises(OSError):
        Catalog.fetch_remote(url, cache)
    assert not cache.exists()


def test_fetch_remote_connection_refused(cache: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(OSError):
        Catalog.fetch_remote(f"http://127.0.0.1:{port}/catalog.json", cache, timeout=2)


@pytest.mark.parametrize("url", [
    "http://example.org/catalog.json",
    "file:///etc/passwd",
    "ftp://example.org/catalog.json",
    "not a url",
])
def test_fetch_remote_requires_https(cache: Path, url: str) -> None:
    with pytest.raises(CatalogError):
        Catalog.fetch_remote(url, cache)


def test_fetch_remote_cache_write_is_atomic(server: FileServer, cache: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    cache.parent.mkdir(parents=True)
    previous = json.dumps(catalog_doc("2098-01-01.1")).encode()
    cache.write_bytes(previous)
    url = server.add("/catalog.json", json.dumps(catalog_doc("2099-01-01.1")).encode())

    def broken_replace(src: Any, dst: Any) -> None:
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch, pytest.raises(OSError):
        patch.setattr(catalog_mod.os, "replace", broken_replace)
        Catalog.fetch_remote(url, cache)
    assert cache.read_bytes() == previous
    assert os.listdir(cache.parent) == ["catalog.json"]  # no temporary file left
    assert Catalog.load(cache).revision == "2098-01-01.1"
