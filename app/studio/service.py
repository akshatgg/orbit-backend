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
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.pagination import offset
from app.notifications.service import (
    logistics_changed,
    notify_event_changed,
    notify_registration_changed,
)
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


# ─── studio event list ──────────────────────────────────────────────────────


async def list_events(
    db: AsyncSession, organiser_id: UUID | str, page: int, page_size: int
) -> dict:
    """Every event of one organiser, any status, newest start first, with counts."""
    total = int(
        await _val(
            db, "select count(*) from events where organiser_id = :o", {"o": organiser_id}
        )
        or 0
    )
    items = await _all(
        db,
        f"select {EVENT_COLUMNS} from events where organiser_id = :o"
        " order by starts_at desc, created_at desc limit :lim offset :off",
        {"o": organiser_id, "lim": page_size, "off": offset(page, page_size)},
    )
    return {
        "items": await with_counts(db, items),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ─── guards ─────────────────────────────────────────────────────────────────

# Registrations that hold a place (the frontend's ACTIVE_REGISTRATION).
ACTIVE_REGISTRATION = ["going", "pending", "waitlisted", "awaiting_payment"]


async def _organiser(db: AsyncSession, organiser_id: UUID | str) -> dict:
    org = await _one(
        db,
        "select id, name, slug, status, stripe_charges_enabled from organisers where id = :o",
        {"o": organiser_id},
    )
    if not org:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organiser not found")
    return org


def _require_approved(org: dict) -> None:
    """Only approved organisers create, edit or publish events."""
    if org["status"] != "approved":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "organiser_not_approved")


def _require_charges(org: dict, prices: list[Any]) -> None:
    """A live event can only sell paid tickets once Stripe payouts are connected."""
    paid = any(Decimal(str(price or 0)) > 0 for price in prices)
    if paid and not org["stripe_charges_enabled"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "stripe_not_ready")


def _require_editable(event: dict) -> None:
    if event["status"] in ("cancelled", "removed"):
        raise HTTPException(status.HTTP_409_CONFLICT, "event_not_editable")


async def _has_active_registrations(
    db: AsyncSession, event_id: UUID | str, ticket_type_ids: list[str] | None = None
) -> bool:
    where = " and ticket_type_id = any(:tt)" if ticket_type_ids is not None else ""
    params: dict[str, Any] = {"e": event_id, "active": ACTIVE_REGISTRATION}
    if ticket_type_ids is not None:
        params["tt"] = [UUID(str(i)) for i in ticket_type_ids]
    return bool(
        await _val(
            db,
            "select exists(select 1 from registrations"
            f" where event_id = :e and status = any(:active){where})",
            params,
        )
    )


async def _ticket_prices(db: AsyncSession, event_id: UUID | str) -> list[Any]:
    return [
        row["price"]
        for row in await _all(
            db, "select price from ticket_types where event_id = :e", {"e": event_id}
        )
    ]


# ─── editor ─────────────────────────────────────────────────────────────────


async def event_detail(db: AsyncSession, event_id: UUID | str) -> dict:
    event = await _event(db, event_id)
    organiser = await _one(
        db,
        "select id, name, slug from organisers where id = :o",
        {"o": event["organiser_id"]},
    )
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
        "organiser": organiser,
        "private": private,
        "ticket_types": tickets,
        "questions": questions,
        "counts": counts,
        "registration_total": counts["going"]
        + counts["waitlisted"]
        + counts["pending"],
        "series": series,
    }


# Ticket types and questions are synced row by row (the frontend's syncChildren):
# a row with an id that belongs to the event is updated in place, a row without
# one is inserted, and a current row that is not submitted is deleted. Ticket
# rows that are kept keep their id, so registrations keep their ticket_type_id
# (the foreign key is `on delete set null`).

CHILD_TABLES = ("ticket_types", "event_questions")


def _child_values(table: str, row: dict, index: int, currency: str) -> dict:
    sort_order = row.get("sort_order") or index
    if table == "ticket_types":
        return {
            "name": row["name"],
            "description": row.get("description"),
            "price": row["price"],
            "currency": row.get("currency") or currency,
            "quantity": row.get("quantity"),
            "sales_end_at": row.get("sales_end_at"),
            "sort_order": sort_order,
        }
    return {
        "label": row["label"],
        "kind": row["kind"],
        "options": row.get("options") or [],
        "required": row["required"],
        "sort_order": sort_order,
    }


async def _child_plan(
    db: AsyncSession, table: str, event_id: UUID | str, rows: list[dict]
) -> tuple[set[str], list[str]]:
    """Which current rows are kept (submitted with their id) and which are removed."""
    assert table in CHILD_TABLES
    current = {
        str(row["id"])
        for row in await _all(
            db, f"select id from {table} where event_id = :e", {"e": event_id}
        )
    }
    keep = {str(row["id"]) for row in rows if row.get("id") and str(row["id"]) in current}
    remove = sorted(current - keep)
    return keep, remove


async def _sync_children(
    db: AsyncSession,
    table: str,
    event_id: UUID | str,
    rows: list[dict],
    currency: str,
    keep: set[str],
    remove: list[str],
) -> list[str]:
    """Apply a plan from _child_plan; returns the row ids in submitted order."""
    assert table in CHILD_TABLES
    if remove:
        await db.execute(
            text(f"delete from {table} where event_id = :e and id = any(:ids)"),
            {"e": event_id, "ids": [UUID(i) for i in remove]},
        )
    ids: list[str] = []
    for index, row in enumerate(rows):
        values = _child_values(table, row, index, currency)
        row_id = str(row["id"]) if row.get("id") else None
        if row_id and row_id in keep:
            sets = ", ".join(f"{key} = :{key}" for key in values)
            await db.execute(
                text(f"update {table} set {sets} where id = :id and event_id = :e"),
                {**values, "id": row_id, "e": event_id},
            )
            ids.append(row_id)
        else:
            columns = ", ".join(values)
            placeholders = ", ".join(f":{key}" for key in values)
            new_id = await _val(
                db,
                f"insert into {table} (event_id, {columns}) values (:e, {placeholders})"
                " returning id",
                {**values, "e": event_id},
            )
            ids.append(str(new_id))
    return ids


async def _child_ids(db: AsyncSession, table: str, event_id: UUID | str) -> list[str]:
    assert table in CHILD_TABLES
    return [
        str(row["id"])
        for row in await _all(
            db,
            f"select id from {table} where event_id = :e order by sort_order, created_at",
            {"e": event_id},
        )
    ]


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


async def _with_child_ids(
    db: AsyncSession,
    event_id: UUID | str,
    ticket_ids: list[str] | None,
    question_ids: list[str] | None,
) -> dict:
    """The saved event (fresh, so trigger-set prices show) and its child ids in order."""
    event = await _event(db, event_id)
    event["ticket_type_ids"] = (
        ticket_ids
        if ticket_ids is not None
        else await _child_ids(db, "ticket_types", event_id)
    )
    event["question_ids"] = (
        question_ids
        if question_ids is not None
        else await _child_ids(db, "event_questions", event_id)
    )
    return event


async def create_event(db: AsyncSession, data: dict, user_id: UUID | str) -> dict:
    data = dict(data)
    tickets = data.pop("ticket_types", []) or []
    questions = data.pop("questions", []) or []
    private = data.pop("private", None)
    values = _event_values(data, partial=False)

    _require_approved(await _organiser(db, data["organiser_id"]))

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
    ticket_ids = await _sync_children(
        db, "ticket_types", event["id"], tickets, event["currency"], set(), []
    )
    question_ids = await _sync_children(
        db, "event_questions", event["id"], questions, event["currency"], set(), []
    )
    if private is not None:
        await _save_private(db, event["id"], private)
    return await _with_child_ids(db, event["id"], ticket_ids, question_ids)


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
    before = dict(event)
    _require_editable(event)
    org = await _organiser(db, event["organiser_id"])
    _require_approved(org)

    mode = values.get("registration_mode") or event["registration_mode"]
    if (
        event["registration_mode"] == "tickets"
        and mode != "tickets"
        and await _has_active_registrations(db, event_id)
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "has_registrations")
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

    ticket_plan = question_plan = None
    if tickets is not None:
        ticket_plan = await _child_plan(db, "ticket_types", event_id, tickets)
        removed = ticket_plan[1]
        if removed and await _has_active_registrations(db, event_id, removed):
            raise HTTPException(status.HTTP_409_CONFLICT, "ticket_type_has_guests")
    if questions is not None:
        question_plan = await _child_plan(db, "event_questions", event_id, questions)

    # A live event saves with publish rules (saveEvent), so paid tickets need payouts.
    if event["status"] == "published" and mode == "tickets":
        prices = (
            [ticket["price"] for ticket in tickets]
            if tickets is not None
            else await _ticket_prices(db, event_id)
        )
        _require_charges(org, prices)

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

    ticket_ids = question_ids = None
    if tickets is not None and ticket_plan is not None:
        ticket_ids = await _sync_children(
            db, "ticket_types", event["id"], tickets, event["currency"], *ticket_plan
        )
    if questions is not None and question_plan is not None:
        question_ids = await _sync_children(
            db, "event_questions", event["id"], questions, event["currency"], *question_plan
        )
    if private_provided:
        await _save_private(db, event["id"], private or {})
    # Tell guests when a live event's time or place moved (saveEvent step 5).
    if before["status"] == "published" and logistics_changed(before, event):
        await notify_event_changed(db, event["id"], "updated")
    return await _with_child_ids(db, event["id"], ticket_ids, question_ids)


async def publish_event(db: AsyncSession, event_id: UUID | str) -> dict:
    event = await _event(db, event_id)
    if event["status"] == "published":
        return event
    if event["status"] != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "event_not_publishable")
    org = await _organiser(db, event["organiser_id"])
    _require_approved(org)
    if event["starts_at"] and event["starts_at"] < datetime.now(timezone.utc):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "This event has already started"
        )
    if event["registration_mode"] == "tickets":
        _require_charges(org, await _ticket_prices(db, event_id))
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
    """Only a published event can be cancelled (drafts are deleted instead)."""
    row = await _one(
        db,
        "update events set status = 'cancelled', cancel_reason = :reason, updated_at = now()"
        f" where id = :id and status = 'published' returning {EVENT_COLUMNS}",
        {"id": event_id, "reason": reason.strip()},
    )
    if not row:
        await _event(db, event_id)  # 404 when it does not exist
        raise HTTPException(status.HTTP_409_CONFLICT, "event_not_published")
    await notify_event_changed(db, row["id"], "cancelled")
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
    before = await _waitlisted(db, event_id) if decision == "decline" else set()
    row = await fn.decide_registration(db, registration_id, decision)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registration not found")
    await notify_registration_changed(db, [registration_id], reason="decision")
    # A decline frees a spot and the database moves people off the waitlist: tell them too.
    if before:
        promoted = before - await _waitlisted(db, event_id)
        await notify_registration_changed(db, list(promoted), reason="waitlist")
    return row


async def _waitlisted(db: AsyncSession, event_id: UUID | str) -> set[UUID]:
    return {
        row["id"]
        for row in await _all(
            db,
            "select id from registrations where event_id = :e and status = 'waitlisted'",
            {"e": event_id},
        )
    }


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
    approved: list[UUID] = []
    for registration_id in ids:
        if await fn.decide_registration(db, registration_id, "approve"):
            approved.append(registration_id)
    await notify_registration_changed(db, approved, reason="approve_all")
    return {"ok": True, "approved": len(approved), "total": len(ids)}


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


# ─── more dates (a series) ──────────────────────────────────────────────────

# Editor columns a new date copies from the event it was added to (formToEventRow).
DATE_COPY_COLUMNS = (
    "organiser_id, title, summary, description, cover_url, timezone, format, venue_name,"
    " address, city, country, latitude, longitude, category_id, tags, university,"
    " registration_mode, external_url, external_label, requires_approval, capacity,"
    " waitlist_enabled, price_min, price_max, currency"
)

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _zone(name: Optional[str]) -> ZoneInfo:
    try:
        return ZoneInfo(name or settings.DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(settings.DEFAULT_TIMEZONE)


def _shift_local_days(
    value: Optional[datetime], days: int, tz: ZoneInfo
) -> Optional[datetime]:
    """The same wall-clock time `days` later in the event's zone (shiftLocalDays)."""
    if value is None or days == 0:
        return value
    local = value.astimezone(tz).replace(tzinfo=None) + timedelta(days=days)
    return local.replace(tzinfo=tz).astimezone(timezone.utc)


def weekly_label(starts: list[datetime], tz: ZoneInfo) -> Optional[str]:
    """"Every week on Wednesday until 9 December 2026" when the dates are exactly a
    week apart (by local calendar day), otherwise None (repeat.ts weeklyLabel)."""
    local = sorted(start.astimezone(tz) for start in starts)
    if len(local) < 2:
        return None
    for before, after in zip(local, local[1:]):
        if (after.date() - before.date()).days != 7:
            return None
    first, last = local[0], local[-1]
    return (
        f"Every week on {WEEKDAYS[first.weekday()]} until"
        f" {last.day} {MONTHS[last.month - 1]} {last.year}"
    )


async def create_dates(
    db: AsyncSession, event_id: UUID | str, dates: list[dict], user_id: UUID | str
) -> dict:
    """More dates for an event: each is its own event with the same details, tickets,
    questions and guest-only details, all sharing one series (createMoreDates)."""
    parent = await _event(db, event_id)
    if parent["status"] not in ("draft", "published"):
        raise HTTPException(status.HTTP_409_CONFLICT, "event_not_editable")
    org = await _organiser(db, parent["organiser_id"])
    _require_approved(org)

    series_key = parent["series_key"] or f"orbit:{parent['id']}"
    existing = {
        row["starts_at"]
        for row in await _all(
            db,
            "select starts_at from events where series_key = :k and organiser_id = :o"
            " and status <> 'removed'",
            {"k": series_key, "o": parent["organiser_id"]},
        )
    } | {parent["starts_at"]}
    now = datetime.now(timezone.utc)
    for slot in dates:
        if slot["starts_at"] <= now:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "date_in_past")
        if slot["starts_at"] in existing:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "duplicate_date")

    tickets = await _all(
        db,
        "select name, description, price, currency, quantity, sales_end_at, sort_order"
        " from ticket_types where event_id = :e order by sort_order, created_at",
        {"e": parent["id"]},
    )
    questions = await _all(
        db,
        "select label, kind, options, required, sort_order from event_questions"
        " where event_id = :e order by sort_order, created_at",
        {"e": parent["id"]},
    )
    private = await _one(
        db,
        "select online_url, guest_info from event_private where event_id = :e",
        {"e": parent["id"]},
    )
    publish = parent["status"] == "published"
    if publish and parent["registration_mode"] == "tickets":
        _require_charges(org, [ticket["price"] for ticket in tickets])

    if not parent["series_key"]:
        await db.execute(
            text("update events set series_key = :k, updated_at = now() where id = :id"),
            {"k": series_key, "id": parent["id"]},
        )

    tz = _zone(parent["timezone"])
    parent_day = parent["starts_at"].astimezone(tz).date()
    created: list[dict] = []
    for slot in dates:
        days = (slot["starts_at"].astimezone(tz).date() - parent_day).days
        row = await _one(
            db,
            f"""
            insert into events
              ({DATE_COPY_COLUMNS}, starts_at, ends_at, registration_deadline, series_key,
               status, source, created_by)
            select {DATE_COPY_COLUMNS}, :starts_at, :ends_at, :deadline, :series_key,
                   :status, 'orbit', :created_by
              from events where id = :parent
            returning id, starts_at, ends_at, status
            """,
            {
                "starts_at": slot["starts_at"],
                "ends_at": slot.get("ends_at"),
                "deadline": _shift_local_days(parent["registration_deadline"], days, tz),
                "series_key": series_key,
                "status": "published" if publish else "draft",
                "created_by": user_id,
                "parent": parent["id"],
            },
        )
        shifted = [
            {**ticket, "sales_end_at": _shift_local_days(ticket["sales_end_at"], days, tz)}
            for ticket in tickets
        ]
        await _sync_children(db, "ticket_types", row["id"], shifted, parent["currency"], set(), [])
        await _sync_children(
            db, "event_questions", row["id"], questions, parent["currency"], set(), []
        )
        if private:
            await _save_private(db, row["id"], private)
        created.append(row)

    starts = [
        row["starts_at"]
        for row in await _all(
            db,
            "select starts_at from events where series_key = :k and organiser_id = :o"
            " and status <> 'removed'",
            {"k": series_key, "o": parent["organiser_id"]},
        )
    ]
    label = weekly_label(starts, tz)
    await db.execute(
        text(
            "update events set series_label = :label, updated_at = now()"
            " where series_key = :k and organiser_id = :o"
        ),
        {"label": label, "k": series_key, "o": parent["organiser_id"]},
    )
    return {
        "series_key": series_key,
        "series_label": label,
        "count": len(created),
        "events": created,
    }
