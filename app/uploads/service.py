"""Cloudinary signing and local image storage."""

import hashlib
import time
import uuid
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, UploadFile, status

from app.core.config import settings

KINDS = ("event-covers", "organiser-logos")
ALLOWED_FORMATS = ["jpg", "jpeg", "png", "webp"]
MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def sign(kind: str) -> dict:
    """Signed parameters for a direct browser upload to Cloudinary."""
    timestamp = int(time.time())
    folder = kind
    signature = hashlib.sha1(
        f"folder={folder}&timestamp={timestamp}{settings.CLOUDINARY_API_SECRET}".encode()
    ).hexdigest()
    return {
        "cloud_name": settings.CLOUDINARY_CLOUD_NAME,
        "api_key": settings.CLOUDINARY_API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "folder": folder,
        "allowed_formats": ALLOWED_FORMATS,
    }


def max_bytes(kind: str) -> int:
    limits = settings.IMAGE_MAX_BYTES or {}
    return int(limits.get(kind, 5 * 1024 * 1024))


async def save(file: UploadFile, kind: str, user_id: UUID) -> dict:
    """Validate and store an image under UPLOAD_DIR/<kind>/<user>/."""
    if kind not in KINDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown upload kind")

    extension = MIME_EXTENSIONS.get((file.content_type or "").lower())
    if not extension:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Images must be JPEG, PNG or WebP"
        )

    limit = max_bytes(kind)
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Images must be {limit // (1024 * 1024)} MB or smaller",
        )
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "The file is empty")

    folder = Path(settings.UPLOAD_DIR) / kind / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{extension}"
    (folder / name).write_bytes(data)
    return {"url": f"/uploads/{kind}/{user_id}/{name}"}
