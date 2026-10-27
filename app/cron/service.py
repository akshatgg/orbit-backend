"""Scheduled reminders: event and registration-deadline emails, deduped in the ledger."""

import html as html_lib
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.models import Event
from app.notifications.models import NotificationLog
from app.notifications.service import preferences_for, send_email
from app.tickets.models import Registration
from app.users.models import User, UserEvent

WINDOW_HOURS = 24


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%a %d %b, %H:%M UTC")


def _message(event: Event, kind: str) -> tuple[str, str]:
    where = "Online" if event.format == "online" else (event.venue_name or event.address or "Venue to be confirmed")
    when = _iso(event.starts_at)
    title = html_lib.escape(event.title)
    if kind == "deadline_reminder":
        deadline = _iso(event.registration_deadline or event.starts_at)
        return (
            f"Registration closes for {event.title}",
            f"<p>Registration for <b>{title}</b> closes at {deadline}.</p>"
            f"<p>{html_lib.escape(where)}</p>",
        )
    return (
        f"Reminder: {event.title} is coming up",
        f"<p><b>{title}</b> starts at {when}.</p><p>{html_lib.escape(where)}</p>",
    )


async def _claim(db: AsyncSession, user_id: UUID, event_id: UUID, registration_id: UUID | None, kind: str, now: datetime) -> bool:
    claim_id = (
        await db.execute(
            pg_insert(NotificationLog)
            .values(
                user_id=user_id,
                event_id=event_id,
                registration_id=registration_id,
                kind=kind,
                channel="email",
                sent_at=now,
            )
            .on_conflict_do_nothing(index_elements=["user_id", "event_id", "kind", "channel"])
            .returning(NotificationLog.id)
        )
    ).scalar_one_or_none()
    return claim_id is not None


async def _release(db: AsyncSession, user_id: UUID, event_id: UUID, kind: str) -> None:
    await db.execute(
        delete(NotificationLog).where(
            NotificationLog.user_id == user_id,
            NotificationLog.event_id == event_id,
            NotificationLog.kind == kind,
            NotificationLog.channel == "email",
        )
    )


async def run_reminders(db: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """Send event and registration-deadline reminders for the next 24 hours."""
    now = now or datetime.now(timezone.utc)
    until = now + timedelta(hours=WINDOW_HOURS)

    starting = (
        await db.execute(
            select(Event).where(
                Event.status == "published",
                Event.starts_at > now,
                Event.starts_at <= until,
            )
        )
    ).scalars().all()
    closing = (
        await db.execute(
            select(Event).where(
                Event.status == "published",
                Event.starts_at > now,
                Event.registration_mode != "none",
                Event.registration_deadline.is_not(None),
                Event.registration_deadline > now,
                Event.registration_deadline <= until,
            )
        )
    ).scalars().all()

    events = {event.id: event for event in [*starting, *closing]}
    if not events:
        return {"sent": 0}
    event_ids = list(events)

    registrations = (
        await db.execute(
            select(Registration.user_id, Registration.id, Registration.event_id).where(
                Registration.event_id.in_(event_ids),
                Registration.status == "going",
            )
        )
    ).all()
    marks = (
        await db.execute(
            select(UserEvent.user_id, UserEvent.event_id, UserEvent.kind).where(
                UserEvent.event_id.in_(event_ids),
                UserEvent.kind.in_(("saved", "dismissed")),
            )
        )
    ).all()

    going: dict[UUID, dict[UUID, UUID]] = {}
    registered: dict[UUID, set[UUID]] = {}
    for user_id, registration_id, event_id in registrations:
        going.setdefault(event_id, {})[user_id] = registration_id
        registered.setdefault(event_id, set()).add(user_id)
    saved: dict[UUID, set[UUID]] = {}
    dismissed: dict[UUID, set[UUID]] = {}
    for user_id, event_id, kind in marks:
        if kind == "saved":
            saved.setdefault(event_id, set()).add(user_id)
        elif kind == "dismissed":
            dismissed.setdefault(event_id, set()).add(user_id)

    candidates: set[UUID] = set()
    for event_id in event_ids:
        candidates.update(going.get(event_id, {}))
        candidates.update(saved.get(event_id, set()))
    if not candidates:
        return {"sent": 0}

    users = {
        row.id: row.email
        for row in (
            await db.execute(select(User.id, User.email).where(User.id.in_(list(candidates))))
        ).all()
    }
    preferences = await preferences_for(db, list(candidates))

    sent = 0
    starting_ids = {event.id for event in starting}
    deadline_ids = {event.id for event in closing}
    for event_id, event in events.items():
        recipients: dict[UUID, UUID | None] = {}
        if event_id in starting_ids:
            recipients.update(going.get(event_id, {}))
            for user_id in saved.get(event_id, set()):
                if user_id not in dismissed.get(event_id, set()) and user_id not in recipients:
                    recipients[user_id] = None
        for user_id, registration_id in recipients.items():
            prefs = preferences.get(user_id)
            if prefs is None or not prefs.get("email_enabled"):
                continue
            to = users.get(user_id)
            if not to:
                continue
            subject, body = _message(event, "event_reminder")
            if not await _claim(db, user_id, event_id, registration_id, "event_reminder", now):
                continue
            if await send_email(to, subject, body):
                sent += 1
            else:
                await _release(db, user_id, event_id, "event_reminder")

        if event_id not in deadline_ids:
            continue
        for user_id in saved.get(event_id, set()):
            if user_id in dismissed.get(event_id, set()) or user_id in registered.get(event_id, set()):
                continue
            prefs = preferences.get(user_id)
            if prefs is None or not prefs.get("email_enabled") or not prefs.get("deadline_reminders"):
                continue
            to = users.get(user_id)
            if not to:
                continue
            subject, body = _message(event, "deadline_reminder")
            if not await _claim(db, user_id, event_id, None, "deadline_reminder", now):
                continue
            if await send_email(to, subject, body):
                sent += 1
            else:
                await _release(db, user_id, event_id, "deadline_reminder")

    return {"sent": sent}
