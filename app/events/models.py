import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    organiser_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("organisers.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'Europe/London'")
    )
    format: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'in_person'"))
    venue_name: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text, server_default=text("'London'"))
    country: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("categories.id", ondelete="SET NULL")
    )
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    tags_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    university: Mapped[str | None] = mapped_column(Text)
    registration_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'tickets'")
    )
    external_url: Mapped[str | None] = mapped_column(Text)
    external_label: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'Apply now'")
    )
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    capacity: Mapped[int | None] = mapped_column(Integer)
    waitlist_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    registration_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    price_min: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    price_max: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'GBP'"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    is_featured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    needs_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Product/import provenance. `source` is 'orbit' for hand-made events and
    # 'source:<uuid>' for each imported feed, matching Orbit's sourceKey().
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'orbit'"))
    source_url: Mapped[str | None] = mapped_column(Text)
    source_event_id: Mapped[str | None] = mapped_column(Text)
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="SET NULL")
    )
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    series_key: Mapped[str | None] = mapped_column(Text)
    series_label: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Full-text index maintained by Postgres; the app never writes it.
    search: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', public.event_search_text(title, summary, description, tags))",
            persisted=True,
        ),
    )

    __table_args__ = (
        CheckConstraint("char_length(title) between 3 and 140", name="events_title_check"),
        CheckConstraint("summary is null or char_length(summary) <= 200", name="events_summary_check"),
        CheckConstraint("format in ('in_person','online','hybrid')", name="events_format_check"),
        CheckConstraint(
            "registration_mode in ('tickets','external','none')", name="events_registration_mode_check"
        ),
        CheckConstraint(
            "external_url is null or external_url ~* '^https?://'", name="events_external_url_check"
        ),
        CheckConstraint("capacity is null or capacity > 0", name="events_capacity_check"),
        CheckConstraint(
            "status in ('draft','published','cancelled','expired','removed')",
            name="events_status_check",
        ),
        CheckConstraint("ends_at is null or ends_at >= starts_at", name="events_ends_at_check"),
        CheckConstraint(
            "registration_mode <> 'external' or external_url is not null", name="events_external_needs_url"
        ),
        CheckConstraint(
            "latitude is null or latitude between -90 and 90", name="events_latitude_check"
        ),
        CheckConstraint(
            "longitude is null or longitude between -180 and 180", name="events_longitude_check"
        ),
        CheckConstraint("country is null or country ~ '^[A-Z]{2}$'", name="events_country_check"),
        UniqueConstraint("source", "source_event_id", name="events_source_source_event_id_key"),
        Index("events_starts_at_idx", "starts_at"),
        Index("events_status_idx", "status"),
        Index("events_organiser_idx", "organiser_id"),
        Index("events_category_idx", "category_id"),
        Index("events_tags_idx", "tags", postgresql_using="gin"),
        Index("events_search_idx", "search", postgresql_using="gin"),
        Index("events_review_idx", "needs_review", postgresql_where=text("needs_review")),
        Index(
            "events_duplicate_of_idx",
            "duplicate_of",
            postgresql_where=text("duplicate_of is not null"),
        ),
        Index(
            "events_series_key_idx",
            "series_key",
            "starts_at",
            postgresql_where=text("series_key is not null"),
        ),
        Index(
            "events_country_city_idx",
            "country",
            "city",
            postgresql_where=text("status = 'published'"),
        ),
    )


class EventPrivate(Base):
    """Details only registered guests may read: the online link, guest instructions."""

    __tablename__ = "event_private"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    online_url: Mapped[str | None] = mapped_column(Text)
    guest_info: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
