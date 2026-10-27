#!/usr/bin/env bash
# Dump the public schema of two databases for scripts/schema_diff.py.
#
#   SRC_DB=postgres DST_DB=orbit_backend_test ./scripts/dump_schema.sh
#
# Defaults assume the local Supabase Postgres container (docker).
set -euo pipefail

OUT="${OUT:-/tmp/orbit-schema}"
CONTAINER="${CONTAINER:-supabase_db_orbit}"
SRC_DB="${SRC_DB:-postgres}"
DST_DB="${DST_DB:-orbit_backend_test}"
mkdir -p "$OUT"

COLS="select table_name, column_name, data_type, udt_name, is_nullable, column_default, is_generated, generation_expression from information_schema.columns where table_schema='public' order by table_name, ordinal_position"
CONS="select t.relname as tbl, c.conname, c.contype, pg_get_constraintdef(c.oid) as def from pg_constraint c join pg_class t on t.oid=c.conrelid join pg_namespace n on n.oid=t.relnamespace where n.nspname='public' order by 1,2"
IDX="select tablename as tbl, indexname, indexdef from pg_indexes where schemaname='public' order by 1,2"
TRG="select event_object_table as tbl, trigger_name, action_timing, event_manipulation, action_statement from information_schema.triggers where trigger_schema='public' order by 1,2,4"

dump() {
  local db="$1" prefix="$2"
  for pair in "cols:$COLS" "cons:$CONS" "idx:$IDX" "trg:$TRG"; do
    name="${pair%%:*}"; query="${pair#*:}"
    docker exec -i "$CONTAINER" psql -U postgres -d "$db" -tAc \
      "select coalesce(json_agg(row_to_json(x)), '[]'::json) from ($query) x" > "$OUT/${prefix}_${name}.json"
  done
}

dump "$SRC_DB" src
dump "$DST_DB" dst
echo "dumped to $OUT"
