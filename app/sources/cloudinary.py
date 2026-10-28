"""Has Cloudinary fetch a remote image and keep Orbit's own copy (port of `cloudinary-import.ts`).

Orbit's server never downloads the image: Cloudinary fetches the https URL itself. When the
CLOUDINARY_* settings are missing, `configured()` is False and callers skip covers (the
importers record one error saying so); `import_remote_image` refuses without sending anything.
"""

import hashlib
import time
from typing import Any

import httpx

from app.connectors import urls
from app.core.config import settings

COVER_FOLDER = "orbit/event-covers/sources"


def configured() -> bool:
    return settings.cloudinary_configured


def sign_params(params: dict[str, Any], api_secret: str) -> str:
    """Cloudinary signed-upload signature: SHA-1 of the sorted `k=v` pairs (empty values and
    file/api_key/resource_type/cloud_name left out) followed by the API secret."""
    pairs = sorted(
        (k, v) for k, v in params.items()
        if v is not None and v != "" and k not in ("file", "api_key", "resource_type", "cloud_name")
    )
    to_sign = "&".join(f"{k}={v}" for k, v in pairs)
    return hashlib.sha1(f"{to_sign}{api_secret}".encode()).hexdigest()


async def import_remote_image(image_url: str, folder: str = COVER_FOLDER) -> str:
    """Returns the secure URL of Orbit's copy of `image_url`."""
    if not configured():
        raise Exception("Image import needs Cloudinary (CLOUDINARY_* env vars are missing).")
    url = urls.parse(image_url)
    if url is None:
        raise Exception("That image link isn't a valid URL.")
    if url.protocol != "https:":
        raise Exception("Image links must start with https://")
    timestamp = int(time.time())
    form = {
        "file": url.href,
        "api_key": settings.CLOUDINARY_API_KEY or "",
        "timestamp": str(timestamp),
        "folder": folder,
        "signature": sign_params({"folder": folder, "timestamp": timestamp}, settings.CLOUDINARY_API_SECRET or ""),
    }
    endpoint = f"https://api.cloudinary.com/v1_1/{settings.CLOUDINARY_CLOUD_NAME}/image/upload"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(endpoint, files={k: (None, v) for k, v in form.items()})
    except httpx.TimeoutException as exc:
        raise Exception("The operation was aborted due to timeout") from exc
    except httpx.TransportError as exc:
        raise Exception("fetch failed") from exc
    try:
        body = response.json()
    except ValueError:
        body = None
    secure_url = body.get("secure_url") if isinstance(body, dict) else None
    if not response.is_success or not secure_url:
        error = body.get("error") if isinstance(body, dict) else None
        reason = error.get("message") if isinstance(error, dict) and error.get("message") else f"HTTP {response.status_code}"
        raise Exception(f"Cloudinary couldn't import the image: {reason}")
    return secure_url
