"""Delete Orbit's Cloudinary images once nothing uses them.

Mirrors the frontend's src/lib/cloudinary-release.ts. Call `release_images`
right after permanently deleting events (or anything else that held an image
URL), in the same session. Only images in Orbit's own `orbit/` folders of this
Cloudinary account are touched, and only when no event, organiser, group or
profile still references them (another date of the same series shares one
cover, for instance).
"""

import asyncio
import logging
import time
from typing import Iterable, Optional

import httpx
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.uploads.service import orbit_image_public_id, sign_params

logger = logging.getLogger(__name__)

_STILL_USED = text(
    "select cover_url from events where cover_url = any(:urls)"
    " union select logo_url from organisers where logo_url = any(:urls)"
    " union select cover_url from groups where cover_url = any(:urls)"
    " union select avatar_url from profiles where avatar_url = any(:urls)"
)

# Destroy tasks in flight, held so the event loop does not drop them.
_running: set[asyncio.Task] = set()


async def release_images(db: AsyncSession, urls: Iterable[Optional[str]]) -> int:
    """Schedule the Cloudinary deletion of the given images that are now unused.

    The reference check runs in the caller's transaction, so it sees the rows
    the caller just deleted as gone. The deletions themselves run only after
    that transaction commits; a rollback cancels them, so an image is never
    lost for a delete that did not happen. Returns how many images were
    scheduled. Never raises.
    """
    try:
        if not settings.cloudinary_configured:
            return 0
        owned: dict[str, str] = {}  # url -> public id
        for url in urls:
            public_id = orbit_image_public_id(url, settings.CLOUDINARY_CLOUD_NAME)
            if url and public_id:
                owned[url] = public_id
        if not owned:
            return 0

        # A savepoint, so a failed query cannot abort the caller's transaction.
        async with db.begin_nested():
            result = await db.execute(_STILL_USED, {"urls": list(owned)})
            still_used = {row[0] for row in result}
        unused = sorted({pid for url, pid in owned.items() if url not in still_used})
        if unused:
            _destroy_after_commit(db, unused)
        return len(unused)
    except Exception:
        # Can't tell what is still used: keep everything.
        logger.exception("[cloudinary] release check failed; keeping the images")
        return 0


def _destroy_after_commit(db: AsyncSession, public_ids: list[str]) -> None:
    loop = asyncio.get_running_loop()
    session = db.sync_session
    state = {"settled": False}

    def on_commit(sess) -> None:
        # Savepoint releases also fire after_commit; wait for the outer commit.
        if state["settled"] or sess.get_nested_transaction() is not None:
            return
        state["settled"] = True
        task = loop.create_task(_destroy_all(public_ids))
        _running.add(task)
        task.add_done_callback(_running.discard)

    def on_rollback(sess) -> None:
        state["settled"] = True

    event.listen(session, "after_commit", on_commit)
    event.listen(session, "after_rollback", on_rollback)


async def _destroy_all(public_ids: list[str]) -> int:
    deleted = 0
    for public_id in public_ids:
        try:
            if await _destroy(public_id):
                deleted += 1
        except Exception:
            logger.exception("[cloudinary] destroy %s failed", public_id)
    logger.info("[cloudinary] released %d of %d images", deleted, len(public_ids))
    return deleted


async def _destroy(public_id: str) -> bool:
    """Delete one image (and purge it from the CDN). True when Cloudinary says ok."""
    timestamp = int(time.time())
    data = {
        "public_id": public_id,
        "timestamp": str(timestamp),
        "invalidate": "true",
        "api_key": settings.CLOUDINARY_API_KEY,
        "signature": sign_params(
            {"public_id": public_id, "timestamp": timestamp, "invalidate": "true"},
            settings.CLOUDINARY_API_SECRET or "",
        ),
    }
    url = f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/image/destroy"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, data=data)
    try:
        return response.json().get("result") == "ok"
    except ValueError:
        return False
