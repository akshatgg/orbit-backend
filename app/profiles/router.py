"""The signed-in person: profile, onboarding, preferences, their events and tickets."""

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_user
from app.events import service as events_svc
from app.profiles import service
from app.profiles.schemas import EventState, MyCalendar, MyEventsPage, ProfileUpdate

router = APIRouter(prefix="/me", tags=["me"])


def _ics(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@router.get("")
async def me(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Profile plus the counts the header/side nav shows."""
    row = (
        await db.execute(
            text("select * from public.profiles where id = :id"), {"id": user["id"]}
        )
    ).mappings().first()
    counts = (
        await db.execute(
            text(
                """
                select
                  (select count(*) from public.user_events
                    where user_id = :id and kind = 'saved') as saved,
                  (select count(*) from public.registrations
                    where user_id = :id and status in ('going','pending','awaiting_payment')) as tickets,
                  (select count(*) from public.groups g
                    join public.group_members m on m.group_id = g.id
                    where m.user_id = :id and g.kind = 'circle') as circles,
                  (select count(*) from public.group_members m
                    join public.groups g on g.id = m.group_id
                    where m.user_id = :id and g.kind = 'community') as communities
                """
            ),
            {"id": user["id"]},
        )
    ).mappings().first()
    orgs = (
        await db.execute(
            text(
                """
                select o.*, m.role
                  from public.organiser_members m
                  join public.organisers o on o.id = m.organiser_id
                 where m.user_id = :id
                 order by o.name
                """
            ),
            {"id": user["id"]},
        )
    ).mappings().all()
    return {"profile": dict(row or {}), **dict(counts or {}), "organisers": [dict(o) for o in orgs]}


@router.patch("")
async def update_profile(
    body: ProfileUpdate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update the editable profile fields that were sent. Unknown keys are a 422.

    `preferred_categories` keeps only slugs that exist.
    """
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing to update")
    await service.update_profile(db, user["id"], fields)
    return await me(user, db)


@router.post("/onboarding")
async def onboarding(
    body: ProfileUpdate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Save the onboarding answers and mark the profile onboarded."""
    await service.update_profile(db, user["id"], body.model_dump(exclude_unset=True), onboard=True)
    return await me(user, db)


@router.put("/preferences")
async def preferences(
    body: dict[str, Any],
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Preferences only — categories, formats, interests, location, availability."""
    allowed = {
        "preferred_categories", "preferred_formats", "career_interests",
        "personal_interests", "location_preference", "availability", "goals",
    }
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing to update")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    await db.execute(
        text(f"update public.profiles set {sets}, updated_at = now() where id = :uid"),
        {**fields, "uid": user["id"]},
    )
    return {"ok": True}


@router.get("/event-state", response_model=EventState)
async def event_state(
    event_ids: str | None = Query(
        None, description="Comma-separated event ids, at most 200. Omit for every event."
    ),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Which events I saved, dismissed, registered for elsewhere or attended, and my
    registration status per event."""
    ids = None if event_ids is None else events_svc.parse_ids(event_ids, name="event_ids")
    return await service.event_state(db, user["id"], ids)


@router.get("/events", response_model=MyEventsPage)
async def my_events(
    when: Literal["upcoming", "past"] = "upcoming",
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Events I saved, attended, registered for here or elsewhere.

    `upcoming`: not ended yet, soonest first. `past`: ended, latest first.
    """
    items = await service.my_events(db, user["id"], when)
    return {"items": items, "total": len(items), "when": when}


@router.get("/calendar", response_model=MyCalendar)
async def my_calendar(
    date_from: datetime | None = Query(None, alias="from"),
    date_to: datetime | None = Query(None, alias="to"),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Saved, externally registered and registered events overlapping [from, to).

    Defaults: now to 90 days ahead. Cancelled events are included.
    """
    start = _aware(date_from) or datetime.now(timezone.utc)
    end = _aware(date_to) or start + timedelta(days=90)
    items = await service.my_calendar(db, user["id"], start, end)
    return {"items": items, "total": len(items)}


@router.get("/calendar.ics")
async def my_calendar_ics(
    user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """My upcoming calendar (published events only) as a downloadable calendar file."""
    items = await service.my_calendar(
        db, user["id"], datetime.now(timezone.utc), None, statuses=("published",)
    )
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Orbit//EN", "CALSCALE:GREGORIAN",
    ]
    for e in items:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e['id']}@orbit",
            f"DTSTART:{_ics(e['starts_at'])}",
            f"DTEND:{_ics(e['ends_at'] or e['starts_at'] + timedelta(hours=2))}",
            f"SUMMARY:{e['title']}",
            f"LOCATION:{e.get('venue_name') or e.get('city') or ''}",
            f"DESCRIPTION:{(e.get('summary') or '').replace(chr(10), ' ')}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return Response(
        "\r\n".join(lines),
        media_type="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="orbit.ics"'},
    )


@router.get("/tickets")
async def my_tickets(
    tab: str = Query("upcoming", pattern="^(upcoming|past)$"),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """My registrations with the event and ticket type."""
    op = ">=" if tab == "upcoming" else "<"
    rows = (
        await db.execute(
            text(
                """
                select r.id, r.status, r.payment_status, r.amount, r.currency, r.qr_code,
                       r.checked_in_at, r.created_at, r.answers,
                       e.id as event_id, e.title, e.starts_at, e.ends_at, e.timezone,
                       e.venue_name, e.address, e.city, e.cover_url, e.external_url,
                       t.name as ticket_type
                  from public.registrations r
                  join public.events e on e.id = r.event_id
                  left join public.ticket_types t on t.id = r.ticket_type_id
                 where r.user_id = :uid and e.starts_at {} now()
                 order by e.starts_at {}
                """.format(op, "asc" if tab == "upcoming" else "desc")
            ),
            {"uid": user["id"]},
        )
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows), "tab": tab}


@router.get("/tickets/{registration_id}")
async def my_ticket(
    registration_id: str,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """One ticket, with the private guest details only a guest may see."""
    row = (
        await db.execute(
            text(
                """
                select r.*, e.title, e.starts_at, e.ends_at, e.timezone, e.venue_name,
                       e.address, e.city, e.cover_url, e.external_url,
                       t.name as ticket_type,
                       case when r.status = 'going' then p.online_url end as online_url,
                       case when r.status = 'going' then p.guest_info end as guest_info
                  from public.registrations r
                  join public.events e on e.id = r.event_id
                  left join public.ticket_types t on t.id = r.ticket_type_id
                  left join public.event_private p on p.event_id = e.id
                 where r.id = :id and r.user_id = :uid
                """
            ),
            {"id": registration_id, "uid": user["id"]},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ticket not found")
    return dict(row)


@router.get("/following")
async def following(user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)) -> dict:
    """Organisers I follow."""
    rows = (
        await db.execute(
            text(
                """
                select o.id, o.name, o.slug, o.logo_url, o.university, o.is_verified,
                       (select count(*) from public.follows f2 where f2.organiser_id = o.id) as followers
                  from public.follows f
                  join public.organisers o on o.id = f.organiser_id
                 where f.user_id = :uid
                 order by o.name
                """
            ),
            {"uid": user["id"]},
        )
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows)}
