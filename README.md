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
  notifications/       preferences, push subscriptions, web push + email delivery, in-app inbox
  groups/              Circles, Communities and their Channels
  messaging/           circle and channel chat, direct messages, blocks, attachments, links
  admin/               overview, events, users, organisers, categories, CSV
  sources/             feeds, sync engine, review queue, duplicates
  cron/                nightly refresh + daily reminders + the channels' daily auto-post
  uploads/             Cloudinary upload signature, image release
  public/              landing, sitemap data, OG data
  connectors/          feed readers (iCal implemented; platform ones pending)
alembic/versions/0001_baseline.py   the whole schema in one revision
alembic/versions/0002_*.py          invite kinds, inbox, messaging (hand-written)
alembic/versions/0003_*.py          communities link to several categories
alembic/versions/0006_*.py          channels, chat attachments and links
alembic/versions/0008_*.py          communities an organisation runs
alembic/sql/orbit_functions.sql     generated from Orbit's Supabase migrations
scripts/               extract_sql.py, dump_schema.sh, schema_diff.py,
                       seed_communities.py (the launch communities; idempotent),
                       move_to_community.py (makes them channels of ORBIT; never automatic)
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
| Storage buckets | Cloudinary (signed direct uploads) |
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
| Reminders (event + deadline; each person picks 2h or 24h) | `/api/cron/reminders` | `0 8 * * *` |

`POST /api/cron/refresh-sources` is the admin "refresh now" button (needs an
admin token), not a cron target.

## Importing production data

`seeds/prod.sql` is a **ready-to-apply export of the production database**
(generated 2026-10-28 by `scripts/generate_seed.py`). On a freshly migrated
database:

```bash
TARGET_DATABASE_URL=postgresql://orbit:orbit@localhost:5433/orbit \
  .venv/bin/alembic upgrade head

psql "$TARGET_DATABASE_URL" -f seeds/prod.sql
```

The file guards itself: if the target already has events it aborts with
"Refusing to seed twice" instead of duplicating the catalogue. It applies in
under a second.

### What the export contains

| Source | Exported |
|---|---|
| `public.*` | every non-empty table — 21 tables, 5,143 rows (3,879 events, 520 audit rows, 474 source details, 106 interactions, 81 runs, 12 profiles, 8 sources, 1 registration, 15 categories, 21 duplicate dismissals …) |
| `auth.users` | 12 users → `public.users`, ids and emails kept so profiles and registrations keep their person |
| `auth.identities` | the 8 Google sign-ins → `users.google_sub`, so Google login re-links to the same account |
| `storage.objects` | 1 file, `seeds/storage/logo.jpg` + `manifest.json` (unreferenced test upload) |
| Cloudinary | nothing to move — all 822 event covers and every organiser logo are already Cloudinary URLs |
| `auth.sessions`, `refresh_tokens`, `flow_state` | **intentionally not exported** — invalid on the new server, and carrying live credentials across is a liability |

Two things it deliberately does not carry over:

- **Password hashes.** Supabase stores bcrypt in `auth.users.encrypted_password`;
  this schema stores hex. Migrated users sign in with a magic link (or Google)
  and set a password afterwards.
- **Storage files beyond the manifest.** See above: there are none in use.

### Regenerating after production changes

```bash
PROD_DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres \
  .venv/bin/python scripts/generate_seed.py > seeds/prod.sql
```

`scripts/migrate_prod_data.py` does the same job directly between two live
databases (`--truncate`, `--verify-only`) without an intermediate file, if you
prefer that for the final cutover.

`session_replication_role` is not needed: the generator emits tables in foreign
key order, so the file runs as the ordinary database owner — including on Cloud
SQL, whose owner cannot set that parameter.

## Status

- Schema: verified identical to Orbit's Supabase database (0 differences).
- Read endpoints: exercised against real data.
- **Mutation endpoints: not yet covered by tests** — `tests/` is the next step.
- **Frontend not connected yet**: `orbit` still talks to Supabase directly.
- Connectors: iCal only; Luma/Meetup/Eventbrite/Fatsoma return a clear
  "not ported" error.
