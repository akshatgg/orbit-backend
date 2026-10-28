"""Cron endpoints: reminders and source refreshes."""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_admin, require_cron
from app.cron.service import run_reminders
from app.sources.service import refresh_all_sources

router = APIRouter(prefix="/cron", tags=["cron"])


@router.get("/reminders")
async def reminders(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_cron)
) -> dict[str, int]:
    """Event and registration-deadline reminders by push and email, deduped through notification_log.

    Each person's remind_before_hours (2 or 24) sets how far ahead their reminder goes.
    """
    return await run_reminders(db)


@router.get("/refresh-sources")
async def refresh_sources_cron(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(require_cron)
) -> dict[str, Any]:
    """Refresh every enabled source from the cron secret."""
    return await refresh_all_sources(db, "cron")


@router.post("/refresh-sources")
async def refresh_sources_admin(
    db: AsyncSession = Depends(get_db),
    admin: dict = Depends(require_admin),
) -> dict[str, Any]:
    """Refresh every enabled source on an admin's request."""
    return await refresh_all_sources(db, "manual")
