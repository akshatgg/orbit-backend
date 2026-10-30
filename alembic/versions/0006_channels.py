"""Communities with channels: chat, event messages, attachments, links, the library.

  * `groups`: `kind` gains 'channel'; `parent_id` (self-FK, RESTRICT, set exactly for
    channels), `post_policy`, `is_announcements` (one per community), `auto_join`,
    `last_autopost_at`. A channel may be public, approval or private, so
    `groups_circle_is_private` now only binds circles.
  * `conversations`: `kind` gains 'channel', the same shape as a circle's.
  * `messages`: `kind` (text, media, event, system) and `event_id`, one event message per
    event per conversation. The body may be empty when deleted or for media and event
    messages.
  * `message_attachments` and `message_links`: what a thread's Media, Docs and Links tabs
    read, each with a keyset index.
  * pg_trgm GIN indexes on lower(name), lower(url) and lower(title) for tab search. They
    live here and not on the models, because 0001 builds tables from the models before
    this extension exists. When the server doesn't ship pg_trgm they're skipped and search
    still works, by scanning one conversation's rows.
  * `notifications.kind` gains 'event_digest'.

Rollout: `ADD COLUMN ... DEFAULT <constant>` is metadata-only since Postgres 11, the new
tables are empty, and `groups` is small. `messages_body_check` is added NOT VALID and
validated separately, so it doesn't block writes while it checks old rows. Old rows all
take the defaults, so every CHECK holds.

Downgrade refuses while channels exist (`scripts/move_to_community.py --undo` first).
It drops the attachment and link rows, and media messages that had no text. pg_trgm stays
installed: it's harmless, and something else may have started using it.

As in 0002-0005, 0001 builds tables from the current models, so on a database created
from scratch the columns, tables and constraints already exist; each step checks first.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TRIGRAM_INDEXES = {
    "message_attachments_name_trgm_idx": "message_attachments using gin (lower(name) gin_trgm_ops)",
    "message_links_url_trgm_idx": "message_links using gin (lower(url) gin_trgm_ops)",
    "message_links_title_trgm_idx": "message_links using gin (lower(title) gin_trgm_ops)",
}


def _exists(sql: str, **params: str) -> bool:
    return op.get_bind().execute(sa.text(sql), params).first() is not None


def _has_column(table: str, column: str) -> bool:
    return _exists(
        "select 1 from information_schema.columns"
        " where table_schema = 'public' and table_name = :t and column_name = :c",
        t=table,
        c=column,
    )


def _has_table(table: str) -> bool:
    return _exists(
        "select 1 from information_schema.tables"
        " where table_schema = 'public' and table_name = :t",
        t=table,
    )


def _has_constraint(name: str) -> bool:
    return _exists("select 1 from pg_constraint where conname = :n", n=name)


def _has_index(name: str) -> bool:
    return _exists("select 1 from pg_indexes where schemaname = 'public' and indexname = :n", n=name)


def _add_check(name: str, table: str, condition: str) -> None:
    if not _has_constraint(name):
        op.create_check_constraint(name, table, condition)


def _replace_check(name: str, table: str, condition: str) -> None:
    """Swap a CHECK for a new definition, the same on either side of a fresh build."""
    op.execute(f"alter table public.{table} drop constraint if exists {name}")
    op.create_check_constraint(name, table, condition)


def _add_index(name: str, table: str, columns: list, **kwargs) -> None:
    if not _has_index(name):
        op.create_index(name, table, columns, **kwargs)


def upgrade() -> None:
    # ── groups ──────────────────────────────────────────────────────────────
    _replace_check("groups_kind_check", "groups", "kind in ('circle','community','channel')")
    _replace_check("groups_circle_is_private", "groups", "kind <> 'circle' or visibility = 'private'")
    if not _has_column("groups", "parent_id"):
        op.add_column("groups", sa.Column("parent_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            "groups_parent_id_fkey", "groups", "groups", ["parent_id"], ["id"], ondelete="RESTRICT"
        )
    if not _has_column("groups", "post_policy"):
        op.add_column(
            "groups",
            sa.Column("post_policy", sa.Text(), nullable=False, server_default=sa.text("'everyone'")),
        )
    if not _has_column("groups", "is_announcements"):
        op.add_column(
            "groups",
            sa.Column("is_announcements", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
    if not _has_column("groups", "auto_join"):
        op.add_column(
            "groups",
            sa.Column("auto_join", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
    if not _has_column("groups", "last_autopost_at"):
        op.add_column("groups", sa.Column("last_autopost_at", sa.DateTime(timezone=True), nullable=True))
    _add_check("groups_channel_parent_check", "groups", "(kind = 'channel') = (parent_id is not null)")
    _add_check("groups_post_policy_check", "groups", "post_policy in ('everyone','moderators')")
    _add_index("groups_parent_idx", "groups", ["parent_id"], postgresql_where=sa.text("parent_id is not null"))
    _add_index(
        "groups_one_announcements_idx",
        "groups",
        ["parent_id"],
        unique=True,
        postgresql_where=sa.text("is_announcements"),
    )

    # ── conversations ───────────────────────────────────────────────────────
    _replace_check(
        "conversations_shape_check",
        "conversations",
        "(kind in ('circle','channel') and group_id is not null and user_a is null and user_b is null)"
        " or (kind = 'direct' and group_id is null and user_a is not null"
        " and user_b is not null and user_a < user_b)",
    )

    # ── messages ────────────────────────────────────────────────────────────
    if not _has_column("messages", "kind"):
        op.add_column(
            "messages", sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'text'"))
        )
    if not _has_column("messages", "event_id"):
        op.add_column("messages", sa.Column("event_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            "messages_event_id_fkey", "messages", "events", ["event_id"], ["id"], ondelete="SET NULL"
        )
    _add_check("messages_kind_check", "messages", "kind in ('text','media','event','system')")
    _add_index(
        "messages_one_event_idx",
        "messages",
        ["conversation_id", "event_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'event'"),
    )
    # The Links tab's event rows, and what `ON DELETE SET NULL` reads when an event goes.
    _add_index(
        "messages_events_idx",
        "messages",
        ["conversation_id", sa.text("created_at desc"), sa.text("id desc")],
        postgresql_where=sa.text("kind = 'event' and deleted_at is null"),
    )
    _add_index("messages_event_idx", "messages", ["event_id"], postgresql_where=sa.text("event_id is not null"))
    op.execute("alter table public.messages drop constraint if exists messages_body_check")
    op.execute(
        "alter table public.messages add constraint messages_body_check check ("
        "char_length(body) <= 2000 and (deleted_at is not null or kind in ('media','event')"
        " or char_length(body) >= 1)) not valid"
    )
    op.execute("alter table public.messages validate constraint messages_body_check")

    # ── attachments and links ───────────────────────────────────────────────
    if not _has_table("message_attachments"):
        op.create_table(
            "message_attachments",
            sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column(
                "message_id", sa.Uuid(), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "conversation_id",
                sa.Uuid(),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("public_id", sa.Text(), nullable=False),
            sa.Column("name", sa.Text(), nullable=True),
            sa.Column("mime", sa.Text(), nullable=True),
            sa.Column("bytes", sa.BigInteger(), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column(
                "created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.CheckConstraint("kind in ('image','doc')", name="message_attachments_kind_check"),
        )
    _add_index(
        "message_attachments_library_idx",
        "message_attachments",
        ["conversation_id", "kind", sa.text("created_at desc"), sa.text("id desc")],
    )
    _add_index("message_attachments_message_idx", "message_attachments", ["message_id"])
    if not _has_table("message_links"):
        op.create_table(
            "message_links",
            sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column(
                "message_id", sa.Uuid(), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column(
                "conversation_id",
                sa.Uuid(),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("domain", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("image_url", sa.Text(), nullable=True),
            sa.Column("fetched_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
            sa.Column(
                "created_at", postgresql.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.UniqueConstraint("message_id", "url", name="message_links_message_url_key"),
        )
    _add_index(
        "message_links_library_idx",
        "message_links",
        ["conversation_id", sa.text("created_at desc"), sa.text("id desc")],
    )
    if _exists("select 1 from pg_available_extensions where name = 'pg_trgm'"):
        op.execute("create extension if not exists pg_trgm")
        for name, target in TRIGRAM_INDEXES.items():
            op.execute(f"create index if not exists {name} on public.{target}")

    # ── notifications ───────────────────────────────────────────────────────
    _replace_check(
        "notifications_kind_check",
        "notifications",
        "kind in ('circle_invite','join_request','request_approved','event_shared',"
        "'announcement','event_digest')",
    )


def downgrade() -> None:
    if _exists("select 1 from public.groups where kind = 'channel'"):
        raise RuntimeError(
            "Channels still exist. Run scripts/move_to_community.py --undo and delete any other"
            " channels before downgrading."
        )
    op.execute("delete from public.notifications where kind = 'event_digest'")
    _replace_check(
        "notifications_kind_check",
        "notifications",
        "kind in ('circle_invite','join_request','request_approved','event_shared','announcement')",
    )

    for name in TRIGRAM_INDEXES:
        op.execute(f"drop index if exists public.{name}")
    op.drop_table("message_links")
    op.drop_table("message_attachments")

    # A message with no text only made sense with what it carried.
    op.execute(
        "delete from public.messages where deleted_at is null and char_length(body) = 0"
    )
    op.drop_constraint("messages_body_check", "messages", type_="check")
    op.create_check_constraint(
        "messages_body_check",
        "messages",
        "char_length(body) <= 2000 and (deleted_at is not null or char_length(body) >= 1)",
    )
    op.drop_index("messages_event_idx", table_name="messages")
    op.drop_index("messages_events_idx", table_name="messages")
    op.drop_index("messages_one_event_idx", table_name="messages")
    op.drop_constraint("messages_kind_check", "messages", type_="check")
    op.drop_constraint("messages_event_id_fkey", "messages", type_="foreignkey")
    op.drop_column("messages", "event_id")
    op.drop_column("messages", "kind")

    op.execute("delete from public.conversations where kind = 'channel'")
    _replace_check(
        "conversations_shape_check",
        "conversations",
        "(kind = 'circle' and group_id is not null and user_a is null and user_b is null)"
        " or (kind = 'direct' and group_id is null and user_a is not null"
        " and user_b is not null and user_a < user_b)",
    )

    op.drop_index("groups_one_announcements_idx", table_name="groups")
    op.drop_index("groups_parent_idx", table_name="groups")
    op.drop_constraint("groups_post_policy_check", "groups", type_="check")
    op.drop_constraint("groups_channel_parent_check", "groups", type_="check")
    op.drop_column("groups", "last_autopost_at")
    op.drop_column("groups", "auto_join")
    op.drop_column("groups", "is_announcements")
    op.drop_column("groups", "post_policy")
    op.drop_constraint("groups_parent_id_fkey", "groups", type_="foreignkey")
    op.drop_column("groups", "parent_id")
    _replace_check("groups_circle_is_private", "groups", "(kind = 'community') or visibility = 'private'")
    _replace_check("groups_kind_check", "groups", "kind in ('circle','community')")
