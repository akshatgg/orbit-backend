"""Editing announcements and messages.

  * `group_announcements.edited_at` — when an announcement was last edited.
  * `notifications.announcement_id` — the announcement a notification is about
    (ON DELETE CASCADE: deleting the announcement removes its notifications).
  * `messages.edited_at` — when a message was last edited.
  * `messages.updated_at` + `messages_changed_idx` — set whenever an existing
    message is edited or deleted, so an open thread's poll can fetch what changed
    (`changed_since`). Partial index: only changed messages are in it.
  * `message_edits` — one row per edit (no text), so edits count toward the
    sending rate limit.

Rollout: every new column is nullable with no default (metadata-only). The new
indexes are on `messages` and `notifications`, both new in 0002 and small; the
`messages_changed_idx` index is empty when built.

As in 0002/0003, 0001 builds tables from the current models, so on a database
created from scratch these objects already exist; each step checks first.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _exists(sql: str, **params: str) -> bool:
    return op.get_bind().execute(sa.text(sql), params).first() is not None


def _has_column(table: str, column: str) -> bool:
    return _exists(
        "select 1 from information_schema.columns"
        " where table_schema = 'public' and table_name = :t and column_name = :c",
        t=table,
        c=column,
    )


def _has_index(name: str) -> bool:
    return _exists(
        "select 1 from pg_indexes where schemaname = 'public' and indexname = :n", n=name
    )


def upgrade() -> None:
    if not _has_column("group_announcements", "edited_at"):
        op.add_column(
            "group_announcements", sa.Column("edited_at", sa.DateTime(timezone=True))
        )

    if not _has_column("notifications", "announcement_id"):
        op.add_column(
            "notifications",
            sa.Column(
                "announcement_id",
                sa.Uuid(),
                sa.ForeignKey("group_announcements.id", ondelete="CASCADE"),
            ),
        )
    if not _has_index("notifications_announcement_idx"):
        op.create_index(
            "notifications_announcement_idx",
            "notifications",
            ["announcement_id"],
            postgresql_where=sa.text("announcement_id is not null"),
        )

    if not _has_column("messages", "edited_at"):
        op.add_column("messages", sa.Column("edited_at", sa.DateTime(timezone=True)))
    if not _has_column("messages", "updated_at"):
        op.add_column("messages", sa.Column("updated_at", sa.DateTime(timezone=True)))
    if not _has_index("messages_changed_idx"):
        op.create_index(
            "messages_changed_idx",
            "messages",
            ["conversation_id", "updated_at"],
            postgresql_where=sa.text("updated_at is not null"),
        )

    if not _exists("select 1 where to_regclass('public.message_edits') is not null"):
        op.create_table(
            "message_edits",
            sa.Column(
                "id", sa.BigInteger(), sa.Identity(always=True), primary_key=True
            ),
            sa.Column(
                "message_id",
                sa.Uuid(),
                sa.ForeignKey("messages.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "editor_id",
                sa.Uuid(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "edited_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index("message_edits_editor_idx", "message_edits", ["editor_id", "edited_at"])
        op.create_index("message_edits_message_idx", "message_edits", ["message_id"])


def downgrade() -> None:
    op.drop_table("message_edits")
    op.drop_index("messages_changed_idx", table_name="messages")
    op.drop_column("messages", "updated_at")
    op.drop_column("messages", "edited_at")
    op.drop_index("notifications_announcement_idx", table_name="notifications")
    op.drop_column("notifications", "announcement_id")
    op.drop_column("group_announcements", "edited_at")
