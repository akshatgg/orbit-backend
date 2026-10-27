"""The signed-in person: profile, onboarding, preferences, their events and tickets."""

from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_user

router = APIRouter(prefix="/me", tags=["me"])

PROFILE_FIELDS = {
    "full_name", "avatar_url", "university", "department", "course", "year_of_study",
    "career_interests", "personal_interests", "preferred_categories", "preferred_formats",
    "goals", "user_type", "location_preference", "availability", "graduation_year",
    "company", "industry", "job_title", "host_role",
}

EVENT_CARD = """
select e.id, e.title, e.summary, e.cover_url, e.starts_at, e.ends_at, e.timezone,
       e.format, e.venue_name, e.city, e.country, e.category_id, e.tags, e.status,
       e.external_url, e.external_label, e.price_min, e.price_max, e.currency,
       e.registration_mode, e.capacity, e.series_key, e.series_label,
       o.name as organiser_name, o.slug as organiser_slug, o.logo_url as organiser_logo,
       (select count(*) from public.registrations r
         where r.event_id = e.id and r.status = 'going') as going,
       exists (select 1 from public.user_events ue
                where ue.event_id = e.id and ue.user_id = :uid and ue.kind = 'saved') as saved
  from public.events e
  left join public.organisers o on o.id = e.organiser_id
"""


def _ics(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y%m%dT%H%M%SZ")


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
                select o.id, o.name, o.slug, o.status, m.role
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
    body: dict[str, Any],
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update the editable profile fields. Role, email and verification are never writable here."""
    fields = {k: v for k, v in body.items() if k in PROFILE_FIELDS}
    if not fields:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nothing to update")
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    await db.execute(
        text(f"update public.profiles set {sets}, updated_at = now() where id = :uid"),
        {**fields, "uid": user["id"]},
    )
    return await me(user, db)


@router.post("/onboarding")
async def onboarding(
    body: dict[str, Any],
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Save the onboarding answers and mark the profile onboarded."""
    fields = {k: v for k, v in body.items() if k in PROFILE_FIELDS}
    if fields:
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        await db.execute(
            text(
                f"update public.profiles set {sets}, onboarded_at = now(),"
                " updated_at = now() where id = :uid"
            ),
            {**fields, "uid": user["id"]},
        )
    else:
        await db.execute(
            text("update public.profiles set onboarded_at = now() where id = :uid"),
            {"uid": user["id"]},
        )
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


@router.get("/events")
async def my_events(
    when: str = Query("upcoming", pattern="^(upcoming|past)$"),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Events I saved or registered for, split the way the profile page shows them."""
    op = ">=" if when == "upcoming" else "<"
    rows = (
        await db.execute(
            text(
                EVENT_CARD
                + f"""
                 where e.starts_at {op} now()
                   and (exists (select 1 from public.user_events ue
                                 where ue.event_id = e.id and ue.user_id = :uid
                                   and ue.kind in ('saved','attended'))
                        or exists (select 1 from public.registrations r
                                    where r.event_id = e.id and r.user_id = :uid
                                      and r.status in ('going','pending','waitlisted','awaiting_payment')))
                 order by e.starts_at {'asc' if when == 'upcoming' else 'desc'}
                """
            ),
            {"uid": user["id"]},
        )
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows), "when": when}


@router.get("/calendar")
async def my_calendar(
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Saved events and active registrations in a window (defaults: next 90 days)."""
    start = date_from or datetime.utcnow().isoformat()
    end = date_to or (datetime.utcnow() + timedelta(days=90)).isoformat()
    rows = (
        await db.execute(
            text(
                EVENT_CARD
                + """
                 where e.starts_at >= :start and e.starts_at <= :end
                   and (exists (select 1 from public.user_events ue
                                 where ue.event_id = e.id and ue.user_id = :uid and ue.kind = 'saved')
                        or exists (select 1 from public.registrations r
                                    where r.event_id = e.id and r.user_id = :uid
                                      and r.status = 'going'))
                 order by e.starts_at
                """
            ),
            {"uid": user["id"], "start": start, "end": end},
        )
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "total": len(rows)}


@router.get("/calendar.ics")
async def my_calendar_ics(
    user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
):
    """The same window as a downloadable calendar file."""
    data = await my_calendar(None, None, user, db)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Orbit//EN", "CALSCALE:GREGORIAN",
    ]
    for e in data["items"]:
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
