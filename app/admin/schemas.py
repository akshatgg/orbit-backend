"""Request bodies for the admin console."""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import settings

EventFormat = Literal["in_person", "online", "hybrid"]
RegistrationMode = Literal["tickets", "external", "none"]
CategoryGroup = Literal["career", "social", "other"]
OrganiserDecision = Literal["approved", "rejected", "suspended", "pending"]


def _http_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if not value.lower().startswith(("http://", "https://")):
        raise ValueError("Must be a full link starting with http:// or https://")
    return value


def _tags(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    tags: list[str] = []
    for tag in value:
        cleaned = " ".join(str(tag).strip().lower().split())
        if cleaned and cleaned not in tags:
            tags.append(cleaned[:40])
    return tags[:20]


class EventCreate(BaseModel):
    title: str = Field(min_length=3, max_length=140)
    summary: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    cover_url: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    timezone: str = settings.DEFAULT_TIMEZONE
    format: EventFormat = "in_person"
    venue_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=100)
    country: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    category_id: UUID | None = None
    organiser_id: UUID | None = None
    tags: list[str] = Field(default_factory=list)
    university: str | None = Field(default=None, max_length=120)
    registration_mode: RegistrationMode = "tickets"
    external_url: str | None = None
    external_label: str = Field(default="Apply now", max_length=40)
    requires_approval: bool = False
    capacity: int | None = Field(default=None, gt=0)
    waitlist_enabled: bool = True
    registration_deadline: datetime | None = None
    price_min: Decimal | None = Field(default=None, ge=0)
    price_max: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="GBP", min_length=3, max_length=3)
    is_featured: bool = False

    @field_validator("cover_url", "external_url")
    @classmethod
    def _http_links(cls, value: str | None) -> str | None:
        return _http_url(value)

    @field_validator("tags")
    @classmethod
    def _tag_list(cls, value: list[str]) -> list[str]:
        return _tags(value) or []

    @field_validator("country")
    @classmethod
    def _country(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError("Use a two-letter country code")
        return value

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _consistent(self) -> "EventCreate":
        if self.ends_at is not None:
            starts = self.starts_at
            ends = self.ends_at
            if (starts.tzinfo is None) != (ends.tzinfo is None):
                zone = ZoneInfo(settings.DEFAULT_TIMEZONE)
                starts = starts if starts.tzinfo else starts.replace(tzinfo=zone)
                ends = ends if ends.tzinfo else ends.replace(tzinfo=zone)
            if ends < starts:
                raise ValueError("ends_at must be on or after starts_at")
        if self.registration_mode == "external" and not self.external_url:
            raise ValueError("External registration needs a link")
        return self


class EventUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=140)
    summary: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    cover_url: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str | None = None
    format: EventFormat | None = None
    venue_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=100)
    country: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    category_id: UUID | None = None
    organiser_id: UUID | None = None
    tags: list[str] | None = None
    university: str | None = Field(default=None, max_length=120)
    registration_mode: RegistrationMode | None = None
    external_url: str | None = None
    external_label: str | None = Field(default=None, max_length=40)
    requires_approval: bool | None = None
    capacity: int | None = Field(default=None, gt=0)
    waitlist_enabled: bool | None = None
    registration_deadline: datetime | None = None
    price_min: Decimal | None = Field(default=None, ge=0)
    price_max: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    is_featured: bool | None = None

    @field_validator("cover_url", "external_url")
    @classmethod
    def _http_links(cls, value: str | None) -> str | None:
        return _http_url(value)

    @field_validator("tags")
    @classmethod
    def _tag_list(cls, value: list[str] | None) -> list[str] | None:
        return _tags(value)

    @field_validator("country")
    @classmethod
    def _country(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError("Use a two-letter country code")
        return value

    @field_validator("currency")
    @classmethod
    def _currency(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class FeatureBody(BaseModel):
    on: bool


class ReasonBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class BulkRemoveBody(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=500)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class BulkCategoryBody(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=500)
    category_id: UUID | None = None


class BulkIdsBody(BaseModel):
    ids: list[UUID] = Field(min_length=1, max_length=500)


class VerifyStudentBody(BaseModel):
    verified: bool


class AddAdminBody(BaseModel):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or " " in value:
            raise ValueError("Enter an email address")
        return value


class OrganiserDecisionBody(BaseModel):
    decision: OrganiserDecision
    note: str | None = Field(default=None, max_length=1000)


class VerifyOrganiserBody(BaseModel):
    verified: bool


class CategoryCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=60)
    name: str = Field(min_length=2, max_length=80)
    group: CategoryGroup
    sort_order: int = 0
    keywords: list[str] = Field(default_factory=list)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        value = value.strip().lower()
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
        if not value or value.startswith("-") or value.endswith("-") or any(c not in allowed for c in value):
            raise ValueError("Use lowercase letters, numbers and single hyphens")
        return value

    @field_validator("keywords")
    @classmethod
    def _keywords(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for keyword in value:
            cleaned = " ".join(str(keyword).strip().lower().split())
            if cleaned and cleaned not in out:
                out.append(cleaned[:40])
        return out[:50]


class CategoryUpdate(BaseModel):
    slug: str | None = Field(default=None, min_length=2, max_length=60)
    name: str | None = Field(default=None, min_length=2, max_length=80)
    group: CategoryGroup | None = None
    sort_order: int | None = None
    keywords: list[str] | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-")
        if not value or value.startswith("-") or value.endswith("-") or any(c not in allowed for c in value):
            raise ValueError("Use lowercase letters, numbers and single hyphens")
        return value

    @field_validator("keywords")
    @classmethod
    def _keywords(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        out: list[str] = []
        for keyword in value:
            cleaned = " ".join(str(keyword).strip().lower().split())
            if cleaned and cleaned not in out:
                out.append(cleaned[:40])
        return out[:50]


class ImportBody(BaseModel):
    rows: list[dict[str, Any]] = Field(min_length=1, max_length=500)
