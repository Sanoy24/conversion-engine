"""
Review queue: LLM-drafted emails wait here for a person to approve them.

With `REQUIRE_APPROVAL=true` (the default), the orchestrator queues every
drafted email (cold outreach and warm replies) instead of sending it. A
reviewer then approves it, optionally after editing the subject or body, or
rejects it. Approval sends through `send_email`, so the suppression list and
the LIVE_OUTBOUND_ENABLED kill switch still apply.

Edits are kept next to the original draft: they show where the drafter needs
to improve.

Stored in the same SQLite file as conversations (`CONVERSATION_DB_PATH`);
in memory when that is empty. Single-process, like the conversation store.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from agent.config import settings
from agent.models import EmailDraft

ReviewStatus = Literal["pending", "sending", "sent", "failed", "rejected"]


class ReviewItem(BaseModel):
    id: str
    thread_id: str
    to: str
    prospect_company: str | None = None
    email_type: str
    subject: str
    body: str
    draft: EmailDraft  # what the drafter produced, kept unedited
    status: ReviewStatus = "pending"
    edited: bool = False
    needs_attention: str | None = None  # e.g. the brief's human-review reason
    reject_reason: str | None = None
    delivery: dict | None = None
    created_at: str
    decided_at: str | None = None


def new_review_item(
    thread_id: str,
    to: str,
    draft: EmailDraft,
    prospect_company: str | None = None,
    needs_attention: str | None = None,
) -> ReviewItem:
    return ReviewItem(
        id=f"rev_{uuid.uuid4().hex[:10]}",
        thread_id=thread_id,
        to=to,
        prospect_company=prospect_company,
        email_type=draft.email_type.value,
        subject=draft.subject,
        body=draft.body,
        draft=draft,
        needs_attention=needs_attention,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_queue (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    data       TEXT NOT NULL
)
"""


class ReviewQueue:
    def __init__(self, db_path: str | Path = "") -> None:
        if db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path) or ":memory:", check_same_thread=False)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def save(self, item: ReviewItem) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO review_queue (id, status, created_at, data) VALUES (?, ?, ?, ?)",
                (item.id, item.status, item.created_at, item.model_dump_json()),
            )
            self._db.commit()

    def get(self, item_id: str) -> ReviewItem | None:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM review_queue WHERE id = ?", (item_id,)
            ).fetchone()
        return ReviewItem.model_validate_json(row[0]) if row else None

    def list(self, status: str | None = None) -> list[ReviewItem]:
        query = "SELECT data FROM review_queue"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        with self._lock:
            rows = self._db.execute(query + " ORDER BY created_at", params).fetchall()
        return [ReviewItem.model_validate_json(row[0]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()


_queue: ReviewQueue | None = None


def get_review_queue() -> ReviewQueue:
    global _queue
    if _queue is None:
        _queue = ReviewQueue(settings.conversation_db_path)
    return _queue


def set_review_queue(queue: ReviewQueue) -> None:
    global _queue
    _queue = queue
