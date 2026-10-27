"""Extract Orbit's Postgres functions and triggers into one frozen baseline file.

Run once, from the repo root:

    python scripts/extract_sql.py

Source: ../orbit/supabase/migrations/*.sql (the schema Orbit runs today).
Output: alembic/sql/orbit_functions.sql

What is dropped and why:
  * RLS policies / grants / roles — authorization now lives in FastAPI
    dependencies (app/core/deps.py), so the policies would only add a second,
    conflicting answer.
  * storage.* statements — Supabase Storage is replaced by Cloudinary/GCS.
  * the `on_auth_user_created` trigger — Supabase's auth.users table does not
    exist here; the signup endpoint creates `users` + `profiles` itself.
  * `notify pgrst, …` — that was PostgREST cache invalidation.

`is_service()` is rewritten to trust the API connection (`app.trusted`), because
the backend connects as one role and every route decides authorization before
it reaches the database. Without this the ported guards would block legitimate
admin writes (publishing an event, featuring it).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT.parent / "orbit" / "supabase" / "migrations"
OUT = ROOT / "alembic" / "sql" / "orbit_functions.sql"

DROP_PREFIXES = (
    "grant ",
    "revoke ",
    "notify ",
    "alter publication ",
    "drop policy",
    "create policy",
    "alter table",  # enable row level security / add constraints already in models
)

DROP_IF_CONTAINS = (
    "storage.buckets",
    "storage.objects",
    "auth.users",
    "on_auth_user_created",
)


def statements(sql: str) -> list[str]:
    """Split on `;` at depth zero, honouring dollar-quoted bodies, quotes and comments."""
    out: list[str] = []
    buf: list[str] = []
    dollar: str | None = None
    i = 0
    n = len(sql)
    while i < n:
        if dollar is not None:
            if sql.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
            else:
                buf.append(sql[i])
                i += 1
            continue

        m = re.match(r"\$[A-Za-z_0-9]*\$", sql[i:])
        if m:
            dollar = m.group(0)
            buf.append(dollar)
            i += len(dollar)
            continue

        ch = sql[i]
        # A line comment runs to the newline; a semicolon inside it is not a split.
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end + 1
            buf.append(sql[i:end])
            i = end
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'" and sql[j - 1] != "\\":
                    j += 1
                    break
                j += 1
            buf.append(sql[i:j])
            i = j
            continue

        buf.append(ch)
        i += 1
        if ch == ";":
            out.append("".join(buf).strip())
            buf = []

    rest = "".join(buf).strip()
    if rest and rest != ";":
        out.append(rest)
    return out



IS_SERVICE = """create or replace function public.is_service()
returns boolean language sql stable as $$
  -- The API is the only client. Authorization happened in FastAPI before this
  -- query ran; `app.trusted` marks that connection as the trusted writer.
  select coalesce(current_setting('app.trusted', true), '') = 'on'
      or current_user in ('service_role', 'postgres', 'supabase_admin')
$$;"""


def _bare(stmt: str) -> str:
    """The statement without leading blank/comment lines (for classification)."""
    lines = stmt.splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("--")):
        i += 1
    return "\n".join(lines[i:]).strip()


def main() -> None:
    kept: list[str] = []
    seen_functions: set[str] = set()
    seen_triggers: set[str] = set()

    for path in sorted(SRC.glob("*.sql")):
        for stmt in statements(path.read_text()):
            bare = _bare(stmt)
            low = bare.lower()
            if any(d in stmt for d in DROP_IF_CONTAINS):
                continue
            if low.startswith(DROP_PREFIXES):
                continue
            if low.startswith("create extension"):
                continue
            if low.startswith("insert into ") and "storage." in low:
                continue
            if low.startswith("update public.profiles") or low.startswith("select public.set_user_role"):
                # one-off data fixes from migration history, not schema
                continue

            if low.startswith("create or replace function"):
                name = re.search(r"function\s+(?:public\.)?([a-z0-9_]+)", low)
                if not name:
                    continue
                fn = name.group(1)
                if fn == "handle_new_user":
                    continue
                if fn in seen_functions:
                    # a later migration replaces an earlier definition: keep the last
                    kept = [
                        s for s in kept
                        if not (
                            _bare(s).lower().startswith("create or replace function")
                            and re.search(rf"function\s+(?:public\.)?{fn}\b", _bare(s).lower())
                        )
                    ]
                seen_functions.add(fn)
                kept.append(IS_SERVICE if fn == "is_service" else stmt)
                continue

            if low.startswith("create trigger"):
                name = re.search(r"trigger\s+([a-z0-9_]+)", low)
                if name and name.group(1) not in seen_triggers:
                    seen_triggers.add(name.group(1))
                    kept.append(stmt)
                continue

            # tables, indexes and data seeds come from the SQLAlchemy models / seed
            # script; anything else here is a one-off migration fix.
            if low.startswith("create table") or low.startswith("create index") or low.startswith("create unique index"):
                continue
            if low.startswith("alter table"):
                continue

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        "-- GENERATED by scripts/extract_sql.py — do not edit by hand.\n"
        "-- Orbit's Postgres functions and triggers, ported verbatim from the\n"
        "-- Supabase migrations. Executed once by alembic/versions/0001_baseline.py.\n\n"
        + "\n\n".join(s.rstrip().rstrip(";") + ";" for s in kept)
        + "\n"
    )
    print(f"wrote {OUT} — {len(kept)} statements, {len(seen_functions)} functions, {len(seen_triggers)} triggers")


if __name__ == "__main__":
    main()
