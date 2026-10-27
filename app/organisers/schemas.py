"""Request bodies for the organiser surface."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

SLUG_PATTERN = r"^[a-z0-9]+(-[a-z0-9]+)*$"
URL_PATTERN = r"^https?://"

OrganiserKind = Literal["society", "university", "employer", "community", "other"]
MemberRole = Literal["owner", "editor"]


class OrganiserCreate(BaseModel):
    """ "Become an organiser" — the row is created pending and the caller becomes owner."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=80)
    slug: str = Field(min_length=2, max_length=60, pattern=SLUG_PATTERN)
    kind: OrganiserKind
    bio: str | None = Field(default=None, max_length=1000)
    website: str | None = Field(default=None, max_length=300, pattern=URL_PATTERN)
    contact_email: EmailStr
    university: str | None = Field(default=None, max_length=100)
    logo_url: str | None = Field(default=None, max_length=500, pattern=URL_PATTERN)


class OrganiserUpdate(BaseModel):
    """Only the fields a member may edit. Review/verification fields are rejected outright."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=80)
    bio: str | None = Field(default=None, max_length=1000)
    logo_url: str | None = Field(default=None, max_length=500, pattern=URL_PATTERN)
    website: str | None = Field(default=None, max_length=300, pattern=URL_PATTERN)
    contact_email: EmailStr | None = None
    university: str | None = Field(default=None, max_length=100)
    kind: OrganiserKind | None = None


class MemberAdd(BaseModel):
    """Owner adds a teammate by email."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    role: MemberRole = "editor"
