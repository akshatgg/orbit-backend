"""Daily summaries in Announcements channels.

`messages.kind` gains 'summary': ORBIT's own message counting the new events per category, with
its rows in `messages.data`. Its `body` is a one-line version of the same thing, for anything
that doesn't know the kind (the channel list's preview, older clients).

Every Announcements channel starts counting from now, so the first summary isn't a backlog.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column() -> bool:
    return (
        op.get_bind()
        .execute(
            sa.text(
                "select 1 from information_schema.columns"
                " where table_schema = 'public' and table_name = 'messages' and column_name = 'data'"
            )
        )
        .first()
        is not None
    )


def upgrade() -> None:
    if not _has_column():
        op.add_column("messages", sa.Column("data", JSONB(), nullable=True))
    op.execute("alter table public.messages drop constraint if exists messages_kind_check")
    op.execute(
        "alter table public.messages add constraint messages_kind_check"
        " check (kind in ('text','media','event','system','summary')) not valid"
    )
    op.execute("alter table public.messages validate constraint messages_kind_check")
    op.execute(
        "update public.groups set last_autopost_at = now()"
        " where is_announcements and last_autopost_at is null"
    )


def downgrade() -> None:
    op.execute("delete from public.messages where kind = 'summary'")
    op.execute("alter table public.messages drop constraint if exists messages_kind_check")
    op.execute(
        "alter table public.messages add constraint messages_kind_check"
        " check (kind in ('text','media','event','system'))"
    )
    op.drop_column("messages", "data")
