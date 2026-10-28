"""Cron endpoints: reminders and source refreshes."""

from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_admin, require_cron
from app.cron.service import run_reminders
from app.sources.service import start_refresh

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


@router.get("/refresh-sources", status_code=status.HTTP_202_ACCEPTED)
async def refresh_sources_cron(_: None = Depends(require_cron)) -> dict[str, Any]:
    """Start the nightly refresh of every enabled source (cron secret), in the background."""
    return {"started": start_refresh("cron")}


@router.post("/refresh-sources", status_code=status.HTTP_202_ACCEPTED)
async def refresh_sources_admin(admin: dict = Depends(require_admin)) -> dict[str, Any]:
    """Start a refresh of every enabled source on an admin's request, in the background."""
    return {"started": start_refresh("manual")}
