"""
Email delivery providers.

`send_email` (email_handler.py) does everything provider-neutral: suppression
check, unsubscribe headers and footer, kill switch. When it is time to
actually deliver, it calls `deliver()`, which hands the message to the
provider chosen by `EMAIL_PROVIDER`:

    resend    Resend API (RESEND_API_KEY)
    sendgrid  SendGrid v3 Mail Send API (SENDGRID_API_KEY)
    smtp      Any SMTP server: Amazon SES, Gmail/Workspace, Postmark, Mailgun,
              your own (SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD)

`deliver()` takes the provider-neutral message dict built by `send_email`
(from, to, subject, text, reply_to, headers) and returns the provider's
message id. It raises on failure; `send_email` turns that into a trace.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email import policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

import httpx
import resend

from agent.config import settings

EMAIL_PROVIDERS = ("resend", "sendgrid", "smtp")

SENDGRID_SEND_URL = "https://api.sendgrid.com/v3/mail/send"


async def deliver(message: dict) -> str | None:
    provider = settings.email_provider
    if provider == "resend":
        return await _deliver_resend(message)
    if provider == "sendgrid":
        return await _deliver_sendgrid(message)
    if provider == "smtp":
        return await asyncio.to_thread(_deliver_smtp, message)
    raise ValueError(f"Unknown EMAIL_PROVIDER {provider!r}; expected one of {EMAIL_PROVIDERS}")


# ── Resend ────────────────────────────────────────────────────────────


async def _deliver_resend(message: dict) -> str | None:
    if not settings.resend_api_key:
        raise RuntimeError("RESEND_API_KEY must be set")
    resend.api_key = settings.resend_api_key
    # The SDK call is blocking; run it off the event loop like SMTP.
    response = await asyncio.to_thread(resend.Emails.send, message)
    return response.get("id") if isinstance(response, dict) else None


# ── SendGrid ──────────────────────────────────────────────────────────


def _address(value: str) -> dict:
    """'Name <a@b.c>' -> {"email": "a@b.c", "name": "Name"} (SendGrid's address object)."""
    name, email = parseaddr(value)
    return {"email": email, "name": name} if name else {"email": email}


def _sendgrid_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=15.0)


async def _deliver_sendgrid(message: dict) -> str | None:
    if not settings.sendgrid_api_key:
        raise RuntimeError("SENDGRID_API_KEY must be set")
    body = {
        "personalizations": [{"to": [_address(to) for to in message["to"]]}],
        "from": _address(message["from"]),
        "reply_to": _address(message["reply_to"]),
        "subject": message["subject"],
        "content": [{"type": "text/plain", "value": message["text"]}],
        "headers": message["headers"],
    }
    async with _sendgrid_http() as client:
        response = await client.post(
            SENDGRID_SEND_URL,
            json=body,
            headers={"Authorization": f"Bearer {settings.sendgrid_api_key}"},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"SendGrid returned {response.status_code}: {response.text[:300]}")
    return response.headers.get("X-Message-Id")


# ── SMTP ──────────────────────────────────────────────────────────────


# Allow header lines up to RFC 5322's 998-character limit. With the default
# 78, a long unbreakable value such as the List-Unsubscribe URL gets
# RFC 2047-encoded, and mail clients no longer recognise the link.
_SMTP_POLICY = policy.SMTP.clone(max_line_length=998)


def build_mime(message: dict) -> EmailMessage:
    mime = EmailMessage(policy=_SMTP_POLICY)
    sender_name, sender_email = parseaddr(message["from"])
    mime["From"] = formataddr((sender_name, sender_email))
    mime["To"] = ", ".join(message["to"])
    mime["Subject"] = message["subject"]
    mime["Reply-To"] = message["reply_to"]
    mime["Message-ID"] = make_msgid(domain=sender_email.rpartition("@")[2] or None)
    for name, value in message["headers"].items():
        mime[name] = value
    mime.set_content(message["text"])
    return mime


def _deliver_smtp(message: dict) -> str | None:
    if not settings.smtp_host:
        raise RuntimeError("SMTP_HOST must be set")
    mime = build_mime(message)
    context = ssl.create_default_context()
    if settings.smtp_use_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, context=context, timeout=30
        )
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    with server:
        if not settings.smtp_use_ssl and settings.smtp_starttls:
            server.starttls(context=context)
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(mime)
    return mime["Message-ID"]
