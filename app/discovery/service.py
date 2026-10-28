"""Explore counts, place grouping and the interest list."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.categories.models import Category
from app.discovery.places import group_places
from app.discovery.taxonomy import ALL_INTERESTS, interest_tags
from app.events import service as events
from app.events.models import Event
from app.sql import functions as fn


async def counts(db: AsyncSession) -> dict:
    """Upcoming events per category and per interest, every interest whoever asks; a recurring
    series counts once (the frontend's getDiscoveryCounts over `discovery_counts`)."""
    interests = {value: interest_tags(value) for value, _ in ALL_INTERESTS}
    rows = await fn.discovery_counts(db, interests)
    total = next((int(row["events"] or 0) for row in rows if row["kind"] == "all"), 0)
    by_category: dict[str, int] = {}
    for row in rows:
        if row["kind"] == "category" and row["key"]:
            by_category[row["key"]] = int(row["events"] or 0)

    categories = (
        await db.scalars(select(Category).order_by(Category.sort_order, Category.name))
    ).all()
    category_rows = [
        {
            "slug": category.slug,
            "name": category.name,
            "group": category.group_name,
            "events": by_category.get(str(category.id), 0),
        }
        for category in categories
    ]
    interest_rows = sorted(
        (
            {
                "slug": row["key"],
                "label": events.INTEREST_LABELS.get(
                    row["key"], row["key"].replace("-", " ").title()
                ),
                "events": int(row["events"] or 0),
            }
            for row in rows
            if row["kind"] == "interest" and row["key"]
        ),
        key=lambda row: (-row["events"], row["slug"]),
    )
    return {"total": total, "category": category_rows, "interest": interest_rows}


async def places(db: AsyncSession) -> list[dict]:
    """Where upcoming in-person and hybrid events are, country → city, biggest first; a series
    counts once and spellings are merged (the frontend's listUpcomingPlaces/groupPlaces)."""
    rows = (
        await db.execute(
            select(Event.id, Event.city, Event.country, Event.series_key).where(
                *events.visible(), Event.format != "online"
            )
        )
    ).mappings().all()
    return group_places([dict(row) for row in rows])
