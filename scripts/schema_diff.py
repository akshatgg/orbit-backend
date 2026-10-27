"""Diff the Orbit (Supabase) public schema against the backend's baseline schema.

Usage:
    python scripts/dump_schema.sh          # psql dumps into /tmp/orbit-schema
    python scripts/schema_diff.py DIR

The comparison is semantic where the two schemas are allowed to differ:

  * `auth.users` -> `users`   (Orbit's identity table replaces Supabase's)
  * `x IS NULL OR ...` guards are implied by CHECK semantics, so they are ignored
  * constraint and index *names* are ignored; columns, types, nullability,
    defaults, rules and unique/predicate indexes are compared
  * `auth.uid()` / `gen_random_uuid()` defaults are expected to be gone: the API
    writes those columns explicitly

Exit code 1 when a real difference remains.
"""

import json
import re
import sys
from pathlib import Path

TYPE_ALIASES = {
    "character varying": "text",
    "timestamptz": "timestamptz",
    "timestamp with time zone": "timestamptz",
    "timestamp without time zone": "timestamp",
    "double precision": "float8",
    "integer": "int4",
    "bigint": "int8",
    "boolean": "bool",
}

EXPECTED_EXTRA_TABLES = {"users", "email_tokens", "alembic_version"}
EXPECTED_EXTRA_PREFIX = ("group",)


def load(d: Path, name: str):
    p = d / name
    if not p.exists():
        return []
    raw = p.read_text().strip()
    return json.loads(raw) if raw else []


def strip_outer(v: str) -> str:
    """Remove parentheses that wrap the whole expression, repeatedly."""
    while v.startswith("(") and v.endswith(")"):
        depth = 0
        wraps = True
        for i, ch in enumerate(v):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(v) - 1:
                wraps = False
                break
        if not wraps:
            break
        v = v[1:-1].strip()
    return v


def norm(value: str) -> str:
    v = value.strip().lower()
    v = re.sub(r"::(text|character varying|uuid|jsonb|text\[\]|numeric|integer|bigint|boolean|timestamp with time zone)", "", v)
    v = v.replace("auth.users", "users")
    v = v.replace("'{}'", "{}")
    # A CHECK on a nullable column passes on NULL; the guard is redundant.
    v = re.sub(r"\((?:[a-z_\.\"]+) is null\) or ", "", v)
    v = re.sub(r"\s+", " ", v).strip()
    if v.startswith("check "):
        v = v[6:].strip()
    return strip_outer(v)


def colkey(c):
    t = TYPE_ALIASES.get(c["data_type"], c["data_type"])
    if t in ("array", "user-defined"):
        t = c["udt_name"]
    return (t, c["is_nullable"], norm(c.get("column_default") or ""))


def index_parts(defn: str):
    """(unique, columns, where) from a pg_indexes.indexdef string."""
    unique = "unique index" in defn.lower()
    m = re.search(r"using \w+ \((.+?)\)(?: where (.+))?$", defn, re.S | re.I)
    if not m:
        return (unique, defn.lower(), None)
    cols = norm(m.group(1)).replace('"', "")
    where = norm(m.group(2)) if m.group(2) else None
    return (unique, cols, where)


def main():
    d = Path(sys.argv[1])
    problems: list[str] = []
    notes: list[str] = []

    sc = load(d, "src_cols.json")
    dc = load(d, "dst_cols.json")
    stables: dict[str, dict] = {}
    for c in sc:
        stables.setdefault(c["table_name"], {})[c["column_name"]] = c
    dtables: dict[str, dict] = {}
    for c in dc:
        dtables.setdefault(c["table_name"], {})[c["column_name"]] = c

    missing_tables = sorted(set(stables) - set(dtables))
    extra_tables = sorted(
        t for t in set(dtables) - set(stables)
        if t not in EXPECTED_EXTRA_TABLES and not t.startswith(EXPECTED_EXTRA_PREFIX)
    )
    if missing_tables:
        problems.append(f"MISSING TABLES: {missing_tables}")
    if extra_tables:
        problems.append(f"UNEXPECTED EXTRA TABLES: {extra_tables}")

    for t in sorted(stables):
        if t not in dtables:
            continue
        s, o = stables[t], dtables[t]
        for name in sorted(set(s) - set(o)):
            problems.append(f"{t}.{name}: MISSING COLUMN")
        for name in sorted(set(o) - set(s)):
            problems.append(f"{t}.{name}: extra column")
        for name in sorted(set(s) & set(o)):
            sk, ok = colkey(s[name]), colkey(o[name])
            if sk[0] != ok[0]:
                problems.append(f"{t}.{name}: type {sk[0]} -> {ok[0]}")
            if sk[1] != ok[1]:
                problems.append(f"{t}.{name}: nullability {sk[1]} -> {ok[1]}")
            if sk[2] != ok[2]:
                if "auth.uid" in sk[2] or "gen_random" in sk[2]:
                    notes.append(f"{t}.{name}: default '{sk[2]}' -> '{ok[2]}' (API writes it)")
                else:
                    problems.append(f"{t}.{name}: default '{sk[2]}' -> '{ok[2]}'")

    scons = {(c["tbl"], c["contype"], norm(c["def"])) for c in load(d, "src_cons.json")}
    dcons = {(c["tbl"], c["contype"], norm(c["def"])) for c in load(d, "dst_cons.json")}
    for tbl, ctype, definition in sorted(scons - dcons):
        problems.append(f"CONSTRAINT {ctype} missing on {tbl}: {definition}")

    sidx = {(i["tbl"], *index_parts(i["indexdef"])) for i in load(d, "src_idx.json")}
    didx = {(i["tbl"], *index_parts(i["indexdef"])) for i in load(d, "dst_idx.json")}
    for tbl, unique, cols, where in sorted(sidx - didx):
        if cols == "(id)":  # primary key
            continue
        problems.append(f"INDEX {'unique ' if unique else ''}missing on {tbl}: ({cols})" + (f" where {where}" if where else ""))

    def trg(t):
        return (t["tbl"], t["trigger_name"], t["action_timing"], t["event_manipulation"])

    for row in sorted({trg(t) for t in load(d, "src_trg.json")} - {trg(t) for t in load(d, "dst_trg.json")}):
        problems.append(f"TRIGGER missing: {row}")

    print(f"tables      source {len(stables):>3}   backend {len(dtables):>3}")
    print(f"columns     source {sum(len(v) for v in stables.values()):>3}   backend {sum(len(v) for v in dtables.values()):>3}")
    print(f"constraints source {len(scons):>3}   backend {len(dcons):>3}")
    print(f"indexes     source {len(sidx):>3}   backend {len(didx):>3}")
    print(f"\nreal differences: {len(problems)}")
    for p in problems:
        print("  FAIL", p)
    print(f"\nexpected/documented differences: {len(notes)}")
    for n in notes:
        print("  ok  ", n)
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
