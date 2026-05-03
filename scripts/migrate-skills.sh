#!/usr/bin/env bash
# Migrate runtime-saved extraction skills from the legacy mixed location
# (/app/mycelium/skills/extraction/, where save_skill used to write) into
# the new persistent user dir (~/.mycelium/skills/extraction/).
#
# Identifies "user skills" as files that are NOT tracked in git
# (bundled defaults are git-tracked; runtime-saved are not).
#
# Idempotent: cp -n preserves any existing user files. Safe to re-run.

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
    warn "mycelium-app container not running — skipping container migration."
    info "If you have no runtime-saved skills, you can ignore this."
    exit 0
fi

step "Computing bundled-skills list (from git)"
BUNDLED_LIST="$(mktemp)"
trap 'rm -f "$BUNDLED_LIST"' EXIT
git ls-files mycelium/skills/extraction/ 2>/dev/null \
    | xargs -n1 basename 2>/dev/null \
    | grep '\.md$' > "$BUNDLED_LIST" || true
BUNDLED_COUNT=$(wc -l < "$BUNDLED_LIST" | tr -d ' ')
success "Found $BUNDLED_COUNT bundled skills in repo"

step "Copying user skills from container → host ~/.mycelium/skills/extraction/"
docker cp "$BUNDLED_LIST" mycelium-app:/tmp/bundled-skills.txt

MIGRATED=$(docker exec mycelium-app sh -c '
    set -e
    mkdir -p /root/.mycelium/skills/extraction
    count=0
    if [ -d /app/mycelium/skills/extraction ]; then
        for f in /app/mycelium/skills/extraction/*.md; do
            [ -f "$f" ] || continue
            name=$(basename "$f")
            if ! grep -qFx "$name" /tmp/bundled-skills.txt 2>/dev/null; then
                # cp -n: do not overwrite existing user files (idempotent)
                if cp -n "$f" /root/.mycelium/skills/extraction/ 2>/dev/null; then
                    count=$((count + 1))
                    echo "  migrated: $name" >&2
                fi
            fi
        done
    fi
    echo $count
' 2>&1)

# Last line is the count; preceding lines went to stderr already.
MIGRATED_COUNT=$(printf "%s" "$MIGRATED" | tail -1)

if [ "${MIGRATED_COUNT:-0}" -gt 0 ]; then
    success "Migrated $MIGRATED_COUNT user skill(s) to ~/.mycelium/skills/extraction/"
else
    info "No user skills to migrate (or already migrated)."
fi

step "Done"
info "Future save_skill calls write to ~/.mycelium/skills/extraction/ (persistent)."
info "Add ~/.mycelium/{domains,skills} to syncthing folders for cross-device sync."
