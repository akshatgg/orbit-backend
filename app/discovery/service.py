"""Explore counts, place grouping and the interest list."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.categories.models import Category
from app.events import service as events
from app.events.models import Event
from app.sql import functions as fn
from app.users.models import Profile


async def counts(db: AsyncSession, user: dict | None) -> dict:
    """Event totals per category, plus per interest for the signed-in profile."""
    interests: dict[str, list[str]] = {}
    if user:
        profile = await db.get(Profile, user["id"])
        if profile:
            for slug in [
                *(profile.career_interests or []),
                *(profile.personal_interests or []),
            ]:
                if slug:
                    interests[slug] = events.interest_tags(slug)

    rows = await fn.discovery_counts(db, interests)
    total = (
        await db.scalar(
            select(func.count()).select_from(Event).where(*events.visible())
        )
        or 0
    )
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
    """In-person events grouped country → city, biggest first."""
    rows = (
        await db.execute(
            select(Event.country, Event.city, func.count().label("events"))
            .where(
                *events.visible(),
                Event.format == "in_person",
                Event.country.is_not(None),
                Event.city.is_not(None),
            )
            .group_by(Event.country, Event.city)
            .order_by(func.count().desc(), Event.country, Event.city)
        )
    ).all()

    grouped: dict[str, dict] = {}
    for country, city, total in rows:
        group = grouped.setdefault(country, {"country": country, "cities": []})
        group["cities"].append({"city": city, "events": int(total)})
    return sorted(
        grouped.values(),
        key=lambda group: (
            -sum(city["events"] for city in group["cities"]),
            group["country"],
        ),
    )
