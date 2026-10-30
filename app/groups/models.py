"""Circles, Communities and Channels.

One table pair serves all three because they differ only in policy:

* **Circle** — `kind='circle'`, always `visibility='private'`. You invite people
  you already know; nobody can find it by browsing.
* **Community** — `kind='community'`, `visibility` is 'public' (join instantly)
  or 'approval' (an admin admits you). Admins create them; the directory lists
  them.
* **Channel** — `kind='channel'`, a group inside a community (`parent_id`).
  'public' (one tap), 'approval' or 'private' (hidden, by invitation). It holds a
  chat; `post_policy` says who may post in it.

`auto_join` puts everyone in a community or channel without anyone choosing it, so
it never counts as knowing someone: it doesn't allow direct messages, and its member
list is for those who manage it (see app/groups/service.py).

Sharing an event is a row in `group_events` keyed by (group, event), so the same
event cannot be posted twice to one group — the PDF's "should not need to be
shared individually with every friend".
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.dialects.postgresql import ARRAY
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
    # The primary category (the first of category_ids), kept for older clients.
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("categories.id", ondelete="SET NULL")
    )
    # Every category a community's feed picks up, primary first.
    category_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid), nullable=False, server_default=text("'{}'::uuid[]")
    )
    university: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'private'")
    )
    # Communities only: whether any member may share an event, or only its admins.
    allow_member_shares: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # Channels only: the community it sits in. A community with channels can't be deleted.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="RESTRICT")
    )
    # Channels only: who may post, 'everyone' or 'moderators' (those who can manage it).
    post_policy: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'everyone'")
    )
    is_announcements: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    auto_join: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Channels only: the daily auto-post has looked at events created up to this moment.
    last_autopost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        CheckConstraint("kind in ('circle','community','channel')", name="groups_kind_check"),
        CheckConstraint(
            "visibility in ('private','public','approval')", name="groups_visibility_check"
        ),
        CheckConstraint(
            "char_length(name) between 2 and 60", name="groups_name_check"
        ),
        CheckConstraint(
            "kind <> 'circle' or visibility = 'private'", name="groups_circle_is_private"
        ),
        CheckConstraint(
            "(kind = 'channel') = (parent_id is not null)", name="groups_channel_parent_check"
        ),
        CheckConstraint(
            "post_policy in ('everyone','moderators')", name="groups_post_policy_check"
        ),
        CheckConstraint(
            "slug is null or slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="groups_slug_check"
        ),
        Index("groups_kind_visibility_idx", "kind", "visibility"),
        Index("groups_category_idx", "category_id"),
        Index("groups_kind_category_idx", "kind", "category_id"),
        Index("groups_category_ids_idx", "category_ids", postgresql_using="gin"),
        Index("groups_parent_idx", "parent_id", postgresql_where=text("parent_id is not null")),
        Index(
            "groups_one_announcements_idx",
            "parent_id",
            unique=True,
            postgresql_where=text("is_announcements"),
        ),
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
    """One way into a group, by `kind`:

    * `invite`  — a member invited `invited_user_id`; only that person answers it.
    * `request` — `invited_user_id` asked to join an approval community; only a
      group admin answers it.
    * `link`    — a shareable `code`; anyone with it joins until it expires.

    `role` is what accepting it makes you: an invite from a group admin can be
    'admin' (a community moderator); requests and links are always 'member'.

    A person holds at most one pending invite or request per group (partial
    unique index).
    """

    __tablename__ = "group_invites"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'invite'"))
    invited_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    # Only links carry a code; set explicitly when one is minted.
    code: Mapped[str | None] = mapped_column(Text, unique=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'member'"))
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
        CheckConstraint("kind in ('invite','request','link')", name="group_invites_kind_check"),
        CheckConstraint("role in ('admin','member')", name="group_invites_role_check"),
        Index("group_invites_group_idx", "group_id"),
        Index("group_invites_invited_idx", "invited_user_id"),
        Index(
            "group_invites_one_pending_idx",
            "group_id",
            "invited_user_id",
            unique=True,
            postgresql_where=text("status = 'pending' and invited_user_id is not null"),
        ),
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
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("char_length(body) between 1 and 1000", name="group_announcements_body_check"),
        Index("group_announcements_group_idx", "group_id", text("created_at desc")),
        UniqueConstraint("id", "group_id", name="group_announcements_id_group_key"),
    )
