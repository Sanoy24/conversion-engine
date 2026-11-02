"""
Suppression list: addresses the engine must never contact again.

An address lands here when someone clicks the unsubscribe link, replies with
an opt-out, texts STOP, or when an email bounces or is marked as spam. The
channel senders (`send_email`, `send_sms`) check this list before every send,
including in sink mode, so a suppressed address cannot be reached through any
code path.

Stored in the same SQLite file as conversations (`CONVERSATION_DB_PATH`), in
its own table. With an empty path the list lives in memory only.

This module also signs and verifies unsubscribe-link tokens, so a link can
only unsubscribe the address it was sent to.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from agent.config import settings

logger = logging.getLogger(__name__)

# Reasons recorded with each entry.
UNSUBSCRIBE_LINK = "unsubscribe_link"
REPLY_OPT_OUT = "reply_opt_out"
SMS_STOP = "sms_stop"
BOUNCE = "bounce"
COMPLAINT = "complaint"
MANUAL = "manual"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS suppressions (
    address    TEXT PRIMARY KEY,
    channel    TEXT NOT NULL,
    reason     TEXT NOT NULL,
    thread_id  TEXT,
    created_at TEXT NOT NULL
)
"""


def normalize_address(address: str) -> str:
    """Lowercase emails; reduce phone numbers to digits with an optional leading '+'."""
    address = (address or "").strip()
    if "@" in address:
        return address.lower()
    digits = re.sub(r"\D", "", address)
    return f"+{digits}" if address.startswith("+") else digits


class SuppressionList:
    def __init__(self, db_path: str | Path = "") -> None:
        if db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path) or ":memory:", check_same_thread=False)
        self._db.execute(_SCHEMA)
        self._db.commit()

    def add(
        self,
        address: str,
        channel: str,
        reason: str,
        thread_id: str | None = None,
    ) -> bool:
        """Suppress an address. Returns False if it was already suppressed (first reason wins)."""
        key = normalize_address(address)
        if not key:
            return False
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO suppressions (address, channel, reason, thread_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, channel, reason, thread_id, datetime.now(UTC).isoformat()),
            )
            self._db.commit()
        added = cursor.rowcount == 1
        if added:
            logger.info("Suppressed %s (channel=%s, reason=%s)", key, channel, reason)
        return added

    def get(self, address: str) -> dict | None:
        key = normalize_address(address)
        if not key:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT address, channel, reason, thread_id, created_at"
                " FROM suppressions WHERE address = ?",
                (key,),
            ).fetchone()
        return (
            dict(zip(("address", "channel", "reason", "thread_id", "created_at"), row, strict=True))
            if row
            else None
        )

    def is_suppressed(self, address: str) -> bool:
        return self.get(address) is not None

    def all(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT address, channel, reason, thread_id, created_at"
                " FROM suppressions ORDER BY created_at"
            ).fetchall()
        keys = ("address", "channel", "reason", "thread_id", "created_at")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()


_suppressions: SuppressionList | None = None


def get_suppression_list() -> SuppressionList:
    """The process-wide suppression list, created on first use from settings."""
    global _suppressions
    if _suppressions is None:
        _suppressions = SuppressionList(settings.conversation_db_path)
    return _suppressions


def set_suppression_list(suppressions: SuppressionList) -> None:
    """Replace the process-wide list (used by tests and embedding apps)."""
    global _suppressions
    _suppressions = suppressions


# ── Unsubscribe links ─────────────────────────────────────────────────


def _sign(payload: str) -> str:
    digest = hmac.new(
        settings.unsubscribe_secret.encode(), payload.encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest[:16]).decode().rstrip("=")


def make_unsubscribe_token(email: str) -> str:
    payload = base64.urlsafe_b64encode(normalize_address(email).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload)}"


def verify_unsubscribe_token(token: str) -> str | None:
    """Return the email a token was issued for, or None if it is malformed or forged."""
    if not settings.unsubscribe_secret or "." not in (token or ""):
        return None
    payload, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(payload)):
        return None
    try:
        return base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None


def unsubscribe_url(email: str) -> str | None:
    """Signed one-click unsubscribe URL, or None if PUBLIC_BASE_URL / UNSUBSCRIBE_SECRET are unset."""
    if not (settings.public_base_url and settings.unsubscribe_secret):
        return None
    token = quote(make_unsubscribe_token(email))
    return f"{settings.public_base_url.rstrip('/')}/unsubscribe?t={token}"
