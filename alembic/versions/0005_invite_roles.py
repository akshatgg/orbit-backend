"""Invites carry the role the invitee joins with.

  * `group_invites.role text not null default 'member'` with
    `group_invites_role_check` (`role in ('admin','member')`) — an invite from a
    group admin can make the invitee an admin (a community moderator) when they
    accept. Join requests and links are always 'member'.

Rollout: one `ADD COLUMN ... DEFAULT <constant>` (metadata-only since Postgres
11) and one CHECK on the small `group_invites` table. Every existing row takes
the default, so the CHECK holds.

As in 0002-0004, 0001 builds tables from the current models, so on a database
created from scratch the column and constraint already exist; each step checks
first.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _exists(sql: str, **params: str) -> bool:
    return op.get_bind().execute(sa.text(sql), params).first() is not None


def upgrade() -> None:
    has_column = _exists(
        "select 1 from information_schema.columns"
        " where table_schema = 'public' and table_name = 'group_invites' and column_name = 'role'"
    )
    if not has_column:
        op.add_column(
            "group_invites",
            sa.Column("role", sa.Text(), nullable=False, server_default=sa.text("'member'")),
        )
    if not _exists("select 1 from pg_constraint where conname = 'group_invites_role_check'"):
        op.create_check_constraint(
            "group_invites_role_check", "group_invites", "role in ('admin','member')"
        )


def downgrade() -> None:
    op.drop_constraint("group_invites_role_check", "group_invites", type_="check")
    op.drop_column("group_invites", "role")
