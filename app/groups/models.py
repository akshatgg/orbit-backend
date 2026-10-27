"""Circles and Communities.

One table pair serves both features because they differ only in policy:

* **Circle** — `kind='circle'`, always `visibility='private'`. You invite people
  you already know; nobody can find it by browsing.
* **Community** — `kind='community'`, `visibility` is 'public' (join instantly)
  or 'approval' (an admin admits you). Admins create them; the directory lists
  them.

Sharing an event is a row in `group_events` keyed by (group, event), so the same
event cannot be posted twice to one group — the PDF's "should not need to be
shared individually with every friend".
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str | None] = mapped_column(Text, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("categories.id", ondelete="SET NULL")
    )
    university: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'private'")
    )
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
        CheckConstraint("kind in ('circle','community')", name="groups_kind_check"),
        CheckConstraint(
            "visibility in ('private','public','approval')", name="groups_visibility_check"
        ),
        CheckConstraint(
            "char_length(name) between 2 and 60", name="groups_name_check"
        ),
        CheckConstraint(
            "(kind = 'community') or visibility = 'private'", name="groups_circle_is_private"
        ),
        CheckConstraint(
            "slug is null or slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="groups_slug_check"
        ),
        Index("groups_kind_visibility_idx", "kind", "visibility"),
        Index("groups_category_idx", "category_id"),
    )


class GroupMember(Base):
    """Only accepted members have a row. Pending invites live in `group_invites`."""

    __tablename__ = "group_members"

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'member'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("role in ('admin','member')", name="group_members_role_check"),
        Index("group_members_user_idx", "user_id"),
    )


class GroupInvite(Base):
    """A targeted Circle invite (`invited_user_id`) or a shareable Community link (`code`)."""

    __tablename__ = "group_invites"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    invited_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    code: Mapped[str | None] = mapped_column(
        Text,
        unique=True,
        default=lambda: uuid.uuid4().hex[:20],
    )
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.utcnow() + timedelta(days=14),
        server_default=text("now() + interval '14 days'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','accepted','declined','revoked')",
            name="group_invites_status_check",
        ),
        CheckConstraint(
            "invited_user_id is not null or code is not null", name="group_invites_target_check"
        ),
        Index("group_invites_group_idx", "group_id"),
        Index("group_invites_invited_idx", "invited_user_id"),
    )


class GroupEvent(Base):
    """An event shared into a group. The primary key stops double-sharing."""

    __tablename__ = "group_events"

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    shared_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("group_events_event_idx", "event_id"),)


class GroupEventInterest(Base):
    """"Count me in" — one row per member per shared event."""

    __tablename__ = "group_event_interests"

    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'interested'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('interested','going')", name="group_event_interests_status_check"
        ),
    )


class GroupAnnouncement(Base):
    """A short note from an admin; optionally pinned to one event."""

    __tablename__ = "group_announcements"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("char_length(body) between 1 and 1000", name="group_announcements_body_check"),
        Index("group_announcements_group_idx", "group_id", text("created_at desc")),
        UniqueConstraint("id", "group_id", name="group_announcements_id_group_key"),
    )
