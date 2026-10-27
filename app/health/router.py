"""Health / readiness."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    await db.execute(text("select 1"))
    return {
        "status": "ok",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "google": "configured" if settings.google_configured else "disabled",
        "stripe": "configured" if settings.stripe_configured else "disabled",
        "cloudinary": "configured" if settings.cloudinary_configured else "disabled",
    }
