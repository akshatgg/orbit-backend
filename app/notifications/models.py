import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, Text, UniqueConstraint, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class PushSubscription(Base):
    """One row per browser/device that turned push on."""

    __tablename__ = "push_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("endpoint ~* '^https://'", name="push_subscriptions_endpoint_check"),
        Index("push_subscriptions_user_idx", "user_id"),
    )


class NotificationPreference(Base):
    """No row means defaults; the API returns the defaults rather than 404."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    push_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    email_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    remind_before_hours: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("24")
    )
    deadline_reminders: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "remind_before_hours in (2, 24)", name="notification_preferences_hours_check"
        ),
    )


class NotificationLog(Base):
    """Dedupe ledger: one (user, event, kind, channel) is sent once."""

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("events.id", ondelete="CASCADE"), nullable=False
    )
    registration_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("registrations.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("channel in ('push','email')", name="notification_log_channel_check"),
        UniqueConstraint(
            "user_id", "event_id", "kind", "channel", name="notification_log_dedupe_key"
        ),
        Index("notification_log_event_idx", "event_id"),
    )


class Notification(Base):
    """The in-app inbox: one row per person per thing worth telling them.

    Unlike `notification_log` (a push/email dedupe ledger tied to an event),
    these are what the bell shows. `invite_id` lets an invite be answered inline.
    """

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(Text)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("groups.id", ondelete="CASCADE")
    )
    invite_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("group_invites.id", ondelete="SET NULL")
    )
    # Deleting the announcement deletes its notifications.
    announcement_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("group_announcements.id", ondelete="CASCADE")
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "kind in ('circle_invite','join_request','request_approved','event_shared',"
            "'announcement','event_digest')",
            name="notifications_kind_check",
        ),
        Index("notifications_user_idx", "user_id", text("created_at desc"), text("id desc")),
        Index(
            "notifications_unread_idx",
            "user_id",
            text("created_at desc"),
            postgresql_where=text("read_at is null"),
        ),
        Index("notifications_group_idx", "group_id"),
        Index("notifications_invite_idx", "invite_id", postgresql_where=text("invite_id is not null")),
        Index(
            "notifications_announcement_idx",
            "announcement_id",
            postgresql_where=text("announcement_id is not null"),
        ),
    )
