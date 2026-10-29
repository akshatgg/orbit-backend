"""Communities link to several categories.

  * `groups.category_ids uuid[] not null default '{}'` — every category a
    community's feed picks up, primary first. Backfilled from `category_id`,
    which stays as the primary category (= category_ids[1]) for older clients.
  * `groups_category_ids_idx` (GIN) — the directory's "has this category" filter.

Rollout: one `ADD COLUMN ... DEFAULT <constant>` (metadata-only since Postgres
11) and an index, both on the small `groups` table.

As in 0002, 0001 builds tables from the current models, so on a database
created from scratch the column and index already exist; each step checks the
catalogue first.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _exists(sql: str, **params: str) -> bool:
    return op.get_bind().execute(sa.text(sql), params).first() is not None


def upgrade() -> None:
    has_column = _exists(
        "select 1 from information_schema.columns"
        " where table_schema = 'public' and table_name = 'groups' and column_name = 'category_ids'"
    )
    if not has_column:
        op.add_column(
            "groups",
            sa.Column(
                "category_ids",
                postgresql.ARRAY(sa.Uuid()),
                nullable=False,
                server_default=sa.text("'{}'::uuid[]"),
            ),
        )
        op.execute(
            "update public.groups set category_ids = array[category_id]"
            " where category_id is not null"
        )
    if not _exists(
        "select 1 from pg_indexes where schemaname = 'public' and indexname = 'groups_category_ids_idx'"
    ):
        op.create_index(
            "groups_category_ids_idx", "groups", ["category_ids"], postgresql_using="gin"
        )


def downgrade() -> None:
    op.drop_index("groups_category_ids_idx", table_name="groups")
    op.drop_column("groups", "category_ids")
