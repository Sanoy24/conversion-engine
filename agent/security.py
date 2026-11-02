"""
Request authentication: API key for `/api/*`, signatures for webhooks.

Every check follows one rule: when its secret is configured it is always
enforced; when it is not, requests are let through in development (with a
warning) and rejected in production. A production deploy can therefore never
run with an open API or unauthenticated webhooks by accident.

- API: `X-API-Key: <key>` or `Authorization: Bearer <key>` (`API_KEY`)
- Resend: Svix signature headers (`RESEND_WEBHOOK_SECRET`, the `whsec_...` value)
- Cal.com: `X-Cal-Signature-256`, hex HMAC-SHA256 of the body (`CALCOM_WEBHOOK_SECRET`)
- Africa's Talking: it does not sign requests, so the callback URL carries a
  shared token instead: `/webhooks/sms/inbound?token=<AT_WEBHOOK_TOKEN>`
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import time
from collections.abc import Mapping

from agent.config import settings

logger = logging.getLogger(__name__)

# Svix rejects webhooks whose timestamp is further than this from now (replay protection).
SVIX_TOLERANCE_SECONDS = 5 * 60

_warned: set[str] = set()


def _secret_missing(name: str) -> bool:
    """Decide what to do when a secret is unset: allow in development, refuse in production."""
    if settings.is_dev:
        if name not in _warned:
            logger.warning("%s is not set: accepting unauthenticated requests (development)", name)
            _warned.add(name)
        return True
    logger.error("%s is not set: rejecting request (production)", name)
    return False


def check_api_key(headers: Mapping[str, str]) -> bool:
    if not settings.api_key:
        return _secret_missing("API_KEY")
    provided = headers.get("x-api-key", "")
    auth = headers.get("authorization", "")
    if not provided and auth.lower().startswith("bearer "):
        provided = auth[7:].strip()
    return hmac.compare_digest(provided.encode(), settings.api_key.encode())


def check_resend_signature(headers: Mapping[str, str], body: bytes) -> bool:
    """Verify a Resend (Svix) webhook: HMAC-SHA256 over `{id}.{timestamp}.{body}`."""
    secret = settings.resend_webhook_secret
    if not secret:
        return _secret_missing("RESEND_WEBHOOK_SECRET")

    msg_id = headers.get("svix-id", "")
    timestamp = headers.get("svix-timestamp", "")
    signatures = headers.get("svix-signature", "")
    if not (msg_id and timestamp and signatures):
        return False
    try:
        if abs(time.time() - int(timestamp)) > SVIX_TOLERANCE_SECONDS:
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except (ValueError, binascii.Error):
        return False

    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    # The header may hold several space-separated "v1,<sig>" entries (secret rotation).
    return any(
        hmac.compare_digest(candidate.partition(",")[2], expected)
        for candidate in signatures.split()
    )


def check_calcom_signature(headers: Mapping[str, str], body: bytes) -> bool:
    secret = settings.calcom_webhook_secret
    if not secret:
        return _secret_missing("CALCOM_WEBHOOK_SECRET")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(headers.get("x-cal-signature-256", ""), expected)


def check_sms_webhook_token(token: str) -> bool:
    if not settings.at_webhook_token:
        return _secret_missing("AT_WEBHOOK_TOKEN")
    return hmac.compare_digest(token.encode(), settings.at_webhook_token.encode())
