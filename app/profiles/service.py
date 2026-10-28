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


# A registration with its event and ticket type (the frontend's TICKET_SELECT). Only events the
# person could open (public, or their team's) are listed, as RLS did.
_TICKET_SQL = f"""
    select r.*,
           e.title, e.summary, e.starts_at, e.ends_at, e.timezone, e.format, e.venue_name,
           e.address, e.city, e.cover_url, e.external_url, e.status as event_status,
           e.cancel_reason, e.registration_mode,
           o.id as org_id, o.slug as org_slug, o.name as org_name, o.logo_url as org_logo_url,
           o.is_verified as org_is_verified,
           t.name as ticket_type, t.price as ticket_price, t.currency as ticket_currency,
           p.online_url, p.guest_info
      from public.registrations r
      join public.events e on e.id = r.event_id
      left join public.organisers o on o.id = e.organiser_id
      left join public.ticket_types t on t.id = r.ticket_type_id
      left join public.event_private p on p.event_id = e.id
     where r.user_id = :uid
       and ({events.PUBLICLY_VISIBLE_SQL} or public.can_manage_event(e.id))
"""
# The flat fields a ticket row always had; the single ticket also keeps the rest of its
# registration row, as before.
_TICKET_FIELDS = (
    "id", "status", "payment_status", "amount", "currency", "qr_code", "checked_in_at", "created_at",
    "answers", "ticket_type_id", "event_id", "title", "starts_at", "ends_at", "timezone", "venue_name",
    "address", "city", "cover_url", "external_url", "ticket_type",
)
_TICKET_DETAIL_FIELDS = (
    "user_id", "stripe_checkout_session_id", "stripe_payment_intent_id", "checked_in_by",
    "decided_at", "updated_at",
)


def _ticket(row: Any, *, detail: bool) -> dict:
    """One ticket: the flat fields plus `ticket` and `event` (with its organiser) nested, as the
    frontend's TicketRow had them."""
    out = {key: row[key] for key in _TICKET_FIELDS + (_TICKET_DETAIL_FIELDS if detail else ())}
    out["ticket"] = (
        {
            "id": row["ticket_type_id"],
            "name": row["ticket_type"],
            "price": row["ticket_price"],
            "currency": row["ticket_currency"],
        }
        if row["ticket_type"] is not None
        else None
    )
    out["event"] = {
        "id": row["event_id"],
        "title": row["title"],
        "summary": row["summary"],
        "cover_url": row["cover_url"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "format": row["format"],
        "venue_name": row["venue_name"],
        "address": row["address"],
        "city": row["city"],
        "status": row["event_status"],
        "cancel_reason": row["cancel_reason"],
        "registration_mode": row["registration_mode"],
        "organiser": (
            {
                "id": row["org_id"],
                "slug": row["org_slug"],
                "name": row["org_name"],
                "logo_url": row["org_logo_url"],
                "is_verified": row["org_is_verified"],
            }
            if row["org_id"] is not None
            else None
        ),
    }
    if detail:
        # The private guest details are for confirmed guests only.
        going = row["status"] == "going"
        out["online_url"] = row["online_url"] if going else None
        out["guest_info"] = row["guest_info"] if going else None
    return out


async def my_tickets(db: AsyncSession, user_id: UUID, tab: str) -> list[dict]:
    """`upcoming`: not ended yet (so a started event is still here), soonest first;
    `past`: ended, latest first. Every registration status is included."""
    ended = "coalesce(e.ends_at, e.starts_at) < now()"
    where = f"not ({ended})" if tab == "upcoming" else ended
    order = "e.starts_at asc" if tab == "upcoming" else "e.starts_at desc"
    rows = (
        await db.execute(text(f"{_TICKET_SQL} and {where} order by {order}, r.created_at"), {"uid": user_id})
    ).mappings().all()
    return [_ticket(row, detail=False) for row in rows]


async def my_ticket(db: AsyncSession, user_id: UUID, registration_id: UUID) -> dict | None:
    row = (
        await db.execute(text(f"{_TICKET_SQL} and r.id = :id"), {"uid": user_id, "id": registration_id})
    ).mappings().first()
    return _ticket(row, detail=True) if row else None


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
