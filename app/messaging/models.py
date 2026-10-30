"""Circle and channel chat, and direct messages.

* A **circle** or **channel** conversation belongs to one group (`group_id`). Who may
  read it is decided live from `group_members`, never copied here, so leaving or being
  removed drops access at once.
* A **direct** conversation is between `user_a` and `user_b` (stored in uuid
  order, so a pair has exactly one row).

`conversation_members` holds per-person read state (`last_read_at`) and when
they were last pushed about it (`last_push_at`, for the DM push throttle). It is
not an access list for group chats.

`message_attachments` and `message_links` are what a thread's Media, Docs and Links
tabs read. A deleted message loses its rows in both.

The pg_trgm indexes for tab search are in migration 0006, not here: 0001 builds these
tables before that extension exists.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="CASCADE"), unique=True
    )
    user_a: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    user_b: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "(kind in ('circle','channel') and group_id is not null"
            " and user_a is null and user_b is null)"
            " or (kind = 'direct' and group_id is null and user_a is not null"
            " and user_b is not null and user_a < user_b)",
            name="conversations_shape_check",
        ),
        UniqueConstraint("user_a", "user_b", name="conversations_direct_pair_key"),
        Index("conversations_user_b_idx", "user_b"),
    )


class ConversationMember(Base):
    __tablename__ = "conversation_members"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_push_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("conversation_members_user_idx", "user_id"),)


class Message(Base):
    """A deleted message keeps its row (so the thread reads "Message deleted") but not its text.

    `updated_at` is set whenever an existing message changes (edited or deleted), so an
    open thread's poll can pick those changes up (`changed_since`); it is null until then.

    `kind`: 'text'; 'media' (has attachments, the text is optional); 'event' (a card for
    `event_id`, the text is an optional note); 'system' (a line from Orbit, no sender).
    """

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'text'"))
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(body) <= 2000 and (deleted_at is not null or kind in ('media','event')"
            " or char_length(body) >= 1)",
            name="messages_body_check",
        ),
        CheckConstraint("kind in ('text','media','event','system')", name="messages_kind_check"),
        Index("messages_conversation_idx", "conversation_id", text("created_at desc"), text("id desc")),
        Index("messages_sender_idx", "sender_id", "created_at"),
        Index(
            "messages_changed_idx",
            "conversation_id",
            "updated_at",
            postgresql_where=text("updated_at is not null"),
        ),
        Index(
            "messages_one_event_idx",
            "conversation_id",
            "event_id",
            unique=True,
            postgresql_where=text("kind = 'event'"),
        ),
        Index(
            "messages_events_idx",
            "conversation_id",
            text("created_at desc"),
            text("id desc"),
            postgresql_where=text("kind = 'event' and deleted_at is null"),
        ),
        Index("messages_event_idx", "event_id", postgresql_where=text("event_id is not null")),
    )


class MessageAttachment(Base):
    """An image or a PDF on a message, already uploaded to Cloudinary (`orbit/chat/{sender}`)."""

    __tablename__ = "message_attachments"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    public_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text)
    mime: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(BigInteger)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    # Its place in the message, as sent: one message's attachments share a created_at.
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("kind in ('image','doc')", name="message_attachments_kind_check"),
        Index(
            "message_attachments_library_idx",
            "conversation_id",
            "kind",
            text("created_at desc"),
            text("id desc"),
        ),
        Index("message_attachments_message_idx", "message_id"),
    )


class MessageLink(Base):
    """A URL in a message's text. `fetched_at` is set once its preview was looked for,
    whether or not one was found; own-site links never need fetching."""

    __tablename__ = "message_links"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("message_id", "url", name="message_links_message_url_key"),
        Index(
            "message_links_library_idx",
            "conversation_id",
            text("created_at desc"),
            text("id desc"),
        ),
    )


class MessageEdit(Base):
    """One row per edit, so edits count toward the sending rate limit. Holds no text."""

    __tablename__ = "message_edits"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    editor_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    edited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("message_edits_editor_idx", "editor_id", "edited_at"),
        Index("message_edits_message_idx", "message_id"),
    )


class UserBlock(Base):
    """`blocker_id` blocked `blocked_id`: no direct messages either way, and their
    circle-chat messages are hidden from the blocker."""

    __tablename__ = "user_blocks"

    blocker_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    blocked_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("blocker_id <> blocked_id", name="user_blocks_not_self_check"),
        Index("user_blocks_blocked_idx", "blocked_id"),
    )
