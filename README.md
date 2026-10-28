# Orbit Backend

FastAPI + PostgreSQL backend for Orbit. Postgres holds the rules (tickets,
capacity, waitlists, approvals, check-in, sources); this service is the API
surface and the place authorization is decided.

## Quick start

```bash
./dev_start.sh
```

That script:

1. copies `.env.example` → `.env` on first run;
2. creates `.venv` and installs `requirements.txt` if needed;
3. starts Postgres 17 in Docker on **localhost:5433** (5432 is taken on this
   machine, 54322 by the frontend's local Supabase);
4. waits for it, checks the connection, runs `alembic upgrade head`;
5. runs uvicorn with `--reload` on **http://localhost:8090**.

| | |
|---|---|
| API | http://localhost:8090 |
| Docs | http://localhost:8090/docs |
| Health | http://localhost:8090/api/health |
| Database | `postgresql://orbit:orbit@localhost:5433/orbit` |

```bash
./dev_stop.sh             # stop uvicorn + Postgres (data kept)
./dev_stop.sh --volumes   # also delete the database volume
./dev_reset.sh            # wipe the database and rebuild from migrations
SKIP_DOCKER=1 ./dev_start.sh   # use the DATABASE_URL already in .env
```

`.env` is read by the app (pydantic-settings), never `source`d by the scripts —
that way a value containing spaces or `<...>` cannot break the shell.

## Layout

```
app/
  main.py              mounts every router under /api
  core/                config, async session, auth deps, security, pagination
  db_base.py           the one DeclarativeBase
  models.py            imports every model so Base.metadata is complete
  sql/functions.py     typed wrappers for the 41 Postgres functions
  users/               identity, profiles, saved/followed
  events/              public events, search, detail, marks, registrations
  feed/                for-you and this-week
  discovery/           counts, places, interests
  categories/          taxonomy
  organisers/          applications, team, invites, follow
  studio/              organiser dashboard, event editing, guests, check-in
  tickets/             ticket types, questions, registrations (models)
  payments/            Stripe checkout, Connect, webhook
  notifications/       preferences, push subscriptions, email
  groups/              Circles and Communities
  admin/               overview, events, users, organisers, categories, CSV
  sources/             feeds, sync engine, review queue, duplicates
  cron/                nightly refresh + daily reminders
  uploads/             Cloudinary signature, local fallback
  public/              landing, sitemap data, OG data
  connectors/          feed readers (iCal implemented; platform ones pending)
alembic/versions/0001_baseline.py   the whole schema in one revision
alembic/sql/orbit_functions.sql     generated from Orbit's Supabase migrations
scripts/               extract_sql.py, dump_schema.sh, schema_diff.py
```

## Database

The baseline migration is the whole schema: **30 tables, 41 functions, 13
triggers**, ported from Orbit's Supabase migrations. Supabase-specific pieces
are replaced, not dropped by accident:

| Supabase | Here |
|---|---|
| `auth.users` | `public.users` (own identity table) + `auth.users` compatibility shim |
| `auth.uid()` | reads `app.current_user_id`, set per request |
| RLS policies | dependency guards in `app/core/deps.py` |
| `service_role` key | the API connects as one trusted role (`is_service()` → true) |
| Storage buckets | Cloudinary, with local disk fallback |
| `handle_new_user` trigger | signup creates `users` + `profiles` in one transaction |

Every schema change after the baseline is a normal revision:

```bash
.venv/bin/alembic revision --autogenerate -m "add ..."
.venv/bin/alembic upgrade head
```

### Schema audit

```bash
OUT=/tmp/orbit-schema ./scripts/dump_schema.sh
.venv/bin/python scripts/schema_diff.py /tmp/orbit-schema
```

Exit code 0 means the backend's schema matches the source Orbit database
(columns, types, nullability, constraints, indexes, triggers), allowing for the
documented differences above.

## Cron

Both are `GET` and need `Authorization: Bearer $CRON_SECRET`.

| Job | Path | Schedule |
|---|---|---|
| Refresh sources, expire, dedupe | `/api/cron/refresh-sources` | `0 3 * * *` |
| Reminders (event + deadline) | `/api/cron/reminders` | `0 8 * * *` |

`POST /api/cron/refresh-sources` is the admin "refresh now" button (needs an
admin token), not a cron target.

## Importing production data

`scripts/migrate_prod_data.py` moves a live Orbit database into another
Postgres (local, Cloud SQL, Neon, your own cluster). It reads the source in a
read-only session and rebuilds the target.

```bash
# 1. schema first
TARGET_DATABASE_URL=postgresql://orbit:orbit@localhost:5433/orbit \
  .venv/bin/alembic upgrade head

# 2. data
PROD_DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres \
TARGET_DATABASE_URL=postgresql://orbit:orbit@localhost:5433/orbit \
  .venv/bin/python scripts/migrate_prod_data.py --truncate

# 3. verify only (no writes)
... .venv/bin/python scripts/migrate_prod_data.py --verify-only
```

What it does:

- copies **auth.users → public.users** (id, email, verified timestamp), so
  profiles, registrations and marks keep pointing at the same people;
- copies every `public` table with `COPY`, skipping generated columns
  (`events.search` is rebuilt by Postgres);
- runs the load with `session_replication_role = replica`, so table order
  cannot break it, then checks for orphans;
- advances identity sequences (`event_interactions`, `notification_log`) past
  the copied ids;
- compares row counts table by table and exits non-zero on any difference.

Tested against production on 2026-10-28: 3,879 events, 12 users, 22 tables —
every count matched, and a second `--truncate` run produced the same result.

Two things it deliberately does not carry over:

- **Password hashes.** Supabase stores bcrypt in `auth.users.encrypted_password`;
  this schema stores hex. Migrated users sign in with a magic link (or Google,
  which matches the existing row by email) and set a password afterwards.
- **Storage objects.** Orbit's images are Cloudinary URLs; the one file in
  Supabase Storage is not copied.

`session_replication_role` needs a superuser or the database owner. On Cloud SQL
run the import as the instance owner, not the application user.

## Status

- Schema: verified identical to Orbit's Supabase database (0 differences).
- Read endpoints: exercised against real data.
- **Mutation endpoints: not yet covered by tests** — `tests/` is the next step.
- **Frontend not connected yet**: `orbit` still talks to Supabase directly.
- Connectors: iCal only; Luma/Meetup/Eventbrite/Fatsoma return a clear
  "not ported" error.
