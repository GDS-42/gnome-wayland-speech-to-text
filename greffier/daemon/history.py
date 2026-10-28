"""Recent transcriptions, kept in memory and optionally on disk (JSON lines)."""

from __future__ import annotations

import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class History:
    def __init__(self, path: Path, size: int = 50, persist: bool = False) -> None:
        self._path = path
        self._size = size
        self._persist = False
        self._items: deque[dict[str, Any]] = deque(maxlen=max(size, 1))
        self.configure(size, persist)

    def configure(self, size: int, persist: bool) -> None:
        self._size = size
        items = list(self._items)[-size:] if size else []
        self._items = deque(items, maxlen=max(size, 1))
        if persist and not self._persist:
            self._load()
        elif not persist and self._persist:
            # Privacy: switching persistence off erases what was written.
            self._path.unlink(missing_ok=True)
        self._persist = persist
        if persist:
            self._rewrite()

    def add(self, entry: dict[str, Any]) -> None:
        if self._size == 0:
            return
        self._items.append(entry)
        if self._persist:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                if self._file_lines() > 2 * self._size:
                    self._rewrite()
            except OSError as exc:
                log.warning("cannot write history: %s", exc)

    def items(self) -> list[dict[str, Any]]:
        """Newest first."""
        return list(reversed(self._items)) if self._size else []

    def clear(self) -> None:
        self._items.clear()
        self._path.unlink(missing_ok=True)

    # -- persistence --------------------------------------------------------- #

    def _file_lines(self) -> int:
        try:
            with self._path.open(encoding="utf-8") as fh:
                return sum(1 for _ in fh)
        except OSError:
            return 0

    def _load(self) -> None:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("cannot read history: %s", exc)
            return
        loaded = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                loaded.append(item)
        merged = loaded + [i for i in self._items if i not in loaded]
        self._items = deque(merged[-self._size:] if self._size else [], maxlen=max(self._size, 1))

    def _rewrite(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for item in self._items:
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
            os.replace(tmp, self._path)
        except OSError as exc:
            log.warning("cannot write history: %s", exc)
