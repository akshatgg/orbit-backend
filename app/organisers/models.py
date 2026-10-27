import uuid
from datetime import datetime, timedelta

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db_base import Base


class Organiser(Base):
    __tablename__ = "organisers"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'society'")
    )
    logo_url: Mapped[str | None] = mapped_column(Text)
    bio: Mapped[str | None] = mapped_column(Text)
    website: Mapped[str | None] = mapped_column(Text)
    contact_email: Mapped[str | None] = mapped_column(Text)
    university: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stripe_account_id: Mapped[str | None] = mapped_column(Text)
    stripe_charges_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
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
        CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="organisers_slug_check"
        ),
        CheckConstraint(
            "kind in ('society','university','employer','community','other')",
            name="organisers_kind_check",
        ),
        CheckConstraint(
            "status in ('pending','approved','rejected','suspended')",
            name="organisers_status_check",
        ),
        Index("organisers_status_idx", "status"),
    )


class OrganiserMember(Base):
    __tablename__ = "organiser_members"

    organiser_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organisers.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'editor'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "role in ('owner','editor')", name="organiser_members_role_check"
        ),
        Index("organiser_members_user_idx", "user_id"),
    )


class OrganiserInvite(Base):
    """A shareable join link for an approved organiser. Codes are single-purpose and cheap."""

    __tablename__ = "organiser_invites"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    organiser_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organisers.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
        default=lambda: uuid.uuid4().hex[:20],
        server_default=text("substr(replace(gen_random_uuid()::text, '-', ''), 1, 20)"),
    )
    role: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'editor'")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.utcnow() + timedelta(days=14),
        server_default=text("now() + interval '14 days'"),
    )
    max_uses: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("25")
    )
    uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    revoked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "role in ('owner','editor')", name="organiser_invites_role_check"
        ),
        CheckConstraint("max_uses > 0", name="organiser_invites_max_uses_check"),
        CheckConstraint("uses >= 0", name="organiser_invites_uses_check"),
        Index("organiser_invites_org_idx", "organiser_id"),
    )
