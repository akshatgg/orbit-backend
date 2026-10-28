"""Organiser Studio routes — event editor, guest list, approvals, stats and the door."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_event_manager, require_user
from app.core.pagination import pagination
from app.core.routing import NumbersRoute
from app.organisers.service import org_role

from . import service
from .schemas import (
    CancelIn,
    CheckInByRegistrationIn,
    CheckInIn,
    DatesIn,
    DecisionIn,
    EventCreate,
    EventUpdate,
    GuestStatus,
)

router = APIRouter(prefix="/organiser", tags=["studio"], route_class=NumbersRoute)


@router.get("/overview")
async def overview(
    organiser_id: UUID = Query(),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Dashboard numbers for one organiser: events, followers, registrations, next event."""
    await org_role(db, organiser_id, user)
    return await service.overview(db, organiser_id)


@router.get("/events")
async def list_events(
    organiser_id: UUID = Query(),
    page: int = Query(1, ge=1),
    page_size: int = Query(500, ge=1, le=500),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Every event of the organiser in any status, newest start first, with counts.

    Members only (the require_org_member rule, via org_role so a malformed id is a 422).
    """
    await org_role(db, organiser_id, user)
    return await service.list_events(db, organiser_id, page, page_size)


@router.get("/events/{event_id}")
async def event_detail(
    event_id: UUID,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Everything the editor loads for one event, including other dates in its series."""
    return await service.event_detail(db, event_id)


@router.post("/events", status_code=status.HTTP_201_CREATED)
async def create_event(
    body: EventCreate,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a draft event with its tickets, questions and guest-only details."""
    await org_role(db, body.organiser_id, user)
    return await service.create_event(db, body.model_dump(), user["id"])


@router.patch("/events/{event_id}")
async def update_event(
    event_id: UUID,
    body: EventUpdate,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Edit an event. Ticket types and questions, when supplied, are synced by id:
    rows with an id are updated, rows without one are added, missing ones are deleted."""
    data = body.model_dump(exclude_unset=True)
    # exclude_unset also drops unset fields inside each row; rows keep their defaults.
    for key in ("ticket_types", "questions"):
        rows = getattr(body, key)
        if rows is not None:
            data[key] = [row.model_dump() for row in rows]
    return await service.update_event(db, event_id, data)


@router.post("/events/{event_id}/dates", status_code=status.HTTP_201_CREATED)
async def add_dates(
    event_id: UUID,
    body: DatesIn,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """More dates for the event, each its own event in the same series."""
    return await service.create_dates(
        db, event_id, [slot.model_dump() for slot in body.dates], user["id"]
    )


@router.post("/events/{event_id}/publish")
async def publish_event(
    event_id: UUID,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Publish an event that has not started yet."""
    return await service.publish_event(db, event_id)


@router.post("/events/{event_id}/cancel")
async def cancel_event(
    event_id: UUID,
    body: CancelIn,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cancel a published event and record why."""
    return await service.cancel_event(db, event_id, body.reason)


@router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_id: UUID,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Delete a draft event."""
    await service.delete_event(db, event_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/events/{event_id}/stats")
async def event_stats(
    event_id: UUID,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Registrations by status, check-ins, capacity and traffic for one event."""
    return await service.event_stats(db, event_id)


@router.get("/events/{event_id}/guests")
async def guests(
    event_id: UUID,
    guest_status: GuestStatus | None = Query(default=None, alias="status"),
    pg: tuple[int, int] = Depends(pagination),
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The guest list, newest first, optionally filtered by status."""
    return await service.guests_page(db, event_id, guest_status, *pg)


@router.get("/events/{event_id}/guests.csv")
async def guests_csv(
    event_id: UUID,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The guest list as a CSV download, with one column per application question."""
    filename, content = await service.guests_csv(db, event_id)
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/events/{event_id}/registrations/{registration_id}/decision")
async def decide_registration(
    event_id: UUID,
    registration_id: UUID,
    body: DecisionIn,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve or decline one pending registration."""
    return await service.decide(db, event_id, registration_id, body.decision)


@router.post("/events/{event_id}/registrations/approve-all")
async def approve_all_registrations(
    event_id: UUID,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Approve every pending registration on the event."""
    return await service.approve_all(db, event_id)


@router.post("/events/{event_id}/check-in")
async def check_in(
    event_id: UUID,
    body: CheckInIn,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Check a guest in by their ticket code."""
    return await service.check_in(db, event_id, body.code)


@router.post("/events/{event_id}/check-in/by-registration")
async def check_in_by_registration(
    event_id: UUID,
    body: CheckInByRegistrationIn,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Check a guest in from their registration row (the QR code is looked up)."""
    return await service.check_in_by_registration(db, event_id, body.registration_id)


@router.delete(
    "/events/{event_id}/check-in/{registration_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def undo_check_in(
    event_id: UUID,
    registration_id: UUID,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Undo a check-in."""
    await service.undo_check_in(db, event_id, registration_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/events/{event_id}/check-in/count")
async def check_in_count(
    event_id: UUID,
    user: dict = Depends(require_event_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """How many guests are checked in, against how many are going."""
    return await service.check_in_count(db, event_id)
