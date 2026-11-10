"""Past-event cleanup: permanently delete events whose date has passed, and their Cloudinary covers.

An event is past when it ended (or, with no end time, started) before the start of today in
London. Deleting it cascades to its tickets, registrations, questions, saves, group links and
notifications. Covers go through `release_images`, which only destroys images nothing else still
uses (another date of the same series may share one) and only after the batch commits.

The nightly cron runs it only while the admin switch `past_event_cleanup.enabled` is on; an
admin's "Run now" runs it whatever the switch says.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.admin.models import AdminAuditLog, AppSetting
from app.core.database import AsyncSessionLocal
from app.events.models import Event
from app.uploads.release import release_images

logger = logging.getLogger(__name__)

SETTING_KEY = "past_event_cleanup"
BATCH_SIZE = 200
MAX_BATCHES = 250  # 50,000 events per run; the next run picks up the rest.
LONDON = ZoneInfo("Europe/London")

Trigger = Literal["cron", "manual"]

# One cleanup at a time in this process.
_lock = asyncio.Lock()


def cutoff(now: Optional[datetime] = None) -> datetime:
    """Midnight at the start of today in London, as UTC."""
    local = (now or datetime.now(timezone.utc)).astimezone(LONDON)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _is_past(before: datetime):
    return func.coalesce(Event.ends_at, Event.starts_at) < before


async def _trusted_session():
    session = AsyncSessionLocal()
    await session.execute(text("select set_config('app.trusted', 'on', true)"))
    return session


async def read_setting() -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        value = await db.scalar(select(AppSetting.value).where(AppSetting.key == SETTING_KEY))
        pending = await db.scalar(select(func.count()).select_from(Event).where(_is_past(cutoff())))
    value = value or {}
    return {
        "enabled": bool(value.get("enabled")),
        "last_run": value.get("last_run"),
        "pending": int(pending or 0),
        "cutoff": cutoff().isoformat(),
        "running": _lock.locked(),
    }


async def _merge_setting(db, patch: dict[str, Any]) -> None:
    stmt = pg_insert(AppSetting).values(key=SETTING_KEY, value=patch)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": AppSetting.value.op("||")(stmt.excluded.value), "updated_at": func.now()},
        )
    )


async def set_enabled(enabled: bool, admin_id: Optional[UUID]) -> dict[str, Any]:
    session = await _trusted_session()
    async with session as db:
        await _merge_setting(db, {"enabled": enabled})
        await db.execute(
            insert(AdminAuditLog).values(
                actor=admin_id,
                action="settings.past_event_cleanup",
                target_type="setting",
                target_id=SETTING_KEY,
                details={"enabled": enabled},
            )
        )
        await db.commit()
    return await read_setting()


async def run_cleanup(trigger: Trigger, admin_id: Optional[UUID] = None) -> dict[str, Any]:
    """Delete past events in batches. The cron trigger does nothing while the switch is off."""
    if trigger == "cron" and not (await read_setting())["enabled"]:
        return {"ran": False, "reason": "disabled"}
    if _lock.locked():
        return {"ran": False, "reason": "already_running"}

    async with _lock:
        before = cutoff()
        started = datetime.now(timezone.utc)
        deleted = 0
        images = 0
        errors: list[str] = []
        for _ in range(MAX_BATCHES):
            session = await _trusted_session()
            try:
                async with session as db:
                    ids = select(Event.id).where(_is_past(before)).limit(BATCH_SIZE).scalar_subquery()
                    covers = (
                        await db.execute(delete(Event).where(Event.id.in_(ids)).returning(Event.cover_url))
                    ).scalars().all()
                    if not covers:
                        break
                    # Checked inside this transaction, destroyed on Cloudinary after it commits.
                    images += await release_images(db, covers)
                    await db.commit()
                    deleted += len(covers)
            except Exception as exc:  # noqa: BLE001
                logger.exception("[cleanup] batch failed")
                errors.append(str(exc)[:300])
                break

        result = {
            "ran": True,
            "trigger": trigger,
            "deleted": deleted,
            "images": images,
            "cutoff": before.isoformat(),
            "started_at": started.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "errors": errors,
        }
        session = await _trusted_session()
        async with session as db:
            await _merge_setting(db, {"last_run": result})
            await db.execute(
                insert(AdminAuditLog).values(
                    actor=admin_id,
                    action="events.cleanup_past",
                    target_type="event",
                    details={k: result[k] for k in ("trigger", "deleted", "images", "cutoff", "errors")},
                )
            )
            await db.commit()
        logger.info("[cleanup] %s: deleted %d events, released %d images", trigger, deleted, images)
        return result
