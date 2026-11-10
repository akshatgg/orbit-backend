"""Admin switches: `app_settings` (key → JSON value).

First key: `past_event_cleanup` — when `enabled`, the nightly cron deletes every event that
ended before today (London) and releases its Cloudinary cover.

Rollout: one new, empty table. Downgrade drops it (the cleanup then reads as "off").

As in 0002-0008, 0001 builds tables from the current models, so on a database created from
scratch the table already exists; the step checks first.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    exists = op.get_bind().execute(
        sa.text("select 1 from information_schema.tables where table_schema = 'public' and table_name = 'app_settings'")
    ).first()
    if exists:
        return
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.execute("drop table if exists public.app_settings")
