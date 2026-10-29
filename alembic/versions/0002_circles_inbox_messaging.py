"""Circles & Communities fixes, the in-app inbox, and messaging.

  * `groups.allow_member_shares` — whether any community member may share an
    event, or only its admins (default: anyone).
  * `group_invites.kind` — `invite` (answered by the invitee), `request`
    (answered by a group admin) or `link` (a shareable code). Existing rows are
    classified from their shape: no invitee means a link; invitee = inviter
    means a join request.
  * `group_invites_one_pending_idx` — one pending invite/request per person per
    group. Older duplicates are revoked first so the index can be built.
  * `notifications` — the bell's inbox.
  * `conversations`, `conversation_members`, `messages`, `user_blocks` — circle
    chat and direct messages. A direct conversation stores its pair in uuid
    order (`user_a < user_b`) under a unique constraint, so get-or-create is
    one upsert.
  * Indexes for the hot paths: `groups (kind, category_id)` for the directory
    and `events (category_id, starts_at, id) where published` for community
    feeds (built CONCURRENTLY, outside the migration transaction, so writes to
    `events` are never blocked). Everything else indexed here is a new table.

Rollout: the only DDL on existing tables is two `ADD COLUMN ... DEFAULT
<constant>` (metadata-only since Postgres 11), one CHECK and one unique index
on the small `group_invites` table, one index on the small `groups` table, and
the concurrent `events` index.

0001 builds its tables from the *current* models, so on a database created
from scratch everything below already exists by the time this revision runs.
Each step therefore checks the catalogue first and skips what is present; on
an existing database (created by an earlier 0001) every step runs.

Downgrade removes all of it. The invite classification and the revoked
duplicates are data and are not restored.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _exists(sql: str, **params: str) -> bool:
    return op.get_bind().execute(sa.text(sql), params).first() is not None


def _has_table(name: str) -> bool:
    return _exists("select 1 where to_regclass(:n) is not null", n=f"public.{name}")


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


def _has_constraint(name: str) -> bool:
    return _exists("select 1 from pg_constraint where conname = :n", n=name)


def _uuid_pk() -> sa.Column:
    return sa.Column(
        "id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


def upgrade() -> None:
    # ── groups ──────────────────────────────────────────────────────────────
    if not _has_column("groups", "allow_member_shares"):
        op.add_column(
            "groups",
            sa.Column(
                "allow_member_shares", sa.Boolean(), nullable=False, server_default=sa.text("true")
            ),
        )

    if not _has_index("groups_kind_category_idx"):
        op.create_index("groups_kind_category_idx", "groups", ["kind", "category_id"])

    # ── group_invites: invite vs request vs link ───────────────────────────
    if not _has_column("group_invites", "kind"):
        op.add_column(
            "group_invites",
            sa.Column("kind", sa.Text(), nullable=False, server_default=sa.text("'invite'")),
        )
        op.execute("update public.group_invites set kind = 'link' where invited_user_id is null")
        op.execute(
            "update public.group_invites set kind = 'request'"
            " where invited_user_id is not null and invited_by = invited_user_id"
        )
    if not _has_constraint("group_invites_kind_check"):
        op.create_check_constraint(
            "group_invites_kind_check", "group_invites", "kind in ('invite','request','link')"
        )
    if not _has_index("group_invites_one_pending_idx"):
        # Keep the newest pending row per (group, person); revoke the rest.
        op.execute(
            """
            update public.group_invites set status = 'revoked'
             where id in (
               select id from (
                 select id, row_number() over (
                          partition by group_id, invited_user_id order by created_at desc
                        ) as n
                   from public.group_invites
                  where status = 'pending' and invited_user_id is not null
               ) ranked
               where n > 1
             )
            """
        )
        op.create_index(
            "group_invites_one_pending_idx",
            "group_invites",
            ["group_id", "invited_user_id"],
            unique=True,
            postgresql_where=sa.text("status = 'pending' and invited_user_id is not null"),
        )

    # ── notifications (in-app inbox) ───────────────────────────────────────
    if not _has_table("notifications"):
        op.create_table(
            "notifications",
            _uuid_pk(),
            sa.Column(
                "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("body", sa.Text()),
            sa.Column("link", sa.Text()),
            sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("group_id", sa.Uuid(), sa.ForeignKey("groups.id", ondelete="CASCADE")),
            sa.Column(
                "invite_id", sa.Uuid(), sa.ForeignKey("group_invites.id", ondelete="SET NULL")
            ),
            sa.Column("read_at", sa.DateTime(timezone=True)),
            _created_at(),
            sa.CheckConstraint(
                "kind in ('circle_invite','join_request','request_approved',"
                "'event_shared','announcement')",
                name="notifications_kind_check",
            ),
        )
        op.create_index(
            "notifications_user_idx",
            "notifications",
            ["user_id", sa.text("created_at desc"), sa.text("id desc")],
        )
        op.create_index(
            "notifications_unread_idx",
            "notifications",
            ["user_id", sa.text("created_at desc")],
            postgresql_where=sa.text("read_at is null"),
        )
        op.create_index("notifications_group_idx", "notifications", ["group_id"])
        op.create_index(
            "notifications_invite_idx",
            "notifications",
            ["invite_id"],
            postgresql_where=sa.text("invite_id is not null"),
        )

    # ── messaging ──────────────────────────────────────────────────────────
    if not _has_table("conversations"):
        op.create_table(
            "conversations",
            _uuid_pk(),
            sa.Column("kind", sa.Text(), nullable=False),
            sa.Column("group_id", sa.Uuid(), sa.ForeignKey("groups.id", ondelete="CASCADE")),
            sa.Column("user_a", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE")),
            sa.Column("user_b", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE")),
            sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("last_message_at", sa.DateTime(timezone=True)),
            _created_at(),
            sa.UniqueConstraint("group_id", name="conversations_group_id_key"),
            sa.UniqueConstraint("user_a", "user_b", name="conversations_direct_pair_key"),
            sa.CheckConstraint(
                "(kind = 'circle' and group_id is not null and user_a is null and user_b is null)"
                " or (kind = 'direct' and group_id is null and user_a is not null"
                " and user_b is not null and user_a < user_b)",
                name="conversations_shape_check",
            ),
        )
        op.create_index("conversations_user_b_idx", "conversations", ["user_b"])

    if not _has_table("conversation_members"):
        op.create_table(
            "conversation_members",
            sa.Column(
                "conversation_id",
                sa.Uuid(),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "user_id",
                sa.Uuid(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("last_read_at", sa.DateTime(timezone=True)),
            sa.Column("last_push_at", sa.DateTime(timezone=True)),
            _created_at(),
        )
        op.create_index("conversation_members_user_idx", "conversation_members", ["user_id"])

    if not _has_table("messages"):
        op.create_table(
            "messages",
            _uuid_pk(),
            sa.Column(
                "conversation_id",
                sa.Uuid(),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("sender_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True)),
            _created_at(),
            sa.CheckConstraint(
                "char_length(body) <= 2000 and (deleted_at is not null or char_length(body) >= 1)",
                name="messages_body_check",
            ),
        )
        op.create_index(
            "messages_conversation_idx",
            "messages",
            ["conversation_id", sa.text("created_at desc"), sa.text("id desc")],
        )
        op.create_index("messages_sender_idx", "messages", ["sender_id", "created_at"])

    if not _has_table("user_blocks"):
        op.create_table(
            "user_blocks",
            sa.Column(
                "blocker_id",
                sa.Uuid(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "blocked_id",
                sa.Uuid(),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            _created_at(),
            sa.CheckConstraint("blocker_id <> blocked_id", name="user_blocks_not_self_check"),
        )
        op.create_index("user_blocks_blocked_idx", "user_blocks", ["blocked_id"])

    # ── community feed: last, outside the transaction ──────────────────────
    # CONCURRENTLY never blocks writes to `events`; it cannot run in a
    # transaction, so everything above is committed first.
    if not _has_index("events_feed_idx"):
        with op.get_context().autocommit_block():
            op.create_index(
                "events_feed_idx",
                "events",
                ["category_id", "starts_at", "id"],
                postgresql_where=sa.text("status = 'published' and duplicate_of is null"),
                postgresql_concurrently=True,
                if_not_exists=True,
            )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "events_feed_idx", table_name="events", postgresql_concurrently=True, if_exists=True
        )
    op.drop_table("user_blocks")
    op.drop_table("messages")
    op.drop_table("conversation_members")
    op.drop_table("conversations")
    op.drop_table("notifications")
    op.drop_index("group_invites_one_pending_idx", table_name="group_invites")
    op.drop_constraint("group_invites_kind_check", "group_invites", type_="check")
    op.drop_column("group_invites", "kind")
    op.drop_index("groups_kind_category_idx", table_name="groups")
    op.drop_column("groups", "allow_member_shares")
