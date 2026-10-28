#!/usr/bin/env bash
# PID 1 for the single-container image.
#
#   initialise Postgres (first boot only)  ->  start it  ->  create role/db
#   ->  alembic upgrade head  ->  seed once if the catalogue is empty
#   ->  uvicorn  ->  on SIGTERM stop uvicorn, then Postgres (fast shutdown)
#
# Postgres writes to $PGDATA, which must be a mounted volume. Without one the
# database is gone the moment this container is replaced.
set -euo pipefail

export PATH="/usr/lib/postgresql/17/bin:$PATH"
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
DB_NAME="${POSTGRES_DB:-orbit}"
DB_USER="${POSTGRES_USER:-orbit}"
DB_PASS="${POSTGRES_PASSWORD:-orbit}"
PORT="${PORT:-8090}"

mkdir -p "$PGDATA" /var/run/postgresql
chown -R postgres:postgres "$PGDATA" /var/run/postgresql
chmod 700 "$PGDATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "[entrypoint] first boot — initialising the database in $PGDATA"
  su postgres -c "initdb -D '$PGDATA' --auth-local=trust --auth-host=scram-sha-256" >/dev/null
fi

echo "[entrypoint] starting Postgres"
su postgres -c "pg_ctl -D '$PGDATA' -l /tmp/postgres.log -o \"-c listen_addresses=127.0.0.1\" -w start"

for _ in $(seq 1 60); do
  pg_isready -h 127.0.0.1 -q && break
  sleep 1
done

su postgres -c "psql -h /var/run/postgresql -tAc \"select 1 from pg_roles where rolname='$DB_USER'\"" | grep -q 1 \
  || su postgres -c "psql -h /var/run/postgresql -c \"create role \\\"$DB_USER\\\" login password '$DB_PASS'\""

su postgres -c "psql -h /var/run/postgresql -tAc \"select 1 from pg_database where datname='$DB_NAME'\"" | grep -q 1 \
  || su postgres -c "createdb -h /var/run/postgresql -O \"$DB_USER\" \"$DB_NAME\""

export DATABASE_URL="postgresql://${DB_USER}:${DB_PASS}@127.0.0.1:5432/${DB_NAME}"

echo "[entrypoint] applying migrations"
alembic upgrade head

if [ "${SEED_ON_EMPTY:-1}" = "1" ] && [ -f /app/seeds/prod.sql ]; then
  EVENTS=$(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -tAc "select count(*) from public.events" 2>/dev/null || echo 0)
  if [ "${EVENTS:-0}" = "0" ]; then
    echo "[entrypoint] empty database — loading seeds/prod.sql"
    PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -q -f /app/seeds/prod.sql
    echo "[entrypoint] seed complete: $(PGPASSWORD="$DB_PASS" psql -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME" -tAc 'select count(*) from public.events') events"
  else
    echo "[entrypoint] database already has $EVENTS events — not seeding"
  fi
fi

echo "[entrypoint] starting uvicorn on :$PORT"
uvicorn app.main:app --host 0.0.0.0 --port "$PORT" &
API_PID=$!

shutdown() {
  echo "[entrypoint] stopping"
  kill -TERM "$API_PID" 2>/dev/null || true
  wait "$API_PID" 2>/dev/null || true
  su postgres -c "pg_ctl -D '$PGDATA' stop -m fast" || true
}
trap shutdown TERM INT

wait "$API_PID"
