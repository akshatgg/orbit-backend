"""Admin API for event sources, sync runs and the review queue."""

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Text, cast, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.categories.models import Category
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_admin
from app.events.models import Event
from app.organisers.models import Organiser
from app.sources.models import EventDuplicateDismissal, EventSource, SourceEventDetails, SourceRun
from app.sources.schemas import (
    BulkReviewBody,
    EnabledBody,
    ImportedDetailsBody,
    ReviewBody,
    SourceCreate,
    SourceUpdate,
)
from app.sources.service import (
    detect_duplicates_now,
    run_source,
    source_id_from_key,
    source_key,
    start_refresh,
)

router = APIRouter(prefix="/admin/sources", tags=["admin:sources"])

SOURCE_LABELS = {"orbit": "ORBIT (organiser)", "csv": "CSV import"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _source_dict(source: EventSource) -> dict[str, Any]:
    return {
        "id": source.id,
        "name": source.name,
        "kind": source.kind,
        "url": source.url,
        "default_organiser_id": source.default_organiser_id,
        "default_category_id": source.default_category_id,
        "default_tags": list(source.default_tags or []),
        "attribution": source.attribution,
        "permission_confirmed": source.permission_confirmed,
        "permission_note": source.permission_note,
        "enabled": source.enabled,
        "auto_category": source.auto_category,
        "last_run_at": source.last_run_at,
        "last_status": source.last_status,
        "last_error": source.last_error,
        "created_by": source.created_by,
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }


def _run_dict(run: SourceRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "source_id": run.source_id,
        "trigger": run.trigger,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "status": run.status,
        "found": run.found,
        "inserted": run.inserted,
        "updated": run.updated,
        "unchanged": run.unchanged,
        "flagged_missing": run.flagged_missing,
        "enriched": run.enriched,
        "errors": run.errors or [],
    }


def _event_summary(event: Event, source_names: dict[str, str]) -> dict[str, Any]:
    return {
        "id": event.id,
        "title": event.title,
        "summary": event.summary,
        "description": event.description,
        "starts_at": event.starts_at,
        "ends_at": event.ends_at,
        "venue_name": event.venue_name,
        "address": event.address,
        "city": event.city,
        "format": event.format,
        "status": event.status,
        "source": event.source,
        "source_name": _source_label(event.source, source_names),
        "source_url": event.source_url,
        "source_event_id": event.source_event_id,
        "external_url": event.external_url,
        "cover_url": event.cover_url,
        "duplicate_of": event.duplicate_of,
        "needs_review": event.needs_review,
        "last_verified_at": event.last_verified_at,
        "created_at": event.created_at,
        "series_key": event.series_key,
        "series_label": event.series_label,
        "organiser_id": event.organiser_id,
        "category_id": event.category_id,
    }


def _source_label(source: str, names: dict[str, str]) -> str:
    if source in SOURCE_LABELS:
        return SOURCE_LABELS[source]
    source_id = source_id_from_key(source)
    if source_id is not None:
        return names.get(str(source_id), "Deleted source")
    return source


async def _source_names(db: AsyncSession) -> dict[str, str]:
    rows = (await db.execute(select(EventSource.id, EventSource.name))).all()
    return {str(row[0]): row[1] for row in rows}


async def _latest_runs(db: AsyncSession, source_ids: list[UUID]) -> dict[str, SourceRun]:
    if not source_ids:
        return {}
    rows = (
        await db.execute(
            select(SourceRun)
            .distinct(SourceRun.source_id)
            .where(SourceRun.source_id.in_(source_ids))
            .order_by(SourceRun.source_id, SourceRun.started_at.desc())
        )
    ).scalars().all()
    return {str(run.source_id): run for run in rows}


def _validate_enabling(enabled: Any, confirmed: Any, note: Any) -> None:
    if enabled and (not confirmed or not str(note or "").strip()):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Confirm you have permission to use this feed, and note who granted it, before enabling it.",
        )


def _days_away(starts_at: datetime, now: datetime) -> int:
    zone = ZoneInfo(settings.DEFAULT_TIMEZONE)

    def day(value: datetime) -> str:
        return value.astimezone(zone).date().isoformat()

    return (datetime.fromisoformat(day(starts_at)) - datetime.fromisoformat(day(now))).days


async def _review_counts(db: AsyncSession) -> dict[str, int]:
    now = _now()
    missing = await db.scalar(
        select(func.count()).select_from(Event).where(
            Event.needs_review.is_(True), Event.duplicate_of.is_(None)
        )
    )
    duplicates = await db.scalar(
        select(func.count()).select_from(Event).where(
            Event.needs_review.is_(True), Event.duplicate_of.is_not(None)
        )
    )
    hidden = await db.scalar(
        select(func.count()).select_from(Event).where(
            Event.needs_review.is_(False),
            Event.duplicate_of.is_not(None),
            Event.starts_at >= now,
        )
    )
    return {
        "missing": int(missing or 0),
        "duplicates": int(duplicates or 0),
        "hidden": int(hidden or 0),
    }


async def _imported_events(db: AsyncSession, limit: int) -> list[dict[str, Any]]:
    now = _now()
    rows = (
        await db.execute(
            select(Event, SourceEventDetails)
            .outerjoin(SourceEventDetails, SourceEventDetails.event_id == Event.id)
            .where(
                Event.source.not_in(["orbit", "csv"]),
                Event.status == "published",
                Event.starts_at >= now - timedelta(days=1),
            )
            .order_by(Event.cover_url.is_(None).desc(), Event.starts_at.asc())
            .limit(limit)
        )
    ).all()
    kinds = {
        f"source:{row[0]}": row[1]
        for row in (await db.execute(select(EventSource.id, EventSource.kind))).all()
    }
    items = []
    for event, details in rows:
        kind = kinds.get(event.source)
        if kind is None:
            continue
        items.append(
            {
                "id": event.id,
                "title": event.title,
                "starts_at": event.starts_at,
                "cover_url": event.cover_url,
                "description": event.description,
                "price_min": event.price_min,
                "price_max": event.price_max,
                "currency": event.currency,
                "kind": kind,
                "external_url": event.external_url,
                "external_label": event.external_label,
                "source": event.source,
                "source_event_id": event.source_event_id,
                "details": (
                    {
                        "status": details.status,
                        "attempts": details.attempts,
                        "last_error": details.last_error,
                        "fetched_at": details.fetched_at,
                    }
                    if details is not None
                    else None
                ),
            }
        )
    return items


@router.get("/overview")
async def sources_overview(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Every source with its latest run, event counts, review counts and imported events."""
    sources = (
        await db.execute(select(EventSource).order_by(EventSource.created_at))
    ).scalars().all()
    source_ids = [source.id for source in sources]
    runs = await _latest_runs(db, source_ids)

    keys = [source_key(sid) for sid in source_ids]
    counts: dict[str, dict[str, int]] = {}
    if keys:
        rows = (
            await db.execute(
                select(
                    Event.source,
                    func.count().label("dates"),
                    func.count(
                        func.distinct(func.coalesce(Event.series_key, cast(Event.id, Text)))
                    ).label("events"),
                )
                .where(Event.source.in_(keys))
                .group_by(Event.source)
            )
        ).all()
        counts = {row[0]: {"events": int(row[2] or 0), "dates": int(row[1] or 0)} for row in rows}

    organiser_names = dict(
        (await db.execute(select(Organiser.id, Organiser.name))).all()
    )
    category_names = dict(
        (await db.execute(select(Category.id, Category.name))).all()
    )
    items = []
    for source in sources:
        item = _source_dict(source)
        item["organiser_name"] = organiser_names.get(source.default_organiser_id)
        item["category_name"] = category_names.get(source.default_category_id)
        item["latest_run"] = _run_dict(runs.get(str(source.id)))
        item["event_count"] = counts.get(source_key(source.id), {"events": 0, "dates": 0})["events"]
        item["date_count"] = counts.get(source_key(source.id), {"events": 0, "dates": 0})["dates"]
        items.append(item)

    return {
        "sources": items,
        "total": len(items),
        "page": 1,
        "page_size": len(items),
        "review_counts": await _review_counts(db),
        "imported": await _imported_events(db, 20),
    }


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_sources(admin: dict = Depends(require_admin)) -> dict[str, Any]:
    """Start a refresh of every enabled source in the background, then expiry and duplicate
    detection. Each source's result lands in /admin/sources/runs."""
    return {"started": start_refresh("manual")}


@router.get("/runs")
async def list_runs(
    source_id: UUID | None = None,
    limit: int = Query(25, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Recent source runs, newest first."""
    query = select(SourceRun).order_by(SourceRun.started_at.desc()).limit(limit)
    if source_id is not None:
        query = query.where(SourceRun.source_id == source_id)
    runs = (await db.execute(query)).scalars().all()
    names = await _source_names(db)
    items = []
    for run in runs:
        item = _run_dict(run)
        item["source_name"] = names.get(str(run.source_id), "Deleted source")
        items.append(item)
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.get("/review/counts")
async def review_counts(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, int]:
    """How many events wait in each part of the review dashboard."""
    return await _review_counts(db)


@router.get("/review/missing")
async def review_missing(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    source_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Events flagged for review that aren't duplicates, soonest first, with per-source counts."""
    filters = [Event.needs_review.is_(True), Event.duplicate_of.is_(None)]
    if source_id is not None:
        filters.append(Event.source == source_key(source_id))
    total = int(
        await db.scalar(select(func.count()).select_from(Event).where(*filters)) or 0
    )
    events = (
        await db.execute(
            select(Event)
            .where(*filters)
            .order_by(Event.starts_at.asc(), Event.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    names = await _source_names(db)
    sources = {
        str(row.id): row
        for row in (await db.execute(select(EventSource))).scalars().all()
    }
    now = _now()

    count_rows = (
        await db.execute(
            select(Event.source, func.count())
            .where(Event.needs_review.is_(True), Event.duplicate_of.is_(None))
            .group_by(Event.source)
        )
    ).all()
    per_source = []
    for key, count in count_rows:
        sid = source_id_from_key(key)
        per_source.append(
            {
                "source": key,
                "id": sid,
                "name": _source_label(key, names),
                "count": int(count),
            }
        )
    per_source.sort(key=lambda item: item["count"], reverse=True)

    items = []
    for event in events:
        source = sources.get(str(source_id_from_key(event.source) or ""))
        missing = bool(
            source
            and source.last_run_at
            and source.last_status == "ok"
            and event.last_verified_at < source.last_run_at
        )
        item = _event_summary(event, names)
        item["reason"] = "missing" if missing else "flagged"
        item["days_away"] = _days_away(event.starts_at, now)
        items.append(item)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "sources": per_source,
    }


@router.get("/review/duplicates")
async def review_duplicates(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Possible duplicates waiting for a decision, each with the event it may duplicate."""
    filters = [Event.needs_review.is_(True), Event.duplicate_of.is_not(None)]
    total = int(
        await db.scalar(select(func.count()).select_from(Event).where(*filters)) or 0
    )
    events = (
        await db.execute(
            select(Event)
            .where(*filters)
            .order_by(Event.starts_at.asc(), Event.id.asc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    originals = await _originals(db, [event.duplicate_of for event in events])
    names = await _source_names(db)
    items = [
        {
            "event": _event_summary(event, names),
            "original": _event_summary(originals[event.duplicate_of], names)
            if event.duplicate_of in originals
            else None,
            "reason": "duplicate",
        }
        for event in events
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/review/hidden")
async def review_hidden(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Duplicates an admin chose to keep hidden, so they can be revisited."""
    events = (
        await db.execute(
            select(Event)
            .where(
                Event.needs_review.is_(False),
                Event.duplicate_of.is_not(None),
                Event.starts_at >= _now(),
            )
            .order_by(Event.starts_at.asc(), Event.id.asc())
            .limit(limit)
        )
    ).scalars().all()
    originals = await _originals(db, [event.duplicate_of for event in events])
    names = await _source_names(db)
    items = [
        {
            "event": _event_summary(event, names),
            "original": _event_summary(originals[event.duplicate_of], names)
            if event.duplicate_of in originals
            else None,
        }
        for event in events
    ]
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


async def _originals(db: AsyncSession, ids: list[Any]) -> dict[Any, Event]:
    wanted = [value for value in ids if value is not None]
    if not wanted:
        return {}
    rows = (
        await db.execute(select(Event).where(Event.id.in_(set(wanted))))
    ).scalars().all()
    return {event.id: event for event in rows}


@router.post("/review/bulk")
async def bulk_review(
    body: BulkReviewBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, int]:
    """Mark many non-duplicate review items reviewed."""
    result = await db.execute(
        update(Event)
        .where(
            Event.id.in_(body.ids),
            Event.needs_review.is_(True),
            Event.duplicate_of.is_(None),
        )
        .values(needs_review=False, updated_at=_now())
    )
    return {"updated": result.rowcount or 0}


@router.post("/review/{event_id}")
async def resolve_review(
    event_id: UUID,
    body: ReviewBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Resolve one review item: not a duplicate, keep hidden, or mark reviewed."""
    event = (
        await db.execute(select(Event).where(Event.id == event_id))
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That event no longer exists.")
    now = _now()

    if body.decision == "not_duplicate":
        if event.duplicate_of is not None:
            first, second = sorted((str(event.id), str(event.duplicate_of)))
            await db.execute(
                pg_insert(EventDuplicateDismissal)
                .values(
                    event_id=UUID(first),
                    other_event_id=UUID(second),
                    dismissed_by=admin["id"],
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["event_id", "other_event_id"])
            )
        await db.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(duplicate_of=None, needs_review=False, updated_at=now)
        )
    elif body.decision == "keep_hidden":
        if event.duplicate_of is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "This event isn't marked as a duplicate."
            )
        await db.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(needs_review=False, updated_at=now)
        )
    else:
        await db.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(needs_review=False, updated_at=now)
        )
    return {"ok": True, "decision": body.decision}


@router.get("/imported")
async def list_imported(
    limit: int = Query(60, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Upcoming platform-imported events, ones without a cover first."""
    items = await _imported_events(db, limit)
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.put("/imported/{event_id}")
async def update_imported(
    event_id: UUID,
    body: ImportedDetailsBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Set an imported event's cover, description or price by hand."""
    event = (
        await db.execute(select(Event).where(Event.id == event_id))
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That event no longer exists.")
    if not event.source.startswith("source:"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Only imported platform events can be edited here."
        )
    values = body.model_dump(exclude_unset=True)
    if not values:
        return {"ok": True, "event": _event_summary(event, await _source_names(db))}
    values["updated_at"] = _now()
    await db.execute(update(Event).where(Event.id == event_id).values(**values))
    refreshed = (
        await db.execute(select(Event).where(Event.id == event_id))
    ).scalar_one()
    return {"ok": True, "event": _event_summary(refreshed, await _source_names(db))}


@router.post("/imported/{event_id}/retry-details")
async def retry_details(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, bool]:
    """Queue an imported event's page to be read again: its details row is deleted, so the
    next run reads the page straight away."""
    exists = await db.scalar(select(Event.id).where(Event.id == event_id))
    if exists is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That event no longer exists.")
    await db.execute(delete(SourceEventDetails).where(SourceEventDetails.event_id == event_id))
    return {"ok": True}


@router.get("")
async def list_sources(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Every source with its default organiser/category names and latest run."""
    sources = (
        await db.execute(select(EventSource).order_by(EventSource.created_at))
    ).scalars().all()
    runs = await _latest_runs(db, [source.id for source in sources])
    organiser_names = dict(
        (await db.execute(select(Organiser.id, Organiser.name))).all()
    )
    category_names = dict(
        (await db.execute(select(Category.id, Category.name))).all()
    )
    items = []
    for source in sources:
        item = _source_dict(source)
        item["organiser_name"] = organiser_names.get(source.default_organiser_id)
        item["category_name"] = category_names.get(source.default_category_id)
        item["latest_run"] = _run_dict(runs.get(str(source.id)))
        items.append(item)
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_source(
    body: SourceCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Add a feed, refusing to enable one whose permission isn't confirmed."""
    values = body.model_dump()
    _validate_enabling(values.get("enabled"), values.get("permission_confirmed"), values.get("permission_note"))
    values["created_by"] = admin["id"]
    try:
        source_id = (
            await db.execute(insert(EventSource).values(**values).returning(EventSource.id))
        ).scalar_one()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "That feed has already been added.")
    source = await db.get(EventSource, source_id)
    return _source_dict(source)


@router.get("/{source_id}")
async def get_source_detail(
    source_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """One source with its defaults and latest run."""
    source = (
        await db.execute(select(EventSource).where(EventSource.id == source_id))
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown source")
    item = _source_dict(source)
    item["organiser_name"] = await db.scalar(
        select(Organiser.name).where(Organiser.id == source.default_organiser_id)
    ) if source.default_organiser_id else None
    item["category_name"] = await db.scalar(
        select(Category.name).where(Category.id == source.default_category_id)
    ) if source.default_category_id else None
    item["latest_run"] = _run_dict((await _latest_runs(db, [source.id])).get(str(source.id)))
    return item


@router.patch("/{source_id}")
async def update_source(
    source_id: UUID,
    body: SourceUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Update a source's settings."""
    source = (
        await db.execute(select(EventSource).where(EventSource.id == source_id))
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown source")
    values = body.model_dump(exclude_unset=True)
    for required in ("name", "kind", "url"):
        if required in values and values[required] is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"{required} cannot be empty"
            )
    if "default_tags" in values and values["default_tags"] is None:
        values["default_tags"] = []
    merged = {**_source_dict(source), **values}
    _validate_enabling(
        merged.get("enabled"), merged.get("permission_confirmed"), merged.get("permission_note")
    )
    if values:
        values["updated_at"] = _now()
        try:
            await db.execute(
                update(EventSource).where(EventSource.id == source_id).values(**values)
            )
        except IntegrityError:
            raise HTTPException(status.HTTP_409_CONFLICT, "That feed has already been added.")
    refreshed = (
        await db.execute(select(EventSource).where(EventSource.id == source_id))
    ).scalar_one()
    return _source_dict(refreshed)


@router.delete("/{source_id}")
async def delete_source(
    source_id: UUID,
    remove_events: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Delete a source, optionally taking its upcoming events with it."""
    source = (
        await db.execute(select(EventSource).where(EventSource.id == source_id))
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown source")
    removed = 0
    if remove_events:
        result = await db.execute(
            update(Event)
            .where(
                Event.source == source_key(source_id),
                Event.status.in_(("published", "draft")),
                Event.starts_at >= _now(),
            )
            .values(status="removed", updated_at=_now())
        )
        removed = result.rowcount or 0
    await db.execute(delete(EventSource).where(EventSource.id == source_id))
    return {"deleted": True, "events_removed": removed}


@router.post("/{source_id}/enabled")
async def set_enabled(
    source_id: UUID,
    body: EnabledBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Turn a source on or off; enabling needs confirmed permission."""
    source = (
        await db.execute(select(EventSource).where(EventSource.id == source_id))
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown source")
    if body.on and (not source.permission_confirmed or not (source.permission_note or "").strip()):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Confirm permission (and who granted it) on the source before enabling it.",
        )
    await db.execute(
        update(EventSource)
        .where(EventSource.id == source_id)
        .values(enabled=body.on, updated_at=_now())
    )
    return {"id": source_id, "enabled": body.on}


@router.post("/{source_id}/run")
async def run_now(
    source_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Fetch one source now and apply the diff."""
    source = (
        await db.execute(select(EventSource).where(EventSource.id == source_id))
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown source")
    if not source.permission_confirmed:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Confirm permission to use this feed before running it."
        )
    await db.commit()  # the run uses its own trusted session; don't hold this transaction open
    summary = await run_source(source_id, trigger="manual")
    if summary["status"] == "ok":
        await detect_duplicates_now()
    return summary
