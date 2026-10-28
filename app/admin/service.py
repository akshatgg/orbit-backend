"""Admin write paths: event creation/editing and the CSV importer."""

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.schemas import EventCreate, EventUpdate
from app.categories.models import Category
from app.core.config import settings
from app.events.models import Event
from app.organisers.models import Organiser
from app.sql import functions as fn

EDITABLE_FIELDS: tuple[str, ...] = (
    "title",
    "summary",
    "description",
    "cover_url",
    "starts_at",
    "ends_at",
    "timezone",
    "format",
    "venue_name",
    "address",
    "city",
    "country",
    "latitude",
    "longitude",
    "category_id",
    "organiser_id",
    "tags",
    "university",
    "registration_mode",
    "external_url",
    "external_label",
    "requires_approval",
    "capacity",
    "waitlist_enabled",
    "registration_deadline",
    "price_min",
    "price_max",
    "currency",
    "is_featured",
    "source_url",
)

_TIMESTAMP_FIELDS = {"starts_at", "ends_at", "registration_deadline"}
FORMAT_ALIASES = {
    "in_person": "in_person",
    "in person": "in_person",
    "in-person": "in_person",
    "inperson": "in_person",
    "online": "online",
    "virtual": "online",
    "hybrid": "hybrid",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo(settings.DEFAULT_TIMEZONE)).astimezone(timezone.utc)
    return value.astimezone(timezone.utc)


def _convert(values: dict[str, Any]) -> dict[str, Any]:
    converted: dict[str, Any] = {}
    for key, value in values.items():
        if key in _TIMESTAMP_FIELDS:
            converted[key] = _aware(value) if value is not None else None
        elif key == "country":
            converted[key] = value.upper() if value else None
        elif key == "currency":
            converted[key] = value.upper() if value else value
        else:
            converted[key] = value
    return converted


def _validate(values: dict[str, Any]) -> None:
    starts = values.get("starts_at")
    ends = values.get("ends_at")
    if starts and ends and ends < starts:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "ends_at must be on or after starts_at")
    if values.get("registration_mode") == "external" and not values.get("external_url"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "External registration needs a link")
    title = values.get("title")
    if title is not None and not (3 <= len(str(title)) <= 140):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Title must be 3–140 characters")


async def create_event(db: AsyncSession, payload: EventCreate, actor_id: UUID) -> UUID:
    """Insert an admin-created published event (source "manual")."""
    values = _convert(payload.model_dump())
    _validate(values)
    now = _now()
    values.update(
        {
            "status": "published",
            # Added by an admin, as the frontend stored it (not an organiser's "orbit" event).
            "source": "manual",
            "published_at": now,
            "created_by": actor_id,
            "needs_review": False,
            "last_verified_at": now,
            "updated_at": now,
        }
    )
    try:
        event_id = (
            await db.execute(insert(Event).values(**values).returning(Event.id))
        ).scalar_one()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Check the organiser, category and event fields",
        ) from exc
    await fn.log_admin_action(
        db, "event.create", "event", str(event_id), {"title": values["title"]}
    )
    return event_id


async def update_event(db: AsyncSession, event: Event, payload: EventUpdate) -> None:
    """Apply an admin edit, locking tags when the admin supplies their own."""
    supplied = payload.model_dump(exclude_unset=True)
    changes = _convert(supplied)
    merged = {field: getattr(event, field) for field in EDITABLE_FIELDS}
    merged.update(changes)
    _validate(merged)
    if "tags" in changes:
        changes["tags_locked"] = True
    changes["updated_at"] = _now()
    try:
        await db.execute(update(Event).where(Event.id == event.id).values(**changes))
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Check the organiser, category and event fields",
        ) from exc
    await fn.log_admin_action(
        db,
        "event.update",
        "event",
        str(event.id),
        {"title": merged.get("title"), "fields": sorted(supplied)},
    )


def _text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _line(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text else None


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return _aware(value)
    raw = str(value).strip()
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        raise ValueError(f'"{raw}" is not a date')
    return _aware(parsed)


def _parse_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw or raw == "free":
        return Decimal("0") if raw == "free" else None
    raw = raw.replace("£", "").replace(",", "")
    try:
        price = Decimal(raw)
    except InvalidOperation:
        raise ValueError(f'"{value}" is not a price')
    if price < 0:
        raise ValueError("A price can't be negative")
    return price


def _http_link(value: Any) -> str | None:
    text = _text(value, 2000)
    if text is None:
        return None
    if not text.lower().startswith(("http://", "https://")):
        raise ValueError("A link must start with http:// or https://")
    return text


def _csv_tags(value: Any) -> list[str]:
    if value is None:
        return []
    raw = str(value)
    parts = raw.split(";") if ";" in raw else raw.split(",")
    tags: list[str] = []
    for part in parts:
        cleaned = " ".join(part.strip().lower().split())
        if cleaned and cleaned not in tags:
            tags.append(cleaned[:40])
    return tags[:20]


def _csv_event_id(title: str, starts_at: datetime) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80] or "event"
    return f"{slug}-{starts_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"


def _csv_row(
    row: dict[str, Any], organisers: dict[str, UUID], categories: dict[str, UUID]
) -> dict[str, Any]:
    title = _line(row.get("title"), 140)
    if not title or not (3 <= len(title) <= 140):
        raise ValueError("title must be 3–140 characters")
    starts_at = _parse_datetime(row.get("starts_at"))
    if starts_at is None:
        raise ValueError("starts_at is required")
    ends_at = _parse_datetime(row.get("ends_at"))
    if ends_at is not None and ends_at < starts_at:
        raise ValueError("ends_at is before starts_at")

    format_value = FORMAT_ALIASES.get(" ".join(str(row.get("format") or "").split()).lower(), "in_person")
    organiser_slug = str(row.get("organiser_slug") or "").strip().lower() or None
    if organiser_slug and organiser_slug not in organisers:
        raise ValueError(f'No organiser with slug "{organiser_slug}"')
    category_slug = str(row.get("category_slug") or "").strip().lower() or None
    if category_slug and category_slug not in categories:
        raise ValueError(f'No category with slug "{category_slug}"')

    external_url = _http_link(row.get("external_url"))
    mode = str(row.get("registration_mode") or "").strip().lower()
    if mode not in ("external", "none"):
        mode = "external" if external_url else "none"
    if mode == "external" and external_url is None:
        raise ValueError("registration_mode external needs external_url")

    source_event_id = (
        str(row.get("id") or row.get("source_event_id") or "").strip()[:200]
        or _csv_event_id(title, starts_at)
    )
    return {
        "title": title,
        "summary": _line(row.get("summary"), 200),
        "description": _text(row.get("description"), 10000),
        "starts_at": starts_at,
        "ends_at": ends_at,
        "format": format_value,
        "venue_name": _text(row.get("venue_name"), 200),
        "address": _text(row.get("address"), 300),
        "city": _text(row.get("city"), 100),
        "organiser_id": organisers.get(organiser_slug) if organiser_slug else None,
        "category_id": categories.get(category_slug) if category_slug else None,
        "tags": _csv_tags(row.get("tags")),
        "registration_mode": mode,
        "external_url": external_url,
        "external_label": _line(row.get("external_label"), 40) or "Register",
        "price_min": _parse_price(row.get("price_min")),
        "price_max": _parse_price(row.get("price_max")),
        "currency": "GBP",
        "source_url": _http_link(row.get("source_url")),
        "source_event_id": source_event_id,
    }


def _chunks(items: list[Any], size: int = 100) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


async def import_csv(
    db: AsyncSession, rows: list[dict[str, Any]], actor_id: UUID
) -> dict[str, Any]:
    """Upsert CSV rows as published events, leaving events an admin removed alone."""
    organisers = dict((await db.execute(select(Organiser.slug, Organiser.id))).all())
    categories = dict((await db.execute(select(Category.slug, Category.id))).all())

    errors: list[dict[str, Any]] = []
    skipped = 0
    good: list[tuple[int, dict[str, Any]]] = []
    for index, row in enumerate(rows, start=1):
        try:
            good.append((index, _csv_row(row, organisers, categories)))
        except ValueError as exc:
            errors.append({"row": index, "message": str(exc)})
            skipped += 1

    existing: dict[str, dict[str, Any]] = {}
    ids = [data["source_event_id"] for _, data in good]
    for chunk in _chunks(ids):
        found = (
            await db.execute(
                select(Event.id, Event.source_event_id, Event.status).where(
                    Event.source == "csv", Event.source_event_id.in_(chunk)
                )
            )
        ).mappings().all()
        for record in found:
            existing[record["source_event_id"]] = dict(record)

    inserted = 0
    updated = 0
    written: dict[str, UUID] = {}
    now = _now()
    for index, data in good:
        source_event_id = data["source_event_id"]
        current = existing.get(source_event_id)
        if current and current["status"] == "removed":
            skipped += 1
            continue
        values = {
            key: value
            for key, value in data.items()
            if key != "source_event_id"
        }
        values.update(
            {
                "source": "csv",
                "status": "published",
                "needs_review": False,
                "last_verified_at": now,
                "updated_at": now,
            }
        )
        try:
            async with db.begin_nested():
                if current:
                    await db.execute(
                        update(Event).where(Event.id == current["id"]).values(**values)
                    )
                    updated += 1
                elif source_event_id in written:
                    await db.execute(
                        update(Event).where(Event.id == written[source_event_id]).values(**values)
                    )
                    updated += 1
                else:
                    event_id = (
                        await db.execute(
                            insert(Event)
                            .values(**values, source_event_id=source_event_id)
                            .returning(Event.id)
                        )
                    ).scalar_one()
                    written[source_event_id] = event_id
                    inserted += 1
        except SQLAlchemyError as exc:
            errors.append({"row": index, "message": str(exc)[:300]})
            skipped += 1

    await fn.log_admin_action(
        db,
        "import.csv",
        "import",
        None,
        {"inserted": inserted, "updated": updated, "skipped": skipped},
    )
    return {"inserted": inserted, "updated": updated, "skipped": skipped, "errors": errors}
