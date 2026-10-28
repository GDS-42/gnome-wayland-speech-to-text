"""Tests for tools/catalog_tool.py (maintainer CLI), against a local server."""

from __future__ import annotations

import importlib.util
import json
import tarfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from greffier.models.catalog import Catalog, parse_entry
from tests.test_models_support import (
    ROOT,
    D,
    Entry,
    F,
    FileServer,
    entry_dict,
    model_archive,
    noise,
    tar_bz2,
)

TOOL = Path(__file__).resolve().parents[1] / "tools" / "catalog_tool.py"


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("catalog_tool", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server():
    with FileServer() as srv:
        yield srv


def run(tool: ModuleType, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Any]:
    code = tool.main(list(argv))
    out, err = capsys.readouterr()
    return code, (out, err)


def test_entry_for_archive(tool: ModuleType, server: FileServer,
                           capsys: pytest.CaptureFixture[str]) -> None:
    data, disk_size = model_archive(encoder=b"E" * 1000)
    url = server.add("/sherpa-onnx-test-model.tar.bz2", data)
    code, (out, err) = run(tool, capsys, "entry", url, "--id", "test-model",
                           "--family", "nemo_canary", "--languages", "fr,en")
    assert code == 0
    entry = json.loads(out)
    spec = parse_entry(entry)  # a valid catalog entry
    assert spec.download.size == len(data)
    assert spec.download.root == ROOT
    assert spec.download.format == "tar.bz2"
    assert spec.disk_size == disk_size  # test_wavs excluded
    assert spec.exclude == ("test_wavs",)
    assert spec.languages == ("fr", "en")
    assert entry["files"] == {"encoder": "encoder.int8.onnx", "decoder": "TODO",
                              "tokens": "tokens.txt"}
    assert "test_wavs/en.wav  (excluded)" in err
    assert "encoder.int8.onnx" in err


def test_entry_guesses_roles(tool: ModuleType) -> None:
    files = ["turbo-encoder.onnx", "turbo-encoder.int8.onnx", "turbo-decoder.int8.onnx",
             "turbo-tokens.txt", "conv_frontend.onnx", "tokenizer/vocab.json"]
    assert tool.guess_roles("whisper", files, ["tokenizer"]) == {
        "encoder": "turbo-encoder.int8.onnx", "decoder": "turbo-decoder.int8.onnx",
        "tokens": "turbo-tokens.txt"}
    assert tool.guess_roles("qwen3_asr", files, ["tokenizer"])["tokenizer"] == "tokenizer"


def test_entry_for_single_file(tool: ModuleType, server: FileServer,
                               capsys: pytest.CaptureFixture[str]) -> None:
    data = noise(3000)
    url = server.add("/silero_vad.onnx", data)
    code, (out, _err) = run(tool, capsys, "entry", url, "--id", "silero-vad",
                            "--family", "silero_vad", "--kind", "vad")
    assert code == 0
    spec = parse_entry(json.loads(out))
    assert spec.download.format == "file"
    assert spec.files == {"model": "silero_vad.onnx"}
    assert spec.disk_size == len(data)


def test_entry_refuses_unsafe_archive(tool: ModuleType, server: FileServer,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    data = tar_bz2([D(f"{ROOT}/"), F(f"{ROOT}/encoder.onnx"),
                    Entry(f"{ROOT}/link", type=tarfile.SYMTYPE, linkname="/etc/passwd")])
    url = server.add("/evil.tar.bz2", data)
    code, (out, err) = run(tool, capsys, "entry", url, "--id", "x", "--family", "whisper")
    assert code == 1
    assert out == ""
    assert "would refuse" in err


def write_catalog(path: Path, models: list[dict[str, Any]],
                  revision: str = "2026-10-28.1") -> Path:
    path.write_text(json.dumps({"schema": 1, "revision": revision, "min_app_version": "0.1.0",
                                "models": models}))
    return path


def test_check_ok(tool: ModuleType, server: FileServer, tmp_path: Path,
                  capsys: pytest.CaptureFixture[str]) -> None:
    data, _ = model_archive()
    vad = noise(500)
    catalog = write_catalog(tmp_path / "catalog.json", [
        entry_dict(server.add("/m.tar.bz2", data), data),
        entry_dict(server.add("/vad.onnx", vad), vad, model_id="vad", kind="vad", fmt="file"),
    ])
    code, (out, _err) = run(tool, capsys, "check", str(catalog), "--sha", "--allow-loopback")
    assert code == 0, out
    assert "all good" in out
    assert [r.method for r in server.requests].count("HEAD") == 2


def test_check_reports_problems(tool: ModuleType, server: FileServer, tmp_path: Path,
                                capsys: pytest.CaptureFixture[str]) -> None:
    data, _ = model_archive()
    wrong_size = entry_dict(server.add("/a.tar.bz2", data), data, model_id="wrong-size",
                            size=len(data) + 1)
    missing = entry_dict(server.url("/missing.tar.bz2"), data, model_id="missing")
    bad_sha = entry_dict(server.add("/b.tar.bz2", data), data, model_id="bad-sha",
                         sha256="0" * 64)
    invalid = entry_dict("http://example.org/x", data, model_id="plain-http")
    good = entry_dict(server.add("/c.tar.bz2", data), data, model_id="good")
    catalog = write_catalog(tmp_path / "catalog.json",
                            [wrong_size, missing, bad_sha, invalid, good, good],
                            revision="2026-10-28")
    code, (out, _err) = run(tool, capsys, "check", str(catalog), "--sha", "--allow-loopback")
    assert code == 1
    for needle in ("FAIL revision", "FAIL wrong-size", "FAIL missing", "FAIL bad-sha",
                   "plain-http", "duplicate id 'good'", "ok   good"):
        assert needle in out, needle
    assert "6 problem(s)" in out


def test_check_refuses_loopback_urls_by_default(tool: ModuleType, server: FileServer,
                                                tmp_path: Path,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    data, _ = model_archive()
    catalog = write_catalog(tmp_path / "catalog.json", [
        entry_dict(server.add("/m.tar.bz2", data), data),
        entry_dict("https://example.org/x.tar.bz2", data, model_id="other")])
    code, (out, _err) = run(tool, capsys, "check", str(catalog), "--offline")
    assert code == 1
    assert "must use https" in out


def test_check_builtin_offline(tool: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    code, (out, _err) = run(tool, capsys, "check", "--offline")
    assert code == 0, out
    assert out.count("ok   ") == len(Catalog.load())


def test_check_unreadable_catalog(tool: ModuleType, tmp_path: Path,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "bad.json").write_text("{")
    code, (out, _err) = run(tool, capsys, "check", str(tmp_path / "bad.json"))
    assert code == 1
    assert out.startswith("FAIL")
