"""Public routes that need no session."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.public import service

router = APIRouter(prefix="/public", tags=["public"])


@router.get("/landing-events")
async def landing_events(
    limit: int = Query(6, ge=1, le=24), db: AsyncSession = Depends(get_db)
) -> dict:
    """The soonest upcoming events for the landing page."""
    items = await service.landing_events(db, limit)
    return {"items": items, "total": len(items), "page": 1, "page_size": limit}


@router.get("/sitemap-data")
async def sitemap_data(db: AsyncSession = Depends(get_db)) -> dict:
    """Published event ids and organiser slugs with their update times."""
    return await service.sitemap_data(db)


@router.get("/og/events/{event_id}")
async def og_event(event_id: UUID, db: AsyncSession = Depends(get_db)) -> dict:
    """Title, summary and image for an event's social card."""
    payload = await service.og_event(db, event_id)
    if not payload:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found")
    return payload
