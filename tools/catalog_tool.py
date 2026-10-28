#!/usr/bin/env python3
"""Maintainer helper for ``greffier/data/catalog.json``.

Print a catalog entry for a new model archive::

    python tools/catalog_tool.py entry URL --id ID --family FAMILY [--name ...]

The archive is downloaded (or read with ``--file PATH``) and inspected with the
same safety rules as the application; the JSON entry (size, sha256, archive
root, disk_size excluding ``--exclude`` prefixes, guessed file roles) goes to
stdout, the archive listing to stderr. Review the roles and fill the TODOs.

Check a catalog before publishing it::

    python tools/catalog_tool.py check [CATALOG] [--sha] [--offline]

Validates the document like the application does, then checks that every
download URL answers with the expected Content-Length (HEAD, following
redirects). ``--sha`` also downloads every file and verifies its SHA-256;
``--offline`` only validates. The exit status is 1 if anything is wrong.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
import tarfile
import tempfile
import time
import urllib.parse
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from greffier.models import download as http  # noqa: I001 - needs the path set above
from greffier.models.archive import ArchiveError, check_member
from greffier.models.catalog import Catalog, parse_entry
from greffier.models.types import CatalogError

DEFAULT_CATALOG = REPO / "greffier" / "data" / "catalog.json"

#: File roles expected by each engine family (see docs/ARCHITECTURE.md §5).
ROLES: dict[str, list[str]] = {
    "nemo_transducer": ["encoder", "decoder", "joiner", "tokens"],
    "nemo_canary": ["encoder", "decoder", "tokens"],
    "whisper": ["encoder", "decoder", "tokens"],
    "cohere_transcribe": ["encoder", "decoder", "tokens"],
    "qwen3_asr": ["conv_frontend", "encoder", "decoder", "tokenizer"],
    "silero_vad": ["model"],
}


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def fmt_size(size: int) -> str:
    return f"{size:,} bytes ({http.human_size(size)})"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def stream(url: str, dest: Path | None = None, quiet: bool = False) -> tuple[int, str]:
    """GET ``url`` (saving it to ``dest`` if given); return (size, sha256)."""
    hasher = hashlib.sha256()
    size = 0
    last = 0.0
    with http.open_url(url, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        out = open(dest, "wb") if dest else None  # noqa: SIM115 - closed below
        try:
            while chunk := response.read1(1 << 20):
                hasher.update(chunk)
                size += len(chunk)
                if out:
                    out.write(chunk)
                if not quiet and time.monotonic() - last > 2:
                    last = time.monotonic()
                    pct = f" {100 * size / total:.0f} %" if total else ""
                    eprint(f"  {http.human_size(size)}{pct}")
        finally:
            if out:
                out.close()
    return size, hasher.hexdigest()


def remote_size(url: str) -> int | None:
    """Content-Length of ``url`` (HEAD, or a 1-byte Range GET as a fallback)."""
    with http.open_url(url, method="HEAD") as response:
        length = response.headers.get("Content-Length")
    if length and length.isdigit():
        return int(length)
    with http.open_url(url, headers={"Range": "bytes=0-0"}) as response:
        match = re.search(r"/(\d+)$", response.headers.get("Content-Range") or "")
    return int(match.group(1)) if match else None


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #


def find_root(names: list[str]) -> str:
    """The single top-level directory of an archive, or ""."""
    parts = [PurePosixPath(n).parts for n in names]
    parts = [p for p in parts if p]
    tops = {p[0] for p in parts}
    if len(tops) == 1 and any(len(p) > 1 for p in parts):
        return tops.pop()
    return ""


def analyse_archive(path: Path, exclude: Sequence[str]) -> dict[str, Any]:
    """Inspect a tar.bz2 with the application's rules; raise ArchiveError."""
    with tarfile.open(path, "r:bz2") as tar:
        members = tar.getmembers()
    root = find_root([m.name for m in members])
    files: dict[str, int] = {}
    dirs: set[str] = set()
    excluded: dict[str, int] = {}
    for member in members:
        parts = check_member(member, root, exclude)
        if parts is None:
            rel = PurePosixPath(member.name).parts[1 if root else 0:]
            if member.isreg() and rel:
                excluded["/".join(rel)] = member.size
            continue
        rel = "/".join(parts)
        if member.isdir():
            dirs.add(rel)
        else:
            files[rel] = member.size
            dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
    present = sorted(p for p in exclude
                     if any(x == p or x.startswith(p + "/") for x in excluded))
    return {"root": root, "files": files, "dirs": sorted(dirs), "excluded": excluded,
            "exclude": present, "disk_size": sum(files.values())}


def guess_roles(family: str, files: Sequence[str], dirs: Sequence[str],
                roles: Sequence[str] | None = None) -> dict[str, str]:
    """Best guess of role -> path; "TODO" where nothing matches."""
    result: dict[str, str] = {}
    for role in roles or ROLES.get(family, ["model"]):
        pool = list(dirs) if role == "tokenizer" else list(files)
        candidates = [p for p in pool if role in PurePosixPath(p).name.lower()]
        if role == "tokens":
            candidates = [p for p in candidates if p.endswith(".txt")] or candidates
        if role in ("encoder", "decoder", "joiner", "model", "conv_frontend"):
            candidates = [p for p in candidates if p.endswith(".onnx")] or candidates
        candidates.sort(key=lambda p: ("int8" not in p, p.count("/"), len(p)))
        result[role] = candidates[0] if candidates else "TODO"
    return result


def cmd_entry(args: argparse.Namespace) -> int:
    url = args.url
    if not http.is_allowed_url(url):
        eprint(f"error: {url} is not an HTTPS URL")
        return 1
    fmt = args.format or ("tar.bz2" if url.endswith(".tar.bz2") else "file")
    with tempfile.TemporaryDirectory(prefix="greffier-catalog-") as tmp:
        if args.file:
            path = Path(args.file)
            hasher = hashlib.sha256()
            with open(path, "rb") as fh:
                while chunk := fh.read(1 << 20):
                    hasher.update(chunk)
            size, sha256 = path.stat().st_size, hasher.hexdigest()
        else:
            path = Path(args.keep) if args.keep else Path(tmp) / "download"
            eprint(f"downloading {url}")
            size, sha256 = stream(url, path)
        eprint(f"size {fmt_size(size)}, sha256 {sha256}")

        if fmt == "tar.bz2":
            try:
                info = analyse_archive(path, args.exclude)
            except (ArchiveError, tarfile.TarError, OSError, EOFError) as exc:
                eprint(f"error: the application would refuse this archive: {exc}")
                return 1
            root, disk_size, exclude = info["root"], info["disk_size"], info["exclude"]
            eprint(f"root: {root or '(none)'}")
            for rel, member_size in sorted(info["files"].items()):
                eprint(f"  {member_size:>14,}  {rel}")
            for rel, member_size in sorted(info["excluded"].items()):
                eprint(f"  {member_size:>14,}  {rel}  (excluded)")
            files = guess_roles(args.family, list(info["files"]), info["dirs"], args.role)
        else:
            root, disk_size, exclude = "", size, []
            name = PurePosixPath(urllib.parse.urlsplit(url).path).name or "model.onnx"
            files = {"model": name}
        eprint(f"disk_size: {fmt_size(disk_size)}")

    entry = {
        "id": args.id,
        "kind": args.kind,
        "name": args.name or args.id,
        "vendor": args.vendor,
        "family": args.family,
        "revision": args.revision or datetime.date.today().isoformat(),
        "recommended": False,
        "languages": [x for x in args.languages.split(",") if x],
        "language_mode": args.language_mode,
        "description": {"fr": "TODO", "en": "TODO"},
        "license": args.license,
        "homepage": args.homepage,
        "download": {"url": url, "sha256": sha256, "size": size, "format": fmt,
                     "root": root},
        "files": files,
        "exclude": exclude,
        "disk_size": disk_size,
        "ram_mb": 0,
        "speed": 3,
        "accuracy": 3,
        "max_segment_s": 30,
        "options": {},
    }
    if "TODO" in files.values():
        eprint("warning: some file roles could not be guessed (TODO); use --role or edit")
    try:
        parse_entry(entry)
    except CatalogError as exc:
        eprint(f"warning: the entry does not validate yet: {exc}")
    print(json.dumps(entry, indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.catalog) if args.catalog else DEFAULT_CATALOG
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
        catalog = Catalog.from_dict(doc, source=str(path), https_only=not args.allow_loopback)
    except (OSError, ValueError, CatalogError) as exc:
        print(f"FAIL {path}: {exc}")
        return 1
    problems = 0
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", catalog.revision):
        print(f"FAIL revision {catalog.revision!r} is not YYYY-MM-DD.N")
        problems += 1
    print(f"catalog {path}: revision {catalog.revision}, "
          f"min_app_version {catalog.min_app_version or '-'}")

    # Catalog.from_dict skips bad entries (as the application does); here they are errors.
    seen: set[str] = set()
    specs = []
    for index, entry in enumerate(doc["models"]):
        try:
            spec = parse_entry(entry, https_only=not args.allow_loopback)
        except CatalogError as exc:
            print(f"FAIL entry #{index}: {exc}")
            problems += 1
            continue
        if spec.id in seen:
            print(f"FAIL entry #{index}: duplicate id {spec.id!r}")
            problems += 1
            continue
        seen.add(spec.id)
        specs.append(spec)

    for spec in specs:
        dl = spec.download
        if args.offline:
            print(f"ok   {spec.id} (not checked online)")
            continue
        try:
            length = remote_size(dl.url)
        except (OSError, ValueError) as exc:
            print(f"FAIL {spec.id}: {dl.url}: {exc}")
            problems += 1
            continue
        if length != dl.size:
            print(f"FAIL {spec.id}: Content-Length {length} != size {dl.size}")
            problems += 1
            continue
        if args.sha:
            try:
                size, sha256 = stream(dl.url, quiet=True)
            except (OSError, ValueError) as exc:
                print(f"FAIL {spec.id}: download failed: {exc}")
                problems += 1
                continue
            if (size, sha256) != (dl.size, dl.sha256):
                print(f"FAIL {spec.id}: got {size} bytes with sha256 {sha256}")
                problems += 1
                continue
        print(f"ok   {spec.id}: {fmt_size(dl.size)}{', sha256 verified' if args.sha else ''}")
    print("all good" if not problems else f"{problems} problem(s)")
    return 1 if problems else 0


# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    entry = sub.add_parser("entry", help="print a catalog entry for an archive URL")
    entry.add_argument("url")
    entry.add_argument("--id", required=True)
    entry.add_argument("--family", required=True, help=", ".join(ROLES))
    entry.add_argument("--kind", default="asr", choices=["asr", "vad"])
    entry.add_argument("--name", default="")
    entry.add_argument("--vendor", default="")
    entry.add_argument("--revision", default="", help="default: today")
    entry.add_argument("--languages", default="", help="comma-separated ISO 639-1 codes")
    entry.add_argument("--language-mode", default="auto",
                       choices=["auto", "required", "optional"])
    entry.add_argument("--license", default="TODO")
    entry.add_argument("--homepage", default="")
    entry.add_argument("--format", choices=["tar.bz2", "file"],
                       help="default: from the URL suffix")
    entry.add_argument("--exclude", action="append", default=None,
                       help="archive prefix not installed (repeatable, default test_wavs)")
    entry.add_argument("--role", action="append", default=None,
                       help="file role to look for (repeatable, default: from --family)")
    entry.add_argument("--file", help="use this local copy instead of downloading")
    entry.add_argument("--keep", help="save the download to this path")
    entry.set_defaults(func=cmd_entry)

    check = sub.add_parser("check", help="validate a catalog and its download URLs")
    check.add_argument("catalog", nargs="?", help=f"default: {DEFAULT_CATALOG}")
    check.add_argument("--sha", action="store_true", help="download and verify SHA-256")
    check.add_argument("--offline", action="store_true", help="only validate the JSON")
    check.add_argument("--allow-loopback", action="store_true",
                       help="accept http://127.0.0.1 URLs (test servers)")
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "exclude", "unset") is None:
        args.exclude = ["test_wavs"]
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
