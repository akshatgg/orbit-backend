"""Reads and writes for the signed-in person: their marks, their events, their profile."""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.categories.models import Category
from app.events import service as events
from app.events.models import Event
from app.tickets.models import Registration
from app.users.models import UserEvent

ACTIVE_REGISTRATION = ("going", "pending", "waitlisted", "awaiting_payment")
MARK_KINDS = ("saved", "dismissed", "external_registered", "attended")
CALENDAR_LIMIT = 500


async def event_state(db: AsyncSession, user_id: UUID, event_ids: list[UUID] | None) -> dict:
    """Marks and registration status per event; `event_ids=None` means all of them."""
    state: dict[str, Any] = {kind: [] for kind in MARK_KINDS}
    state["registrations"] = {}
    if event_ids is not None and not event_ids:
        return state

    marks = select(UserEvent.event_id, UserEvent.kind).where(UserEvent.user_id == user_id)
    regs = select(Registration.event_id, Registration.status).where(Registration.user_id == user_id)
    if event_ids is not None:
        marks = marks.where(UserEvent.event_id.in_(event_ids))
        regs = regs.where(Registration.event_id.in_(event_ids))

    for event_id, kind in (await db.execute(marks.order_by(UserEvent.created_at))).all():
        if kind in state:
            state[kind].append(str(event_id))
    for event_id, status in (await db.execute(regs)).all():
        state["registrations"][str(event_id)] = status
    return state


async def _marked_cards(db: AsyncSession, user_id: UUID, stmt) -> list[dict]:
    """Run a `card_query()` statement and add the viewer's marks and registration."""
    items = await events.cards(db, stmt)
    if not items:
        return items
    state = await event_state(db, user_id, [item["id"] for item in items])
    for item in items:
        key = str(item["id"])
        status = state["registrations"].get(key)
        item["saved"] = key in state["saved"]
        item["attended"] = key in state["attended"]
        item["external_registered"] = key in state["external_registered"]
        item["registration"] = status if status in ACTIVE_REGISTRATION else None
    return items


def _mine(user_id: UUID, kinds: tuple[str, ...]):
    """Events the user marked with one of `kinds` or holds an active registration for."""
    marked = select(UserEvent.event_id).where(
        UserEvent.user_id == user_id, UserEvent.kind.in_(kinds)
    )
    registered = select(Registration.event_id).where(
        Registration.user_id == user_id, Registration.status.in_(ACTIVE_REGISTRATION)
    )
    return or_(Event.id.in_(marked), Event.id.in_(registered))


async def my_events(db: AsyncSession, user_id: UUID, when: str) -> list[dict]:
    """Saved, attended, externally registered and registered events; upcoming or ended."""
    end = func.coalesce(Event.ends_at, Event.starts_at)
    stmt = events.card_query().where(
        _mine(user_id, ("saved", "attended", "external_registered")),
        Event.status.notin_(("draft", "removed")),
    )
    if when == "upcoming":
        stmt = stmt.where(end >= func.now()).order_by(Event.starts_at)
    else:
        stmt = stmt.where(end < func.now()).order_by(Event.starts_at.desc())
    return await _marked_cards(db, user_id, stmt)


async def my_calendar(
    db: AsyncSession,
    user_id: UUID,
    start: datetime,
    end: datetime | None,
    statuses: tuple[str, ...] = ("published", "cancelled"),
) -> list[dict]:
    """Saved, externally registered and actively registered events not ended by `start`.

    Cancelled events stay so people notice them.
    """
    stmt = events.card_query().where(
        _mine(user_id, ("saved", "external_registered")),
        Event.status.in_(statuses),
        func.coalesce(Event.ends_at, Event.starts_at) >= start,
    )
    if end is not None:
        stmt = stmt.where(Event.starts_at < end)
    return await _marked_cards(db, user_id, stmt.order_by(Event.starts_at).limit(CALENDAR_LIMIT))


async def known_categories(db: AsyncSession, slugs: list[str]) -> list[str]:
    """`slugs` without the ones that are not categories, order kept (keepKnownCategories)."""
    if not slugs:
        return []
    known = set((await db.scalars(select(Category.slug).where(Category.slug.in_(slugs)))).all())
    return [slug for slug in slugs if slug in known]


async def update_profile(
    db: AsyncSession, user_id: UUID, fields: dict[str, Any], *, onboard: bool = False
) -> None:
    """Write the given profile columns (already validated), optionally marking onboarding done."""
    if "preferred_categories" in fields:
        fields["preferred_categories"] = await known_categories(db, fields["preferred_categories"] or [])
    for key in ("goals", "career_interests", "personal_interests", "preferred_categories", "preferred_formats"):
        if key in fields and fields[key] is None:
            fields[key] = []  # these columns are not null; null clears them
    sets = [f"{key} = :{key}" for key in fields]
    if "availability" in fields:
        sets[list(fields).index("availability")] = "availability = cast(:availability as jsonb)"
        fields["availability"] = None if fields["availability"] is None else json.dumps(fields["availability"])
    if onboard:
        sets.append("onboarded_at = now()")
    if not sets:
        return
    await db.execute(
        text(f"update public.profiles set {', '.join(sets)}, updated_at = now() where id = :uid"),
        {**fields, "uid": user_id},
    )
