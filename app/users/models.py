"""Identity, profiles and per-user marks.

`users` is Orbit's own identity table — the replacement for Supabase's
`auth.users`. Everything else that used to reference `auth.users(id)` now
references `public.users(id)`, so a `pg_dump` from the Supabase project maps
one-to-one.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_sub: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EmailToken(Base):
    """Magic-link / email-verification / password-reset tokens (single use)."""

    __tablename__ = "email_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind in ('magic_link','verify_email','reset_password','oauth_state')",
            name="email_tokens_kind_check",
        ),
        Index("email_tokens_user_idx", "user_id"),
    )


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    email: Mapped[str | None] = mapped_column(Text)
    full_name: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    university: Mapped[str | None] = mapped_column(Text)
    department: Mapped[str | None] = mapped_column(Text)
    course: Mapped[str | None] = mapped_column(Text)
    year_of_study: Mapped[str | None] = mapped_column(Text)
    career_interests: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    personal_interests: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    preferred_categories: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    preferred_formats: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    goals: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    )
    user_type: Mapped[str | None] = mapped_column(Text)
    location_preference: Mapped[str | None] = mapped_column(Text)
    availability: Mapped[dict | None] = mapped_column(JSONB)
    is_verified_student: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'user'"))
    graduation_year: Mapped[int | None] = mapped_column(Integer)
    company: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    job_title: Mapped[str | None] = mapped_column(Text)
    host_role: Mapped[str | None] = mapped_column(Text)
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "preferred_formats <@ array['in_person','online','hybrid']::text[]",
            name="profiles_preferred_formats_check",
        ),
        CheckConstraint("role in ('user','admin','super_admin')", name="profiles_role_check"),
        CheckConstraint(
            "goals <@ array['career','network','social','host']::text[]",
            name="profiles_goals_check",
        ),
        CheckConstraint(
            "user_type is null or user_type in ('student','graduate','professional','other')",
            name="profiles_user_type_check",
        ),
        CheckConstraint(
            "graduation_year is null or graduation_year between 1950 and 2100",
            name="profiles_graduation_year_check",
        ),
        Index("profiles_email_idx", text("lower(email)")),
    )


class UserEvent(Base):
    """A user's personal mark on an event: saved, dismissed, registered elsewhere, attended."""

    __tablename__ = "user_events"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind in ('saved','dismissed','external_registered','attended')",
            name="user_events_kind_check",
        ),
        Index("user_events_event_idx", "event_id"),
    )


class Follow(Base):
    __tablename__ = "follows"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    organiser_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organisers.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("follows_organiser_idx", "organiser_id"),)


class EventInteraction(Base):
    """Anonymous-ok analytics: view, apply_click, share, calendar_add."""

    __tablename__ = "event_interactions"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind in ('view','apply_click','share','calendar_add')",
            name="event_interactions_kind_check",
        ),
        Index("event_interactions_event_idx", "event_id", "kind"),
    )
