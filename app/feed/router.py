"""Feed routes for the signed-in home page."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_user
from app.events.schemas import EventCard, EventPage
from app.feed import service

router = APIRouter(prefix="/feed", tags=["feed"])


class SectionOut(BaseModel):
    group: str
    title: str
    eyebrow: str
    events: list[EventCard]


class WeekOut(BaseModel):
    total: int
    top_picks: list[EventCard]
    sections: list[SectionOut]


@router.get("/for-you", response_model=EventPage)
async def for_you(
    days: int = Query(60, ge=1, le=365),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Events in the next `days` days ranked for this person, one card per series."""
    items = await service.for_you(db, user, days)
    return {"items": items, "total": len(items), "page": 1, "page_size": len(items)}


@router.get("/week", response_model=WeekOut)
async def week(
    days: int = Query(7, ge=1, le=365),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The next `days` days: three top picks, then career / social / other sections."""
    return await service.week(db, user, days)
