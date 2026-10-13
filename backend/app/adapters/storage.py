"""Storage adapter. Local: files under var/. Google Cloud: a Cloud Storage bucket (Cloud Run's filesystem is
ephemeral and per-instance). Both validate keys the same way."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from ..config import get_settings

_SAFE = re.compile(r"^[A-Za-z0-9_\-./]+$")


def _check_key(key: str) -> str:
    if not _SAFE.match(key) or ".." in key.split("/") or key.startswith("/"):
        raise ValueError("invalid storage key")
    return key


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        _check_key(key)
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


class GCSStorage:
    def __init__(self, bucket: str, prefix: str = "", client: Any = None):
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self._bucket = client.bucket(bucket)
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""

    def _blob(self, key: str):
        return self._bucket.blob(self.prefix + _check_key(key))

    def put(self, key: str, data: bytes) -> str:
        self._blob(key).upload_from_string(data)
        return key

    def get(self, key: str) -> bytes:
        return self._blob(key).download_as_bytes()

    def exists(self, key: str) -> bool:
        return self._blob(key).exists()


@lru_cache
def _gcs(bucket: str, prefix: str) -> GCSStorage:
    return GCSStorage(bucket, prefix)


def get_storage() -> Storage:
    s = get_settings()
    if s.storage_backend.strip().lower() == "gcs":
        if not s.gcs_bucket:
            raise RuntimeError("STORAGE_BACKEND=gcs requires GCS_BUCKET")
        return _gcs(s.gcs_bucket, s.gcs_prefix)
    return LocalStorage(s.var_path)
