from datetime import datetime

from sqlalchemy import Boolean, DateTime, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class StripeEvent(Base):
    """Webhook idempotency. A replayed `evt_…` is ignored, never applied twice."""

    __tablename__ = "stripe_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    account: Mapped[str | None] = mapped_column(Text)
    livemode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
