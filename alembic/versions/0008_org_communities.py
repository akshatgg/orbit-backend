"""Organisations run their own communities.

  * `groups.organiser_id` (nullable, FK `organisers` ON DELETE SET NULL): the approved
    organisation whose team runs a community. Set only on communities
    (`groups_organiser_community_check`); channels take it from their community, and circles
    never have one. Partial index `groups_organiser_idx` for "this organisation's
    communities" (the per-organisation limit, the Studio entry, the daily auto-post).

Rollout: one nullable `ADD COLUMN` (metadata-only) and a CHECK that every existing row passes
(all null), on the small `groups` table. Downgrade drops them: the communities stay, run by
their moderators and Orbit admins as before.

As in 0002-0007, 0001 builds tables from the current models, so on a database created from
scratch the column, index and constraint already exist; each step checks first.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _exists(sql: str, **params: str) -> bool:
    return op.get_bind().execute(sa.text(sql), params).first() is not None


def upgrade() -> None:
    has_column = _exists(
        "select 1 from information_schema.columns"
        " where table_schema = 'public' and table_name = 'groups' and column_name = 'organiser_id'"
    )
    if not has_column:
        op.add_column("groups", sa.Column("organiser_id", sa.Uuid(), nullable=True))
    if not _exists("select 1 from pg_constraint where conname = 'groups_organiser_id_fkey'"):
        op.create_foreign_key(
            "groups_organiser_id_fkey", "groups", "organisers", ["organiser_id"], ["id"], ondelete="SET NULL"
        )
    if not _exists("select 1 from pg_constraint where conname = 'groups_organiser_community_check'"):
        op.create_check_constraint(
            "groups_organiser_community_check", "groups", "organiser_id is null or kind = 'community'"
        )
    if not _exists("select 1 from pg_indexes where schemaname = 'public' and indexname = 'groups_organiser_idx'"):
        op.create_index(
            "groups_organiser_idx",
            "groups",
            ["organiser_id"],
            postgresql_where=sa.text("organiser_id is not null"),
        )


def downgrade() -> None:
    op.execute("drop index if exists public.groups_organiser_idx")
    op.execute("alter table public.groups drop constraint if exists groups_organiser_community_check")
    op.execute("alter table public.groups drop constraint if exists groups_organiser_id_fkey")
    op.execute("alter table public.groups drop column if exists organiser_id")
