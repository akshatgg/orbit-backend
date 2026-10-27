"""Feed routes for the signed-in home page."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_user
from app.events.schemas import EventCard, EventPage
from app.feed import service

router = APIRouter(prefix="/feed", tags=["feed"])


class SectionOut(BaseModel):
    title: str
    events: list[EventCard]


class WeekOut(BaseModel):
    top_picks: list[EventCard]
    sections: list[SectionOut]


@router.get("/for-you", response_model=EventPage)
async def for_you(
    user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Up to thirty upcoming events ranked for this person."""
    items = await service.for_you(db, user)
    return {"items": items, "total": len(items), "page": 1, "page_size": 30}


@router.get("/week", response_model=WeekOut)
async def week(
    user: dict = Depends(require_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """The next seven days, as six top picks plus category sections."""
    return await service.week(db, user)
