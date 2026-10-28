"""Request and response shapes for events."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

InteractionKind = Literal["view", "share", "apply_click", "calendar_add"]
MarkKind = Literal["saved", "dismissed", "external_registered", "attended"]
EventFormat = Literal["in_person", "online", "hybrid"]
# soonest and date are the same order; relevance and deadline rank like the frontend's Explore.
EventSort = Literal["soonest", "date", "popular", "newest", "relevance", "deadline"]


class OrganiserBrief(BaseModel):
    id: UUID | None = None
    slug: str
    name: str
    logo_url: str | None = None
    is_verified: bool = False
    kind: str | None = None


class CategoryBrief(BaseModel):
    id: UUID | None = None
    slug: str
    name: str
    group: str | None = None


class EventCounts(BaseModel):
    going: int = 0
    waitlisted: int = 0
    pending: int = 0


class EventCard(BaseModel):
    id: UUID
    title: str
    summary: str | None = None
    cover_url: str | None = None
    starts_at: datetime
    ends_at: datetime | None = None
    timezone: str
    format: str
    venue_name: str | None = None
    address: str | None = None
    city: str | None = None
    country: str | None = None
    tags: list[str] = Field(default_factory=list)
    university: str | None = None
    registration_mode: str
    external_url: str | None = None
    external_label: str
    requires_approval: bool
    capacity: int | None = None
    waitlist_enabled: bool = True
    registration_deadline: datetime | None = None
    price_min: float | None = None
    price_max: float | None = None
    currency: str
    status: str
    is_featured: bool = False
    organiser_id: UUID | None = None
    category_id: UUID | None = None
    source: str = "orbit"
    source_url: str | None = None
    published_at: datetime | None = None
    created_at: datetime
    series_key: str | None = None
    series_label: str | None = None
    organiser: OrganiserBrief | None = None
    category: CategoryBrief | None = None
    counts: EventCounts = Field(default_factory=EventCounts)
    # Set where the list is ranked for the viewer (relevance/deadline search, feeds, happening soon).
    score: float | None = None
    reason: str | None = None


class EventCountsRow(BaseModel):
    event_id: UUID
    going: int = 0
    waitlisted: int = 0
    pending: int = 0


class EventPage(BaseModel):
    items: list[EventCard]
    total: int
    page: int
    page_size: int
    # True when a ranked search read only the soonest 80 matches (narrow the search to see more).
    truncated: bool = False


class TicketTypeOut(BaseModel):
    id: UUID
    event_id: UUID
    name: str
    description: str | None = None
    price: float
    currency: str
    quantity: int | None = None
    sales_end_at: datetime | None = None
    sort_order: int = 0


class TicketTypeAvailability(TicketTypeOut):
    remaining: int | None = None


class TicketAvailabilityOut(BaseModel):
    ticket_types: list[TicketTypeAvailability]
    event_remaining: int | None = None


class EventQuestionOut(BaseModel):
    id: UUID
    label: str
    kind: str
    options: list[str] = Field(default_factory=list)
    required: bool = False
    sort_order: int = 0


class RegistrationOut(BaseModel):
    id: UUID
    event_id: UUID
    user_id: UUID
    ticket_type_id: UUID | None = None
    status: str
    answers: dict[str, Any] = Field(default_factory=dict)
    amount: float
    currency: str
    payment_status: str
    qr_code: str
    checked_in_at: datetime | None = None
    decided_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SeriesDateOut(BaseModel):
    id: UUID
    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    status: str
    series_label: str | None = None


class EventPrivateOut(BaseModel):
    online_url: str | None = None
    guest_info: str | None = None


class EventDetail(EventCard):
    description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    cancel_reason: str | None = None
    ticket_types: list[TicketTypeAvailability] = Field(default_factory=list)
    event_questions: list[EventQuestionOut] = Field(default_factory=list)
    spots_left: int | None = None
    can_manage: bool = False
    my_registration: RegistrationOut | None = None
    saved: bool = False
    external_registered: bool = False
    event_private: EventPrivateOut | None = None
    series: list[SeriesDateOut] = Field(default_factory=list)


class InteractionIn(BaseModel):
    kind: InteractionKind


class MarkIn(BaseModel):
    on: bool = True


class MarkOut(BaseModel):
    kind: MarkKind
    on: bool


class RegistrationIn(BaseModel):
    ticket_type_id: UUID | None = None
    answers: dict[str, Any] = Field(default_factory=dict)


class RegistrationResponse(BaseModel):
    registration: RegistrationOut
    checkout_url: str | None = None
