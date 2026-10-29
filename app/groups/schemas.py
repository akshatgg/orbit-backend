from typing import Literal, Optional

from pydantic import BaseModel, Field


class GroupCreate(BaseModel):
    kind: Literal["circle", "community"]
    name: str = Field(min_length=2, max_length=60)
    description: Optional[str] = Field(default=None, max_length=500)
    # Circles are always private. Communities default to public; "private" means
    # approval-based for a community.
    visibility: Optional[Literal["private", "public", "approval"]] = None
    # Category ids or slugs, primary first. `category_id` alone (older clients) sets just one.
    category_ids: Optional[list[str]] = Field(default=None, max_length=10)
    category_id: Optional[str] = None
    university: Optional[str] = Field(default=None, max_length=120)
    cover_url: Optional[str] = Field(default=None, max_length=1000)
    # Communities only; generated from the name when blank.
    slug: Optional[str] = Field(default=None, max_length=60)
    allow_member_shares: Optional[bool] = None


class GroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=60)
    description: Optional[str] = Field(default=None, max_length=500)
    visibility: Optional[Literal["private", "public", "approval"]] = None
    # Replaces the categories (primary first); `category_id` alone swaps the primary.
    category_ids: Optional[list[str]] = Field(default=None, max_length=10)
    category_id: Optional[str] = None
    university: Optional[str] = Field(default=None, max_length=120)
    cover_url: Optional[str] = Field(default=None, max_length=1000)
    slug: Optional[str] = Field(default=None, max_length=60)
    allow_member_shares: Optional[bool] = None


class MemberRole(BaseModel):
    role: Literal["admin", "member"]


class InviteCreate(BaseModel):
    email: Optional[str] = Field(default=None, max_length=320)
    user_id: Optional[str] = None


class InviteRespond(BaseModel):
    accept: bool


class ShareEvent(BaseModel):
    event_id: str
    note: Optional[str] = Field(default=None, max_length=300)


class InterestBody(BaseModel):
    status: Literal["interested", "going"] = "interested"


class AnnouncementCreate(BaseModel):
    body: str = Field(min_length=1, max_length=1000)
    event_id: Optional[str] = None
