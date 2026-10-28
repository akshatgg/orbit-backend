"""Request and response shapes for the signed-in person's own surface."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.events.schemas import EventCard

Goal = Literal["career", "network", "social", "host"]
UserType = Literal["student", "graduate", "professional", "other"]
EventFormat = Literal["in_person", "online", "hybrid"]

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_PARTS = ("morning", "day", "evening")

Slug = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9-]{1,60}$")]

# Text columns that may be cleared: trimmed, and "" is stored as null.
OPTIONAL_TEXT = {
    "avatar_url": 500,
    "university": 120,
    "department": 120,
    "course": 120,
    "year_of_study": 40,
    "company": 120,
    "industry": 120,
    "job_title": 120,
    "host_role": 80,
    "location_preference": 80,
}


def _unique(values: list) -> list:
    return list(dict.fromkeys(values))


class ProfileUpdate(BaseModel):
    """The editable columns of `profiles`. Role, email and verification are never writable.

    Mirrors the frontend's `profilePrefsSchema`: trimmed text, blank → null, lists de-duplicated,
    availability cleaned to known days and parts. Every field is optional; only the ones sent are
    written. Unknown keys are refused.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)] | None = None
    avatar_url: str | None = None
    university: str | None = None
    department: str | None = None
    course: str | None = None
    year_of_study: str | None = None
    graduation_year: int | None = Field(default=None, ge=1950, le=2100)
    user_type: UserType | None = None
    goals: list[Goal] | None = Field(default=None, max_length=4)
    career_interests: list[Slug] | None = Field(default=None, max_length=50)
    personal_interests: list[Slug] | None = Field(default=None, max_length=50)
    preferred_categories: list[Slug] | None = Field(default=None, max_length=50)
    preferred_formats: list[EventFormat] | None = Field(default=None, max_length=3)
    location_preference: str | None = None
    availability: dict[str, list[str]] | None = None
    company: str | None = None
    industry: str | None = None
    job_title: str | None = None
    host_role: str | None = None

    @field_validator(*OPTIONAL_TEXT, mode="before")
    @classmethod
    def _optional_text(cls, value: Any, info) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be text")
        value = value.strip()
        if not value:
            return None
        limit = OPTIONAL_TEXT[info.field_name]
        if len(value) > limit:
            raise ValueError(f"at most {limit} characters")
        if info.field_name == "avatar_url" and not value.lower().startswith(("http://", "https://")):
            raise ValueError("must be an http(s) URL")
        return value

    @field_validator("user_type", "graduation_year", mode="before")
    @classmethod
    def _blank_is_null(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "goals", "career_interests", "personal_interests", "preferred_categories", "preferred_formats"
    )
    @classmethod
    def _dedupe(cls, value: list | None) -> list | None:
        return None if value is None else _unique(value)

    @field_validator("availability")
    @classmethod
    def _availability(cls, value: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
        """Known days and parts only, parts in day order, empty days dropped; empty → null."""
        if value is None:
            return None
        clean = {
            day: [part for part in DAY_PARTS if part in value.get(day, [])]
            for day in WEEKDAYS
        }
        clean = {day: parts for day, parts in clean.items() if parts}
        return clean or None


class EventState(BaseModel):
    """What the viewer has done with each event."""

    saved: list[str] = Field(default_factory=list)
    dismissed: list[str] = Field(default_factory=list)
    external_registered: list[str] = Field(default_factory=list)
    attended: list[str] = Field(default_factory=list)
    registrations: dict[str, str] = Field(default_factory=dict)


class MyEventCard(EventCard):
    """An `/events` card plus the viewer's own marks on it."""

    saved: bool = False
    attended: bool = False
    external_registered: bool = False
    registration: str | None = None


class MyEventsPage(BaseModel):
    items: list[MyEventCard]
    total: int
    when: Literal["upcoming", "past"]


class MyCalendar(BaseModel):
    items: list[MyEventCard]
    total: int
