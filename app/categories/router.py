"""Public category list."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.categories.models import Category
from app.categories.schemas import CategoryOut
from app.core.database import get_db

router = APIRouter(prefix="/categories", tags=["categories"])


@router.get("", response_model=list[CategoryOut])
async def list_categories(db: AsyncSession = Depends(get_db)) -> list[dict]:
    """Every category, in display order."""
    rows = (await db.scalars(select(Category).order_by(Category.sort_order, Category.name))).all()
    return [
        {
            "id": row.id,
            "slug": row.slug,
            "name": row.name,
            "group": row.group_name,
            "sort_order": row.sort_order,
            "keywords": row.keywords,
            "created_at": row.created_at,
        }
        for row in rows
    ]
