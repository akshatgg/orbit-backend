"""Baseline — the whole Orbit schema in one migration.

Orbit already runs this schema on Supabase. Rather than hand-porting 22 SQL
files (and risking a silent difference), this revision:

  1. installs pgcrypto and an `auth` compatibility shim (`auth.uid()` reads the
     request's actor from `app.current_user_id`, which the FastAPI session sets);
  2. creates every table, index and constraint from the SQLAlchemy models;
  3. executes Orbit's Postgres functions and triggers verbatim, extracted from
     the Supabase migrations by scripts/extract_sql.py.

Deliberately absent: RLS policies, `anon`/`authenticated`/`service_role`
grants, Supabase Storage buckets and the `auth.users` trigger. Authorization is
enforced in FastAPI (app/core/deps.py) and the API connects as one trusted
role — `is_service()` answers true for it, so the ported guards never block a
legitimate admin write.

After this revision exists, every schema change is a normal Alembic revision:
    alembic revision --autogenerate -m "..."

Revision ID: 0001
Revises:
Create Date: 2026-10-28
"""

from pathlib import Path
from typing import Sequence, Union

from alembic import op

import app.models  # noqa: F401 — registers every model on Base.metadata
from app.db_base import Base

revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

AUTH_SHIM = """
create extension if not exists pgcrypto;

create schema if not exists auth;

-- A stand-in for Supabase's auth.users. Nothing signs in through it: it exists
-- so `references auth.users (id)`-style joins and any tooling that expects the
-- schema keep working. Real identities live in public.users.
create table if not exists auth.users (
  id                 uuid primary key,
  email              text,
  raw_user_meta_data jsonb not null default '{}'::jsonb,
  created_at         timestamptz not null default now()
);

-- The acting user for this request, set by FastAPI with set_config(..., true).
create or replace function auth.uid()
returns uuid language sql stable as $$
  select nullif(current_setting('app.current_user_id', true), '')::uuid
$$;

create or replace function auth.role()
returns text language sql stable as $$
  select coalesce(current_setting('app.role', true), 'authenticated')
$$;
"""

# The generated `events.search` column calls this at CREATE TABLE time, so the
# function has to exist before the models are created.
SEARCH_TEXT = """
create or replace function public.event_search_text(
  title text, summary text, description text, tags text[]
)
returns text language sql immutable parallel safe as $$
  select coalesce(title, '') || ' ' || coalesce(summary, '') || ' ' ||
         coalesce(description, '') || ' ' ||
         coalesce(array_to_string(tags, ' '), '');
$$;
"""


def upgrade() -> None:
    bind = op.get_bind()

    for statement in (AUTH_SHIM, SEARCH_TEXT):
        op.execute(statement)

    # Every table, index and constraint — generated from the models, so the
    # baseline and the ORM can never disagree.
    Base.metadata.create_all(bind=bind)

    # Orbit's functions and triggers, verbatim (41 functions, 13 triggers).
    op.execute((SQL_DIR / "orbit_functions.sql").read_text())


def downgrade() -> None:
    # Destructive by definition: this revision owns the whole schema. It only
    # ever runs on a database this baseline created.
    bind = op.get_bind()
    names = (
        "accept_organiser_invite add_organiser_member add_organiser_owner admin_signups_daily "
        "admin_stats can_manage_event cancel_registration check_in create_organiser_invite "
        "decide_registration discovery_counts event_counts event_search_text guard_event_write "
        "guard_organiser_insert guard_organiser_update guard_profile_update has_room is_admin "
        "is_org_member is_org_owner is_service is_super_admin log_admin_action mark_registration_paid "
        "organiser_follower_count organiser_members_list organiser_slug_available payment_hold_ok "
        "peek_organiser_invite promote_waitlist register_for_event remove_organiser_member "
        "save_push_subscription set_updated_at set_user_role super_admin_email sync_event_price "
        "taken_spots ticket_availability undo_check_in"
    ).split()
    for name in names:
        op.execute(f"drop function if exists public.{name} cascade")
    Base.metadata.drop_all(bind=bind)
    op.execute("drop schema if exists auth cascade")
