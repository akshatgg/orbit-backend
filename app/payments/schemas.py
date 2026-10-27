"""Request bodies for the payments surface."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ConnectIn(BaseModel):
    """Which organiser's Stripe account to work on."""

    model_config = ConfigDict(extra="forbid")

    organiser_id: UUID
