"""Notifications: who hears about what, deduped through `notification_log`.

Ported from the frontend's `src/lib/notifications/{index,deliver,plan}.ts`.

Features call `notify_registration_changed`, `notify_event_changed` and
`notify_organiser_decision` after making a change. They never raise into the
caller: each queues a job on the caller's session, and the job runs after that
session's transaction commits, in the background with its own session (the
frontend sends after the response for the same reason). A transaction or
savepoint that rolls back drops its jobs, so nobody hears about a change that
never happened.

Delivery claims one `notification_log` row per (user, event, kind, channel)
before sending and releases the claim when sending fails, so a later run retries.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from typing import Any, Literal, Optional
from uuid import UUID

from sqlalchemy import delete, event as sa_event, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from app.core.database import AsyncSessionLocal
from app.notifications.channels import (
    StoredSubscription,
    app_url,
    channels_available,
    delete_subscriptions,
    send_email,
    send_emails,
    send_push,
)
from app.notifications.messages import (
    Message,
    RegistrationUpdate,
    event_changed_message,
    organiser_decision_message,
    registration_message,
    render_email,
)
from app.notifications.models import NotificationLog

__all__ = [
    "DEFAULT_PREFERENCES",
    "logistics_changed",
    "notify_event_changed",
    "notify_organiser_decision",
    "notify_registration_changed",
    "send_email",
]

logger = logging.getLogger(__name__)

Channel = Literal["push", "email"]

DEFAULT_PREFERENCES: dict[str, Any] = {
    "push_enabled": True,
    "email_enabled": True,
    "remind_before_hours": 24,
    "deadline_reminders": True,
}

# Registration statuses that still hold (or wait for) a place.
ACTIVE_REGISTRATION_STATUSES = ("going", "pending", "waitlisted", "awaiting_payment")

# A row updated well after it was created has moved (e.g. off the waitlist).
MOVED_AFTER_SECONDS = 5 * 60

EVENT_COLUMNS = (
    "id, title, starts_at, ends_at, format, venue_name, address, registration_deadline, "
    "registration_mode, external_label, status, cancel_reason"
)

PUSH_CONCURRENCY = 8
CLAIM_CHUNK = 500


# ─── recipients ─────────────────────────────────────────────────────────────


@dataclass
class Recipients:
    preferences: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    subscriptions: dict[UUID, list[StoredSubscription]] = field(default_factory=dict)
    emails: dict[UUID, str] = field(default_factory=dict)

    def prefs(self, user_id: UUID) -> dict[str, Any]:
        return self.preferences.get(user_id, DEFAULT_PREFERENCES)


async def _rows(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]


async def load_recipients(db: AsyncSession, user_ids: list[UUID]) -> Recipients:
    """Preferences, push devices and email addresses for a set of users."""
    out = Recipients()
    ids = list(dict.fromkeys(user_ids))
    if not ids:
        return out
    params = {"ids": ids}
    for row in await _rows(
        db,
        "select user_id, push_enabled, email_enabled, remind_before_hours, deadline_reminders"
        " from notification_preferences where user_id = any(:ids)",
        params,
    ):
        out.preferences[row.pop("user_id")] = row
    for row in await _rows(
        db,
        "select user_id, endpoint, p256dh, auth from push_subscriptions"
        " where user_id = any(:ids) order by created_at",
        params,
    ):
        out.subscriptions.setdefault(row["user_id"], []).append(
            StoredSubscription(row["endpoint"], row["p256dh"], row["auth"])
        )
    for row in await _rows(
        db,
        "select u.id, coalesce(nullif(p.email, ''), u.email) as email"
        " from users u left join profiles p on p.id = u.id where u.id = any(:ids)",
        params,
    ):
        if row["email"]:
            out.emails[row["id"]] = row["email"]
    return out


def channels_for(
    user_id: UUID,
    prefs: dict[str, Any],
    recipients: Recipients,
    available: dict[str, bool],
) -> list[Channel]:
    """Channels this person can get right now, given their preferences and devices."""
    channels: list[Channel] = []
    if available["push"] and prefs["push_enabled"] and recipients.subscriptions.get(user_id):
        channels.append("push")
    if available["email"] and prefs["email_enabled"] and user_id in recipients.emails:
        channels.append("email")
    return channels


# ─── delivery ───────────────────────────────────────────────────────────────


@dataclass
class DeliveryJob:
    user_id: UUID
    event_id: UUID
    registration_id: Optional[UUID]
    kind: str
    channels: list[Channel]
    message: Message


@dataclass
class DeliveryResult:
    push_sent: int = 0
    emails_sent: int = 0
    skipped: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "push_sent": self.push_sent,
            "emails_sent": self.emails_sent,
            "skipped": self.skipped,
            "failed": self.failed,
        }


def _key(user_id: Any, event_id: Any, kind: str, channel: str) -> tuple[str, str, str, str]:
    return (str(user_id), str(event_id), kind, channel)


async def deliver(
    db: AsyncSession, jobs: list[DeliveryJob], recipients: Recipients
) -> DeliveryResult:
    """Claim, send, and release the claims of failed sends (deliver.ts)."""
    result = DeliveryResult()

    # 1. Claim every (user, event, kind, channel) we intend to send. Existing rows = already sent.
    rows = [
        {
            "user_id": j.user_id,
            "event_id": j.event_id,
            "registration_id": j.registration_id,
            "kind": j.kind,
            "channel": channel,
            "sent_at": datetime.now(timezone.utc),
        }
        for j in jobs
        for channel in j.channels
    ]
    claimed: dict[tuple[str, str, str, str], int] = {}
    for start in range(0, len(rows), CLAIM_CHUNK):
        inserted = await db.execute(
            pg_insert(NotificationLog)
            .values(rows[start : start + CLAIM_CHUNK])
            .on_conflict_do_nothing(index_elements=["user_id", "event_id", "kind", "channel"])
            .returning(
                NotificationLog.id,
                NotificationLog.user_id,
                NotificationLog.event_id,
                NotificationLog.kind,
                NotificationLog.channel,
            )
        )
        for row in inserted.all():
            claimed[_key(row.user_id, row.event_id, row.kind, row.channel)] = row.id
    result.skipped += len(rows) - len(claimed)

    def claim_of(job: DeliveryJob, channel: str) -> Optional[int]:
        return claimed.get(_key(job.user_id, job.event_id, job.kind, channel))

    release: list[int] = []

    # 2. Push, a few people at a time.
    push_jobs = [j for j in jobs if "push" in j.channels and claim_of(j, "push") is not None]
    gate = asyncio.Semaphore(PUSH_CONCURRENCY)
    expired: list[str] = []

    async def push_one(job: DeliveryJob) -> None:
        async with gate:
            sent = await send_push(recipients.subscriptions.get(job.user_id, []), job.message)
        expired.extend(sent.expired_endpoints)
        if sent.delivered > 0:
            result.push_sent += 1
            return
        if sent.failed > 0:
            result.failed += 1
        else:
            result.skipped += 1  # every device had expired
        release.append(claim_of(job, "push"))  # type: ignore[arg-type]

    await asyncio.gather(*(push_one(j) for j in push_jobs))
    await delete_subscriptions(db, expired)

    # 3. Email, batched.
    email_jobs: list[DeliveryJob] = []
    for job in jobs:
        claim = claim_of(job, "email") if "email" in job.channels else None
        if claim is None:
            continue
        if job.user_id in recipients.emails:
            email_jobs.append(job)
        else:
            result.skipped += 1
            release.append(claim)
    base = app_url()
    sent = await send_emails(
        [(recipients.emails[j.user_id], render_email(j.message, base)) for j in email_jobs]
    )
    for job, ok in zip(email_jobs, sent):
        if ok:
            result.emails_sent += 1
        else:
            result.failed += 1
            release.append(claim_of(job, "email"))  # type: ignore[arg-type]

    # 4. Let failed sends be retried next time.
    if release:
        await db.execute(delete(NotificationLog).where(NotificationLog.id.in_(release)))
    return result


# ─── after-commit queue ─────────────────────────────────────────────────────

_JOBS = "orbit.notification_jobs"
_HOOKED = "orbit.notification_hooked"
_running: set[asyncio.Task] = set()

Job = Callable[[AsyncSession], Awaitable[None]]


def _within(tx: Optional[SessionTransaction], ancestor: SessionTransaction) -> bool:
    while tx is not None:
        if tx is ancestor:
            return True
        tx = tx.parent
    return False


def _on_commit(session: Session) -> None:
    if session.get_nested_transaction() is not None:
        return  # a savepoint was released; wait for the real commit
    for _, label, job in session.info.pop(_JOBS, []):
        _spawn(label, job)


def _on_rollback(session: Session, previous: SessionTransaction) -> None:
    queue = session.info.get(_JOBS)
    if not queue:
        return
    if previous.parent is None:
        session.info[_JOBS] = []
    else:
        session.info[_JOBS] = [item for item in queue if not _within(item[0], previous)]


def _after_commit(db: AsyncSession, label: str, job: Job) -> None:
    sync = db.sync_session
    tx = sync.get_nested_transaction() or sync.get_transaction()
    sync.info.setdefault(_JOBS, []).append((tx, label, job))
    if not sync.info.get(_HOOKED):
        sa_event.listen(sync, "after_commit", _on_commit)
        sa_event.listen(sync, "after_soft_rollback", _on_rollback)
        sync.info[_HOOKED] = True


def _spawn(label: str, job: Job) -> None:
    try:
        task = asyncio.get_running_loop().create_task(_run(label, job))
    except RuntimeError:
        logger.error("[notifications] %s dropped: no running event loop", label)
        return
    _running.add(task)
    task.add_done_callback(_running.discard)


async def _run(label: str, job: Job) -> None:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("select set_config('app.trusted', 'on', true)"))
            await job(session)
            await session.commit()
    except Exception:
        logger.exception("[notifications] %s failed", label)


# ─── event changed ──────────────────────────────────────────────────────────


def event_change_recipients(
    registrations: list[dict[str, Any]], marks: list[dict[str, Any]]
) -> dict[UUID, Optional[UUID]]:
    """Everyone to tell when an event changes: active registrations + people who saved it."""
    out: dict[UUID, Optional[UUID]] = {}
    for r in registrations:
        if r["status"] in ACTIVE_REGISTRATION_STATUSES:
            out[r["user_id"]] = r["id"]
    inactive = {r["user_id"] for r in registrations if r["status"] not in ACTIVE_REGISTRATION_STATUSES}
    for m in marks:
        if (
            m["kind"] in ("saved", "external_registered")
            and m["user_id"] not in out
            and m["user_id"] not in inactive
        ):
            out[m["user_id"]] = None
    return out


def _iso(value: Optional[datetime]) -> str:
    return value.astimezone(timezone.utc).isoformat() if value else ""


def event_change_fingerprint(e: dict[str, Any]) -> str:
    """A short, stable fingerprint of what changed, so the same update isn't announced twice."""
    joined = "|".join(
        [_iso(e.get("starts_at")), _iso(e.get("ends_at"))]
        + [e.get(k) or "" for k in ("venue_name", "address", "format")]
    )
    units = joined.encode("utf-16-le")
    h = 5381
    for i in range(0, len(units), 2):
        h = ((h * 33) & 0xFFFFFFFF) ^ int.from_bytes(units[i : i + 2], "little")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        h, r = divmod(h, 36)
        out = digits[r] + out
        if h == 0:
            return out


LOGISTICS = ("starts_at", "ends_at", "venue_name", "address", "format")


def logistics_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Time, end, venue, address or format differ (the frontend's logisticsChanged)."""
    return any((before.get(key) or None) != (after.get(key) or None) for key in LOGISTICS)


def _ended(event: dict[str, Any]) -> bool:
    end = event.get("ends_at") or event["starts_at"]
    return end < datetime.now(timezone.utc)


async def _load_event(db: AsyncSession, event_id: UUID) -> Optional[dict[str, Any]]:
    rows = await _rows(db, f"select {EVENT_COLUMNS} from events where id = :id", {"id": event_id})
    return rows[0] if rows else None


async def _send_event_changed(
    db: AsyncSession, event_id: UUID, change: Literal["updated", "cancelled"]
) -> None:
    event = await _load_event(db, event_id)
    if not event:
        return
    if change == "updated" and event["status"] != "published":
        return
    if _ended(event):
        return

    registrations = await _rows(
        db, "select id, user_id, status from registrations where event_id = :e", {"e": event_id}
    )
    marks = await _rows(
        db,
        "select user_id, kind from user_events"
        " where event_id = :e and kind in ('saved', 'external_registered')",
        {"e": event_id},
    )
    people = event_change_recipients(registrations, marks)
    if not people:
        return

    recipients = await load_recipients(db, list(people))
    available = channels_available()
    kind = "event_cancelled" if change == "cancelled" else f"event_updated:{event_change_fingerprint(event)}"
    message = event_changed_message(event, change)
    jobs = [
        DeliveryJob(user_id, event_id, registration_id, kind, channels, message)
        for user_id, registration_id in people.items()
        if (channels := channels_for(user_id, recipients.prefs(user_id), recipients, available))
    ]
    result = await deliver(db, jobs, recipients)
    logger.info("[notifications] event %s %s: %s", change, event_id, result.as_dict())


async def notify_event_changed(
    db: AsyncSession, event_id: UUID | str, change: Literal["updated", "cancelled"]
) -> None:
    """An event's time/place changed, or it was cancelled: tell everyone registered or who saved it."""
    try:
        eid = UUID(str(event_id))
        _after_commit(db, f"notify_event_changed({eid}, {change})", partial(_send_event_changed, event_id=eid, change=change))
    except Exception:
        logger.exception("[notifications] notify_event_changed(%s, %s) not queued", event_id, change)


# ─── registration changed ───────────────────────────────────────────────────


def classify_registration(r: dict[str, Any], reason: str = "") -> Optional[RegistrationUpdate]:
    """Works out what happened to a registration from its current row. None = nothing to say."""
    moved = (r["updated_at"] - r["created_at"]).total_seconds() > MOVED_AFTER_SECONDS
    if reason == "waitlist":
        moved = True  # the caller saw it leave the waitlist
    status = r["status"]
    if status == "going":
        if r["payment_status"] == "paid":
            return "paid"
        if r["decided_at"]:
            return "approved"
        return "off_waitlist" if moved else "confirmed"
    if status == "awaiting_payment":
        if r["decided_at"]:
            return "approved"
        return "off_waitlist" if moved else "awaiting_payment"
    if status == "pending":
        return "pending_review"
    if status == "declined":
        return "declined"
    return None  # waitlisted / cancelled: the attendee did it, or nothing changed for them


async def _send_registration_changed(db: AsyncSession, registration_id: UUID, reason: str) -> None:
    rows = await _rows(
        db,
        "select id, event_id, user_id, status, decided_at, payment_status, created_at, updated_at"
        " from registrations where id = :id",
        {"id": registration_id},
    )
    if not rows:
        return
    reg = rows[0]
    update = classify_registration(reg, reason)
    if not update:
        return

    event = await _load_event(db, reg["event_id"])
    if not event or event["status"] == "cancelled" or _ended(event):
        return

    recipients = await load_recipients(db, [reg["user_id"]])
    channels = channels_for(
        reg["user_id"], recipients.prefs(reg["user_id"]), recipients, channels_available()
    )
    if not channels:
        return
    job = DeliveryJob(
        user_id=reg["user_id"],
        event_id=reg["event_id"],
        registration_id=reg["id"],
        kind=f"registration_{reg['status']}",
        channels=channels,
        message=registration_message(event, update, reg["status"]),
    )
    result = await deliver(db, [job], recipients)
    logger.info(
        "[notifications] registration %s (%s) %s: %s",
        reg["status"],
        reason,
        registration_id,
        result.as_dict(),
    )


async def notify_registration_changed(
    db: AsyncSession, registration_ids: list[UUID | str], reason: str
) -> None:
    """An organiser approved/declined someone, someone moved off the waitlist, or a payment landed.

    reason: "decision", "approve_all", "waitlist" (promoted off the waitlist) or "paid".
    """
    try:
        for registration_id in dict.fromkeys(registration_ids):
            rid = UUID(str(registration_id))
            _after_commit(
                db,
                f"notify_registration_changed({rid}, {reason})",
                partial(_send_registration_changed, registration_id=rid, reason=reason),
            )
    except Exception:
        logger.exception(
            "[notifications] notify_registration_changed(%s, %s) not queued",
            registration_ids,
            reason,
        )


# ─── organiser decision ─────────────────────────────────────────────────────


async def _send_organiser_decision(
    db: AsyncSession, organiser_id: UUID, decision: Literal["approved", "rejected"]
) -> None:
    rows = await _rows(
        db, "select id, name, status, review_note from organisers where id = :id", {"id": organiser_id}
    )
    if not rows or rows[0]["status"] != decision:
        return  # changed again since; the newer decision speaks for itself
    org = rows[0]
    owners = [
        row["user_id"]
        for row in await _rows(
            db,
            "select user_id from organiser_members where organiser_id = :o and role = 'owner'",
            {"o": organiser_id},
        )
    ]
    if not owners:
        return
    recipients = await load_recipients(db, owners)
    available = channels_available()
    message = organiser_decision_message(org, decision)

    pushed = 0
    expired: list[str] = []
    emails: dict[str, None] = {}
    for user_id in owners:
        # Push follows the owner's push preference.
        if "push" in channels_for(user_id, recipients.prefs(user_id), recipients, available):
            sent = await send_push(recipients.subscriptions.get(user_id, []), message)
            expired.extend(sent.expired_endpoints)
            pushed += sent.delivered > 0
        # The email is a transactional account notice: always sent, as the frontend does.
        if available["email"] and user_id in recipients.emails:
            emails[recipients.emails[user_id]] = None
    await delete_subscriptions(db, expired)
    if not available["email"]:
        logger.info(
            "[notifications] organiser %s email skipped (no email channel): %d owner(s)",
            decision,
            len(owners),
        )
    rendered = render_email(message, app_url())
    ok = await send_emails([(to, rendered) for to in emails])
    logger.info(
        "[notifications] organiser %s %s: %d/%d pushed, %d/%d emailed",
        decision,
        organiser_id,
        pushed,
        len(owners),
        sum(ok),
        len(ok),
    )


async def notify_organiser_decision(
    db: AsyncSession, organiser_id: UUID | str, decision: str
) -> None:
    """An admin approved or rejected an organisation: tell its owners. Other decisions are silent.

    Owners always get the email (an account notice, whatever their email preference);
    push follows their push preference.

    There is no ledger row (notification_log needs an event), so call this only when
    the status actually changed.
    """
    if decision not in ("approved", "rejected"):
        return
    try:
        oid = UUID(str(organiser_id))
        _after_commit(
            db,
            f"notify_organiser_decision({oid}, {decision})",
            partial(_send_organiser_decision, organiser_id=oid, decision=decision),
        )
    except Exception:
        logger.exception(
            "[notifications] notify_organiser_decision(%s, %s) not queued", organiser_id, decision
        )

