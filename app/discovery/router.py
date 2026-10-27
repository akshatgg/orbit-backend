"""Explore routes."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import current_user
from app.discovery import service
from app.events.service import INTERESTS

router = APIRouter(prefix="", tags=["discovery"])


@router.get("/discovery/counts")
async def discovery_counts(
    user: dict | None = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """How many upcoming events each category and profile interest has."""
    return await service.counts(db, user)


@router.get("/discovery/places")
async def discovery_places(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Countries and cities with upcoming in-person events, biggest first."""
    return await service.places(db)


@router.get("/interests")
async def list_interests() -> list[dict]:
    """The interest slugs events are tagged and filtered by."""
    return [{"slug": slug, "label": label} for slug, label in INTERESTS]
