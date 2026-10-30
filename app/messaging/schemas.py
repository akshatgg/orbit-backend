from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_ATTACHMENTS = 10


class DirectStart(BaseModel):
    user_id: str


class AttachmentIn(BaseModel):
    """A file the browser already uploaded to Cloudinary (see `POST /uploads/sign`)."""

    kind: Literal["image", "doc"]
    url: str = Field(max_length=1000)
    public_id: str = Field(max_length=500)
    name: Optional[str] = Field(default=None, max_length=255)
    mime: str = Field(max_length=100)
    bytes: int = Field(ge=1)
    width: Optional[int] = Field(default=None, ge=1, le=100_000)
    height: Optional[int] = Field(default=None, ge=1, le=100_000)


class MessageCreate(BaseModel):
    body: str = Field(default="", max_length=4000)
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)

    @field_validator("body")
    @classmethod
    def _length(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 2000:
            raise ValueError("A message is 1 to 2000 characters")
        return value

    @model_validator(mode="after")
    def _something(self) -> "MessageCreate":
        if not self.body and not self.attachments:
            raise ValueError("A message is 1 to 2000 characters")
        return self


class MessageUpdate(BaseModel):
    """New text for a message. Only a message that carries something else may end up empty."""

    body: str = Field(max_length=4000)

    @field_validator("body")
    @classmethod
    def _length(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 2000:
            raise ValueError("A message is 1 to 2000 characters")
        return value


class BlockCreate(BaseModel):
    user_id: str
