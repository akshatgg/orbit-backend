#!/usr/bin/env bash
# Orbit Backend — local dev orchestration.
#
#   1. bootstrap .env from .env.example on first run
#   2. ensure the virtualenv and its dependencies exist
#   3. start Postgres (docker compose, host port 5433) and wait for it
#   4. verify the database is reachable, then apply Alembic migrations
#   5. run uvicorn with --reload on :8090
#
#   ./dev_start.sh              # normal run
#   PORT=8095 ./dev_start.sh    # different port
#   SKIP_DOCKER=1 ./dev_start.sh   # use the DATABASE_URL already in .env
set -euo pipefail

cd "$(dirname "$0")"

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

PORT="${PORT:-8090}"
COMPOSE="docker compose -f docker-compose.dev.yml"

echo -e "${BLUE}==========================================${NC}"
echo -e "${BLUE}   Orbit Backend — Dev Environment${NC}"
echo -e "${BLUE}==========================================${NC}"

# ── 1. .env ────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo -e "${YELLOW}No .env found — copying .env.example -> .env${NC}"
    cp .env.example .env
fi

# ── 2. virtualenv ──────────────────────────────────────────────────────────
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import fastapi" >/dev/null 2>&1; then
    echo -e "${YELLOW}[1/5] Creating virtualenv and installing dependencies...${NC}"
    rm -rf .venv
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip >/dev/null
    .venv/bin/python -m pip install -q -r requirements.txt
    echo -e "${GREEN}✓ Dependencies installed${NC}"
else
    echo -e "${GREEN}✓ Virtualenv ready${NC}"
fi

# ── 3. Postgres ────────────────────────────────────────────────────────────
if [ "${SKIP_DOCKER:-0}" != "1" ]; then
    if ! command -v docker >/dev/null 2>&1; then
        echo -e "${RED}Docker is required for the local database.${NC}"
        echo "Install Docker Desktop, or point DATABASE_URL at a database you already run and use SKIP_DOCKER=1."
        exit 1
    fi
    echo -e "${YELLOW}[2/5] Starting Postgres (docker compose)...${NC}"
    $COMPOSE up -d postgres

    echo -e "${YELLOW}      Waiting for Postgres to accept connections...${NC}"
    for i in $(seq 1 30); do
        if $COMPOSE exec -T postgres pg_isready -U orbit -d orbit >/dev/null 2>&1; then
            echo -e "${GREEN}✓ Postgres is ready on localhost:5433${NC}"
            break
        fi
        if [ "$i" -eq 30 ]; then
            echo -e "${RED}Postgres did not become ready. Check: $COMPOSE logs postgres${NC}"
            exit 1
        fi
        sleep 1
    done
else
    echo -e "${YELLOW}[2/5] SKIP_DOCKER=1 — using DATABASE_URL from .env${NC}"
fi

# .env is read by the app itself (pydantic-settings), so it is never `source`d
# here: a value like `EMAIL_FROM=Orbit <hello@orbit.example>` would otherwise be
# parsed as a shell redirection and kill the script.
DB_URL=$(.venv/bin/python -c "from app.core.config import settings; print(settings.DATABASE_URL or '')" 2>/dev/null || true)
if [ -z "$DB_URL" ]; then
    echo -e "${RED}DATABASE_URL is not set in .env${NC}"
    exit 1
fi

# ── 4. database reachable + migrations ─────────────────────────────────────
echo -e "${YELLOW}[3/5] Checking the database...${NC}"
if ! .venv/bin/python - <<'PY'
import sys

from sqlalchemy import create_engine, text

from app.core.database import get_sync_database_url

url = get_sync_database_url()
if not url:
    print("DATABASE_URL is not set in .env")
    sys.exit(1)
try:
    engine = create_engine(url, connect_args={"connect_timeout": 4})
    with engine.connect() as conn:
        conn.execute(text("select 1"))
except Exception as exc:  # noqa: BLE001 - the message is the point
    print(str(exc).strip().splitlines()[0])
    sys.exit(1)
PY
then
    echo -e "${RED}Cannot reach the database at DATABASE_URL.${NC}"
    echo -e "Local default is ${GREEN}postgresql://orbit:orbit@localhost:5433/orbit${NC} (docker compose)."
    echo "Fix the DATABASE_URL line in .env, or start your own database and re-run with SKIP_DOCKER=1."
    exit 1
fi
echo -e "${GREEN}✓ Database reachable${NC}"

echo -e "${YELLOW}[4/5] Applying migrations (alembic upgrade head)...${NC}"
.venv/bin/alembic upgrade head
echo -e "${GREEN}✓ Schema is up to date${NC}"

# ── 5. run ─────────────────────────────────────────────────────────────────
if command -v lsof >/dev/null 2>&1; then
    PIDS=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
    if [ -n "${PIDS:-}" ]; then
        echo -e "${YELLOW}Port $PORT is in use — stopping $(echo "$PIDS" | tr '\n' ' ')${NC}"
        while IFS= read -r pid; do
            [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
        done <<< "$PIDS"
        sleep 1
    fi
fi

echo -e "${YELLOW}[5/5] Starting uvicorn...${NC}"
echo ""
echo -e "${GREEN}  API       http://localhost:${PORT}${NC}"
echo -e "${GREEN}  Docs      http://localhost:${PORT}/docs${NC}"
echo -e "${GREEN}  Health    http://localhost:${PORT}/api/health${NC}"
echo -e "${GREEN}  Database  ${DB_URL}${NC}"
echo ""
echo -e "${GREEN}  Cron      GET http://localhost:${PORT}/api/cron/refresh-sources${NC}"
echo -e "${GREEN}            GET http://localhost:${PORT}/api/cron/reminders${NC}"
echo -e "            header: Authorization: Bearer \$CRON_SECRET"
echo ""
echo -e "${YELLOW}Ctrl+C stops the server. ./dev_stop.sh also stops the container.${NC}"
echo ""

exec .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port "$PORT"
