import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class EventSource(Base):
    """A calendar / discovery feed Orbit imports from."""

    __tablename__ = "event_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ical'"))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    default_organiser_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("organisers.id", ondelete="SET NULL")
    )
    default_category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("categories.id", ondelete="SET NULL")
    )
    default_tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    attribution: Mapped[str | None] = mapped_column(Text)
    permission_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    permission_note: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    auto_category: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(trim(name)) between 2 and 80", name="event_sources_name_check"
        ),
        CheckConstraint(
            "kind in ('ical','luma','luma-discover','meetup','meetup-search','eventbrite','fatsoma')",
            name="event_sources_kind_check",
        ),
        CheckConstraint(
            "url ~* '^https?://' and char_length(url) <= 2000", name="event_sources_url_check"
        ),
        CheckConstraint(
            "char_length(attribution) <= 200", name="event_sources_attribution_check"
        ),
        CheckConstraint(
            "char_length(permission_note) <= 1000", name="event_sources_permission_note_check"
        ),
        CheckConstraint(
            "last_status in ('ok','error')", name="event_sources_last_status_check"
        ),
        CheckConstraint(
            "not enabled or (permission_confirmed"
            " and char_length(trim(coalesce(permission_note,''))) > 0)",
            name="event_sources_enabled_needs_permission",
        ),
        UniqueConstraint("kind", "url", name="event_sources_kind_url_key"),
        Index("event_sources_enabled_idx", "enabled"),
    )


class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("event_sources.id", ondelete="CASCADE"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(
        "trigger", Text, nullable=False, server_default=text("'cron'")
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    found: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    inserted: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    unchanged: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    flagged_missing: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    enriched: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    errors: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    __table_args__ = (
        CheckConstraint("\"trigger\" in ('cron','manual')", name="source_runs_trigger_check"),
        CheckConstraint("status in ('ok','error')", name="source_runs_status_check"),
        Index("source_runs_source_idx", "source_id", text("started_at desc")),
        Index("source_runs_started_idx", text("started_at desc")),
    )


class EventDuplicateDismissal(Base):
    __tablename__ = "event_duplicate_dismissals"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    other_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    dismissed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("event_id < other_event_id", name="event_duplicate_dismissals_order"),
        Index("event_duplicate_dismissals_other_idx", "other_event_id"),
    )


class SourceEventDetails(Base):
    """Per-event detail enrichment state (cover, prose, price from the platform page)."""

    __tablename__ = "source_event_details"

    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    last_error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('ok','failed','blocked')", name="source_event_details_status_check"
        ),
        CheckConstraint("attempts >= 0", name="source_event_details_attempts_check"),
        CheckConstraint(
            "char_length(last_error) <= 500", name="source_event_details_last_error_check"
        ),
        Index("source_event_details_status_idx", "status"),
    )
