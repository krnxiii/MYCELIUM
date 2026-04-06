#!/usr/bin/env bash
# MYCELIUM self-update: pull latest code, rebuild & restart containers.
# Usage: make update   OR   bash scripts/update.sh
set -euo pipefail
trap 'kill $(jobs -p) 2>/dev/null; printf "\033[?25h" >&2' EXIT INT TERM

GREEN=$'\033[32m'
DIM=$'\033[2m'
NC=$'\033[0m'

BRAILLE=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')

step()    { printf "\n${GREEN}▸${NC} %s\n" "$1"; }
success() { printf "  ${GREEN}✓${NC}  %s\n" "$1"; }
error()   { printf "  \033[0;31m✗${NC}  %s\n" "$1" >&2; }

spin_log() {
    local pid=$1 logfile="$2" label="${3:-}"
    local i=0 start cols phase
    start=$SECONDS
    cols=$(tput cols 2>/dev/null || echo 80)

    while kill -0 "$pid" 2>/dev/null; do
        local elapsed=$(( SECONDS - start ))
        local mins=$(( elapsed / 60 )) secs=$(( elapsed % 60 ))
        local time_str
        (( mins > 0 )) && time_str="${mins}m ${secs}s" || time_str="${secs}s"

        phase="$label"
        local line
        line="$(grep -oE '\[([0-9]+/[0-9]+)\] [A-Z]+' "$logfile" 2>/dev/null | tail -1 || true)"
        if [[ -n "$line" ]]; then
            local step_num="${line%%]*}"; step_num="${step_num#[}"
            phase="Building [$step_num]"
        elif grep -q 'Creating\|Starting\|Recreating' "$logfile" 2>/dev/null; then
            local svc
            svc="$(grep -oE '(Creating|Starting|Recreating) [a-z_-]+' "$logfile" | tail -1 || true)"
            [[ -n "$svc" ]] && phase="$svc"
        fi

        local avail=$(( cols - 12 - ${#time_str} ))
        (( ${#phase} > avail )) && phase="${phase:0:$((avail-1))}…"

        printf "\r  ${DIM}%s${NC} %-${avail}s ${DIM}(%s)${NC}" \
            "${BRAILLE[$((i % ${#BRAILLE[@]}))]}" "$phase" "$time_str" >&2
        sleep 0.15
        ((i++)) || true
    done
    printf "\r\033[K" >&2
    wait "$pid" || return $?
}

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

COMPOSE_CMD="docker compose -f $COMPOSE_FILE ${PROFILES[*]}"

printf "\n${GREEN}MYCELIUM${NC} update  ${DIM}[${COMPOSE_FILE}]${NC}\n"

# ── Pull ────────────────────────────────────────────────────────
step "Pulling latest code"
git pull --ff-only origin dev 2>&1 | tail -5

# ── Build ───────────────────────────────────────────────────────
logfile="$(mktemp)"

step "Rebuilding containers"
BUILDKIT_PROGRESS=plain $COMPOSE_CMD build >>"$logfile" 2>&1 &
if ! spin_log $! "$logfile" "Building images..."; then
    error "Build failed:"; tail -20 "$logfile" >&2; rm -f "$logfile"; exit 1
fi
success "Images built"

# ── Restart ─────────────────────────────────────────────────────
step "Restarting services"
$COMPOSE_CMD up -d >>"$logfile" 2>&1 &
if ! spin_log $! "$logfile" "Starting containers..."; then
    error "Restart failed:"; tail -10 "$logfile" >&2; rm -f "$logfile"; exit 1
fi
success "Containers started"

# ── Health check ────────────────────────────────────────────────
step "Waiting for healthy state"
bash scripts/wait-healthy.sh mycelium-neo4j mycelium-app >>"$logfile" 2>&1 &
if ! spin_log $! "$logfile" "Health checks..."; then
    error "Health check failed:"; tail -5 "$logfile" >&2; rm -f "$logfile"; exit 1
fi
success "All services healthy"
rm -f "$logfile"

printf "\n${GREEN}✓${NC} Update complete.\n"
