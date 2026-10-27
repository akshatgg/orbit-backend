"""Request bodies for the source admin API."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

SOURCE_KINDS = Literal[
    "ical",
    "luma",
    "luma-discover",
    "meetup",
    "meetup-search",
    "eventbrite",
    "fatsoma",
]


class SourceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    kind: SOURCE_KINDS = "ical"
    url: str = Field(min_length=1, max_length=2000)
    default_organiser_id: UUID | None = None
    default_category_id: UUID | None = None
    default_tags: list[str] = Field(default_factory=list)
    attribution: str | None = Field(default=None, max_length=200)
    permission_confirmed: bool = False
    permission_note: str | None = Field(default=None, max_length=1000)
    enabled: bool = False
    auto_category: bool = False

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        value = value.strip()
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("The URL must start with http:// or https://")
        return value

    @field_validator("default_tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        tags = []
        for tag in value:
            cleaned = " ".join(str(tag).strip().lower().split())
            if cleaned and cleaned not in tags:
                tags.append(cleaned[:30])
        return tags[:10]


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    kind: SOURCE_KINDS | None = None
    url: str | None = Field(default=None, min_length=1, max_length=2000)
    default_organiser_id: UUID | None = None
    default_category_id: UUID | None = None
    default_tags: list[str] | None = None
    attribution: str | None = Field(default=None, max_length=200)
    permission_confirmed: bool | None = None
    permission_note: str | None = Field(default=None, max_length=1000)
    enabled: bool | None = None
    auto_category: bool | None = None

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("The URL must start with http:// or https://")
        return value

    @field_validator("default_tags")
    @classmethod
    def _tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        tags = []
        for tag in value:
            cleaned = " ".join(str(tag).strip().lower().split())
            if cleaned and cleaned not in tags:
                tags.append(cleaned[:30])
        return tags[:10]


class EnabledBody(BaseModel):
    on: bool


class ReviewBody(BaseModel):
    decision: Literal["not_duplicate", "keep_hidden", "reviewed"]


class BulkReviewBody(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=500)


class ImportedDetailsBody(BaseModel):
    cover_url: str | None = None
    description: str | None = Field(default=None, max_length=10000)
    price_min: Decimal | None = Field(default=None, ge=0)
    price_max: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("cover_url")
    @classmethod
    def _cover(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not value.lower().startswith(("http://", "https://")):
            raise ValueError("The cover URL must start with http:// or https://")
        return value

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value
