from typing import Literal, Optional

from pydantic import BaseModel, Field


class GroupCreate(BaseModel):
    kind: Literal["circle", "community"]
    name: str = Field(min_length=2, max_length=60)
    description: Optional[str] = Field(default=None, max_length=500)
    visibility: Literal["private", "public", "approval"] = "private"
    category_id: Optional[str] = None
    university: Optional[str] = None
    cover_url: Optional[str] = None


class GroupUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=60)
    description: Optional[str] = Field(default=None, max_length=500)
    visibility: Optional[Literal["private", "public", "approval"]] = None
    category_id: Optional[str] = None
    university: Optional[str] = None
    cover_url: Optional[str] = None


class InviteCreate(BaseModel):
    email: Optional[str] = None
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
