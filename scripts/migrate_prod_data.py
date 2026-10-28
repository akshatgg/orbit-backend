#!/usr/bin/env python3
"""Move Orbit's production data into another Postgres (local, or GCP).

Read-only on the source. The target is rebuilt: `--truncate` empties the public
tables first, so running it twice gives the same result.

    PROD_DATABASE_URL=postgresql://... TARGET_DATABASE_URL=postgresql://... \
        python scripts/migrate_prod_data.py --truncate

What it handles, and why:

* **auth.users -> public.users.** Supabase keeps identities in `auth.users`;
  this schema keeps them in `public.users`, which every other table references.
  Password hashes are not carried over (Supabase's `encrypted_password` is a
  bcrypt string, this app stores hex): migrated users sign in with a magic link
  and set a password. Emails and ids are preserved, so profiles/registrations
  keep pointing at the right person.
* **Generated columns.** `events.search` is `GENERATED ALWAYS`; it is excluded
  from the copy and rebuilt by Postgres.
* **Foreign keys.** The target session runs with `session_replication_role =
  replica`, so table order cannot break the load. Orphans are checked afterwards.
* **Identity sequences.** `event_interactions.id` and `notification_log.id` are
  `GENERATED ALWAYS AS IDENTITY`; their sequences are advanced past the copied
  ids or the next insert would collide.
* Tables that exist only in Supabase (`storage.*`, `auth.*` beyond users) are
  not touched.

Exit code 1 when any verification fails.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from dataclasses import dataclass

import psycopg2
from psycopg2 import sql

# Copied first, in this order; everything else follows alphabetically with FK
# checks disabled.
PRIORITY = ["users", "categories", "organisers", "profiles"]

SKIP_TABLES = {"alembic_version", "schema_migrations"}


@dataclass
class Column:
    name: str
    generated: bool


def connect(url: str, *, readonly: bool = False):
    conn = psycopg2.connect(url, connect_timeout=15, sslmode=os.environ.get("PGSSLMODE", "prefer"))
    conn.autocommit = False
    if readonly:
        with conn.cursor() as cur:
            cur.execute("set session characteristics as transaction read only")
        conn.commit()
    return conn


def public_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "select tablename from pg_tables where schemaname='public' and tablename <> all(%s) order by 1",
            (list(SKIP_TABLES),),
        )
        return [r[0] for r in cur.fetchall()]


def columns(conn, table: str, schema: str = "public") -> list[Column]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select column_name, is_generated
              from information_schema.columns
             where table_schema = %s and table_name = %s
             order by ordinal_position
            """,
            (schema, table),
        )
        return [Column(name=r[0], generated=r[1] == "ALWAYS") for r in cur.fetchall()]


def copy_out(conn, schema: str, table: str, cols: list[str]) -> io.StringIO:
    buf = io.StringIO()
    with conn.cursor() as cur:
        statement = sql.SQL("copy (select {} from {}.{}) to stdout with (format csv)").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in cols),
            sql.Identifier(schema),
            sql.Identifier(table),
        )
        cur.copy_expert(statement, buf)
    buf.seek(0)
    return buf


def copy_in(conn, schema: str, table: str, cols: list[str], buf: io.StringIO) -> None:
    with conn.cursor() as cur:
        statement = sql.SQL("copy {}.{} ({}) from stdin with (format csv)").format(
            sql.Identifier(schema),
            sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        )
        cur.copy_expert(statement, buf)


def row_counts(conn, schema: str, tables: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    with conn.cursor() as cur:
        for t in tables:
            cur.execute(sql.SQL("select count(*) from {}.{}").format(sql.Identifier(schema), sql.Identifier(t)))
            out[t] = cur.fetchone()[0]
    return out


def target_sequences(conn) -> list[tuple[str, str, str]]:
    """(table, column, sequence) for identity and serial columns in public."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select c.relname, a.attname, pg_get_serial_sequence('public.' || quote_ident(c.relname), a.attname)
              from pg_class c
              join pg_namespace n on n.oid = c.relnamespace
              join pg_attribute a on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
             where n.nspname = 'public' and c.relkind = 'r'
               and pg_get_serial_sequence('public.' || quote_ident(c.relname), a.attname) is not null
             order by 1, 2
            """
        )
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def verify(source, target, tables: list[str]) -> list[str]:
    failures: list[str] = []

    s_counts = row_counts(source, "public", tables)
    t_counts = row_counts(target, "public", tables)
    print(f"\n{'table':34} {'prod':>8} {'target':>8}  status")
    for t in tables:
        ok = s_counts[t] == t_counts[t]
        print(f"{t:34} {s_counts[t]:>8} {t_counts[t]:>8}  {'ok' if ok else 'MISMATCH'}")
        if not ok:
            failures.append(f"{t}: prod {s_counts[t]} != target {t_counts[t]}")

    # Users: every auth user must have a row and a profile.
    with source.cursor() as cur:
        cur.execute("select count(*) from auth.users")
        prod_users = cur.fetchone()[0]
    with target.cursor() as cur:
        cur.execute("select count(*) from public.users")
        target_users = cur.fetchone()[0]
        cur.execute(
            """
            select count(*) from public.profiles p
             left join public.users u on u.id = p.id
             where u.id is null
            """
        )
        orphan_profiles = cur.fetchone()[0]
    print(f"\nauth users {prod_users:>5} -> public.users {target_users:>5}", "ok" if prod_users == target_users else "MISMATCH")
    if prod_users != target_users:
        failures.append(f"users: {prod_users} -> {target_users}")
    if orphan_profiles:
        failures.append(f"{orphan_profiles} profiles without a user")

    # Foreign keys must all resolve (the load ran with FK checks off).
    with target.cursor() as cur:
        cur.execute(
            """
            select count(*) from public.events e
             where e.organiser_id is not null
               and not exists (select 1 from public.organisers o where o.id = e.organiser_id)
            """
        )
        orphan_events = cur.fetchone()[0]
    print(f"events with a missing organiser: {orphan_events}")
    if orphan_events:
        failures.append(f"{orphan_events} events reference a missing organiser")

    # Sequences must be ahead of the data, or the next insert collides.
    with target.cursor() as cur:
        cur.execute("select coalesce(max(id), 0) from public.event_interactions")
        max_i = cur.fetchone()[0]
        cur.execute("select last_value from public.event_interactions_id_seq")
        next_i = cur.fetchone()[0]
    if max_i and next_i < max_i:
        failures.append(f"event_interactions sequence at {next_i}, data reaches {max_i}")
    print(f"event_interactions sequence: {next_i} (data max {max_i})")

    # A few product-level spot checks.
    with target.cursor() as cur:
        cur.execute("select count(*) from public.events where starts_at > now()")
        upcoming = cur.fetchone()[0]
        cur.execute("select count(*) from public.events where needs_review and duplicate_of is null")
        review = cur.fetchone()[0]
    print(f"upcoming events: {upcoming} | awaiting review: {review}")
    return failures


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.environ.get("PROD_DATABASE_URL"), help="read from here")
    ap.add_argument("--target", default=os.environ.get("TARGET_DATABASE_URL"), help="write here")
    ap.add_argument("--truncate", action="store_true", help="empty the target's public tables first")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    if not args.source or not args.target:
        sys.exit("Set PROD_DATABASE_URL and TARGET_DATABASE_URL (or pass --source/--target).")
    if args.source == args.target:
        sys.exit("Source and target are the same database.")

    source = connect(args.source, readonly=True)
    target = connect(args.target)
    tables = public_tables(source)

    if args.verify_only:
        failures = verify(source, target, tables)
        target.rollback()
        sys.exit(1 if failures else 0)

    ordered = [t for t in PRIORITY if t in tables] + [t for t in tables if t not in PRIORITY]

    try:
        with target.cursor() as cur:
            if args.truncate:
                cur.execute("truncate table " + ", ".join(f'public."{t}"' for t in ordered) + " cascade")
            cur.execute("set local session_replication_role = replica")

        # 1. identities: auth.users -> public.users
        with source.cursor() as cur:
            cur.execute(
                """
                select id, lower(email), created_at,
                       case when email_confirmed_at is not null then email_confirmed_at end,
                       last_sign_in_at
                  from auth.users
                 where email is not null
                """
            )
            users = cur.fetchall()
        with target.cursor() as cur:
            cur.executemany(
                """
                insert into public.users (id, email, created_at, updated_at, email_verified_at)
                values (%s, %s, %s, %s, %s)
                on conflict (id) do nothing
                """,
                [(u[0], u[1], u[2], u[4] or u[2], u[3]) for u in users],
            )
        print(f"users: {len(users)} copied from auth.users")

        # 2. every public table, data only, generated columns excluded
        for table in ordered:
            if table == "users":
                continue
            cols = [c.name for c in columns(source, table) if not c.generated]
            if not cols:
                continue
            buf = copy_out(source, "public", table, cols)
            copy_in(target, "public", table, cols, buf)
            with target.cursor() as cur:
                cur.execute(sql.SQL("select count(*) from public.{}").format(sql.Identifier(table)))
                n = cur.fetchone()[0]
            print(f"{table:34} {n:>8} rows")

        # 3. advance identity sequences past the copied ids
        with target.cursor() as cur:
            cur.execute("set local session_replication_role = origin")
            for table, column, sequence in target_sequences(target):
                cur.execute(
                    sql.SQL("select setval(%s, coalesce((select max({col}) from {tbl}), 0) + 1, false)").format(
                        col=sql.Identifier(column), tbl=sql.Identifier("public", table)
                    ),
                    (sequence,),
                )

        failures = verify(source, target, ordered)
        if failures:
            target.rollback()
            print("\nFAILED:\n  " + "\n  ".join(failures))
            sys.exit(1)
        target.commit()
        print("\nimport committed.")
    except Exception:
        target.rollback()
        raise
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    main()
