"""Admin console API: overview, moderation, users, organisers, categories and import."""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.models import AdminAuditLog
from app.admin.schemas import (
    AddAdminBody,
    BulkCategoryBody,
    BulkIdsBody,
    BulkRemoveBody,
    CategoryCreate,
    CategoryUpdate,
    EventCreate,
    EventUpdate,
    FeatureBody,
    ImportBody,
    OrganiserDecisionBody,
    ReasonBody,
    VerifyOrganiserBody,
    VerifyStudentBody,
)
from app.admin.service import create_event, import_csv, update_event
from app.categories.models import Category
from app.core.database import get_db
from app.core.deps import require_admin, require_super_admin
from app.core.pagination import pagination
from app.events.models import Event
from app.notifications.service import (
    LOGISTICS,
    logistics_changed,
    notify_event_changed,
    notify_organiser_decision,
)
from app.organisers.models import Organiser, OrganiserMember
from app.sources.models import EventSource
from app.sql import functions as fn
from app.uploads.release import release_images
from app.users.models import Profile, User

router = APIRouter(prefix="/admin", tags=["admin"])

EVENT_STATUSES = ("draft", "published", "cancelled", "expired", "removed")
ORGANISER_STATUSES = ("pending", "approved", "rejected", "suspended")
ORGANISER_ACTIONS = {
    "approved": "organiser.approve",
    "rejected": "organiser.reject",
    "suspended": "organiser.suspend",
    "pending": "organiser.reinstate",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").strip()[:80]


def _event_dict(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "title": event.title,
        "summary": event.summary,
        "description": event.description,
        "cover_url": event.cover_url,
        "starts_at": event.starts_at,
        "ends_at": event.ends_at,
        "timezone": event.timezone,
        "format": event.format,
        "venue_name": event.venue_name,
        "address": event.address,
        "city": event.city,
        "country": event.country,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "category_id": event.category_id,
        "tags": list(event.tags or []),
        "tags_locked": event.tags_locked,
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
        "cancel_reason": event.cancel_reason,
        "is_featured": event.is_featured,
        "needs_review": event.needs_review,
        "published_at": event.published_at,
        "source": event.source,
        "source_url": event.source_url,
        "source_event_id": event.source_event_id,
        "duplicate_of": event.duplicate_of,
        "last_verified_at": event.last_verified_at,
        "series_key": event.series_key,
        "series_label": event.series_label,
        "created_by": event.created_by,
        "created_at": event.created_at,
        "updated_at": event.updated_at,
        "organiser_id": event.organiser_id,
    }


def _source_label(source: str, names: dict[str, str]) -> str:
    if source == "orbit":
        return "ORBIT (organiser)"
    if source == "manual":
        return "Added by an admin"
    if source == "csv":
        return "CSV import"
    if source.startswith("source:"):
        return names.get(source, "Deleted source")
    return source


async def _source_names(db: AsyncSession) -> dict[str, str]:
    rows = (await db.execute(select(EventSource.id, EventSource.name))).all()
    return {f"source:{row[0]}": row[1] for row in rows}


async def _get_event(db: AsyncSession, event_id: UUID) -> Event:
    event = (
        await db.execute(select(Event).where(Event.id == event_id))
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Event not found")
    return event


async def _counts(db: AsyncSession, ids: list[UUID]) -> dict[str, dict[str, int]]:
    if not ids:
        return {}
    rows = await fn.event_counts(db, ids)
    return {
        str(row["event_id"]): {
            "going": row["going"],
            "waitlisted": row["waitlisted"],
            "pending": row["pending"],
        }
        for row in rows
    }


async def _audit_page(db: AsyncSession, page: int, page_size: int) -> dict[str, Any]:
    total = int(
        await db.scalar(select(func.count()).select_from(AdminAuditLog)) or 0
    )
    rows = (
        await db.execute(
            select(AdminAuditLog, Profile.full_name)
            .outerjoin(Profile, Profile.id == AdminAuditLog.actor)
            .order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    items = [
        {
            "id": log.id,
            "actor": log.actor,
            "actor_name": actor_name,
            "action": log.action,
            "target_type": log.target_type,
            "target_id": log.target_id,
            "details": log.details or {},
            "created_at": log.created_at,
        }
        for log, actor_name in rows
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/overview")
async def admin_overview(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Key numbers, sign-ups, organiser applications, review/approval queues and recent activity."""
    stats = await fn.admin_stats(db)
    signups = await fn.admin_signups_daily(db, 30)
    now = _now()

    organisers_pending = int(
        await db.scalar(
            select(func.count()).select_from(Organiser).where(Organiser.status == "pending")
        )
        or 0
    )
    review_queue = int(
        await db.scalar(
            select(func.count())
            .select_from(Event)
            .where(Event.needs_review.is_(True), Event.duplicate_of.is_(None))
        )
        or 0
    )

    approval_events = (
        await db.execute(
            select(Event)
            .where(
                Event.status == "published",
                Event.requires_approval.is_(True),
                Event.starts_at >= now,
            )
            .order_by(Event.starts_at.asc())
            .limit(50)
        )
    ).scalars().all()
    counts = await _counts(db, [event.id for event in approval_events])
    approval_queue = [
        {
            "id": event.id,
            "title": event.title,
            "starts_at": event.starts_at,
            "pending": counts.get(str(event.id), {}).get("pending", 0),
        }
        for event in approval_events
        if counts.get(str(event.id), {}).get("pending", 0) > 0
    ][:20]

    pending_organisers = (
        await db.execute(
            select(Organiser.id, Organiser.name, Organiser.kind, Organiser.university, Organiser.created_at)
            .where(Organiser.status == "pending")
            .order_by(Organiser.created_at.asc())
            .limit(6)
        )
    ).mappings().all()

    return {
        "stats": stats,
        "signups_daily": signups,
        "organisers_pending": organisers_pending,
        "review_queue": review_queue,
        "approval_queue": approval_queue,
        "pending_organisers": [dict(row) for row in pending_organisers],
        "audit": (await _audit_page(db, 1, 12))["items"],
    }


@router.get("/audit")
async def admin_audit(
    page_size: int = Query(50, ge=1, le=100),
    pagination_values: tuple[int, int] = Depends(pagination),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """The admin audit log, newest first."""
    page, _ = pagination_values
    return await _audit_page(db, page, page_size)


@router.get("/events")
async def admin_events(
    q: str | None = None,
    status: str | None = None,
    organiser_id: str | None = None,
    source: str | None = None,
    review: bool | None = None,
    when: str = Query("upcoming"),
    page: int = Query(1, ge=1),
    page_size: int = Query(40, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Events with series collapsing: one row per recurring series, with its other dates counted."""
    filters = []
    if q and q.strip():
        filters.append(Event.title.ilike(f"%{_escape_like(q)}%", escape="\\"))
    if status in EVENT_STATUSES:
        filters.append(Event.status == status)
    if organiser_id == "none":
        filters.append(Event.organiser_id.is_(None))
    elif organiser_id:
        try:
            filters.append(Event.organiser_id == UUID(organiser_id))
        except ValueError:
            raise HTTPException(http_status.HTTP_422_UNPROCESSABLE_ENTITY, "organiser_id must be a UUID")
    if source:
        filters.append(Event.source == source)
    if review is True:
        filters.append(Event.needs_review.is_(True))
    elif review is False:
        filters.append(Event.needs_review.is_(False))
        filters.append(Event.duplicate_of.is_(None))
    else:
        filters.append(Event.duplicate_of.is_(None))

    now = _now()
    when_value = when if when in ("upcoming", "past", "all") else "upcoming"
    if when_value == "upcoming":
        filters.append(func.coalesce(Event.ends_at, Event.starts_at) >= now)
    elif when_value == "past":
        filters.append(func.coalesce(Event.ends_at, Event.starts_at) < now)

    ascending = when_value != "past"
    listing = (
        await db.execute(
            select(Event.id, Event.series_key, Event.starts_at)
            .where(*filters)
            .order_by(
                Event.starts_at.asc() if ascending else Event.starts_at.desc(),
                Event.id.asc(),
            )
        )
    ).all()

    entries: list[list[UUID]] = []
    by_series: dict[str, list[UUID]] = {}
    for event_id, series_key, _starts_at in listing:
        if not series_key:
            entries.append([event_id])
            continue
        dates = by_series.get(series_key)
        if dates is not None:
            dates.append(event_id)
        else:
            dates = [event_id]
            by_series[series_key] = dates
            entries.append(dates)

    total = len(entries)
    page_ids = [event_id for entry in entries[(page - 1) * page_size : page * page_size] for event_id in entry]
    if not page_ids:
        return {"items": [], "total": total, "page": page, "page_size": page_size}

    rows = (
        await db.execute(
            select(Event, Organiser.name, Category.name)
            .outerjoin(Organiser, Organiser.id == Event.organiser_id)
            .outerjoin(Category, Category.id == Event.category_id)
            .where(Event.id.in_(page_ids))
        )
    ).all()
    by_id = {event.id: (event, organiser_name, category_name) for event, organiser_name, category_name in rows}
    counts = await _counts(db, page_ids)
    names = await _source_names(db)

    items = []
    for event_id in page_ids:
        found = by_id.get(event_id)
        if found is None:
            continue
        event, organiser_name, category_name = found
        item = _event_dict(event)
        item["organiser_name"] = organiser_name
        item["category_name"] = category_name
        item["source_name"] = _source_label(event.source, names)
        item["date_count"] = max(0, len(by_series.get(event.series_key or "", [])) - 1)
        item.update(counts.get(str(event.id), {"going": 0, "waitlisted": 0, "pending": 0}))
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.post("/events", status_code=http_status.HTTP_201_CREATED)
async def admin_create_event(
    body: EventCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Create a published Orbit event from the admin console."""
    event_id = await create_event(db, body, admin["id"])
    event = await _get_event(db, event_id)
    return _event_dict(event)


@router.post("/events/bulk/remove")
async def admin_bulk_remove(
    body: BulkRemoveBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, int]:
    """Remove many events with one reason, logged as one audit row."""
    result = await db.execute(
        update(Event)
        .where(Event.id.in_(body.ids))
        .values(status="removed", cancel_reason=body.reason, updated_at=_now())
    )
    updated = result.rowcount or 0
    await fn.log_admin_action(
        db, "event.remove", "event", None, {"count": updated, "reason": body.reason, "bulk": True}
    )
    return {"updated": updated}


@router.post("/events/bulk/category")
async def admin_bulk_category(
    body: BulkCategoryBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, int]:
    """Move many events to one category (or clear it)."""
    if body.category_id is not None:
        exists = await db.scalar(select(Category.id).where(Category.id == body.category_id))
        if exists is None:
            raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Category not found")
    result = await db.execute(
        update(Event)
        .where(Event.id.in_(body.ids))
        .values(category_id=body.category_id, updated_at=_now())
    )
    updated = result.rowcount or 0
    await fn.log_admin_action(
        db,
        "event.category",
        "event",
        None,
        {"count": updated, "category_id": str(body.category_id) if body.category_id else None},
    )
    return {"updated": updated}


@router.post("/events/bulk/delete")
async def admin_bulk_delete(
    body: BulkIdsBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, int]:
    """Permanently delete many events."""
    covers = (await db.scalars(select(Event.cover_url).where(Event.id.in_(body.ids)))).all()
    result = await db.execute(delete(Event).where(Event.id.in_(body.ids)))
    await release_images(db, covers)
    deleted = result.rowcount or 0
    await fn.log_admin_action(
        db, "event.delete", "event", None, {"count": deleted, "bulk": True}
    )
    return {"deleted": deleted}


@router.get("/events/{event_id}")
async def admin_event(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """One event with its organiser, category and source name."""
    event = await _get_event(db, event_id)
    organiser_name = await db.scalar(
        select(Organiser.name).where(Organiser.id == event.organiser_id)
    ) if event.organiser_id else None
    category_name = await db.scalar(
        select(Category.name).where(Category.id == event.category_id)
    ) if event.category_id else None
    names = await _source_names(db)
    item = _event_dict(event)
    item["organiser_name"] = organiser_name
    item["category_name"] = category_name
    item["source_name"] = _source_label(event.source, names)
    item.update((await _counts(db, [event.id])).get(str(event.id), {"going": 0, "waitlisted": 0, "pending": 0}))
    return item


@router.patch("/events/{event_id}")
async def admin_update_event(
    event_id: UUID,
    body: EventUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Edit an event; supplying tags locks them against feed syncs.

    Guests and savers of a published event hear when its time or place moved.
    """
    event = await _get_event(db, event_id)
    before = {key: getattr(event, key) for key in LOGISTICS}
    await update_event(db, event, body)
    updated = await _get_event(db, event_id)
    after = {key: getattr(updated, key) for key in LOGISTICS}
    if updated.status == "published" and logistics_changed(before, after):
        await notify_event_changed(db, event_id, "updated")
    return _event_dict(updated)


@router.post("/events/{event_id}/feature")
async def admin_feature_event(
    event_id: UUID,
    body: FeatureBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Feature or unfeature an event."""
    event = await _get_event(db, event_id)
    await db.execute(
        update(Event).where(Event.id == event_id).values(is_featured=body.on, updated_at=_now())
    )
    await fn.log_admin_action(
        db,
        "event.feature" if body.on else "event.unfeature",
        "event",
        str(event_id),
        {"title": event.title},
    )
    return {"id": event_id, "is_featured": body.on}


@router.post("/events/{event_id}/cancel")
async def admin_cancel_event(
    event_id: UUID,
    body: ReasonBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Cancel an event with a public reason."""
    event = await _get_event(db, event_id)
    await db.execute(
        update(Event)
        .where(Event.id == event_id)
        .values(status="cancelled", cancel_reason=body.reason, updated_at=_now())
    )
    await fn.log_admin_action(
        db, "event.cancel", "event", str(event_id), {"title": event.title, "reason": body.reason}
    )
    await notify_event_changed(db, event_id, "cancelled")
    return {"id": event_id, "status": "cancelled", "cancel_reason": body.reason}


@router.post("/events/{event_id}/remove")
async def admin_remove_event(
    event_id: UUID,
    body: ReasonBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Remove an event from public view with a reason."""
    event = await _get_event(db, event_id)
    await db.execute(
        update(Event)
        .where(Event.id == event_id)
        .values(status="removed", cancel_reason=body.reason, updated_at=_now())
    )
    await fn.log_admin_action(
        db, "event.remove", "event", str(event_id), {"title": event.title, "reason": body.reason}
    )
    return {"id": event_id, "status": "removed", "cancel_reason": body.reason}


@router.post("/events/{event_id}/restore")
async def admin_restore_event(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Restore a cancelled or removed event to published."""
    event = await _get_event(db, event_id)
    await db.execute(
        update(Event)
        .where(Event.id == event_id)
        .values(status="published", cancel_reason=None, updated_at=_now())
    )
    await fn.log_admin_action(
        db, "event.restore", "event", str(event_id), {"title": event.title}
    )
    return {"id": event_id, "status": "published"}


@router.post("/events/{event_id}/reviewed")
async def admin_reviewed_event(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Mark an event reviewed and verified now."""
    event = await _get_event(db, event_id)
    now = _now()
    await db.execute(
        update(Event)
        .where(Event.id == event_id)
        .values(needs_review=False, last_verified_at=now, updated_at=now)
    )
    await fn.log_admin_action(
        db, "event.reviewed", "event", str(event_id), {"title": event.title}
    )
    return {"id": event_id, "needs_review": False}


@router.delete("/events/{event_id}")
async def admin_delete_event(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, bool]:
    """Permanently delete an event, logging the action before the row goes."""
    event = await _get_event(db, event_id)
    await fn.log_admin_action(
        db, "event.delete", "event", str(event_id), {"title": event.title}
    )
    await db.execute(delete(Event).where(Event.id == event_id))
    await release_images(db, [event.cover_url])
    return {"deleted": True}


@router.get("/users")
async def admin_users(
    q: str | None = None,
    only: str | None = None,
    user_type: str | None = Query(None, alias="type"),
    pagination_values: tuple[int, int] = Depends(pagination),
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Profiles with their auth email verification, filterable by student status and type."""
    page, page_size = pagination_values
    filters = []
    if q and q.strip():
        pattern = f"%{_escape_like(q)}%"
        filters.append(Profile.full_name.ilike(pattern, escape="\\") | Profile.email.ilike(pattern, escape="\\"))
    if only == "verified":
        filters.append(Profile.is_verified_student.is_(True))
    elif only == "unverified":
        filters.append(Profile.is_verified_student.is_(False))
    elif only == "admins":
        filters.append(Profile.role.in_(("admin", "super_admin")))
    if user_type:
        filters.append(Profile.user_type == user_type)

    total = int(
        await db.scalar(
            select(func.count())
            .select_from(Profile)
            .join(User, User.id == Profile.id)
            .where(*filters)
        )
        or 0
    )
    rows = (
        await db.execute(
            select(Profile, User.email_verified_at)
            .join(User, User.id == Profile.id)
            .where(*filters)
            .order_by(Profile.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    items = []
    for profile, email_verified_at in rows:
        item = {
            "id": profile.id,
            "email": profile.email,
            "full_name": profile.full_name,
            "avatar_url": profile.avatar_url,
            "university": profile.university,
            "course": profile.course,
            "year_of_study": profile.year_of_study,
            "user_type": profile.user_type,
            "role": profile.role,
            "is_verified_student": profile.is_verified_student,
            "onboarded_at": profile.onboarded_at,
            "created_at": profile.created_at,
            "email_verified_at": email_verified_at,
        }
        items.append(item)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/admins")
async def admin_list_admins(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Profiles with the admin or super admin role."""
    rows = (
        await db.execute(
            select(Profile)
            .where(Profile.role.in_(("admin", "super_admin")))
            .order_by(Profile.role.desc(), Profile.created_at.asc())
        )
    ).scalars().all()
    items = [
        {
            "id": profile.id,
            "email": profile.email,
            "full_name": profile.full_name,
            "role": profile.role,
            "created_at": profile.created_at,
        }
        for profile in rows
    ]
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.post("/users/{user_id}/verify-student")
async def admin_verify_student(
    user_id: UUID,
    body: VerifyStudentBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Grant or revoke the verified-student badge."""
    profile = (
        await db.execute(select(Profile).where(Profile.id == user_id))
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "User not found")
    await db.execute(
        update(Profile)
        .where(Profile.id == user_id)
        .values(is_verified_student=body.verified, updated_at=_now())
    )
    await fn.log_admin_action(
        db,
        "user.verify_student" if body.verified else "user.unverify_student",
        "user",
        str(user_id),
        {"email": profile.email},
    )
    return {"id": user_id, "is_verified_student": body.verified}


@router.post("/admins", status_code=http_status.HTTP_201_CREATED)
async def admin_add_admin(
    body: AddAdminBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_super_admin),
) -> dict[str, Any]:
    """Promote an existing account to admin by email."""
    try:
        target_id = await fn.set_user_role(db, body.email, "admin")
    except DBAPIError as exc:
        message = str(getattr(exc, "orig", exc))
        code = (
            http_status.HTTP_404_NOT_FOUND
            if "No ORBIT account" in message
            else http_status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(code, message)
    if target_id is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"No ORBIT account uses {body.email}")
    await fn.log_admin_action(
        db, "user.add_admin", "user", str(target_id), {"email": body.email}
    )
    return {"id": target_id, "role": "admin"}


@router.delete("/admins/{email}")
async def admin_remove_admin(
    email: str,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_super_admin),
) -> dict[str, Any]:
    """Demote an admin back to a normal user by email."""
    try:
        target_id = await fn.set_user_role(db, email, "user")
    except DBAPIError as exc:
        message = str(getattr(exc, "orig", exc))
        code = (
            http_status.HTTP_404_NOT_FOUND
            if "No ORBIT account" in message
            else http_status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(code, message)
    if target_id is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, f"No ORBIT account uses {email}")
    await fn.log_admin_action(
        db, "user.remove_admin", "user", str(target_id), {"email": email}
    )
    return {"id": target_id, "role": "user"}


@router.get("/organisers")
async def admin_organisers(
    tab: str | None = None,
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Organisers with event counts, member counts and owner names."""
    event_count = (
        select(func.count())
        .select_from(Event)
        .where(Event.organiser_id == Organiser.id)
        .correlate(Organiser)
        .scalar_subquery()
    )
    member_count = (
        select(func.count())
        .select_from(OrganiserMember)
        .where(OrganiserMember.organiser_id == Organiser.id)
        .correlate(Organiser)
        .scalar_subquery()
    )
    owner_names = (
        select(
            func.coalesce(
                func.string_agg(func.coalesce(Profile.full_name, Profile.email, ""), ", "), ""
            )
        )
        .select_from(OrganiserMember)
        .join(Profile, Profile.id == OrganiserMember.user_id)
        .where(
            OrganiserMember.organiser_id == Organiser.id,
            OrganiserMember.role == "owner",
        )
        .correlate(Organiser)
        .scalar_subquery()
    )

    filters = []
    if tab in ORGANISER_STATUSES:
        filters.append(Organiser.status == tab)
    if q and q.strip():
        filters.append(Organiser.name.ilike(f"%{_escape_like(q)}%", escape="\\"))

    order = Organiser.created_at.asc() if tab == "pending" else Organiser.created_at.desc()
    rows = (
        await db.execute(
            select(
                Organiser,
                event_count.label("event_count"),
                member_count.label("member_count"),
                owner_names.label("owner_names"),
            )
            .where(*filters)
            .order_by(order)
        )
    ).all()
    items = []
    for organiser, events, members, owners in rows:
        items.append(
            {
                "id": organiser.id,
                "slug": organiser.slug,
                "name": organiser.name,
                "kind": organiser.kind,
                "logo_url": organiser.logo_url,
                "bio": organiser.bio,
                "website": organiser.website,
                "contact_email": organiser.contact_email,
                "university": organiser.university,
                "status": organiser.status,
                "is_verified": organiser.is_verified,
                "review_note": organiser.review_note,
                "reviewed_by": organiser.reviewed_by,
                "reviewed_at": organiser.reviewed_at,
                "created_at": organiser.created_at,
                "event_count": int(events or 0),
                "member_count": int(members or 0),
                "owner_names": owners or "",
            }
        )
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.post("/organisers/{organiser_id}/decision")
async def admin_organiser_decision(
    organiser_id: UUID,
    body: OrganiserDecisionBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Approve, reject, suspend or reinstate an organiser application."""
    organiser = (
        await db.execute(select(Organiser).where(Organiser.id == organiser_id))
    ).scalar_one_or_none()
    if organiser is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Organiser not found")
    previous = organiser.status
    now = _now()
    await db.execute(
        update(Organiser)
        .where(Organiser.id == organiser_id)
        .values(
            status=body.decision,
            review_note=body.note,
            reviewed_by=admin["id"],
            reviewed_at=now,
            updated_at=now,
        )
    )
    await fn.log_admin_action(
        db,
        ORGANISER_ACTIONS[body.decision],
        "organiser",
        str(organiser_id),
        {"name": organiser.name, "note": body.note},
    )
    # Owners hear about approvals and rejections (only when the status changed).
    if body.decision != previous:
        await notify_organiser_decision(db, organiser_id, body.decision)
    return {"id": organiser_id, "status": body.decision, "review_note": body.note}


@router.post("/organisers/{organiser_id}/verify")
async def admin_verify_organiser(
    organiser_id: UUID,
    body: VerifyOrganiserBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Grant or remove an organiser's verified badge."""
    organiser = (
        await db.execute(select(Organiser).where(Organiser.id == organiser_id))
    ).scalar_one_or_none()
    if organiser is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Organiser not found")
    await db.execute(
        update(Organiser)
        .where(Organiser.id == organiser_id)
        .values(is_verified=body.verified, updated_at=_now())
    )
    await fn.log_admin_action(
        db,
        "organiser.verify" if body.verified else "organiser.unverify",
        "organiser",
        str(organiser_id),
        {"name": organiser.name},
    )
    return {"id": organiser_id, "is_verified": body.verified}


@router.get("/categories")
async def admin_categories(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Every category with how many events use it."""
    event_count = (
        select(func.count())
        .select_from(Event)
        .where(Event.category_id == Category.id)
        .correlate(Category)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(Category, event_count.label("event_count"))
            .order_by(Category.sort_order.asc(), Category.name.asc())
        )
    ).all()
    items = [
        {
            "id": category.id,
            "slug": category.slug,
            "name": category.name,
            "group": category.group_name,
            "sort_order": category.sort_order,
            "keywords": list(category.keywords or []),
            "event_count": int(count or 0),
        }
        for category, count in rows
    ]
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.post("/categories", status_code=http_status.HTTP_201_CREATED)
async def admin_create_category(
    body: CategoryCreate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Create a category."""
    try:
        category_id = (
            await db.execute(
                insert(Category)
                .values(
                    slug=body.slug,
                    name=body.name.strip(),
                    group=body.group,
                    sort_order=body.sort_order,
                    keywords=body.keywords,
                )
                .returning(Category.id)
            )
        ).scalar_one()
    except IntegrityError:
        raise HTTPException(http_status.HTTP_409_CONFLICT, "That slug is already used")
    category = (
        await db.execute(select(Category).where(Category.id == category_id))
    ).scalar_one()
    await fn.log_admin_action(
        db, "category.create", "category", str(category_id), {"name": category.name}
    )
    return {
        "id": category.id,
        "slug": category.slug,
        "name": category.name,
        "group": category.group_name,
        "sort_order": category.sort_order,
        "keywords": list(category.keywords or []),
    }


@router.patch("/categories/{category_id}")
async def admin_update_category(
    category_id: UUID,
    body: CategoryUpdate,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Update a category."""
    category = (
        await db.execute(select(Category).where(Category.id == category_id))
    ).scalar_one_or_none()
    if category is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Category not found")
    values = body.model_dump(exclude_unset=True)
    for required in ("slug", "name", "group"):
        if required in values and values[required] is None:
            raise HTTPException(
                http_status.HTTP_422_UNPROCESSABLE_ENTITY, f"{required} cannot be empty"
            )
    if values:
        try:
            await db.execute(
                update(Category).where(Category.id == category_id).values(**values)
            )
        except IntegrityError:
            raise HTTPException(http_status.HTTP_409_CONFLICT, "That slug is already used")
    refreshed = (
        await db.execute(select(Category).where(Category.id == category_id))
    ).scalar_one()
    await fn.log_admin_action(
        db, "category.update", "category", str(category_id), {"name": refreshed.name}
    )
    return {
        "id": refreshed.id,
        "slug": refreshed.slug,
        "name": refreshed.name,
        "group": refreshed.group_name,
        "sort_order": refreshed.sort_order,
        "keywords": list(refreshed.keywords or []),
    }


@router.delete("/categories/{category_id}")
async def admin_delete_category(
    category_id: UUID,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, bool]:
    """Delete an unused category; refuses while events still reference it."""
    category = (
        await db.execute(select(Category).where(Category.id == category_id))
    ).scalar_one_or_none()
    if category is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "Category not found")
    used = int(
        await db.scalar(
            select(func.count()).select_from(Event).where(Event.category_id == category_id)
        )
        or 0
    )
    if used:
        raise HTTPException(
            http_status.HTTP_409_CONFLICT, f"{used} event(s) still use this category"
        )
    await fn.log_admin_action(
        db, "category.delete", "category", str(category_id), {"name": category.name}
    )
    await db.execute(delete(Category).where(Category.id == category_id))
    return {"deleted": True}


@router.get("/import/options")
async def admin_import_options(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Approved organisers and categories for the CSV importer."""
    organisers = (
        await db.execute(
            select(Organiser.id, Organiser.slug, Organiser.name)
            .where(Organiser.status == "approved")
            .order_by(Organiser.name.asc())
        )
    ).mappings().all()
    categories = (
        await db.execute(
            select(Category.id, Category.slug, Category.name)
            .order_by(Category.sort_order.asc(), Category.name.asc())
        )
    ).mappings().all()
    return {
        "organisers": [dict(row) for row in organisers],
        "categories": [dict(row) for row in categories],
    }


@router.post("/import/csv")
async def admin_import_csv(
    body: ImportBody,
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Import CSV rows as published events, upserting on (source='csv', source_event_id)."""
    return await import_csv(db, body.rows, admin["id"])
