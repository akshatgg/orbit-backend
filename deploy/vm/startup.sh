#!/usr/bin/env bash
# GCE startup-script for markai-orbit (Ubuntu 24.04). Idempotent: runs on
# every boot, installs Docker once, and makes sure the host directories exist.
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2
  systemctl enable --now docker
fi

mkdir -p /opt/markai-orbit /etc/markai-orbit \
         /var/lib/markai-orbit/pgdata /var/lib/markai-orbit/caddy
chmod 700 /etc/markai-orbit
