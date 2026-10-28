"""Studio queries: overview, the event editor, guest list, stats and the door.

Event creation/editing writes the rows directly (drafts only need ordinary
validation); registration, approval, check-in and payment rules stay in the
Postgres functions exposed by app/sql/functions.py.
"""

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.pagination import offset
from app.organisers.service import EVENT_COLUMNS, with_counts
from app.sql import functions as fn
from app.uploads.release import release_images

STATUSES = (
    "going",
    "pending",
    "waitlisted",
    "awaiting_payment",
    "declined",
    "cancelled",
)

GUEST_SELECT = """
select r.id, r.user_id, r.status, r.answers, r.amount, r.currency, r.payment_status,
       r.created_at, r.checked_in_at, p.full_name, p.email, tt.name as ticket
  from registrations r
  left join profiles p on p.id = r.user_id
  left join ticket_types tt on tt.id = r.ticket_type_id
 where r.event_id = :e
"""


async def _one(db: AsyncSession, sql: str, params: dict[str, Any]) -> Optional[dict]:
    row = (await db.execute(text(sql), params)).mappings().first()
    return dict(row) if row else None


async def _all(db: AsyncSession, sql: str, params: dict[str, Any]) -> list[dict]:
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings().all()]


async def _val(db: AsyncSession, sql: str, params: dict[str, Any]) -> Any:
    return (await db.execute(text(sql), params)).scalar()


async def _event(db: AsyncSession, event_id: UUID | str) -> dict:
    event = await _one(
        db, f"select {EVENT_COLUMNS} from events where id = :id", {"id": event_id}
    )
    if not event:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return event


# ─── overview ───────────────────────────────────────────────────────────────


async def overview(db: AsyncSession, organiser_id: UUID | str) -> dict:
    org = await _one(
        db,
        "select id, slug, name, status, logo_url from organisers where id = :o",
        {"o": organiser_id},
    )
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organiser not found")

    event_counts = await _one(
        db,
        """
        select
          count(*) filter (where status = 'draft')::int as drafts,
          count(*) filter (
            where status = 'published' and coalesce(ends_at, starts_at) >= now()
          )::int as upcoming,
          count(*) filter (
            where status in ('published', 'expired') and coalesce(ends_at, starts_at) < now()
          )::int as past,
          count(*) filter (where status in ('cancelled', 'removed'))::int as cancelled
          from events
         where organiser_id = :o and duplicate_of is null
        """,
        {"o": organiser_id},
    )
    event_counts["total"] = sum(
        event_counts[key] for key in ("drafts", "upcoming", "past", "cancelled")
    )

    by_status = {s: 0 for s in STATUSES}
    for row in await _all(
        db,
        """
        select r.status, count(*)::int as count
          from registrations r
          join events e on e.id = r.event_id
         where e.organiser_id = :o
         group by r.status
        """,
        {"o": organiser_id},
    ):
        if row["status"] in by_status:
            by_status[row["status"]] = int(row["count"])
    registrations = {
        **by_status,
        "active": sum(
            by_status[s] for s in ("going", "pending", "waitlisted", "awaiting_payment")
        ),
        "total": sum(by_status.values()),
    }

    next_event = await _one(
        db,
        f"""
        select {EVENT_COLUMNS} from events
         where organiser_id = :o and status = 'published' and duplicate_of is null
           and coalesce(ends_at, starts_at) >= now()
         order by starts_at
         limit 1
        """,
        {"o": organiser_id},
    )
    if next_event:
        next_event = (await with_counts(db, [next_event]))[0]

    return {
        "organiser": org,
        "events": event_counts,
        "followers": await fn.organiser_follower_count(db, organiser_id),
        "registrations": registrations,
        "next_event": next_event,
    }


# ─── editor ─────────────────────────────────────────────────────────────────


async def event_detail(db: AsyncSession, event_id: UUID | str) -> dict:
    event = await _event(db, event_id)
    private = await _one(
        db,
        "select online_url, guest_info, updated_at from event_private where event_id = :e",
        {"e": event_id},
    )
    tickets = await _all(
        db,
        "select id, name, description, price, currency, quantity, sales_end_at, sort_order"
        " from ticket_types where event_id = :e order by sort_order, created_at",
        {"e": event_id},
    )
    questions = await _all(
        db,
        "select id, label, kind, options, required, sort_order from event_questions"
        " where event_id = :e order by sort_order, created_at",
        {"e": event_id},
    )
    counts = {"going": 0, "waitlisted": 0, "pending": 0}
    rows = await fn.event_counts(db, [event["id"]])
    if rows:
        counts = {key: int(rows[0][key]) for key in counts}
    series = []
    if event["series_key"]:
        series = await _all(
            db,
            "select id, title, starts_at, ends_at, status from events"
            " where series_key = :key and id <> :id order by starts_at",
            {"key": event["series_key"], "id": event["id"]},
        )
    return {
        "event": event,
        "private": private,
        "ticket_types": tickets,
        "questions": questions,
        "counts": counts,
        "registration_total": counts["going"]
        + counts["waitlisted"]
        + counts["pending"],
        "series": series,
    }


async def _insert_tickets(
    db: AsyncSession, event_id: UUID, tickets: list[dict], currency: str
) -> None:
    for index, ticket in enumerate(tickets):
        await db.execute(
            text(
                """
                insert into ticket_types
                  (event_id, name, description, price, currency, quantity, sales_end_at, sort_order)
                values
                  (:event_id, :name, :description, :price, :currency, :quantity, :sales_end_at,
                   :sort_order)
                """
            ),
            {
                "event_id": event_id,
                "name": ticket["name"],
                "description": ticket.get("description"),
                "price": ticket["price"],
                "currency": ticket.get("currency") or currency,
                "quantity": ticket.get("quantity"),
                "sales_end_at": ticket.get("sales_end_at"),
                "sort_order": ticket.get("sort_order") or index,
            },
        )


async def _insert_questions(
    db: AsyncSession, event_id: UUID, questions: list[dict]
) -> None:
    for index, question in enumerate(questions):
        await db.execute(
            text(
                """
                insert into event_questions (event_id, label, kind, options, required, sort_order)
                values (:event_id, :label, :kind, :options, :required, :sort_order)
                """
            ),
            {
                "event_id": event_id,
                "label": question["label"],
                "kind": question["kind"],
                "options": question.get("options") or [],
                "required": question["required"],
                "sort_order": question.get("sort_order") or index,
            },
        )


async def _save_private(db: AsyncSession, event_id: UUID, private: dict) -> None:
    await db.execute(
        text(
            """
            insert into event_private (event_id, online_url, guest_info)
            values (:event_id, :online_url, :guest_info)
            on conflict (event_id) do update
              set online_url = excluded.online_url,
                  guest_info = excluded.guest_info,
                  updated_at = now()
            """
        ),
        {
            "event_id": event_id,
            "online_url": private.get("online_url"),
            "guest_info": private.get("guest_info"),
        },
    )


EVENT_INPUT_FIELDS = (
    "title",
    "summary",
    "description",
    "starts_at",
    "ends_at",
    "timezone",
    "format",
    "venue_name",
    "address",
    "city",
    "country",
    "category_id",
    "tags",
    "university",
    "registration_mode",
    "external_url",
    "external_label",
    "requires_approval",
    "capacity",
    "waitlist_enabled",
    "registration_deadline",
    "cover_url",
)


def _event_values(data: dict, *, partial: bool) -> dict:
    keys = [key for key in EVENT_INPUT_FIELDS if key in data or not partial]
    values = {key: data.get(key) for key in keys}
    if "tags" in values:
        values["tags"] = values["tags"] or []
    if "external_label" in values:
        values["external_label"] = values["external_label"] or "Apply now"
    return values


async def create_event(db: AsyncSession, data: dict, user_id: UUID | str) -> dict:
    data = dict(data)
    tickets = data.pop("ticket_types", []) or []
    questions = data.pop("questions", []) or []
    private = data.pop("private", None)
    values = _event_values(data, partial=False)

    event = await _one(
        db,
        f"""
        insert into events
          (organiser_id, title, summary, description, starts_at, ends_at, timezone, format,
           venue_name, address, city, country, category_id, tags, university, registration_mode,
           external_url, external_label, requires_approval, capacity, waitlist_enabled,
           registration_deadline, cover_url, status, source, created_by, currency)
        values
          (:organiser_id, :title, :summary, :description, :starts_at, :ends_at, :timezone, :format,
           :venue_name, :address, :city, :country, :category_id, :tags, :university,
           :registration_mode, :external_url, :external_label, :requires_approval, :capacity,
           :waitlist_enabled, :registration_deadline, :cover_url, 'draft', 'orbit', :created_by,
           :currency)
        returning {EVENT_COLUMNS}
        """,
        {
            **values,
            "organiser_id": data["organiser_id"],
            "created_by": user_id,
            "currency": settings.CURRENCY,
        },
    )
    await _insert_tickets(db, event["id"], tickets, event["currency"])
    await _insert_questions(db, event["id"], questions)
    if private is not None:
        await _save_private(db, event["id"], private)
    return event


async def update_event(db: AsyncSession, event_id: UUID | str, data: dict) -> dict:
    data = dict(data)
    tickets = data.pop("ticket_types", None)
    questions = data.pop("questions", None)
    private_provided = "private" in data
    private = data.pop("private", None) if private_provided else None
    values = _event_values(data, partial=True)
    # These columns are NOT NULL in the database, so an explicit null is ignored.
    values = {
        key: value
        for key, value in values.items()
        if key
        not in (
            "title",
            "starts_at",
            "timezone",
            "format",
            "registration_mode",
            "requires_approval",
            "waitlist_enabled",
        )
        or value is not None
    }

    event = await _event(db, event_id)

    mode = values.get("registration_mode") or event["registration_mode"]
    if mode == "external" and not (values.get("external_url") or event["external_url"]):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "External registration needs a link",
        )
    starts_at = values.get("starts_at") or event["starts_at"]
    ends_at = values.get("ends_at") or event["ends_at"]
    if ends_at and starts_at and ends_at < starts_at:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "ends_at cannot be before starts_at"
        )

    if values:
        sets = ", ".join(f"{key} = :{key}" for key in values)
        row = await _one(
            db,
            f"update events set {sets}, updated_at = now() where id = :id"
            f" returning {EVENT_COLUMNS}",
            {**values, "id": event_id},
        )
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
        event = row

    if tickets is not None:
        await db.execute(
            text("delete from ticket_types where event_id = :e"), {"e": event_id}
        )
        await _insert_tickets(db, event["id"], tickets, event["currency"])
    if questions is not None:
        await db.execute(
            text("delete from event_questions where event_id = :e"), {"e": event_id}
        )
        await _insert_questions(db, event["id"], questions)
    if private_provided:
        await _save_private(db, event["id"], private or {})
    return event


async def publish_event(db: AsyncSession, event_id: UUID | str) -> dict:
    event = await _event(db, event_id)
    if event["starts_at"] and event["starts_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "This event has already started"
        )
    row = await _one(
        db,
        "update events set status = 'published', published_at = now(), updated_at = now()"
        f" where id = :id returning {EVENT_COLUMNS}",
        {"id": event_id},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return row


async def cancel_event(db: AsyncSession, event_id: UUID | str, reason: str) -> dict:
    row = await _one(
        db,
        "update events set status = 'cancelled', cancel_reason = :reason, updated_at = now()"
        f" where id = :id returning {EVENT_COLUMNS}",
        {"id": event_id, "reason": reason.strip()},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return row


async def delete_event(db: AsyncSession, event_id: UUID | str) -> None:
    event = await _event(db, event_id)
    if event["status"] != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Only drafts can be deleted — cancel it instead"
        )
    await db.execute(text("delete from events where id = :id"), {"id": event_id})
    await release_images(db, [event["cover_url"]])


# ─── stats and guests ───────────────────────────────────────────────────────


async def event_stats(db: AsyncSession, event_id: UUID | str) -> dict:
    event = await _event(db, event_id)
    counts = {key: 0 for key in STATUSES}
    for row in await _all(
        db,
        "select status, count(*)::int as count from registrations"
        " where event_id = :e group by status",
        {"e": event_id},
    ):
        if row["status"] in counts:
            counts[row["status"]] = int(row["count"])

    checked_in = int(
        await _val(
            db,
            "select count(*) from registrations where event_id = :e and checked_in_at is not null",
            {"e": event_id},
        )
        or 0
    )
    interactions = await _one(
        db,
        """
        select count(*) filter (where kind = 'view')::int as views,
               count(*) filter (where kind = 'apply_click')::int as apply_clicks
          from event_interactions
         where event_id = :e
        """,
        {"e": event_id},
    )
    rows = await _all(
        db,
        """
        select to_char(created_at at time zone 'UTC', 'YYYY-MM-DD') as day, count(*)::int as count
          from registrations
         where event_id = :e
           and created_at >= date_trunc('day', now() at time zone 'UTC') - interval '13 days'
         group by 1
        """,
        {"e": event_id},
    )
    by_day_count = {row["day"]: int(row["count"]) for row in rows}
    today = datetime.now(timezone.utc).date()
    by_day = [
        {
            "day": (today - timedelta(days=days_ago)).isoformat(),
            "count": by_day_count.get(
                (today - timedelta(days=days_ago)).isoformat(), 0
            ),
        }
        for days_ago in range(13, -1, -1)
    ]

    return {
        "counts": counts,
        "total": sum(counts.values()),
        "checked_in": checked_in,
        "capacity": event["capacity"],
        "views": int(interactions["views"]) if interactions else 0,
        "apply_clicks": int(interactions["apply_clicks"]) if interactions else 0,
        "by_day": by_day,
    }


def _guest(row: dict) -> dict:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "name": row["full_name"],
        "email": row["email"],
        "status": row["status"],
        "ticket": row["ticket"],
        "answers": row["answers"] or {},
        "amount": row["amount"],
        "currency": row["currency"],
        "payment_status": row["payment_status"],
        "registered_at": row["created_at"],
        "checked_in_at": row["checked_in_at"],
    }


async def guests_page(
    db: AsyncSession,
    event_id: UUID | str,
    guest_status: Optional[str],
    page: int,
    page_size: int,
) -> dict:
    where = " and r.status = :status" if guest_status else ""
    params: dict[str, Any] = {"e": event_id}
    if guest_status:
        params["status"] = guest_status
    total = int(
        await _val(
            db,
            f"select count(*) from registrations r where r.event_id = :e{where}",
            params,
        )
        or 0
    )
    rows = await _all(
        db,
        f"{GUEST_SELECT}{where} order by r.created_at desc limit :lim offset :off",
        {**params, "lim": page_size, "off": offset(page, page_size)},
    )
    return {
        "items": [_guest(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _csv_cell(value: Any) -> Any:
    """Plain text for csv.writer; formulas and control characters are neutralised."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(item) for item in value)
    elif isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    rendered = str(value)
    if re.match(r"^[=+\-@\t\r]", rendered):
        rendered = f"'{rendered}"
    return rendered


def _filename(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60].rstrip("-")
    return f"{slug or 'event'}-guests.csv"


async def guests_csv(db: AsyncSession, event_id: UUID | str) -> tuple[str, str]:
    event = await _event(db, event_id)
    questions = await _all(
        db,
        "select id, label from event_questions where event_id = :e"
        " order by sort_order, created_at",
        {"e": event_id},
    )
    rows = await _all(db, f"{GUEST_SELECT} order by r.created_at desc", {"e": event_id})

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(
        ["Name", "Email", "Status", "Ticket", "Registered at", "Checked in at"]
        + [question["label"] for question in questions]
    )
    for row in rows:
        guest = _guest(row)
        answers = guest["answers"]
        writer.writerow(
            [
                _csv_cell(guest["name"]),
                _csv_cell(guest["email"]),
                _csv_cell(guest["status"]),
                _csv_cell(guest["ticket"]),
                _csv_cell(
                    guest["registered_at"].isoformat() if guest["registered_at"] else ""
                ),
                _csv_cell(
                    guest["checked_in_at"].isoformat() if guest["checked_in_at"] else ""
                ),
            ]
            + [_csv_cell(answers.get(str(question["id"]))) for question in questions]
        )
    return _filename(event["title"]), buffer.getvalue()


# ─── approvals and the door ─────────────────────────────────────────────────


async def _registration_in_event(
    db: AsyncSession, event_id: UUID | str, registration_id: UUID | str
) -> dict:
    row = await _one(
        db,
        "select id, qr_code from registrations where id = :r and event_id = :e",
        {"r": registration_id, "e": event_id},
    )
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration not found")
    return row


async def decide(
    db: AsyncSession, event_id: UUID | str, registration_id: UUID | str, decision: str
) -> dict:
    await _registration_in_event(db, event_id, registration_id)
    row = await fn.decide_registration(db, registration_id, decision)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration not found")
    return row


async def approve_all(db: AsyncSession, event_id: UUID | str) -> dict:
    ids = [
        row["id"]
        for row in await _all(
            db,
            "select id from registrations where event_id = :e and status = 'pending'"
            " order by created_at",
            {"e": event_id},
        )
    ]
    approved = 0
    for registration_id in ids:
        if await fn.decide_registration(db, registration_id, "approve"):
            approved += 1
    return {"ok": True, "approved": approved, "total": len(ids)}


async def check_in(db: AsyncSession, event_id: UUID | str, code: str) -> dict:
    result = await fn.check_in(db, event_id, code)
    payload = (result or {}).get("result") or {}
    return {"ok": payload.get("result") == "checked_in", **payload}


async def check_in_by_registration(
    db: AsyncSession, event_id: UUID | str, registration_id: UUID | str
) -> dict:
    registration = await _registration_in_event(db, event_id, registration_id)
    return await check_in(db, event_id, registration["qr_code"])


async def undo_check_in(
    db: AsyncSession, event_id: UUID | str, registration_id: UUID | str
) -> None:
    await _registration_in_event(db, event_id, registration_id)
    await fn.undo_check_in(db, registration_id)


async def check_in_count(db: AsyncSession, event_id: UUID | str) -> dict:
    row = await _one(
        db,
        """
        select count(*) filter (where checked_in_at is not null)::int as checked_in,
               count(*) filter (where status = 'going')::int as going
          from registrations
         where event_id = :e
        """,
        {"e": event_id},
    )
    return {"checked_in": int(row["checked_in"]), "going": int(row["going"])}
