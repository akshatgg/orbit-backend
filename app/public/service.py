"""Landing, sitemap and Open Graph reads."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import service as events
from app.events.models import Event
from app.organisers.models import Organiser


async def landing_events(db: AsyncSession, limit: int = 6) -> list[dict]:
    """Soonest published events for the logged-out landing page."""
    return await events.cards(
        db, events.card_query().where(*events.visible()).order_by(Event.starts_at).limit(limit)
    )


async def sitemap_data(db: AsyncSession) -> dict:
    """Ids and last-modified stamps for the sitemap."""
    event_rows = (
        await db.execute(
            select(Event.id, Event.updated_at)
            .where(Event.status == "published", Event.duplicate_of.is_(None))
            .order_by(Event.starts_at)
        )
    ).all()
    organiser_rows = (
        await db.execute(
            select(Organiser.slug, Organiser.updated_at)
            .where(Organiser.status == "approved")
            .order_by(Organiser.name)
        )
    ).all()
    return {
        "events": [{"id": row[0], "updated_at": row[1]} for row in event_rows],
        "organisers": [{"slug": row[0], "updated_at": row[1]} for row in organiser_rows],
    }


async def og_event(db: AsyncSession, event_id: UUID) -> dict | None:
    """The fields a social card needs, or None when the event is not public."""
    row = await events.event_row(db, event_id)
    if not row:
        return None
    event, organiser, _ = row
    # getEventForOg: published or cancelled (a cancelled event's card says so), as a visitor sees it.
    if event.status not in ("published", "cancelled") or not events.publicly_visible(event, organiser):
        return None
    summary = event.summary or (event.description or "").strip()[:200] or None
    return {
        "id": event.id,
        "title": event.title,
        "summary": summary,
        "cover_url": event.cover_url,
        "starts_at": event.starts_at,
        "venue_name": event.venue_name,
        "city": event.city,
        "format": event.format,
        "status": event.status,
        "organiser_name": organiser.name if organiser else None,
    }
