"""Request bodies for the organiser studio (event editor, guests, door)."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings

EventFormat = Literal["in_person", "online", "hybrid"]
RegistrationMode = Literal["tickets", "external", "none"]
QuestionKind = Literal[
    "short_text", "long_text", "single_select", "multi_select", "checkbox"
]
GuestStatus = Literal[
    "going", "pending", "waitlisted", "awaiting_payment", "declined", "cancelled"
]

URL_PATTERN = r"^https?://"


def _aware(value: datetime | None) -> datetime | None:
    """Treat a timestamp without an offset as UTC so the database never guesses."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class TicketTypeIn(BaseModel):
    """A ticket type. On edit, an `id` keeps that row (and its guests); no id adds one."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    name: str = Field(min_length=1, max_length=60)
    description: str | None = None
    price: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    quantity: int | None = Field(default=None, gt=0)
    sales_end_at: datetime | None = None
    sort_order: int = 0

    @field_validator("sales_end_at")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return _aware(value)


class QuestionIn(BaseModel):
    """An application question. On edit, an `id` keeps that row (and its answers)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID | None = None
    label: str = Field(min_length=1, max_length=200)
    kind: QuestionKind = "short_text"
    options: list[str] = Field(default_factory=list)
    required: bool = False
    sort_order: int = 0


class EventPrivateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    online_url: str | None = Field(default=None, max_length=500, pattern=URL_PATTERN)
    guest_info: str | None = Field(default=None, max_length=4000)


class EventCreate(BaseModel):
    """A new draft event owned by an organiser the caller manages."""

    model_config = ConfigDict(extra="forbid")

    organiser_id: UUID
    title: str = Field(min_length=3, max_length=140)
    summary: str | None = Field(default=None, max_length=200)
    description: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    timezone: str = settings.DEFAULT_TIMEZONE
    format: EventFormat = "in_person"
    venue_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    category_id: UUID | None = None
    tags: list[str] = Field(default_factory=list)
    university: str | None = Field(default=None, max_length=100)
    registration_mode: RegistrationMode = "tickets"
    external_url: str | None = Field(default=None, max_length=500, pattern=URL_PATTERN)
    external_label: str | None = Field(default=None, max_length=40)
    requires_approval: bool = False
    capacity: int | None = Field(default=None, gt=0)
    waitlist_enabled: bool = True
    registration_deadline: datetime | None = None
    cover_url: str | None = Field(default=None, max_length=500, pattern=URL_PATTERN)
    ticket_types: list[TicketTypeIn] = Field(default_factory=list)
    questions: list[QuestionIn] = Field(default_factory=list)
    private: EventPrivateIn | None = None

    @field_validator("starts_at", "ends_at", "registration_deadline")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def _consistent(self) -> "EventCreate":
        if self.ends_at and self.starts_at and self.ends_at < self.starts_at:
            raise ValueError("ends_at cannot be before starts_at")
        if self.registration_mode == "external" and not self.external_url:
            raise ValueError("external events need an external_url")
        return self


class EventUpdate(BaseModel):
    """Editor fields only — provenance, review and organiser can never be changed here."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=3, max_length=140)
    summary: str | None = Field(default=None, max_length=200)
    description: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str | None = None
    format: EventFormat | None = None
    venue_name: str | None = Field(default=None, max_length=200)
    address: str | None = Field(default=None, max_length=300)
    city: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    category_id: UUID | None = None
    tags: list[str] | None = None
    university: str | None = Field(default=None, max_length=100)
    registration_mode: RegistrationMode | None = None
    external_url: str | None = Field(default=None, max_length=500, pattern=URL_PATTERN)
    external_label: str | None = Field(default=None, max_length=40)
    requires_approval: bool | None = None
    capacity: int | None = Field(default=None, gt=0)
    waitlist_enabled: bool | None = None
    registration_deadline: datetime | None = None
    cover_url: str | None = Field(default=None, max_length=500, pattern=URL_PATTERN)
    ticket_types: list[TicketTypeIn] | None = None
    questions: list[QuestionIn] | None = None
    private: EventPrivateIn | None = None

    @field_validator("starts_at", "ends_at", "registration_deadline")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def _ordered(self) -> "EventUpdate":
        if self.ends_at and self.starts_at and self.ends_at < self.starts_at:
            raise ValueError("ends_at cannot be before starts_at")
        for rows in (self.ticket_types, self.questions):
            ids = [row.id for row in rows or [] if row.id is not None]
            if len(ids) != len(set(ids)):
                raise ValueError("the same id appears twice")
        return self


# The editor's MAX_EXTRA_DATES: the first date plus up to 25 more in one go.
MAX_EXTRA_DATES = 25


class DateSlotIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime | None = None

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _tz(cls, value: datetime | None) -> datetime | None:
        return _aware(value)

    @model_validator(mode="after")
    def _ordered(self) -> "DateSlotIn":
        if self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("each date must end after it starts")
        return self


class DatesIn(BaseModel):
    """More dates for an event: each becomes its own event in the same series."""

    model_config = ConfigDict(extra="forbid")

    dates: list[DateSlotIn] = Field(min_length=1, max_length=MAX_EXTRA_DATES)

    @model_validator(mode="after")
    def _distinct(self) -> "DatesIn":
        starts = [slot.starts_at for slot in self.dates]
        if len(starts) != len(set(starts)):
            raise ValueError("the same date appears twice")
        return self


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=500)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "decline"]


class CheckInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=100)


class CheckInByRegistrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registration_id: UUID
