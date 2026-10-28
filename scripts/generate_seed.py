#!/usr/bin/env python3
"""Turn a live Orbit database into one seed file for a first deploy.

    PROD_DATABASE_URL=postgresql://... python scripts/generate_seed.py > seeds/prod.sql

The output is plain SQL for `psql`, and it is safe on a *fresh* database:

    psql "$DATABASE_URL" -f seeds/prod.sql

Why COPY rather than INSERT: 3,800 events with descriptions are ~1 MB as COPY
and several times that as INSERT statements.

Order matters. Tables are emitted in dependency order (computed from the
target's foreign keys), so the file can run as the normal database owner — no
`session_replication_role`, which Cloud SQL's owner is not allowed to set.

Users come from `auth.users` (Supabase) and land in `public.users`, keeping ids
and emails so profiles and registrations keep their person. Password hashes are
not carried over: migrated users sign in with a magic link or Google and set a
password.

The file starts with a guard: if the target already has events, it aborts
instead of duplicating the catalogue.
"""

from __future__ import annotations

import io
import os
import sys
from collections import defaultdict, deque

import psycopg2
from psycopg2 import sql


def connect(url: str):
    conn = psycopg2.connect(url, connect_timeout=15, sslmode=os.environ.get("PGSSLMODE", "prefer"))
    with conn.cursor() as cur:
        cur.execute("set session characteristics as transaction read only")
    conn.commit()
    return conn


def tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "select tablename from pg_tables"
            " where schemaname='public' and tablename not like 'alembic%' order by 1"
        )
        return [r[0] for r in cur.fetchall()]


def dependencies(conn) -> dict[str, set[str]]:
    """table -> public tables it references (recursive parents only)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select child.relname, parent.relname
              from pg_constraint c
              join pg_class child  on child.oid  = c.conrelid
              join pg_class parent on parent.oid = c.confrelid
              join pg_namespace n  on n.oid = child.relnamespace
             where c.contype = 'f' and n.nspname = 'public'
               and parent.relnamespace = n.oid
            """
        )
        deps: dict[str, set[str]] = defaultdict(set)
        for child, parent in cur.fetchall():
            if child != parent:
                deps[child].add(parent)
        return deps


def ordered(conn, names: list[str]) -> list[str]:
    deps = dependencies(conn)
    pending = {t: {d for d in deps.get(t, set()) if d in names} for t in names}
    out: list[str] = []
    ready = deque(sorted(t for t in names if not pending[t]))
    while ready:
        t = ready.popleft()
        out.append(t)
        for other in list(pending):
            if t in pending[other]:
                pending[other].discard(t)
                if not pending[other] and other not in out:
                    ready.append(other)
    if len(out) != len(names):
        print("warning: foreign-key cycle detected; appending the rest", file=sys.stderr)
        out += [t for t in names if t not in out]
    return out


def columns(conn, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            select column_name from information_schema.columns
             where table_schema='public' and table_name=%s and is_generated <> 'ALWAYS'
             order by ordinal_position
            """,
            (table,),
        )
        return [r[0] for r in cur.fetchall()]


def copy_block(conn, table: str) -> tuple[str, int]:
    cols = columns(conn, table)
    buf = io.StringIO()
    with conn.cursor() as cur:
        statement = sql.SQL("copy (select {} from public.{}) to stdout with (format csv)").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in cols), sql.Identifier(table)
        )
        cur.copy_expert(statement, buf)
        cur.execute(sql.SQL("select count(*) from public.{}").format(sql.Identifier(table)))
        count = cur.fetchone()[0]
    body = buf.getvalue()
    header = (
        f"copy public.{table} ({', '.join('\"'+c+'\"' for c in cols)}) "
        f"from stdin with (format csv);\n"
    )
    return header + (body if body.endswith("\n") else body + "\n") + "\\.\n", count


def sequence_statements(conn) -> list[str]:
    out: list[str] = []
    for table in tables(conn):
        with conn.cursor() as cur:
            cur.execute(
                """
                select a.attname, pg_get_serial_sequence('public.' || quote_ident(%s), a.attname)
                  from pg_attribute a
                 where a.attrelid = ('public.' || quote_ident(%s))::regclass
                   and a.attnum > 0 and not a.attisdropped
                """,
                (table, table),
            )
            for column, sequence in cur.fetchall():
                if sequence:
                    out.append(
                        f"select setval('{sequence}', coalesce((select max(\"{column}\") from public.\"{table}\"), 0) + 1, false);"
                    )
    return out


def main() -> None:
    url = os.environ.get("PROD_DATABASE_URL") or (sys.argv[1] if len(sys.argv) > 1 else None)
    if not url:
        sys.exit("Set PROD_DATABASE_URL (read-only is enough).")

    conn = connect(url)
    names = ordered(conn, tables(conn))

    print("-- Orbit seed: generated from production by scripts/generate_seed.py")
    print("-- Apply once, to a database freshly migrated with `alembic upgrade head`:")
    print("--   psql \"$DATABASE_URL\" -f seeds/prod.sql")
    print("\\set ON_ERROR_STOP on")
    print("begin;")
    print(
        """do $$
begin
  if exists (select 1 from public.events) then
    raise exception 'This database already has events. Refusing to seed twice.';
  end if;
end $$;"""
    )

    # identities first
    with conn.cursor() as cur:
        cur.execute(
            """
            select u.id, lower(u.email), u.created_at, u.email_confirmed_at, u.last_sign_in_at,
                   (select i.provider_id from auth.identities i
                     where i.user_id = u.id and i.provider = 'google' limit 1) as google_sub
              from auth.users u where u.email is not null
            """
        )
        users = cur.fetchall()
    print("\n-- users (auth.users -> public.users, Google identities -> google_sub)")
    for uid, email, created, confirmed, signed_in, google_sub in users:

        def q(v):
            return "null" if v is None else "'" + str(v).replace("'", "''") + "'"

        print(
            "insert into public.users (id, email, created_at, updated_at, email_verified_at, google_sub) values "
            f"({q(uid)}, {q(email)}, {q(created)}, {q(signed_in or created)}, {q(confirmed)}, {q(google_sub)}) "
            "on conflict (id) do nothing;"
        )

    providers = sum(1 for u in users if u[5])
    print(f"-- {providers} of {len(users)} users sign in with Google (google_sub preserved)")

    total = 0
    for table in names:
        if table == "users":
            continue
        block, count = copy_block(conn, table)
        if count == 0:
            continue
        total += count
        print(f"\n-- {table} ({count})")
        print(block, end="")

    print("\n-- sequences")
    for statement in sequence_statements(conn):
        print(statement)

    print("\ncommit;")
    print(f"\n-- done: {total} rows across {len(names) - 1} tables, {len(users)} users", file=sys.stderr)
    conn.close()


if __name__ == "__main__":
    main()
