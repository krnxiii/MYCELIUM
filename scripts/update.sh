#!/usr/bin/env bash
# MYCELIUM update: pull latest code, rebuild & restart containers.
# Usage: make update   OR   bash scripts/update.sh
set -euo pipefail

GREEN=$'\033[32m'
DIM=$'\033[2m'
BOLD=$'\033[1m'
NC=$'\033[0m'

step()    { printf "\n${BOLD}${GREEN}▸${NC} ${BOLD}%s${NC}\n" "$1"; }
success() { printf "  ${GREEN}✓${NC}  %s\n" "$1"; }
error()   { printf "  \033[0;31m✗${NC}  %s\n" "$1" >&2; }

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

COMPOSE_CMD=(docker compose -f "$COMPOSE_FILE" "${PROFILES[@]}")

printf "\n${BOLD}${GREEN}MYCELIUM${NC} update  ${DIM}[${COMPOSE_FILE}]${NC}\n"

# ── Pull ────────────────────────────────────────────────────────
step "Pulling latest code"
git pull --ff-only origin main
echo

# ── Build ───────────────────────────────────────────────────────
step "Rebuilding containers"
"${COMPOSE_CMD[@]}" build
success "Images built"

# ── Graceful stop (prevents data loss on neo4j) ─────────────────
step "Stopping services gracefully"
"${COMPOSE_CMD[@]}" stop -t 30
success "Services stopped"

# ── Start ───────────────────────────────────────────────────────
step "Starting services"
"${COMPOSE_CMD[@]}" up -d
success "Containers started"

# ── Health check ────────────────────────────────────────────────
step "Waiting for healthy state"
bash scripts/wait-healthy.sh mycelium-neo4j mycelium-app
success "All services healthy"

printf "\n${GREEN}✓${NC} Update complete.\n"
