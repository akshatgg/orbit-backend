"""Category response models."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CategoryOut(BaseModel):
    id: UUID
    slug: str
    name: str
    group: str
    sort_order: int
    keywords: list[str]
    created_at: datetime
