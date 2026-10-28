"""Event reads, visibility rules and the shared card shape."""

import inspect
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.categories.models import Category
from app.core.config import settings
from app.events.models import Event, EventPrivate
from app.organisers.models import Organiser
from app.sql import functions as fn
from app.tickets.models import EventQuestion, Registration, TicketType
from app.users.models import EventInteraction, UserEvent

logger = logging.getLogger(__name__)

EMPTY_COUNTS = {"going": 0, "waitlisted": 0, "pending": 0}
MAX_IDS = 200

INTERESTS = [
    ("entrepreneurship", "Entrepreneurship"),
    ("consulting", "Consulting"),
    ("technology", "Technology"),
    ("finance", "Finance"),
    ("marketing", "Marketing"),
    ("sports", "Sports"),
    ("arts", "Arts"),
    ("social", "Social"),
    ("music", "Music"),
    ("food", "Food"),
    ("health", "Health"),
    ("education", "Education"),
    ("sustainability", "Sustainability"),
    ("law", "Law"),
    ("media", "Media"),
    ("design", "Design"),
    ("travel", "Travel"),
    ("community", "Community"),
]
INTEREST_LABELS = dict(INTERESTS)

# Free-text tags people use that mean the same interest as the slug.
INTEREST_SYNONYMS = {
    "entrepreneurship": ["startup", "startups", "founders", "venture"],
    "consulting": ["strategy", "case study"],
    "technology": ["tech", "ai", "data", "software", "engineering"],
    "finance": ["banking", "investment", "fintech"],
    "marketing": ["brand", "advertising", "growth"],
    "sports": ["sport", "fitness", "running"],
    "arts": ["art", "culture", "theatre"],
    "social": ["socialising", "networking", "meetup"],
    "music": ["gig", "concert", "nightlife"],
    "food": ["drink", "cuisine", "dining"],
    "health": ["wellbeing", "wellness", "healthcare"],
    "education": ["learning", "teaching", "students"],
    "sustainability": ["esg", "climate", "net zero", "environment"],
    "law": ["legal", "policy"],
    "media": ["journalism", "publishing", "content"],
    "design": ["ux", "product design", "creative"],
    "travel": ["abroad", "international"],
    "community": ["volunteering", "charity", "local"],
}


def interest_tags(slug: str) -> list[str]:
    """The tags that count as an interest: its slug plus every synonym."""
    return [slug, *INTEREST_SYNONYMS.get(slug, [])]


def visible() -> list:
    """Published, not a duplicate, and not finished yet."""
    return [
        Event.status == "published",
        Event.duplicate_of.is_(None),
        func.coalesce(Event.ends_at, Event.starts_at) >= func.now(),
    ]


def card_query():
    """Events with their organiser and category, one round trip."""
    return (
        select(Event, Organiser, Category)
        .outerjoin(Organiser, Event.organiser_id == Organiser.id)
        .outerjoin(Category, Event.category_id == Category.id)
    )


def card(event: Event, organiser: Organiser | None, category: Category | None, counts: dict | None = None) -> dict:
    """The event shape every list and feed returns."""
    organiser_brief = None
    if organiser is not None:
        organiser_brief = {
            "id": organiser.id,
            "slug": organiser.slug,
            "name": organiser.name,
            "logo_url": organiser.logo_url,
            "is_verified": organiser.is_verified,
            "kind": organiser.kind,
        }
    category_brief = None
    if category is not None:
        category_brief = {
            "id": category.id,
            "slug": category.slug,
            "name": category.name,
            "group": category.group_name,
        }
    return {
        "id": event.id,
        "title": event.title,
        "summary": event.summary,
        "cover_url": event.cover_url,
        "starts_at": event.starts_at,
        "ends_at": event.ends_at,
        "timezone": event.timezone,
        "format": event.format,
        "venue_name": event.venue_name,
        "address": event.address,
        "city": event.city,
        "country": event.country,
        "tags": list(event.tags or []),
        "university": event.university,
        "registration_mode": event.registration_mode,
        "external_url": event.external_url,
        "external_label": event.external_label,
        "requires_approval": event.requires_approval,
        "capacity": event.capacity,
        "waitlist_enabled": event.waitlist_enabled,
        "registration_deadline": event.registration_deadline,
        "price_min": event.price_min,
        "price_max": event.price_max,
        "currency": event.currency,
        "status": event.status,
        "is_featured": event.is_featured,
        "organiser_id": event.organiser_id,
        "category_id": event.category_id,
        "source": event.source,
        "source_url": event.source_url,
        "published_at": event.published_at,
        "created_at": event.created_at,
        "series_key": event.series_key,
        "series_label": event.series_label,
        "organiser": organiser_brief,
        "category": category_brief,
        "counts": dict(counts or EMPTY_COUNTS),
    }


async def counts_map(db: AsyncSession, event_ids: list[UUID]) -> dict[UUID, dict]:
    """going / waitlisted / pending keyed by event id."""
    ids = list(event_ids)
    if not ids:
        return {}
    return {
        row["event_id"]: {
            "going": int(row["going"]),
            "waitlisted": int(row["waitlisted"]),
            "pending": int(row["pending"]),
        }
        for row in await fn.event_counts(db, ids)
    }


def parse_ids(raw: str | None, *, limit: int = MAX_IDS, name: str = "ids") -> list[UUID]:
    """A comma-separated list of event ids: blanks dropped, duplicates removed, order kept.

    422 for a malformed id or more than `limit` ids.
    """
    ids: list[UUID] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = UUID(part)
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"{name}: {part!r} is not an event id"
            ) from None
        if value not in ids:
            ids.append(value)
    if len(ids) > limit:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"{name}: at most {limit} ids per request"
        )
    return ids


async def counts_rows(db: AsyncSession, event_ids: list[UUID]) -> list[dict]:
    """One counts row per requested id, zeros for events nobody registered for."""
    counts = await counts_map(db, event_ids)
    return [
        {"event_id": event_id, **counts.get(event_id, EMPTY_COUNTS)} for event_id in event_ids
    ]


async def cards(db: AsyncSession, stmt) -> list[dict]:
    """Run a `card_query()` statement and attach counts."""
    rows = (await db.execute(stmt)).all()
    items = [card(row[0], row[1], row[2]) for row in rows]
    counts = await counts_map(db, [item["id"] for item in items])
    for item in items:
        item["counts"] = counts.get(item["id"], dict(EMPTY_COUNTS))
    return items


async def event_row(db: AsyncSession, event_id: UUID):
    """An event with organiser and category, or None."""
    return (await db.execute(card_query().where(Event.id == event_id))).first()


def page(items: list[dict], total: int, page_number: int, page_size: int) -> dict:
    return {"items": items, "total": total, "page": page_number, "page_size": page_size}


def ticket_dict(ticket: TicketType) -> dict:
    return {
        "id": ticket.id,
        "event_id": ticket.event_id,
        "name": ticket.name,
        "description": ticket.description,
        "price": ticket.price,
        "currency": ticket.currency,
        "quantity": ticket.quantity,
        "sales_end_at": ticket.sales_end_at,
        "sort_order": ticket.sort_order,
    }


def question_dict(question: EventQuestion) -> dict:
    return {
        "id": question.id,
        "label": question.label,
        "kind": question.kind,
        "options": list(question.options or []),
        "required": question.required,
        "sort_order": question.sort_order,
    }


def registration_dict(registration: Registration) -> dict:
    return {
        "id": registration.id,
        "event_id": registration.event_id,
        "user_id": registration.user_id,
        "ticket_type_id": registration.ticket_type_id,
        "status": registration.status,
        "answers": registration.answers or {},
        "amount": registration.amount,
        "currency": registration.currency,
        "payment_status": registration.payment_status,
        "qr_code": registration.qr_code,
        "checked_in_at": registration.checked_in_at,
        "decided_at": registration.decided_at,
        "created_at": registration.created_at,
        "updated_at": registration.updated_at,
    }


async def detail(
    db: AsyncSession,
    event: Event,
    organiser: Organiser | None,
    category: Category | None,
    user: dict | None,
    can_manage: bool,
) -> dict:
    """Everything the event page needs, including what only this viewer may see."""
    counts = (await counts_map(db, [event.id])).get(event.id, dict(EMPTY_COUNTS))
    tickets = (
        await db.scalars(
            select(TicketType)
            .where(TicketType.event_id == event.id)
            .order_by(TicketType.sort_order, TicketType.created_at)
        )
    ).all()
    questions = (
        await db.scalars(
            select(EventQuestion)
            .where(EventQuestion.event_id == event.id)
            .order_by(EventQuestion.sort_order, EventQuestion.created_at)
        )
    ).all()

    registration: Registration | None = None
    marks: set[str] = set()
    private: dict | None = None
    if user:
        registration = (
            await db.scalars(
                select(Registration).where(
                    Registration.event_id == event.id, Registration.user_id == user["id"]
                )
            )
        ).first()
        marks = set(
            (
                await db.scalars(
                    select(UserEvent.kind).where(
                        UserEvent.user_id == user["id"], UserEvent.event_id == event.id
                    )
                )
            ).all()
        )
        if can_manage or (registration and registration.status == "going"):
            row = await db.get(EventPrivate, event.id)
            if row and (row.online_url or row.guest_info):
                private = {"online_url": row.online_url, "guest_info": row.guest_info}

    remaining = {
        row["ticket_type_id"]: row["remaining"] for row in await fn.ticket_availability(db, event.id)
    }

    series: list[dict] = []
    if event.series_key:
        dates = (
            await db.scalars(
                select(Event)
                .where(
                    Event.series_key == event.series_key,
                    Event.id != event.id,
                    Event.status == "published",
                    Event.duplicate_of.is_(None),
                )
                .order_by(Event.starts_at)
                .limit(25)
            )
        ).all()
        series = [
            {
                "id": other.id,
                "title": other.title,
                "starts_at": other.starts_at,
                "ends_at": other.ends_at,
                "status": other.status,
                "series_label": other.series_label,
            }
            for other in dates
        ]

    return {
        **card(event, organiser, category, counts),
        "description": event.description,
        "ticket_types": [
            {**ticket_dict(ticket), "remaining": remaining.get(ticket.id)} for ticket in tickets
        ],
        "event_questions": [question_dict(question) for question in questions],
        "spots_left": None if event.capacity is None else max(0, event.capacity - counts["going"]),
        "can_manage": can_manage,
        "my_registration": None if registration is None else registration_dict(registration),
        "saved": "saved" in marks,
        "external_registered": "external_registered" in marks,
        "event_private": private,
        "series": series,
    }


async def log_interaction(db: AsyncSession, event_id: UUID, kind: str, user_id: UUID | None) -> None:
    """Analytics row; anonymous interactions keep a null user."""
    db.add(EventInteraction(event_id=event_id, kind=kind, user_id=user_id))


def db_message(exc: DBAPIError) -> str:
    """The human sentence a Postgres function raised."""
    raw = getattr(exc, "orig", None) or exc
    message = str(raw).strip().splitlines()[0].strip()
    return message or "That did not work"


def _blank(value: Any) -> bool:
    """Mirrors `coalesce(trim(answers ->> id), '') in ('', '[]', 'false')`."""
    if value is None or value is False:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def missing_answers(questions: list[EventQuestion], answers: dict[str, Any]) -> list[str]:
    """Labels of required questions the answers do not cover."""
    return [question.label for question in questions if _blank(answers.get(str(question.id)))]


async def checkout_url(db: AsyncSession, registration: dict) -> str | None:
    """Ask the payments package for a Stripe session, when it provides one."""
    try:
        from app.payments.service import create_checkout_for_registration
    except ImportError:
        return None

    try:
        params = inspect.signature(create_checkout_for_registration).parameters.values()
        positional = [
            param
            for param in params
            if param.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        result = (
            create_checkout_for_registration(db, registration)
            if len(positional) > 1
            else create_checkout_for_registration(registration)
        )
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        logger.warning("Stripe checkout could not be created", exc_info=True)
        return None

    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return result.get("checkout_url") or result.get("url")
    return getattr(result, "checkout_url", None) or getattr(result, "url", None)


def calendar_ics(event: Event) -> str:
    """A single-event iCalendar file, folded and CRLF-terminated."""
    start = _utc(event.starts_at)
    end = _utc(event.ends_at) if event.ends_at else start + timedelta(hours=1)
    location = ", ".join(
        part for part in [event.venue_name, event.address, event.city, event.country] if part
    )
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Orbit//Events//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{event.id}@orbit",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{_escape(event.title)}",
    ]
    description = _description(event)
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    lines.append(f"URL:{settings.FRONTEND_URL.rstrip('/')}/events/{event.id}")
    if event.status == "cancelled":
        lines.append("STATUS:CANCELLED")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _description(event: Event) -> str:
    parts = [part.strip() for part in (event.summary, event.description) if part and part.strip()]
    return "\n\n".join(parts)[:1500]


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """Wrap a content line at 75 octets, continuing with a space."""
    chunks: list[str] = []
    current = ""
    size = 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            chunks.append(current)
            current = char
            size = width
        else:
            current += char
            size += width
    chunks.append(current)
    return "\r\n ".join(chunks)
