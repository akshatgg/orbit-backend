"""Event discovery, detail, tickets, marks and registrations."""

from datetime import datetime, time, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import current_user, require_user
from app.core.pagination import offset, pagination
from app.discovery.dates import DatePreset, date_preset_range
from app.discovery.places import clean_location
from app.discovery.rank import load_viewer, rank_events, sort_results
from app.events import service as svc
from app.events.models import Event
from app.events.schemas import (
    EventCountsRow,
    EventDetail,
    EventFormat,
    EventPage,
    EventSort,
    InteractionIn,
    MarkIn,
    MarkKind,
    MarkOut,
    RegistrationIn,
    RegistrationResponse,
    TicketAvailabilityOut,
)
from app.sql import functions as fn
from app.tickets.models import EventQuestion, Registration, TicketType
from app.users.models import UserEvent

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=EventPage)
async def list_events(
    q: str = "",
    category: list[str] = Query(default=[]),
    interest: list[str] = Query(default=[]),
    format: list[EventFormat] = Query(default=[]),
    date: DatePreset | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    country: str | None = Query(default=None, min_length=2, max_length=2),
    city: str | None = None,
    sort: EventSort = "soonest",
    pager: tuple[int, int] = Depends(pagination),
    user: dict | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Search published events like the frontend's Explore, one card per recurring series.

    `soonest`/`date`, `popular` and `newest` page through every match. `relevance` (personal when
    signed in) and `deadline` rank the frontend's candidate set (the 80 soonest matches, plus the
    80 soonest events of organisers whose name matches `q`) and set `truncated` when it was cut.
    """
    page_number, page_size = pager
    now = datetime.now(timezone.utc)
    text = q.strip()
    known_interests = {value for value, _ in svc.INTERESTS}
    conditions = svc.search_filters(
        categories=category,
        interests=[value for value in dict.fromkeys(interest) if value in known_interests],
        formats=list(dict.fromkeys(format)),
        date_range=date_preset_range(date, now) if date else None,
        country=country.upper() if country else None,
        city=clean_location(city or ""),
    )
    start = _moment(date_from)
    end = _moment(date_to, end=True)
    if start:
        conditions.append(func.coalesce(Event.ends_at, Event.starts_at) >= start)
    if end:
        conditions.append(Event.starts_at <= end)

    if sort in ("relevance", "deadline"):
        items, truncated = await svc.search_candidates(db, conditions, text)
        viewer = await load_viewer(db, user, state=False)
        ranked = sort_results(rank_events(items, viewer, now, keep_personal=True), sort, now)
        first = offset(page_number, page_size)
        return {
            **svc.page(ranked[first : first + page_size], len(ranked), page_number, page_size),
            "truncated": truncated,
        }

    if text:
        conditions.append(
            or_(svc.text_match(text), Event.organiser_id.in_(svc.organiser_match(text)))
        )
    conditions.append(svc.next_dates_only(conditions))

    stmt = svc.card_query().where(*conditions)
    if sort == "popular":
        going = (
            select(Registration.event_id, func.count().label("going"))
            .where(Registration.status == "going")
            .group_by(Registration.event_id)
            .subquery()
        )
        stmt = stmt.outerjoin(going, going.c.event_id == Event.id).order_by(
            func.coalesce(going.c.going, 0).desc(), Event.starts_at, Event.id
        )
    elif sort == "newest":
        stmt = stmt.order_by(Event.published_at.desc().nulls_last(), Event.created_at.desc())
    else:
        stmt = stmt.order_by(Event.starts_at, Event.id)

    total = await db.scalar(select(func.count()).select_from(Event).where(*conditions)) or 0
    items = await svc.cards(
        db, stmt.limit(page_size).offset(offset(page_number, page_size))
    )
    return svc.page(items, total, page_number, page_size)


@router.get("/upcoming", response_model=EventPage)
async def upcoming_events(
    pager: tuple[int, int] = Depends(pagination),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Published events that have not finished, soonest first, one card per series."""
    page_number, page_size = pager
    conditions = svc.visible()
    conditions.append(svc.next_dates_only(conditions))
    total = await db.scalar(select(func.count()).select_from(Event).where(*conditions)) or 0
    items = await svc.cards(
        db,
        svc.card_query()
        .where(*conditions)
        .order_by(Event.starts_at, Event.id)
        .limit(page_size)
        .offset(offset(page_number, page_size)),
    )
    return svc.page(items, total, page_number, page_size)


@router.get("/happening-soon", response_model=EventPage)
async def happening_soon(
    limit: int = Query(12, ge=1, le=40),
    user: dict | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The soonest events starting in the next fourteen days, one card per series.

    Signed in, each card carries the viewer's `score` and `reason`.
    """
    now = datetime.now(timezone.utc)
    items = await svc.feed_candidates(db, until=now + timedelta(days=14), limit=40)
    viewer = await load_viewer(db, user, state=False)
    ranked = sort_results(rank_events(items, viewer, now, keep_personal=True), "date", now)
    return svc.page(ranked[:limit], len(ranked[:limit]), 1, limit)


@router.get("/counts", response_model=list[EventCountsRow])
async def event_counts(
    ids: str = Query(description="Comma-separated event ids, at most 200"),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """Going / waitlisted / pending for each id, in the order asked (zeros when none)."""
    return await svc.counts_rows(db, svc.parse_ids(ids))


@router.get("/{event_id}", response_model=EventDetail)
async def get_event(
    event_id: UUID,
    user: dict | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One event with tickets, questions, counts and this viewer's state."""
    row = await svc.event_row(db, event_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")

    event = row[0]
    can_manage = bool(user) and await fn.can_manage_event(db, event_id)
    # Cancelled and expired events keep their page; drafts and removed ones are the team's only.
    if not (can_manage or svc.publicly_visible(event, row[1])):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")

    return await svc.detail(db, event, row[1], row[2], user, can_manage)


@router.get("/{event_id}/tickets", response_model=TicketAvailabilityOut)
async def event_tickets(
    event_id: UUID,
    user: dict | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Ticket types with live remaining counts."""
    await _visible_event(db, event_id, user)
    rows = await fn.ticket_availability(db, event_id)
    availability = {row["ticket_type_id"]: row for row in rows}
    tickets = (
        await db.scalars(
            select(TicketType)
            .where(TicketType.event_id == event_id)
            .order_by(TicketType.sort_order, TicketType.created_at)
        )
    ).all()
    return {
        "ticket_types": [
            {**svc.ticket_dict(ticket), "remaining": (availability.get(ticket.id) or {}).get("remaining")}
            for ticket in tickets
        ],
        "event_remaining": rows[0]["event_remaining"] if rows else None,
    }


@router.get("/{event_id}/calendar.ics", response_class=Response)
async def event_calendar(
    event_id: UUID,
    user: dict | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Download the event as an .ics file and record the calendar add."""
    event = await _visible_event(db, event_id, user)
    await svc.log_interaction(db, event.id, "calendar_add", user["id"] if user else None)
    return Response(
        content=svc.calendar_ics(event),
        media_type="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="orbit-{event.id}.ics"'},
    )


@router.get("/{event_id}/out")
async def event_out(
    event_id: UUID,
    user: dict | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Send the visitor to the organiser's own page and record the click."""
    event = await db.get(Event, event_id)
    if not event or event.registration_mode != "external" or not event.external_url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This event has no external link")
    await svc.log_interaction(db, event.id, "apply_click", user["id"] if user else None)
    return RedirectResponse(event.external_url, status_code=status.HTTP_302_FOUND)


@router.post(
    "/{event_id}/interactions",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def log_event_interaction(
    event_id: UUID,
    body: InteractionIn,
    user: dict | None = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Record a view, share, apply click or calendar add."""
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    await svc.log_interaction(db, event.id, body.kind, user["id"] if user else None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{event_id}/marks/{kind}", response_model=MarkOut)
async def mark_event(
    event_id: UUID,
    kind: MarkKind,
    body: MarkIn | None = None,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Save, dismiss, mark as registered elsewhere or attended."""
    await _event_or_404(db, event_id)
    if body is not None and not body.on:
        await _delete_mark(db, event_id, kind, user["id"])
        return {"kind": kind, "on": False}
    await db.execute(
        pg_insert(UserEvent)
        .values(user_id=user["id"], event_id=event_id, kind=kind)
        .on_conflict_do_nothing(index_elements=["user_id", "event_id", "kind"])
    )
    return {"kind": kind, "on": True}


@router.delete("/{event_id}/marks/{kind}", response_model=MarkOut)
async def unmark_event(
    event_id: UUID,
    kind: MarkKind,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove one of your marks from an event."""
    await _event_or_404(db, event_id)
    await _delete_mark(db, event_id, kind, user["id"])
    return {"kind": kind, "on": False}


@router.post("/{event_id}/registrations", response_model=RegistrationResponse)
async def register_for_event(
    event_id: UUID,
    body: RegistrationIn,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Register for an event; includes a Stripe checkout URL when payment is due."""
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")

    questions = (
        await db.scalars(
            select(EventQuestion).where(
                EventQuestion.event_id == event_id, EventQuestion.required.is_(True)
            )
        )
    ).all()
    missing = svc.missing_answers(questions, body.answers)
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Please answer: {', '.join(missing)}"
        )

    try:
        registration = await fn.register_for_event(
            db, event_id, body.ticket_type_id, body.answers
        )
    except DBAPIError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, svc.db_message(exc)) from exc

    if not registration:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This event is not open for registration")

    checkout = None
    if registration["status"] == "awaiting_payment":
        checkout = await svc.checkout_url(db, registration)
    return {"registration": registration, "checkout_url": checkout}


@router.delete(
    "/{event_id}/registrations/me",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def cancel_my_registration(
    event_id: UUID,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Cancel your registration and promote the next person on the waitlist."""
    try:
        await fn.cancel_registration(db, event_id)
    except DBAPIError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, svc.db_message(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _moment(value: datetime | None, *, end: bool = False) -> datetime | None:
    """Make a search date timezone-aware; a bare date covers that whole day."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if end and value.time() == time.min:
        value += timedelta(hours=24)
    return value


async def _event_or_404(db: AsyncSession, event_id: UUID) -> Event:
    event = await db.get(Event, event_id)
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return event


async def _visible_event(db: AsyncSession, event_id: UUID, user: dict | None) -> Event:
    """The event if the viewer may open its page (the same rule as GET /events/{id}), else 404."""
    row = await svc.event_row(db, event_id)
    if row and await svc.can_view(db, row[0], row[1], user):
        return row[0]
    raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")


async def _delete_mark(db: AsyncSession, event_id: UUID, kind: str, user_id: UUID) -> None:
    await db.execute(
        delete(UserEvent).where(
            UserEvent.user_id == user_id,
            UserEvent.event_id == event_id,
            UserEvent.kind == kind,
        )
    )
