"""Scheduled reminders: event and registration-deadline notifications, by push and email.

Ported from the frontend's `src/lib/notifications/plan.ts` and `reminders.ts`.
Each person's `remind_before_hours` (2 or 24) picks how far ahead their event
reminder goes out; the ledger (`notification_log`) makes a second run send nothing new.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.notifications.channels import channels_available
from app.notifications.messages import Audience, deadline_reminder_message, event_reminder_message
from app.notifications.service import (
    DEFAULT_PREFERENCES,
    DeliveryJob,
    channels_for,
    deliver,
    load_recipients,
)

logger = logging.getLogger(__name__)

ReminderKind = Literal["event_reminder", "deadline_reminder"]

# The reminder timings a person can pick.
REMIND_BEFORE_OPTIONS = (24, 2)
# The reminders cron runs once a day (the frontend's vercel.json: 08:00 UTC).
CRON_INTERVAL_HOURS = 24
# Deadline reminders go out for registration deadlines within this many hours.
DEADLINE_WINDOW_HOURS = 48

EVENT_COLUMNS = (
    "id, title, starts_at, ends_at, format, venue_name, address, registration_deadline, "
    "registration_mode, external_label, status"
)


def reminder_window_hours(remind_before: int, interval: int = CRON_INTERVAL_HOURS) -> float:
    """How far ahead an event reminder can go out for someone who wants it `remind_before` hours ahead.

    The cron sends at the run closest to the requested time: anything starting within
    `remind_before + interval / 2` hours, never less than one interval, so every event is
    caught by at least one run. With the daily cron: 24 → the next 36h, 2 → the next 24h.
    """
    return max(remind_before + interval / 2, interval)


def max_reminder_window_hours(interval: int = CRON_INTERVAL_HOURS) -> float:
    return max(reminder_window_hours(h, interval) for h in REMIND_BEFORE_OPTIONS)


@dataclass
class Relation:
    registration: Optional[dict[str, Any]] = None
    saved: bool = False
    external_registered: bool = False
    dismissed: bool = False


def _relations(
    events: list[dict[str, Any]], registrations: list[dict[str, Any]], marks: list[dict[str, Any]]
) -> dict[UUID, dict[UUID, Relation]]:
    ids = {e["id"] for e in events}
    by_event: dict[UUID, dict[UUID, Relation]] = {}

    def get(event_id: UUID, user_id: UUID) -> Relation:
        return by_event.setdefault(event_id, {}).setdefault(user_id, Relation())

    for r in registrations:
        if r["event_id"] in ids:
            get(r["event_id"], r["user_id"]).registration = r
    for m in marks:
        if m["event_id"] not in ids:
            continue
        rel = get(m["event_id"], m["user_id"])
        if m["kind"] == "saved":
            rel.saved = True
        elif m["kind"] == "external_registered":
            rel.external_registered = True
        elif m["kind"] == "dismissed":
            rel.dismissed = True
    return by_event


def event_reminder_audience(rel: Relation) -> Optional[Audience]:
    """Who an event reminder is for, and why; None for nobody."""
    status = rel.registration["status"] if rel.registration else None
    if status == "going" or rel.external_registered:
        return "going"
    if status == "awaiting_payment":
        return "unpaid"
    # Pending / waitlisted people hear when their status changes; cancelled / declined chose or got a no.
    if status:
        return None
    if rel.saved and not rel.dismissed:
        return "saved"
    return None


def needs_deadline_reminder(rel: Relation) -> bool:
    """Saved, not registered (in ORBIT or externally), deadline coming up."""
    return rel.saved and not rel.dismissed and not rel.registration and not rel.external_registered


@dataclass
class PlannedDelivery:
    user_id: UUID
    event_id: UUID
    registration_id: Optional[UUID]
    kind: ReminderKind
    audience: Audience
    channels: list[str] = field(default_factory=list)


def plan_reminders(
    now: datetime,
    events: list[dict[str, Any]],
    registrations: list[dict[str, Any]],
    marks: list[dict[str, Any]],
    recipients: Any,
    sent: set[tuple[str, str, str, str]],
    available: dict[str, bool],
    interval: int = CRON_INTERVAL_HOURS,
) -> tuple[list[PlannedDelivery], int]:
    """One run of the reminders cron (planReminders).

    (a) Event reminders: published events starting inside the person's reminder window,
        for people going (or with an unpaid ticket) and people who saved them.
    (b) Deadline reminders: saved events with a registration deadline in the next 48h
        that the person hasn't registered for.
    Channels already in the log are dropped, so running twice sends nothing new.
    """
    by_event = _relations(events, registrations, marks)
    deliveries: list[PlannedDelivery] = []
    skipped = 0

    def add(user_id: UUID, event: dict[str, Any], kind: ReminderKind, audience: Audience, rel: Relation, prefs: dict[str, Any]) -> None:
        nonlocal skipped
        wanted = channels_for(user_id, prefs, recipients, available)
        channels = [c for c in wanted if (str(user_id), str(event["id"]), kind, c) not in sent]
        if not channels:
            skipped += 1
            return
        deliveries.append(
            PlannedDelivery(
                user_id=user_id,
                event_id=event["id"],
                registration_id=rel.registration["id"] if rel.registration else None,
                kind=kind,
                audience=audience,
                channels=channels,
            )
        )

    for event in events:
        if event["status"] != "published":
            continue
        starts_in = (event["starts_at"] - now).total_seconds() / 3600
        if starts_in <= 0:
            continue
        users = by_event.get(event["id"])
        if not users:
            continue
        deadline = event.get("registration_deadline")
        deadline_in = (deadline - now).total_seconds() / 3600 if deadline else None
        deadline_soon = (
            deadline_in is not None
            and 0 < deadline_in <= DEADLINE_WINDOW_HOURS
            and event["registration_mode"] != "none"
        )
        for user_id, rel in users.items():
            prefs = recipients.preferences.get(user_id, DEFAULT_PREFERENCES)
            audience = event_reminder_audience(rel)
            if audience and starts_in <= reminder_window_hours(prefs["remind_before_hours"], interval):
                add(user_id, event, "event_reminder", audience, rel, prefs)
            if deadline_soon and prefs["deadline_reminders"] and needs_deadline_reminder(rel):
                add(user_id, event, "deadline_reminder", "saved", rel, prefs)

    return deliveries, skipped


async def _rows(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]


async def run_reminders(db: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    """One run of the daily reminders cron: event reminders + registration-deadline reminders."""
    now = now or datetime.now(timezone.utc)
    empty = {"events": 0, "reminders": 0, "push_sent": 0, "emails_sent": 0, "skipped": 0, "failed": 0}

    soon = await _rows(
        db,
        f"select {EVENT_COLUMNS} from events where status = 'published'"
        " and starts_at > :now and starts_at <= :until",
        {"now": now, "until": now + timedelta(hours=max_reminder_window_hours())},
    )
    closing = await _rows(
        db,
        f"select {EVENT_COLUMNS} from events where status = 'published' and starts_at > :now"
        " and registration_deadline > :now and registration_deadline <= :until",
        {"now": now, "until": now + timedelta(hours=DEADLINE_WINDOW_HOURS)},
    )
    by_id = {e["id"]: e for e in [*soon, *closing]}
    events = list(by_id.values())
    if not events:
        return empty
    event_ids = list(by_id)

    registrations = await _rows(
        db,
        "select id, event_id, user_id, status from registrations where event_id = any(:ids)",
        {"ids": event_ids},
    )
    marks = await _rows(
        db,
        "select event_id, user_id, kind from user_events where event_id = any(:ids)"
        " and kind in ('saved', 'external_registered', 'dismissed')",
        {"ids": event_ids},
    )
    log = await _rows(
        db,
        "select user_id, event_id, kind, channel from notification_log where event_id = any(:ids)"
        " and kind in ('event_reminder', 'deadline_reminder')",
        {"ids": event_ids},
    )

    user_ids = list(dict.fromkeys([r["user_id"] for r in registrations] + [m["user_id"] for m in marks]))
    if not user_ids:
        return {**empty, "events": len(events)}
    recipients = await load_recipients(db, user_ids)

    available = channels_available()
    if not available["email"]:
        logger.info("[notifications] email not configured (SMTP_* or RESEND_API_KEY): reminders go by push only")
    if not available["push"]:
        logger.info("[notifications] VAPID keys not set: reminders go by email only")

    deliveries, skipped = plan_reminders(
        now,
        events,
        registrations,
        marks,
        recipients,
        {(str(r["user_id"]), str(r["event_id"]), r["kind"], r["channel"]) for r in log},
        available,
    )
    jobs = [
        DeliveryJob(
            user_id=d.user_id,
            event_id=d.event_id,
            registration_id=d.registration_id,
            kind=d.kind,
            channels=d.channels,  # type: ignore[arg-type]
            message=(
                deadline_reminder_message(by_id[d.event_id], now)
                if d.kind == "deadline_reminder"
                else event_reminder_message(by_id[d.event_id], d.audience, now)
            ),
        )
        for d in deliveries
    ]
    result = await deliver(db, jobs, recipients)
    summary = {
        "events": len(events),
        "reminders": len(deliveries),
        "push_sent": result.push_sent,
        "emails_sent": result.emails_sent,
        "skipped": skipped + result.skipped,
        "failed": result.failed,
    }
    logger.info("[cron/reminders] %s", summary)
    return summary
