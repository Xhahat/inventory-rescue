"""Activity journal: a short, append-only feed of what people did (messages read, signals confirmed or rejected,
plans built and approved, questions asked). Local: a JSON-lines file under var/. Google Cloud: a Firestore
collection, so every Cloud Run instance sees the same feed. Writing to the journal never fails a request."""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

from ..config import get_settings

log = logging.getLogger(__name__)

KINDS = {"message", "signal_confirmed", "signal_rejected", "plan_created", "plan_approved", "question", "import"}
_MAX_TEXT = 300


class Journal(Protocol):
    backend: str

    def add(self, entry: dict) -> None: ...
    def recent(self, limit: int) -> list[dict]: ...


class LocalJournal:
    backend = "local"

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def add(self, entry: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def recent(self, limit: int) -> list[dict]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in reversed(lines):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(out) >= limit:
                break
        return out


class FirestoreJournal:
    backend = "firestore"

    def __init__(self, collection: str, project: str | None = None, database: str = "(default)", client: Any = None):
        if client is None:
            from google.cloud import firestore

            client = firestore.Client(project=project, database=database)
        self._col = client.collection(collection)

    def add(self, entry: dict) -> None:
        self._col.add(entry, timeout=5)

    def recent(self, limit: int) -> list[dict]:
        q = self._col.order_by("ts", direction="DESCENDING").limit(limit)
        return [d.to_dict() for d in q.stream(timeout=10)]


@lru_cache
def _firestore(collection: str, project: str | None, database: str) -> FirestoreJournal:
    return FirestoreJournal(collection, project, database)


def get_journal() -> Journal:
    s = get_settings()
    if s.activity_backend.strip().lower() == "firestore":
        return _firestore(s.firestore_collection, s.gcp_project, s.firestore_database)
    return LocalJournal(s.var_path / "activity.jsonl")


def record(kind: str, title: str, detail: str = "", **data: Any) -> None:
    if kind not in KINDS:
        raise ValueError(f"unknown activity kind {kind!r}")
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind,
             "title": title[:_MAX_TEXT], "detail": detail[:_MAX_TEXT],
             "data": {k: v for k, v in data.items() if isinstance(v, (str, int, float, bool)) or v is None}}
    try:
        get_journal().add(entry)
    except Exception as exc:  # the journal is a side record; it must never fail the user's action
        log.warning("activity journal write failed: %s", type(exc).__name__)


def recent(limit: int = 20) -> dict:
    j = None
    try:
        j = get_journal()
        return {"backend": j.backend, "items": j.recent(max(1, min(limit, 100)))}
    except Exception as exc:
        log.warning("activity journal read failed: %s", type(exc).__name__)
        return {"backend": j.backend if j else "unavailable", "items": [], "error": "journal unavailable"}
