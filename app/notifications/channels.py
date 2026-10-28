"""Channel senders: web push (VAPID) and email (SMTP or Resend).

Ported from the frontend's `src/lib/notifications/channels.ts` and
`email-config.ts`. Senders never raise; they report what was delivered.
"""

import asyncio
import json
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Literal, Optional

import aiohttp
import httpx
from py_vapid import Vapid
from pywebpush import WebPushException, webpush_async
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.notifications.messages import Message, RenderedEmail, html_to_text, push_payload
from app.notifications.models import PushSubscription

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
RESEND_BATCH_URL = "https://api.resend.com/emails/batch"
PUSH_TTL_SECONDS = 60 * 60 * 12
PUSH_TIMEOUT_SECONDS = 10
DEFAULT_SMTP_PORT = 465


def app_url() -> str:
    return settings.FRONTEND_URL.rstrip("/")


# ─── web push ───────────────────────────────────────────────────────────────


@dataclass
class StoredSubscription:
    endpoint: str
    p256dh: str
    auth: str


@dataclass
class PushResult:
    delivered: int = 0
    failed: int = 0
    expired: int = 0
    expired_endpoints: list[str] = field(default_factory=list)


def push_available() -> bool:
    return settings.push_configured


async def send_push(subscriptions: list[StoredSubscription], message: Message) -> PushResult:
    """Send one notification to every device a person has. Never raises.

    Subscriptions the push service says are gone (404/410) come back in
    `expired_endpoints`; pass them to `delete_subscriptions`.
    """
    result = PushResult()
    if not subscriptions:
        return result
    if not push_available():
        logger.warning("[notifications] push skipped: VAPID keys are not set")
        result.failed = len(subscriptions)
        return result
    try:
        vapid = Vapid.from_string(private_key=settings.VAPID_PRIVATE_KEY)
    except Exception as exc:  # malformed key: report, never raise
        logger.error("[notifications] push skipped: VAPID_PRIVATE_KEY is unusable (%s)", exc.__class__.__name__)
        result.failed = len(subscriptions)
        return result

    payload = json.dumps(push_payload(message))
    timeout = aiohttp.ClientTimeout(total=PUSH_TIMEOUT_SECONDS)

    async def one(session: aiohttp.ClientSession, sub: StoredSubscription) -> None:
        try:
            await webpush_async(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=vapid,
                vapid_claims={"sub": settings.VAPID_SUBJECT},
                ttl=PUSH_TTL_SECONDS,
                headers={"Urgency": "normal"},
                timeout=timeout,
                aiohttp_session=session,
            )
            result.delivered += 1
        except WebPushException as exc:
            code = exc.status_code
            if code in (404, 410):
                result.expired += 1
                result.expired_endpoints.append(sub.endpoint)
            else:
                result.failed += 1
                logger.error("[notifications] push failed: HTTP %s", code)
        except Exception as exc:
            result.failed += 1
            logger.error("[notifications] push failed: %s", exc.__class__.__name__)

    try:
        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*(one(session, sub) for sub in subscriptions))
    except Exception as exc:
        logger.error("[notifications] push failed: %s", exc.__class__.__name__)
        result.failed = len(subscriptions) - result.delivered - result.expired

    return result


async def delete_subscriptions(db: AsyncSession, endpoints: list[str]) -> None:
    """Forget devices the push service says are gone. Never raises."""
    if not endpoints:
        return
    try:
        async with db.begin_nested():
            await db.execute(
                delete(PushSubscription).where(PushSubscription.endpoint.in_(endpoints))
            )
        logger.info("[notifications] deleted %d expired push subscription(s)", len(endpoints))
    except Exception as exc:
        logger.error(
            "[notifications] couldn't delete expired subscriptions: %s", exc.__class__.__name__
        )


# ─── email ──────────────────────────────────────────────────────────────────


@dataclass
class SmtpConfig:
    host: str
    port: int
    secure: bool
    user: str
    password: str


@dataclass
class EmailChannel:
    # "log": local development with no provider; the email is logged and counted as sent.
    kind: Literal["smtp", "resend", "log", "none"]
    sender: str = ""
    smtp: Optional[SmtpConfig] = None
    api_key: Optional[str] = None


def _clean(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    return value or None


def _smtp_config() -> Optional[SmtpConfig]:
    host = _clean(settings.SMTP_HOST)
    user = _clean(settings.SMTP_USER)
    password = settings.SMTP_PASS or None
    if not host or not user or not password:
        return None
    try:
        port = int(_clean(settings.SMTP_PORT) or "")
    except ValueError:
        port = DEFAULT_SMTP_PORT
    if not 0 < port < 65536:
        port = DEFAULT_SMTP_PORT
    # 465 is implicit TLS; anything else starts plain and upgrades with STARTTLS.
    return SmtpConfig(host=host, port=port, secure=port == 465, user=user, password=password)


def email_channel() -> EmailChannel:
    configured_from = _clean(settings.EMAIL_FROM)
    smtp = _smtp_config()
    if smtp:
        return EmailChannel("smtp", configured_from or f"ORBIT <{smtp.user}>", smtp=smtp)
    api_key = _clean(settings.RESEND_API_KEY)
    if api_key:
        return EmailChannel("resend", configured_from or "ORBIT <onboarding@resend.dev>", api_key=api_key)
    if settings.is_local:
        return EmailChannel("log", configured_from or "ORBIT <onboarding@resend.dev>")
    return EmailChannel("none")


def email_available() -> bool:
    return email_channel().kind != "none"


def channels_available() -> dict[str, bool]:
    return {"push": push_available(), "email": email_available()}


def _smtp_send(cfg: SmtpConfig, sender: str, jobs: list[tuple[str, RenderedEmail]]) -> list[bool]:
    ok = [False] * len(jobs)
    try:
        context = ssl.create_default_context()
        if cfg.secure:
            client: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=20, context=context)
        else:
            client = smtplib.SMTP(cfg.host, cfg.port, timeout=20)
            client.starttls(context=context)
        with client:
            client.login(cfg.user, cfg.password)
            for i, (to, email) in enumerate(jobs):
                msg = EmailMessage()
                msg["From"] = sender
                msg["To"] = to
                msg["Subject"] = email.subject
                msg.set_content(email.text)
                msg.add_alternative(email.html, subtype="html")
                try:
                    client.send_message(msg)
                    ok[i] = True
                except Exception as exc:
                    logger.error("[notifications] email failed: %s", exc.__class__.__name__)
    except Exception as exc:
        logger.error("[notifications] email failed: %s", exc.__class__.__name__)
    return ok


async def _resend_send(api_key: str, sender: str, jobs: list[tuple[str, RenderedEmail]]) -> list[bool]:
    ok = [False] * len(jobs)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        for start in range(0, len(jobs), 100):
            chunk = jobs[start : start + 100]
            payload: list[dict[str, Any]] = [
                {"from": sender, "to": [to], "subject": e.subject, "html": e.html, "text": e.text}
                for to, e in chunk
            ]
            try:
                if len(chunk) == 1:
                    response = await client.post(RESEND_URL, headers=headers, json=payload[0])
                else:
                    response = await client.post(RESEND_BATCH_URL, headers=headers, json=payload)
                if response.status_code >= 400:
                    logger.error(
                        "[notifications] email failed: Resend HTTP %s %s",
                        response.status_code,
                        response.text[:300],
                    )
                else:
                    for k in range(len(chunk)):
                        ok[start + k] = True
            except httpx.HTTPError as exc:
                logger.error("[notifications] email failed: %s", exc.__class__.__name__)
            if start + 100 < len(jobs):
                await asyncio.sleep(0.6)  # stays inside Resend's rate limit
    return ok


async def send_emails(jobs: list[tuple[str, RenderedEmail]]) -> list[bool]:
    """Send (to, email) pairs; returns, per job, whether it was accepted. Never raises."""
    if not jobs:
        return []
    try:
        channel = email_channel()
        if channel.kind == "none":
            logger.info(
                "[notifications] email skipped (no SMTP or RESEND_API_KEY set): %d message(s)",
                len(jobs),
            )
            return [False] * len(jobs)
        if channel.kind == "log":
            for to, email in jobs:
                logger.info(
                    "[email] not sent (local, no email provider)\nto: %s\nsubject: %s\n%s",
                    to,
                    email.subject,
                    email.text,
                )
            return [True] * len(jobs)
        if channel.kind == "smtp" and channel.smtp:
            return await asyncio.to_thread(_smtp_send, channel.smtp, channel.sender, jobs)
        return await _resend_send(channel.api_key or "", channel.sender, jobs)
    except Exception as exc:
        logger.error("[notifications] email failed: %s", exc.__class__.__name__)
        return [False] * len(jobs)


async def send_email(to: str, subject: str, html: str) -> bool:
    """One ad-hoc email (magic links). Returns whether it was accepted."""
    # Keep link targets in the text part: "Sign in to Orbit (https://...)".
    linked = re.sub(r'<a\s[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r"\2 (\1)", html)
    text = html_to_text(re.sub(r"</p>\s*", "</p>\n", linked)).strip()
    ok = await send_emails([(to, RenderedEmail(subject=subject, html=html, text=text))])
    return bool(ok and ok[0])
