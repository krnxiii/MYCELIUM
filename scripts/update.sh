#!/usr/bin/env bash
# MYCELIUM self-update: pull latest code, rebuild & restart containers.
# Usage: make update   OR   bash scripts/update.sh
set -euo pipefail

GREEN=$'\033[32m'
NC=$'\033[0m'

step() { printf "\n${GREEN}▸${NC} %s\n" "$1"; }

# ── Detect environment ──────────────────────────────────────────
if [ -f docker-compose.vps.yml ] && docker compose -f docker-compose.vps.yml ps --format '{{.Name}}' 2>/dev/null | grep -q mycelium; then
    COMPOSE_FILE="docker-compose.vps.yml"
else
    COMPOSE_FILE="docker-compose.yml"
fi

PROFILES=()
if docker compose -f "$COMPOSE_FILE" ps --format '{{.Name}}' 2>/dev/null | grep -q telegram; then
    PROFILES+=(--profile telegram)
fi
if docker compose -f "$COMPOSE_FILE" ps --format '{{.Name}}' 2>/dev/null | grep -q whisper; then
    PROFILES+=(--profile voice-whisper)
fi
if docker compose -f "$COMPOSE_FILE" ps --format '{{.Name}}' 2>/dev/null | grep -q mycelium-app; then
    PROFILES+=(--profile app)
fi

printf "${GREEN}MYCELIUM${NC} update  ${GREEN}[${COMPOSE_FILE}]${NC}\n"

# ── Pull ────────────────────────────────────────────────────────
step "Pulling latest code"
git pull --ff-only origin dev 2>&1 | tail -5

# ── Build ───────────────────────────────────────────────────────
step "Rebuilding containers"
docker compose -f "$COMPOSE_FILE" "${PROFILES[@]}" build --quiet 2>&1

# ── Restart ─────────────────────────────────────────────────────
step "Restarting services"
docker compose -f "$COMPOSE_FILE" "${PROFILES[@]}" up -d 2>&1

# ── Health check ────────────────────────────────────────────────
step "Waiting for healthy state"
bash scripts/wait-healthy.sh mycelium-neo4j mycelium-app

printf "\n${GREEN}✓${NC} Update complete.\n"
