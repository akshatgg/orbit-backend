"""Upload routes."""

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.deps import require_user
from app.uploads import service

router = APIRouter(prefix="/uploads", tags=["uploads"])


class SignIn(BaseModel):
    kind: Literal["event-covers", "organiser-logos"]


@router.post("/sign")
async def sign_upload(body: SignIn, user: dict = Depends(require_user)) -> dict:
    """Cloudinary upload signature; 503 when Cloudinary is not configured."""
    if not settings.cloudinary_configured:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Cloudinary is not configured"
        )
    return service.sign(body.kind)


@router.post("")
async def upload_image(
    file: UploadFile = File(...),
    kind: str = Form(...),
    user: dict = Depends(require_user),
) -> dict:
    """Store an image and return its /uploads/... URL (mounted in app/main.py)."""
    return await service.save(file, kind, user["id"])
