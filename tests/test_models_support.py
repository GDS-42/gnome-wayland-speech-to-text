"""Helpers for the model tests: a local HTTP server and archive builders.

Imported by the other ``test_models_*`` modules; it contains no test itself
except a sanity check of the server.

``FileServer`` is a threaded HTTP/1.0 server on 127.0.0.1 with Range support
and per-resource failure modes: error statuses before success, connection
dropped mid-way, stalls (read timeouts), a gate that blocks the transfer
until the test releases it, ignored Range headers and lying Content-Length.
"""

from __future__ import annotations

import hashlib
import io
import random
import re
import socket
import tarfile
import threading
import time
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from greffier.models.catalog import Catalog, parse_entry
from greffier.models.types import ModelSpec

# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #


@dataclass
class Resource:
    data: bytes = b""
    file: Path | None = None  # serve this file instead of ``data``
    fail: list[int] = field(default_factory=list)  # statuses answered first, in order
    ignore_range: bool = False  # always answer 200 with the whole body
    drop_after: int | None = None  # cut the connection at this absolute offset...
    drops: int = 1  # ...this many times
    stall_after: int | None = None  # pause at this offset (once), for read timeouts
    stall: float = 0.0
    gate: threading.Event | None = None  # block at ``gate_after`` until set (once)
    gate_after: int = 0
    content_length: int | None = None  # lie about the length (200 answers), -1 = omit
    redirect: str | None = None  # answer 302 to this location instead

    def size(self) -> int:
        return self.file.stat().st_size if self.file else len(self.data)

    def read(self, start: int, end: int) -> bytes:
        if self.file is None:
            return self.data[start:end]
        with open(self.file, "rb") as fh:
            fh.seek(start)
            return fh.read(end - start)


@dataclass
class Request:
    method: str
    path: str
    range: str | None
    user_agent: str | None


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_HEAD(self) -> None:
        self._serve(head=True)

    def do_GET(self) -> None:
        self._serve(head=False)

    def _serve(self, head: bool) -> None:
        owner = self.server.owner
        owner.requests.append(Request(self.command, self.path, self.headers.get("Range"),
                                      self.headers.get("User-Agent")))
        res = owner.resources.get(self.path)
        if res is None:
            self.send_error(404)
            return
        if res.fail:
            self.send_error(res.fail.pop(0))
            return
        if res.redirect:
            self.send_response(302)
            self.send_header("Location", res.redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        size = res.size()
        start, status = 0, 200
        match = re.fullmatch(r"bytes=(\d+)-", self.headers.get("Range") or "")
        if match and not res.ignore_range:
            start = int(match.group(1))
            if start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        length = size - start
        if status == 200 and res.content_length is not None:
            length = res.content_length
        if length >= 0:  # content_length=-1: no header, the body ends when we close
            self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
        self.end_headers()
        if head:
            return

        stop = size
        cut = res.drop_after is not None and res.drops > 0 and start < res.drop_after < size
        if cut:
            res.drops -= 1
            stop = res.drop_after
        pos = start
        while pos < stop:
            limit = stop
            for mark in (res.gate_after if res.gate else None, res.stall_after):
                if mark is not None and pos < mark < limit:
                    limit = mark
            chunk = res.read(pos, min(limit, pos + 65536))
            self.wfile.write(chunk)
            pos += len(chunk)
            if res.gate is not None and pos == res.gate_after:
                self.wfile.flush()
                gate, res.gate = res.gate, None
                gate.wait(10)
            if res.stall_after is not None and pos == res.stall_after:
                self.wfile.flush()
                res.stall_after = None
                time.sleep(res.stall)
        self.wfile.flush()
        if cut:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    owner: FileServer

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # clients hanging up mid-transfer are expected in these tests


class FileServer:
    """``with FileServer() as srv: url = srv.add("/a", data)``."""

    def __init__(self) -> None:
        self.resources: dict[str, Resource] = {}
        self.requests: list[Request] = []
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.owner = self
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)

    def __enter__(self) -> FileServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}{path}"

    def add(self, path: str, data: bytes = b"", **options: Any) -> str:
        self.resources[path] = Resource(data=data, **options)
        return self.url(path)

    def gets(self, path: str) -> list[Request]:
        return [r for r in self.requests if r.path == path and r.method == "GET"]


# --------------------------------------------------------------------------- #
# Archives and catalog entries
# --------------------------------------------------------------------------- #


@dataclass
class Entry:
    name: str
    data: bytes = b""
    type: bytes = tarfile.REGTYPE
    mode: int = 0o644
    linkname: str = ""


def F(name: str, data: bytes = b"x", mode: int = 0o644) -> Entry:
    return Entry(name, data, tarfile.REGTYPE, mode)


def D(name: str, mode: int = 0o755) -> Entry:
    return Entry(name, b"", tarfile.DIRTYPE, mode)


def tar_bz2(entries: Iterable[Entry]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for entry in entries:
            info = tarfile.TarInfo(entry.name)
            info.type = entry.type
            info.mode = entry.mode
            info.linkname = entry.linkname
            if entry.type == tarfile.REGTYPE:
                info.size = len(entry.data)
                tar.addfile(info, io.BytesIO(entry.data))
            else:
                tar.addfile(info)
    return buf.getvalue()


def noise(size: int, seed: int = 0) -> bytes:
    """Incompressible bytes, so archives are as big as their content."""
    return random.Random(seed).randbytes(size)


ROOT = "sherpa-onnx-test-model"


def model_archive(encoder: bytes = b"encoder", tokens: bytes = b"a 0\nb 1\n",
                  root: str = ROOT) -> tuple[bytes, int]:
    """A sherpa-like archive and its installed size (test_wavs excluded)."""
    data = tar_bz2([
        D(f"{root}/"),
        F(f"{root}/encoder.int8.onnx", encoder),
        F(f"{root}/tokens.txt", tokens),
        F(f"{root}/run.sh", b"#!/bin/sh\n", mode=0o775),
        D(f"{root}/test_wavs/"),
        F(f"{root}/test_wavs/en.wav", b"RIFF" + b"\0" * 100),
    ])
    return data, len(encoder) + len(tokens) + len(b"#!/bin/sh\n")


def entry_dict(url: str, data: bytes, *, model_id: str = "test-model",
               revision: str = "2026-01-01", root: str = ROOT,
               files: dict[str, str] | None = None, fmt: str = "tar.bz2",
               exclude: Iterable[str] = ("test_wavs",), disk_size: int | None = None,
               kind: str = "asr", size: int | None = None,
               sha256: str | None = None) -> dict[str, Any]:
    if files is None:
        files = {"model": "model.onnx"} if fmt == "file" else {
            "encoder": "encoder.int8.onnx", "tokens": "tokens.txt"}
    return {
        "id": model_id,
        "kind": kind,
        "name": f"Test {model_id}",
        "vendor": "Tests",
        "family": "silero_vad" if kind == "vad" else "nemo_canary",
        "revision": revision,
        "languages": ["fr"],
        "language_mode": "auto",
        "description": {"en": "test"},
        "license": "MIT",
        "homepage": "https://example.org",
        "download": {
            "url": url,
            "sha256": sha256 or hashlib.sha256(data).hexdigest(),
            "size": len(data) if size is None else size,
            "format": fmt,
            "root": root if fmt == "tar.bz2" else "",
        },
        "files": files,
        "exclude": list(exclude),
        "disk_size": len(data) if disk_size is None else disk_size,
    }


def make_spec(url: str, data: bytes, **options: Any) -> ModelSpec:
    return parse_entry(entry_dict(url, data, **options))


def catalog_of(*specs: ModelSpec, revision: str = "2026-01-01.1") -> Catalog:
    return Catalog(specs, revision, source="test")


class ProgressLog:
    """A ProgressFn recording its calls; optionally runs a hook on each call."""

    def __init__(self, hook: Any = None) -> None:
        self.calls: list[tuple[str, int, int]] = []
        self.hook = hook

    def __call__(self, phase: str, done: int, total: int) -> None:
        self.calls.append((phase, done, total))
        if self.hook is not None:
            self.hook(phase, done, total)

    def phases(self) -> list[str]:
        out: list[str] = []
        for phase, _done, _total in self.calls:
            if not out or out[-1] != phase:
                out.append(phase)
        return out


# --------------------------------------------------------------------------- #
# Sanity check of the server itself
# --------------------------------------------------------------------------- #


def test_server_supports_ranges() -> None:
    with FileServer() as srv:
        url = srv.add("/blob", b"0123456789")
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"Range": "bytes=4-"}), timeout=5) as resp:
            assert resp.status == 206
            assert resp.headers["Content-Range"] == "bytes 4-9/10"
            assert resp.read() == b"456789"
