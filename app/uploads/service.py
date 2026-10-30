"""Cloudinary signing, mirroring the frontend's src/lib/cloudinary.ts."""

import hashlib
import re
import time
from typing import Any, Optional
from urllib.parse import unquote, urlsplit
from uuid import UUID

from app.core.config import settings

UPLOAD_KINDS = ("event-covers", "organiser-logos", "chat", "chat_doc")
ALLOWED_FORMATS = "jpg,png,webp"
CHAT_FOLDER = "orbit/chat"
CHAT_IMAGE_MAX_BYTES = 5 * 1024 * 1024
CHAT_DOC_MAX_BYTES = 10 * 1024 * 1024

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
    to https://api.cloudinary.com/v1_1/<cloudName>/<resourceType>/upload with api_key,
    timestamp, signature, folder and allowed_formats exactly as returned here.

    Chat files share orbit/chat/<user>: `chat` is an image, `chat_doc` a PDF, which
    Cloudinary keeps as a `raw` file.
    """
    folder = f"{CHAT_FOLDER}/{user_id}" if kind in ("chat", "chat_doc") else f"orbit/{kind}/{user_id}"
    resource_type = "raw" if kind == "chat_doc" else "image"
    formats = "pdf" if kind == "chat_doc" else ALLOWED_FORMATS
    timestamp = int(time.time())
    signature = sign_params(
        {"allowed_formats": formats, "folder": folder, "timestamp": timestamp},
        settings.CLOUDINARY_API_SECRET or "",
    )
    return {
        "cloudName": settings.CLOUDINARY_CLOUD_NAME,
        "apiKey": settings.CLOUDINARY_API_KEY,
        "timestamp": timestamp,
        "signature": signature,
        "folder": folder,
        "allowedFormats": formats,
        "resourceType": resource_type,
        "maxBytes": int(
            (settings.IMAGE_MAX_BYTES or {}).get(
                kind, CHAT_DOC_MAX_BYTES if kind == "chat_doc" else CHAT_IMAGE_MAX_BYTES
            )
        ),
    }


_VERSION = re.compile(r"v\d+")
_TRANSFORMATION = re.compile(r"[a-z]{1,3}_[^/]*")
_EXTENSION = re.compile(r"\.[a-z0-9]+\Z", re.IGNORECASE)


def orbit_image_public_id(url: Optional[str], cloud_name: str) -> Optional[str]:
    """The public id of an image Orbit stored in its own Cloudinary account
    ("orbit/event-covers/…"), or None for anything else (another account,
    another folder, Supabase Storage, Meetup/Luma images). Only these ids are
    ever deleted."""
    asset = orbit_asset(url, cloud_name)
    return asset[1] if asset and asset[0] == "image" else None


def orbit_asset(url: Optional[str], cloud_name: str) -> Optional[tuple[str, str]]:
    """(resource type, public id) of an image or raw file in Orbit's own folders, else
    None. A raw file's public id keeps its extension; an image's doesn't."""
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.hostname != "res.cloudinary.com":
            return None
        path = unquote(parsed.path)
    except ValueError:
        return None
    resource_type = next(
        (t for t in ("image", "raw") if path.startswith(f"/{cloud_name}/{t}/upload/")), None
    )
    if resource_type is None:
        return None
    prefix = f"/{cloud_name}/{resource_type}/upload/"
    # Drop transformation segments ("f_auto,q_auto,…") and the version ("v1792…").
    parts = path[len(prefix):].split("/")
    while len(parts) > 1 and (
        _VERSION.fullmatch(parts[0])
        or _TRANSFORMATION.fullmatch(parts[0])
        or "," in parts[0]
    ):
        parts.pop(0)
    public_id = "/".join(parts)
    if resource_type == "image":
        public_id = _EXTENSION.sub("", public_id)
    if not public_id.startswith("orbit/") or ".." in public_id:
        return None
    return resource_type, public_id
