"""Cloudinary signing, mirroring the frontend's src/lib/cloudinary.ts."""

import hashlib
import re
import time
from typing import Any, Optional
from urllib.parse import unquote, urlsplit
from uuid import UUID

from app.core.config import settings

UPLOAD_KINDS = ("event-covers", "organiser-logos")
ALLOWED_FORMATS = "jpg,png,webp"

# Never part of a Cloudinary signature.
_UNSIGNED = {"file", "api_key", "resource_type", "cloud_name"}


def sign_params(params: dict[str, Any], api_secret: str) -> str:
    """Cloudinary signature: SHA-1 of the params (sorted by name, "k=v" joined with
    "&", empty values and file/api_key/resource_type/cloud_name dropped) followed
    directly by the API secret."""
    to_sign = "&".join(
        f"{key}={value}"
        for key, value in sorted(params.items())
        if value is not None and value != "" and key not in _UNSIGNED
    )
    return hashlib.sha1(f"{to_sign}{api_secret}".encode()).hexdigest()


def sign(kind: str, user_id: UUID | str) -> dict:
    """Signed parameters for a direct browser upload into orbit/<kind>/<user>.

    Same shape as the frontend's /api/uploads/sign. The browser posts the file
    to https://api.cloudinary.com/v1_1/<cloudName>/image/upload with api_key,
    timestamp, signature, folder and allowed_formats exactly as returned here.
    """
    folder = f"orbit/{kind}/{user_id}"
    timestamp = int(time.time())
    signature = sign_params(
        {"allowed_formats": ALLOWED_FORMATS, "folder": folder, "timestamp": timestamp},
        settings.CLOUDINARY_API_SECRET or "",
    )
    return {
        "cloudName": settings.CLOUDINARY_CLOUD_NAME,
        "apiKey": settings.CLOUDINARY_API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "folder": folder,
        "allowedFormats": ALLOWED_FORMATS,
        "maxBytes": int((settings.IMAGE_MAX_BYTES or {}).get(kind, 5 * 1024 * 1024)),
    }


_VERSION = re.compile(r"v\d+")
_TRANSFORMATION = re.compile(r"[a-z]{1,3}_[^/]*")
_EXTENSION = re.compile(r"\.[a-z0-9]+\Z", re.IGNORECASE)


def orbit_image_public_id(url: Optional[str], cloud_name: str) -> Optional[str]:
    """The public id of an image Orbit stored in its own Cloudinary account
    ("orbit/event-covers/…"), or None for anything else (another account,
    another folder, Supabase Storage, Meetup/Luma images). Only these ids are
    ever deleted."""
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.hostname != "res.cloudinary.com":
            return None
        path = unquote(parsed.path)
    except ValueError:
        return None
    prefix = f"/{cloud_name}/image/upload/"
    if not path.startswith(prefix):
        return None
    # Drop transformation segments ("f_auto,q_auto,…") and the version ("v1792…").
    parts = path[len(prefix):].split("/")
    while len(parts) > 1 and (
        _VERSION.fullmatch(parts[0])
        or _TRANSFORMATION.fullmatch(parts[0])
        or "," in parts[0]
    ):
        parts.pop(0)
    public_id = _EXTENSION.sub("", "/".join(parts))
    return public_id if public_id.startswith("orbit/") and ".." not in public_id else None
