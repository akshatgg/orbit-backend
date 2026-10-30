"""What a message may carry: files this app's Cloudinary holds in the sender's own folder.

The browser uploads to Cloudinary directly (`POST /uploads/sign`), so these checks are all
the server sees of a file. Whoever signs a request can only upload into `orbit/chat/{their
id}`, so a URL in that folder is theirs. Sizes come from the browser, as for covers and logos.
"""

import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.messaging.schemas import AttachmentIn
from app.uploads.service import (
    CHAT_DOC_MAX_BYTES,
    CHAT_FOLDER,
    CHAT_IMAGE_MAX_BYTES,
    orbit_asset,
)

IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
DOC_MIME = "application/pdf"
# Only a plain delivery URL: no transformations, which could fetch or rewrite anything.
_DELIVERY_PATH = re.compile(r"/(image|raw)/upload/(?:v\d+/)?(orbit/chat/[^?#]+)\Z")


def _refuse(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)


def check_attachment(item: AttachmentIn, user_id: UUID | str) -> None:
    """422 unless it's a file of the right type in `orbit/chat/{user_id}` of this app's account."""
    cloud = settings.CLOUDINARY_CLOUD_NAME or ""
    try:
        parts = urlsplit(item.url)
        path = unquote(parts.path)
    except ValueError:
        raise _refuse("That attachment's address isn't valid") from None
    match = _DELIVERY_PATH.fullmatch(path[len(f"/{cloud}"):]) if path.startswith(f"/{cloud}/") else None
    if (
        parts.scheme != "https"
        or parts.hostname != "res.cloudinary.com"
        or parts.port is not None
        or parts.username
        or parts.query
        or parts.fragment
        or not match
    ):
        raise _refuse("Attachments must be uploaded through Orbit")
    resource_type = match.group(1)
    folder = f"{CHAT_FOLDER}/{user_id}/"
    asset = orbit_asset(item.url, cloud)
    if not asset or not asset[1].startswith(folder) or asset[1] != item.public_id:
        raise _refuse("Attachments must be your own uploads")

    if item.kind == "image":
        if resource_type != "image" or not path.lower().endswith(IMAGE_EXTENSIONS):
            raise _refuse("Images must be JPG, PNG or WebP")
        if item.mime not in IMAGE_MIMES:
            raise _refuse("Images must be JPG, PNG or WebP")
        if item.bytes > CHAT_IMAGE_MAX_BYTES:
            raise _refuse("Images can be up to 5 MB")
    else:
        if resource_type != "raw" or item.mime != DOC_MIME:
            raise _refuse("Documents must be PDFs")
        if not (item.name or "").lower().endswith(".pdf"):
            raise _refuse("Documents must be PDFs")
        if item.bytes > CHAT_DOC_MAX_BYTES:
            raise _refuse("Documents can be up to 10 MB")


def check_attachments(items: list[AttachmentIn], user_id: UUID | str) -> None:
    if not items:
        return
    if not settings.cloudinary_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Uploads are not configured (Cloudinary keys missing)",
        )
    for item in items:
        check_attachment(item, user_id)


async def save(db: AsyncSession, message_id: str, conversation_id: str, items: list[AttachmentIn]) -> None:
    """One INSERT however many there are."""
    if not items:
        return
    await db.execute(
        text(
            """
            insert into public.message_attachments
              (message_id, conversation_id, kind, url, public_id, name, mime, bytes, width, height, position)
            select :m, :c, x.kind, x.url, x.public_id, x.name, x.mime, x.bytes, x.width, x.height, x.position
              from jsonb_to_recordset(cast(:rows as jsonb)) as x(
                kind text, url text, public_id text, name text, mime text,
                bytes bigint, width int, height int, position smallint
              )
            """
        ),
        {"m": message_id, "c": conversation_id, "rows": json.dumps([{**i.model_dump(), "position": n} for n, i in enumerate(items)])},
    )


def attachment_out(r: Any) -> dict[str, Any]:
    return {
        "id": str(r["id"]),
        "kind": r["kind"],
        "url": r["url"],
        "public_id": r["public_id"],
        "name": r["name"],
        "mime": r["mime"],
        "bytes": r["bytes"],
        "width": r["width"],
        "height": r["height"],
    }
