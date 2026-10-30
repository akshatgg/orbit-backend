"""Upload routes. Files go straight from the browser to Cloudinary."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.deps import require_user
from app.uploads import service

router = APIRouter(prefix="/uploads", tags=["uploads"])


class SignIn(BaseModel):
    kind: Literal["event-covers", "organiser-logos", "chat", "chat_doc"]


@router.post("/sign")
async def sign_upload(
    body: SignIn, response: Response, user: dict = Depends(require_user)
) -> dict:
    """Cloudinary upload signature for the signed-in user; 503 when Cloudinary
    is not configured. The API secret never leaves the server."""
    if not settings.cloudinary_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Image uploads are not configured (Cloudinary keys missing)",
        )
    response.headers["Cache-Control"] = "no-store"
    return service.sign(body.kind, user["id"])
