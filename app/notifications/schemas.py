"""Request bodies for notification preferences and push subscriptions."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class NotificationPreferencesUpdate(BaseModel):
    push_enabled: bool | None = None
    email_enabled: bool | None = None
    remind_before_hours: Literal[2, 24] | None = None
    deadline_reminders: bool | None = None


class PushKeys(BaseModel):
    p256dh: str = Field(min_length=1, max_length=300)
    auth: str = Field(min_length=1, max_length=100)


class PushSubscriptionBody(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1000)
    keys: PushKeys
    user_agent: str | None = Field(default=None, max_length=300)
    old_endpoint: str | None = Field(default=None, max_length=1000)

    @field_validator("endpoint")
    @classmethod
    def _https(cls, value: str) -> str:
        value = value.strip()
        if not value.lower().startswith("https://"):
            raise ValueError("A push endpoint must start with https://")
        return value


class EndpointBody(BaseModel):
    endpoint: str = Field(min_length=1, max_length=1000)
