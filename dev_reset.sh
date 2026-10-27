#!/usr/bin/env bash
# Throw the local database away and rebuild it from the migrations.
# Use after changing a model or the baseline migration.
#
#   ./dev_reset.sh          # prompts before deleting
#   ./dev_reset.sh --yes    # no prompt
set -euo pipefail

cd "$(dirname "$0")"

RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m'

if [ "${1:-}" != "--yes" ]; then
    echo -e "${RED}This deletes the local Orbit database (docker volume orbit_backend_pgdata).${NC}"
    read -r -p "Type 'yes' to continue: " answer
    [ "$answer" = "yes" ] || { echo "Cancelled."; exit 1; }
fi

echo -e "${YELLOW}Stopping Postgres and removing its volume...${NC}"
docker compose -f docker-compose.dev.yml down -v

echo -e "${YELLOW}Rebuilding...${NC}"
./dev_start.sh
