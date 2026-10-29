"""Circle chat and direct messages.

* A **circle** conversation belongs to one Circle (`group_id`). Who may read it
  is decided live from `group_members`, never copied here, so leaving or being
  removed drops access at once.
* A **direct** conversation is between `user_a` and `user_b` (stored in uuid
  order, so a pair has exactly one row).

`conversation_members` holds per-person read state (`last_read_at`) and when
they were last pushed about it (`last_push_at`, for the DM push throttle). It is
not an access list for circle chats.
"""

import uuid
from datetime import datetime

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
            "(kind = 'circle' and group_id is not null and user_a is null and user_b is null)"
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
    """A deleted message keeps its row (so the thread reads "Message deleted") but not its text."""

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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(body) <= 2000 and (deleted_at is not null or char_length(body) >= 1)",
            name="messages_body_check",
        ),
        Index("messages_conversation_idx", "conversation_id", text("created_at desc"), text("id desc")),
        Index("messages_sender_idx", "sender_id", "created_at"),
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
