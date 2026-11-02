"""
Sending guardrails that protect the sending domain's reputation.

- Daily cap: at most DAILY_SEND_LIMIT live emails per UTC day.
- Warm-up: with EMAIL_WARMUP_START set (a new sending domain), the cap
  starts at 10/day and doubles every week until it reaches DAILY_SEND_LIMIT.
- Auto-pause: sending stops when, over the last 7 days, the bounce rate
  exceeds BOUNCE_PAUSE_RATE (once at least 20 emails went out) or the spam
  complaint rate exceeds COMPLAINT_PAUSE_RATE. A paused engine stays paused
  until someone resumes it (POST /api/sending/resume).

Only live sends count; sink mode (LIVE_OUTBOUND_ENABLED=false) is never
limited. Bounces and complaints arrive through the email webhook.

Stored in the same SQLite file as conversations (`CONVERSATION_DB_PATH`).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from agent.config import settings

logger = logging.getLogger(__name__)

WINDOW_DAYS = 7
MIN_SENDS_FOR_BOUNCE_RATE = 20
WARMUP_START_PER_DAY = 10

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS send_events (
        at      TEXT NOT NULL,
        kind    TEXT NOT NULL,  -- sent | bounce | complaint
        address TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS send_events_at ON send_events (at)",
    """CREATE TABLE IF NOT EXISTS sending_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    )""",
)


class SendingGuard:
    def __init__(self, db_path: str | Path = "") -> None:
        if db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path) or ":memory:", check_same_thread=False)
        for statement in _SCHEMA:
            self._db.execute(statement)
        self._db.commit()

    # ── Events ────────────────────────────────────────────────────────

    def record(self, kind: str, address: str | None = None, at: datetime | None = None) -> None:
        moment = (at or datetime.now(UTC)).isoformat()
        with self._lock:
            self._db.execute(
                "INSERT INTO send_events (at, kind, address) VALUES (?, ?, ?)",
                (moment, kind, address),
            )
            self._db.commit()
        if kind in ("bounce", "complaint"):
            self._check_health()

    def _count(self, kind: str, since: datetime) -> int:
        # Strictly after `since`: an event in the same clock tick as a resume
        # belongs to the period before it.
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM send_events WHERE kind = ? AND at > ?",
                (kind, since.isoformat()),
            ).fetchone()
        return row[0]

    # ── Pause state ───────────────────────────────────────────────────

    def _get_state(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM sending_state WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    def _set_state(self, key: str, value: str | None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO sending_state (key, value) VALUES (?, ?)", (key, value)
            )
            self._db.commit()

    @property
    def paused_reason(self) -> str | None:
        return self._get_state("paused_reason")

    def pause(self, reason: str) -> None:
        if not self.paused_reason:
            logger.error("Email sending PAUSED: %s", reason)
        self._set_state("paused_reason", reason)

    def resume(self) -> None:
        logger.warning("Email sending resumed (was: %s)", self.paused_reason)
        self._set_state("paused_reason", None)
        # Health is judged on events after the resume, so old bounces don't re-pause at once.
        self._set_state("resumed_at", datetime.now(UTC).isoformat())

    # ── Limits ────────────────────────────────────────────────────────

    def daily_limit(self, today: date | None = None) -> int:
        limit = settings.daily_send_limit
        if settings.email_warmup_start:
            start = date.fromisoformat(settings.email_warmup_start)
            weeks = max(0, ((today or datetime.now(UTC).date()) - start).days // 7)
            limit = min(limit, WARMUP_START_PER_DAY * 2**weeks)
        return limit

    def sent_today(self) -> int:
        midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return self._count("sent", midnight)

    def _window_start(self) -> datetime:
        start = datetime.now(UTC) - timedelta(days=WINDOW_DAYS)
        resumed = self._get_state("resumed_at")
        return max(start, datetime.fromisoformat(resumed)) if resumed else start

    def health(self) -> dict:
        since = self._window_start()
        sent = self._count("sent", since)
        bounces = self._count("bounce", since)
        complaints = self._count("complaint", since)
        return {
            "window_days": WINDOW_DAYS,
            "sent": sent,
            "bounces": bounces,
            "complaints": complaints,
            "bounce_rate": round(bounces / sent, 4) if sent else 0.0,
            "complaint_rate": round(complaints / sent, 4) if sent else 0.0,
        }

    def _check_health(self) -> None:
        h = self.health()
        if h["complaints"] and (
            not h["sent"] or h["complaint_rate"] > settings.complaint_pause_rate
        ):
            self.pause(
                f"spam complaint rate {h['complaint_rate']:.1%} "
                f"({h['complaints']}/{h['sent']}) over the last {WINDOW_DAYS} days"
            )
        elif (
            h["sent"] >= MIN_SENDS_FOR_BOUNCE_RATE and h["bounce_rate"] > settings.bounce_pause_rate
        ):
            self.pause(
                f"bounce rate {h['bounce_rate']:.1%} ({h['bounces']}/{h['sent']}) "
                f"over the last {WINDOW_DAYS} days"
            )

    def check(self) -> tuple[bool, str | None]:
        """Whether one more live email may go out now, and if not, why."""
        if self.paused_reason:
            return False, f"paused: {self.paused_reason}"
        limit = self.daily_limit()
        if self.sent_today() >= limit:
            return False, f"daily limit of {limit} reached"
        return True, None

    def status(self) -> dict:
        limit = self.daily_limit()
        sent = self.sent_today()
        return {
            "paused": bool(self.paused_reason),
            "paused_reason": self.paused_reason,
            "daily_limit": limit,
            "sent_today": sent,
            "remaining_today": max(0, limit - sent),
            "warming_up": limit < settings.daily_send_limit,
            "health": self.health(),
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()


_guard: SendingGuard | None = None


def get_sending_guard() -> SendingGuard:
    global _guard
    if _guard is None:
        _guard = SendingGuard(settings.conversation_db_path)
    return _guard


def set_sending_guard(guard: SendingGuard) -> None:
    global _guard
    _guard = guard
