"""Ranked event feeds: interests first, then followed organisers, then soonest."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import service as events
from app.events.models import Event
from app.users.models import Follow, Profile, UserEvent

CANDIDATES = 300
TOP_PICKS = 6


async def _ranked(db: AsyncSession, user: dict, days: int) -> list[dict]:
    uid = user["id"]
    profile = await db.get(Profile, uid)
    followed = set(
        (await db.scalars(select(Follow.organiser_id).where(Follow.user_id == uid))).all()
    )
    dismissed = set(
        (
            await db.scalars(
                select(UserEvent.event_id).where(
                    UserEvent.user_id == uid, UserEvent.kind == "dismissed"
                )
            )
        ).all()
    )

    interests: set[str] = set()
    categories: set[str] = set()
    if profile:
        interests = {
            slug
            for slug in [
                *(profile.career_interests or []),
                *(profile.personal_interests or []),
            ]
            if slug
        }
        categories = {slug for slug in (profile.preferred_categories or []) if slug}

    stmt = events.card_query().where(
        *events.visible(), Event.starts_at <= datetime.now(timezone.utc) + timedelta(days=days)
    )
    if dismissed:
        stmt = stmt.where(Event.id.not_in(dismissed))
    cards = await events.cards(db, stmt.order_by(Event.starts_at).limit(CANDIDATES))

    def score(item: dict) -> tuple:
        tags = {str(tag).lower() for tag in item.get("tags") or []}
        category = item.get("category") or {}
        if category:
            tags.update(
                {
                    str(category.get("slug") or "").lower(),
                    str(category.get("group") or "").lower(),
                }
            )
        tags.discard("")
        hits = sum(
            1 for slug in interests if tags.intersection(events.interest_tags(slug))
        )
        if category.get("slug") in categories:
            hits += 1
        return (-hits, 0 if item.get("organiser_id") in followed else 1, item["starts_at"])

    cards.sort(key=score)
    return cards


async def for_you(db: AsyncSession, user: dict, days: int = 60, limit: int = 30) -> list[dict]:
    """Upcoming events over the next `days` days, best matches first."""
    return (await _ranked(db, user, days))[:limit]


async def week(db: AsyncSession, user: dict) -> dict:
    """The next seven days: six top picks, then everything else by category."""
    ranked = await _ranked(db, user, 7)
    top_picks = ranked[:TOP_PICKS]
    grouped: dict[str, list[dict]] = {}
    for item in ranked[TOP_PICKS:]:
        title = (item.get("category") or {}).get("name") or "Other"
        grouped.setdefault(title, []).append(item)
    return {
        "top_picks": top_picks,
        "sections": [{"title": title, "events": items} for title, items in grouped.items()],
    }
