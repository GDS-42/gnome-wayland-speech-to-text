"""Model metadata shared by the catalog, the installer and the engines.

A catalog entry (``ModelSpec``) is pure data: where to download a model, how
to verify it and which file plays which role for the engine family. Installed
models keep a copy of the spec they were installed from (``InstalledModel``),
so an engine never depends on the current catalog and updates are detected by
comparing ``revision`` values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("asr", "vad")
LANGUAGE_MODES = ("auto", "required", "optional")
FORMATS = ("tar.bz2", "file")


class CatalogError(ValueError):
    """Malformed catalog or model entry."""


@dataclass(frozen=True)
class Download:
    url: str
    sha256: str
    size: int  # bytes of the downloaded file
    format: str  # "tar.bz2" (archive) or "file" (single file, stored as files[*])
    root: str = ""  # top-level directory inside the archive, stripped on extraction


@dataclass(frozen=True)
class ModelSpec:
    id: str
    kind: str  # "asr" | "vad"
    name: str
    vendor: str
    family: str  # engine adapter: nemo_transducer, nemo_canary, whisper, ...
    revision: str  # changes when the published files change -> update available
    languages: tuple[str, ...]  # ISO 639-1 codes, ("*",) = many
    language_mode: str  # auto: detected; required: must be given; optional: either
    description: dict[str, str]  # locale -> text ("fr", "en")
    license: str
    homepage: str
    download: Download
    files: dict[str, str]  # role -> path relative to the model directory
    exclude: tuple[str, ...] = ()  # archive members (relative to root) not installed
    disk_size: int = 0  # bytes once installed
    ram_mb: int = 0
    speed: int = 3  # 1..5 UI hints
    accuracy: int = 3
    max_segment_s: float = 30.0  # longer audio is split with the VAD (0 = n/a)
    recommended: bool = False
    options: dict[str, Any] = field(default_factory=dict)  # family-specific knobs

    def supports(self, language: str) -> bool:
        return "*" in self.languages or language in self.languages

    def describe(self, locale: str = "fr") -> str:
        lang = (locale or "en").split("_")[0].split("-")[0]
        return self.description.get(lang) or self.description.get("en") or ""

    # -- (de)serialisation ------------------------------------------------- #

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ModelSpec:
        try:
            dl = d["download"]
            spec = cls(
                id=str(d["id"]),
                kind=str(d["kind"]),
                name=str(d["name"]),
                vendor=str(d.get("vendor", "")),
                family=str(d["family"]),
                revision=str(d["revision"]),
                languages=tuple(str(x) for x in d.get("languages", ())),
                language_mode=str(d.get("language_mode", "auto")),
                description={str(k): str(v) for k, v in d.get("description", {}).items()},
                license=str(d.get("license", "")),
                homepage=str(d.get("homepage", "")),
                download=Download(
                    url=str(dl["url"]),
                    sha256=str(dl["sha256"]).lower(),
                    size=int(dl["size"]),
                    format=str(dl["format"]),
                    root=str(dl.get("root", "")),
                ),
                files={str(k): str(v) for k, v in d["files"].items()},
                exclude=tuple(str(x) for x in d.get("exclude", ())),
                disk_size=int(d.get("disk_size", 0)),
                ram_mb=int(d.get("ram_mb", 0)),
                speed=int(d.get("speed", 3)),
                accuracy=int(d.get("accuracy", 3)),
                max_segment_s=float(d.get("max_segment_s", 30)),
                recommended=bool(d.get("recommended", False)),
                options=dict(d.get("options", {})),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise CatalogError(f"invalid model entry {d.get('id', '?')!r}: {exc}") from exc
        spec.validate()
        return spec

    def validate(self) -> None:
        def bad(msg: str) -> CatalogError:
            return CatalogError(f"model {self.id!r}: {msg}")

        if not self.id or "/" in self.id or self.id.startswith("."):
            raise bad("invalid id")
        if self.kind not in KINDS:
            raise bad(f"unknown kind {self.kind!r}")
        if self.language_mode not in LANGUAGE_MODES:
            raise bad(f"unknown language_mode {self.language_mode!r}")
        if self.download.format not in FORMATS:
            raise bad(f"unknown download format {self.download.format!r}")
        url = self.download.url
        local = url.startswith(("http://127.0.0.1", "http://localhost"))  # test servers
        if not (url.startswith("https://") or local):
            raise bad("download url must use https")
        if len(self.download.sha256) != 64:
            raise bad("sha256 must be 64 hex characters")
        if not self.files:
            raise bad("no files")
        for rel in self.files.values():
            p = Path(rel)
            if p.is_absolute() or ".." in p.parts:
                raise bad(f"unsafe file path {rel!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "vendor": self.vendor,
            "family": self.family,
            "revision": self.revision,
            "recommended": self.recommended,
            "languages": list(self.languages),
            "language_mode": self.language_mode,
            "description": dict(self.description),
            "license": self.license,
            "homepage": self.homepage,
            "download": {
                "url": self.download.url,
                "sha256": self.download.sha256,
                "size": self.download.size,
                "format": self.download.format,
                "root": self.download.root,
            },
            "files": dict(self.files),
            "exclude": list(self.exclude),
            "disk_size": self.disk_size,
            "ram_mb": self.ram_mb,
            "speed": self.speed,
            "accuracy": self.accuracy,
            "max_segment_s": self.max_segment_s,
            "options": dict(self.options),
        }


@dataclass(frozen=True)
class InstalledModel:
    spec: ModelSpec  # the spec this copy was installed from
    path: Path  # <models_dir>/<id>
    installed_at: str = ""  # ISO-8601

    @property
    def id(self) -> str:
        return self.spec.id

    def file(self, role: str) -> Path:
        """Absolute path of the file (or directory) playing ``role``."""
        try:
            return self.path / self.spec.files[role]
        except KeyError:
            raise KeyError(f"model {self.spec.id!r} has no {role!r} file") from None
