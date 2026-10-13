"""Storage adapter. Local: files under var/. Later: Cloud Storage with the same interface."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from ..config import get_settings

_SAFE = re.compile(r"^[A-Za-z0-9_\-./]+$")


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        if not _SAFE.match(key) or ".." in key.split("/"):
            raise ValueError("invalid storage key")
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid storage key")
        return p

    def put(self, key: str, data: bytes) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


def get_storage() -> Storage:
    return LocalStorage(get_settings().var_path)
