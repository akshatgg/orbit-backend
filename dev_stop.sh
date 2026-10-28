#!/usr/bin/env bash
# Stop the Orbit backend dev environment: the reload server and the container.
#
#   ./dev_stop.sh            # stop uvicorn + Postgres container (data kept)
#   ./dev_stop.sh --volumes  # also delete the database volume
set -euo pipefail

cd "$(dirname "$0")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PORT="${PORT:-8090}"

echo -e "${YELLOW}Stopping Orbit backend...${NC}"

if command -v lsof >/dev/null 2>&1; then
    PIDS=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
    if [ -n "${PIDS:-}" ]; then
        echo -e "${YELLOW}Killing uvicorn on port $PORT ($(echo "$PIDS" | tr '\n' ' '))${NC}"
        while IFS= read -r pid; do
            [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
        done <<< "$PIDS"
    fi
fi

# The reloader can outlive its child; catch anything still holding the app.
if command -v pgrep >/dev/null 2>&1; then
    STILL=$(pgrep -f "uvicorn app.main:app.*--port $PORT" 2>/dev/null || true)
    if [ -n "${STILL:-}" ]; then
        while IFS= read -r pid; do
            [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true
        done <<< "$STILL"
    fi
fi

if command -v docker >/dev/null 2>&1; then
    if [ "${1:-}" = "--volumes" ]; then
        echo -e "${YELLOW}Removing the Postgres container and its volume...${NC}"
        docker compose -f docker-compose.dev.yml down -v
    else
        docker compose -f docker-compose.dev.yml down
    fi
fi

echo -e "${GREEN}✓ Stopped${NC}"
