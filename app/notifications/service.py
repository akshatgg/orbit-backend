"""Notification delivery and dedupe ledger.

Email goes through Resend when `RESEND_API_KEY` is set. Web push delivery is not
implemented in this backend: `push_subscriptions` is stored for the frontend's
service worker to use, and the ledger rows keep the same (user, event, kind,
channel) contract as Orbit.
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.notifications.models import NotificationLog, NotificationPreference
from app.tickets.models import Registration
from app.users.models import UserEvent

logger = logging.getLogger(__name__)

DEFAULT_PREFERENCES: dict[str, Any] = {
    "push_enabled": True,
    "email_enabled": True,
    "remind_before_hours": 24,
    "deadline_reminders": True,
}

RESEND_URL = "https://api.resend.com/emails"


async def send_email(to: str, subject: str, html: str) -> bool:
    """Send one email through Resend, or log it when no API key is configured."""
    if not settings.RESEND_API_KEY:
        logger.info("[email] no RESEND_API_KEY; would send %r to %s: %s", subject, to, html)
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                RESEND_URL,
                headers={
                    "Authorization": f"Bearer {settings.RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": settings.EMAIL_FROM,
                    "to": [to],
                    "subject": subject,
                    "html": html,
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("[email] send to %s failed: %s", to, exc.__class__.__name__)
        return False
    if response.status_code >= 400:
        logger.warning(
            "[email] Resend returned HTTP %s for %s: %s",
            response.status_code,
            to,
            response.text[:300],
        )
        return False
    return True


async def preferences_for(db: AsyncSession, user_ids: list[UUID]) -> dict[UUID, dict[str, Any]]:
    """Notification preferences for each user, with defaults for anyone without a row."""
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(NotificationPreference).where(NotificationPreference.user_id.in_(user_ids))
        )
    ).scalars().all()
    stored = {
        row.user_id: {
            "push_enabled": row.push_enabled,
            "email_enabled": row.email_enabled,
            "remind_before_hours": row.remind_before_hours,
            "deadline_reminders": row.deadline_reminders,
        }
        for row in rows
    }
    return {user_id: stored.get(user_id, dict(DEFAULT_PREFERENCES)) for user_id in user_ids}


async def notify_event_change(db: AsyncSession, event_id: UUID, kind: str) -> int:
    """Queue a notification for going guests and people who saved the event."""
    going = (
        await db.execute(
            select(Registration.user_id, Registration.id).where(
                Registration.event_id == event_id,
                Registration.status == "going",
            )
        )
    ).all()
    saved = (
        await db.execute(
            select(UserEvent.user_id).where(
                UserEvent.event_id == event_id, UserEvent.kind == "saved"
            )
        )
    ).scalars().all()

    recipients: dict[UUID, UUID | None] = {}
    for user_id, registration_id in going:
        recipients[user_id] = registration_id
    for user_id in saved:
        recipients.setdefault(user_id, None)
    if not recipients:
        return 0

    user_ids = list(recipients)
    preferences = await preferences_for(db, user_ids)
    now = datetime.now(timezone.utc)
    rows = []
    for user_id in user_ids:
        prefs = preferences[user_id]
        for channel, enabled in (
            ("push", prefs["push_enabled"]),
            ("email", prefs["email_enabled"]),
        ):
            if not enabled:
                continue
            rows.append(
                {
                    "user_id": user_id,
                    "event_id": event_id,
                    "registration_id": recipients[user_id],
                    "kind": kind,
                    "channel": channel,
                    "sent_at": now,
                }
            )
    if not rows:
        return 0
    result = await db.execute(
        pg_insert(NotificationLog)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["user_id", "event_id", "kind", "channel"])
        .returning(NotificationLog.id)
    )
    return len(result.scalars().all())
