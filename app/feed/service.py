"""Ranked event feeds (the frontend's getRankedFeed): one card per series, scored for the viewer."""

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.discovery.rank import load_viewer, rank_events
from app.events import service as events

TOP_PICKS = 3

# This Week's sections after the top picks, by category group (anything else is "other").
WEEK_SECTIONS = [
    ("career", "Career & academic", "Grow"),
    ("social", "Social & community", "Meet people"),
    ("other", "Other interests", "Everything else"),
]


async def ranked(db: AsyncSession, user: dict, days: int) -> list[dict]:
    """Events in the next `days` days, best for this person first.

    Dismissed events and ones they already booked are left out; each card has `score` and `reason`.
    """
    now = datetime.now(timezone.utc)
    candidates = await events.feed_candidates(db, until=now + timedelta(days=days))
    viewer = await load_viewer(db, user)
    return rank_events(candidates, viewer, now)


async def for_you(db: AsyncSession, user: dict, days: int = 60) -> list[dict]:
    """The For You feed."""
    return await ranked(db, user, days)


async def week(db: AsyncSession, user: dict, days: int = 7) -> dict:
    """This Week: three top picks, then every other event in its category group's section."""
    items = await ranked(db, user, days)
    rest = items[TOP_PICKS:]
    sections = []
    for group, title, eyebrow in WEEK_SECTIONS:
        members = [
            item
            for item in rest
            if ((item.get("category") or {}).get("group") or "other") == group
        ]
        if members:
            sections.append({"group": group, "title": title, "eyebrow": eyebrow, "events": members})
    return {"total": len(items), "top_picks": items[:TOP_PICKS], "sections": sections}
