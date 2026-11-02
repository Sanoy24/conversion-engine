"""
SMS delivery providers.

`send_sms` (sms_handler.py) enforces the warm-lead policy, the suppression
list and the kill switch. For live delivery it calls `deliver_sms()`, which
uses the provider chosen by `SMS_PROVIDER`:

    africastalking  Africa's Talking (AT_USERNAME, AT_API_KEY, AT_SHORTCODE)
    twilio          Twilio Messages API (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                    and TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID)

`deliver_sms()` returns a dict with the provider's message id and raises on
failure.

Inbound: Africa's Talking posts to /webhooks/sms/inbound, Twilio to
/webhooks/sms/twilio. `normalize_twilio_inbound` maps Twilio's form fields
onto the shape the shared SMS router expects.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from collections.abc import Mapping

import httpx

from agent.config import settings

SMS_PROVIDERS = ("africastalking", "twilio")

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


async def deliver_sms(to_phone: str, message: str) -> dict:
    provider = settings.sms_provider
    if provider == "africastalking":
        return await asyncio.to_thread(_deliver_africastalking, to_phone, message)
    if provider == "twilio":
        return await _deliver_twilio(to_phone, message)
    raise ValueError(f"Unknown SMS_PROVIDER {provider!r}; expected one of {SMS_PROVIDERS}")


# ── Africa's Talking ──────────────────────────────────────────────────

_at_sms = None


def _get_at_client():
    """Initialize the Africa's Talking SMS client once."""
    global _at_sms
    if _at_sms is None:
        import africastalking

        africastalking.initialize(username=settings.at_username, api_key=settings.at_api_key)
        _at_sms = africastalking.SMS
    return _at_sms


def _deliver_africastalking(to_phone: str, message: str) -> dict:
    response = _get_at_client().send(
        message=message, recipients=[to_phone], sender_id=settings.at_shortcode or None
    )
    return {"provider": "africastalking", "response": str(response)}


# ── Twilio ────────────────────────────────────────────────────────────


def _twilio_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15.0)


async def _deliver_twilio(to_phone: str, message: str) -> dict:
    sid, token = settings.twilio_account_sid, settings.twilio_auth_token
    if not (sid and token):
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    form = {"To": to_phone, "Body": message}
    if settings.twilio_messaging_service_sid:
        form["MessagingServiceSid"] = settings.twilio_messaging_service_sid
    elif settings.twilio_from_number:
        form["From"] = settings.twilio_from_number
    else:
        raise RuntimeError("Set TWILIO_FROM_NUMBER or TWILIO_MESSAGING_SERVICE_SID")

    async with _twilio_http() as client:
        response = await client.post(
            f"{TWILIO_API_BASE}/Accounts/{sid}/Messages.json", data=form, auth=(sid, token)
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Twilio returned {response.status_code}: {response.text[:300]}")
    return {"provider": "twilio", "id": response.json().get("sid")}


def twilio_signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """X-Twilio-Signature: base64 HMAC-SHA1 of the full URL plus sorted form key/values."""
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def normalize_twilio_inbound(form: Mapping[str, str]) -> dict:
    """Map Twilio's inbound-message fields onto the shared SMS router's payload."""
    return {
        "from": form.get("From", ""),
        "to": form.get("To", ""),
        "text": form.get("Body", ""),
        "id": form.get("MessageSid", ""),
    }
