#!/usr/bin/env bash
# Migrate runtime-created user data out of container-internal locations
# (which are about to be masked by the new ~/.mycelium volume mount) into
# the persistent host directory ~/.mycelium/.
#
# Three classes of data are at risk during the persistence-PR upgrade:
#   1. User extraction skills    /app/mycelium/skills/extraction/*.md
#                              → ~/.mycelium/skills/extraction/
#      (bundled defaults — those tracked in git — are skipped)
#   2. Domain blueprints         /root/.mycelium/domains/*.yaml
#                              → ~/.mycelium/domains/
#   3. Global env file           /root/.mycelium/.env
#                              → ~/.mycelium/.env
#
# Idempotent: existing host-side files are never overwritten. Safe to re-run.

set -euo pipefail

GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; NC=$'\033[0m'

step()    { printf "\n${BOLD}${GREEN}▸${NC} ${BOLD}%s${NC}\n" "$1"; }
success() { printf "  ${GREEN}✓${NC}  %s\n" "$1"; }
info()    { printf "  ${DIM}ℹ${NC}  %s\n" "$1"; }
warn()    { printf "  ${YELLOW}!${NC}  %s\n" "$1"; }

# ── Detect project root ─────────────────────────────────────────
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# ── Detect compose file + container ─────────────────────────────
if [ -f docker-compose.vps.yml ] && \
   docker compose -f docker-compose.vps.yml ps --format '{{.Name}}' 2>/dev/null | grep -q mycelium-app; then
    COMPOSE_FILE="docker-compose.vps.yml"
elif docker compose -f docker-compose.yml ps --format '{{.Name}}' 2>/dev/null | grep -q mycelium-app; then
    COMPOSE_FILE="docker-compose.yml"
else
    warn "mycelium-app container not running — skipping migration."
    info "If you have no runtime-saved data, you can ignore this."
    exit 0
fi

DATA_DIR="${MYCELIUM_DATA_DIR:-$HOME/.mycelium}"
mkdir -p "$DATA_DIR/skills/extraction" "$DATA_DIR/domains"

# ── 1. Skills ───────────────────────────────────────────────────
step "Migrating user extraction skills"
BUNDLED_LIST="$(mktemp)"
trap 'rm -f "$BUNDLED_LIST"' EXIT
git ls-files mycelium/skills/extraction/ 2>/dev/null \
    | xargs -n1 basename 2>/dev/null \
    | grep '\.md$' > "$BUNDLED_LIST" || true
BUNDLED_COUNT=$(wc -l < "$BUNDLED_LIST" | tr -d ' ')
info "Bundled skills tracked in git: $BUNDLED_COUNT"

# List candidate skills inside container, then copy missing ones via docker cp
# (works regardless of whether new mount is active — goes through host FS).
SKILL_NAMES=$(docker exec mycelium-app sh -c '
    for f in /app/mycelium/skills/extraction/*.md; do
        [ -f "$f" ] && basename "$f"
    done
' 2>/dev/null || true)

SKILLS_MIGRATED=0
SKILLS_FOUND=0
if [ -n "$SKILL_NAMES" ]; then
    while IFS= read -r name; do
        [ -z "$name" ] && continue
        # Skip bundled defaults
        if grep -qFx "$name" "$BUNDLED_LIST"; then
            continue
        fi
        SKILLS_FOUND=$((SKILLS_FOUND + 1))
        if [ ! -e "$DATA_DIR/skills/extraction/$name" ]; then
            docker cp "mycelium-app:/app/mycelium/skills/extraction/$name" "$DATA_DIR/skills/extraction/$name" 2>/dev/null
            SKILLS_MIGRATED=$((SKILLS_MIGRATED + 1))
        fi
    done <<< "$SKILL_NAMES"
fi

if [ "$SKILLS_MIGRATED" -gt 0 ]; then
    success "Migrated $SKILLS_MIGRATED user skill(s) → $DATA_DIR/skills/extraction/"
elif [ "$SKILLS_FOUND" -gt 0 ]; then
    info "All $SKILLS_FOUND user skill(s) already on host (idempotent skip)."
else
    info "No user skills to migrate."
fi

# ── 2. Domain blueprints ────────────────────────────────────────
step "Migrating domain blueprints"
# Single source of truth: list filenames in container, then copy each
# missing one to host. Avoids stdout/stderr interleaving in subshells.
DOMAIN_NAMES=$(docker exec mycelium-app sh -c '
    for f in /root/.mycelium/domains/*.yaml /root/.mycelium/domains/*.yml; do
        [ -f "$f" ] && basename "$f"
    done
' 2>/dev/null || true)

DOMAINS_MIGRATED=0
DOMAINS_FOUND=0
if [ -n "$DOMAIN_NAMES" ]; then
    while IFS= read -r name; do
        [ -z "$name" ] && continue
        DOMAINS_FOUND=$((DOMAINS_FOUND + 1))
        if [ ! -e "$DATA_DIR/domains/$name" ]; then
            docker cp "mycelium-app:/root/.mycelium/domains/$name" "$DATA_DIR/domains/$name" 2>/dev/null
            DOMAINS_MIGRATED=$((DOMAINS_MIGRATED + 1))
        fi
    done <<< "$DOMAIN_NAMES"
fi

if [ "$DOMAINS_MIGRATED" -gt 0 ]; then
    success "Migrated $DOMAINS_MIGRATED domain blueprint(s) → $DATA_DIR/domains/"
elif [ "$DOMAINS_FOUND" -gt 0 ]; then
    info "All $DOMAINS_FOUND domain blueprint(s) already on host (idempotent skip)."
else
    info "No domain blueprints to migrate."
fi

# ── 3. .env file ────────────────────────────────────────────────
step "Migrating .env (if present in container, missing on host)"
if [ -e "$DATA_DIR/.env" ]; then
    info "$DATA_DIR/.env already exists — leaving untouched."
elif docker exec mycelium-app test -f /root/.mycelium/.env 2>/dev/null; then
    docker cp mycelium-app:/root/.mycelium/.env "$DATA_DIR/.env"
    success "Migrated .env → $DATA_DIR/.env"
else
    info "No container .env to migrate."
fi

# ── Summary ─────────────────────────────────────────────────────
step "Done"
info "After this, run: make update  (rebuilds with the new full mount)."
info "Future writes (save_skill, create_domain, telegram setup) land in"
info "$DATA_DIR/ directly, persistent across rebuilds."
info "For cross-device sync, add ~/.mycelium/{domains,skills} to syncthing."
