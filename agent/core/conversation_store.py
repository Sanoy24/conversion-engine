"""
Conversation storage backends.

Two backends share one interface:

- `InMemoryConversationStore`: process-local dict. State is lost on restart.
  Used by the test suite and when `CONVERSATION_DB_PATH` is empty.
- `SQLiteConversationStore`: write-through cache over a SQLite file. Every
  thread is loaded into memory at startup, every `save()` is written to disk,
  so threads survive restarts.

Both backends hand out the same object for the same thread within a process,
so in-place edits stay visible to later reads. Edits are only durable once
`save()` is called.

The SQLite backend assumes a single process. Running several workers against
one file would give each worker its own stale cache.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Protocol

from agent.models import ConversationState

logger = logging.getLogger(__name__)


class ConversationStore(Protocol):
    def get(self, thread_id: str) -> ConversationState | None: ...

    def save(self, state: ConversationState) -> None: ...

    def all(self) -> list[ConversationState]: ...

    def clear(self) -> None: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._conversations: dict[str, ConversationState] = {}

    def get(self, thread_id: str) -> ConversationState | None:
        return self._conversations.get(thread_id)

    def save(self, state: ConversationState) -> None:
        self._conversations[state.thread_id] = state

    def all(self) -> list[ConversationState]:
        return list(self._conversations.values())

    def clear(self) -> None:
        self._conversations.clear()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    thread_id  TEXT PRIMARY KEY,
    company    TEXT NOT NULL,
    status     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    data       TEXT NOT NULL
)
"""


class SQLiteConversationStore(InMemoryConversationStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self._path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(_SCHEMA)
        self._db.commit()
        self._load()

    def _load(self) -> None:
        rows = self._db.execute("SELECT thread_id, data FROM conversations").fetchall()
        for thread_id, data in rows:
            try:
                self._conversations[thread_id] = ConversationState.model_validate_json(data)
            except ValueError:
                logger.exception("Skipping unreadable conversation %s in %s", thread_id, self._path)
        logger.info("Loaded %d conversations from %s", len(self._conversations), self._path)

    def save(self, state: ConversationState) -> None:
        super().save(state)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO conversations (thread_id, company, status, updated_at, data)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    state.thread_id,
                    (state.prospect.company or "").lower(),
                    state.status.value,
                    state.updated_at,
                    state.model_dump_json(),
                ),
            )
            self._db.commit()

    def clear(self) -> None:
        super().clear()
        with self._lock:
            self._db.execute("DELETE FROM conversations")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


def create_store(db_path: str) -> ConversationStore:
    """SQLite store at `db_path`, or an in-memory store when it is empty."""
    if not db_path:
        return InMemoryConversationStore()
    return SQLiteConversationStore(db_path)
