#!/usr/bin/env bash
set -euo pipefail

# ── Colors & Constants ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'
DIM='\033[2m'; NC='\033[0m'

COMPOSE_FILE="docker-compose.vps.yml"
ENV_FILE=".env"
ENV_EXAMPLE=".env.example"

# ── Helpers ─────────────────────────────────────────────────────────
info()    { printf "${BLUE}ℹ${NC}  %s\n" "$1"; }
success() { printf "${GREEN}✓${NC}  %s\n" "$1"; }
warn()    { printf "${YELLOW}⚠${NC}  %s\n" "$1"; }
error()   { printf "${RED}✗${NC}  %s\n" "$1" >&2; }
step()    { printf "\n${BOLD}${CYAN}[%s]${NC} %s\n" "$1" "$2"; }

ask() {
    local prompt="$1" default="${2:-}"
    if [[ -n "$default" ]]; then
        printf "${BOLD}?${NC}  %s ${DIM}[%s]${NC}: " "$prompt" "$default" >&2
    else
        printf "${BOLD}?${NC}  %s: " "$prompt" >&2
    fi
    read -r answer
    echo "${answer:-$default}"
}

ask_secret() {
    local prompt="$1" default="${2:-}"
    if [[ -n "$default" ]]; then
        printf "${BOLD}?${NC}  %s ${DIM}[%s]${NC}: " "$prompt" "$default" >&2
    else
        printf "${BOLD}?${NC}  %s: " "$prompt" >&2
    fi
    read -rs answer
    echo >&2
    echo "${answer:-$default}"
}

set_env_val() {
    local key="$1" val="$2" file="${3:-$ENV_FILE}"
    local tmp="${file}.tmp"
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            "${key}="*) printf '%s=%s\n' "$key" "$val" ;;
            *)          printf '%s\n' "$line" ;;
        esac
    done < "$file" > "$tmp"
    mv "$tmp" "$file"
}

# ── Project Root ────────────────────────────────────────────────────
detect_project_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [[ "$(basename "$dir")" == "scripts" ]] && dir="$(dirname "$dir")"
    if [[ ! -f "$dir/$COMPOSE_FILE" ]]; then
        error "Cannot find $COMPOSE_FILE in project root"
        exit 1
    fi
    echo "$dir"
}

# ── Dependency Checks ──────────────────────────────────────────────
check_deps() {
    local all_ok=true

    printf "\n  %-22s %s\n" "Dependency" "Status"
    printf "  %-22s %s\n" "──────────────────────" "──────"

    for cmd in docker make curl; do
        if command -v "$cmd" &>/dev/null; then
            printf "  %-22s ${GREEN}✓ found${NC}\n" "$cmd"
        else
            printf "  %-22s ${RED}✗ missing${NC}\n" "$cmd"
            all_ok=false
        fi
    done

    if docker compose version &>/dev/null; then
        printf "  %-22s ${GREEN}✓ found${NC}\n" "docker compose"
    else
        printf "  %-22s ${RED}✗ missing${NC}\n" "docker compose"
        all_ok=false
    fi

    if docker info &>/dev/null; then
        printf "  %-22s ${GREEN}✓ running${NC}\n" "docker daemon"
    else
        printf "  %-22s ${RED}✗ not running${NC}\n" "docker daemon"
        all_ok=false
    fi

    echo
    if [[ "$all_ok" == false ]]; then
        error "Fix missing dependencies and re-run."
        exit 1
    fi
}

# ── Generate Auth Token ────────────────────────────────────────────
generate_token() {
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null \
        || openssl rand -base64 32 | tr -d '/+=' | head -c 43
}

# ── Embeddings Mode ────────────────────────────────────────────────
select_embeddings() {
    echo >&2
    printf "  ${BOLD}1)${NC}  DeepInfra API  — no local GPU needed (default)\n" >&2
    printf "  ${BOLD}2)${NC}  Local TEI      — BGE-M3 on VPS CPU (downloads ~2 GB model)\n" >&2
    echo >&2
    while true; do
        local choice
        choice="$(ask "Embeddings mode [1/2]" "1")"
        case "$choice" in
            1|2) echo "$choice"; return ;;
            *) warn "Enter 1 or 2" >&2 ;;
        esac
    done
}

# ── Configure .env ──────────────────────────────────────────────────
configure_env() {
    # Backup existing .env
    if [[ -f "$ENV_FILE" ]]; then
        local backup="$ENV_FILE.backup.$(date +%Y%m%d_%H%M%S)"
        cp "$ENV_FILE" "$backup"
        success "Existing .env backed up: $backup"
    fi
    cp "$ENV_EXAMPLE" "$ENV_FILE"

    # ── Auth token ──
    local token
    token="$(generate_token)"
    set_env_val "MYCELIUM_MCP__AUTH_TOKEN" "$token"
    success "Auth token generated"
    echo
    info "Save this token — you'll need it on your laptop to connect:"
    printf "\n  ${BOLD}${CYAN}%s${NC}\n\n" "$token"

    # ── Neo4j password ──
    local neo4j_pass
    neo4j_pass="$(ask_secret "Neo4j password" "password")"
    [[ "${#neo4j_pass}" -lt 4 ]] && neo4j_pass="password"
    set_env_val "MYCELIUM_NEO4J__PASSWORD" "$neo4j_pass"

    # ── Embeddings ──
    local emb_mode
    emb_mode="$(select_embeddings)"
    if [[ "$emb_mode" == "1" ]]; then
        info "Get a free key at: https://deepinfra.com/dash/api_keys"
        local api_key
        api_key="$(ask_secret "DeepInfra API key")"
        if [[ -z "$api_key" ]]; then
            warn "No key provided — set MYCELIUM_SEMANTIC__API_KEY in .env later"
        else
            set_env_val "MYCELIUM_SEMANTIC__API_KEY" "$api_key"
        fi
    else
        set_env_val "MYCELIUM_SEMANTIC__API_BASE_URL" "http://embeddings:8080"
        set_env_val "MYCELIUM_SEMANTIC__API_KEY" ""
    fi

    # ── Owner ──
    echo
    local owner_name
    owner_name="$(ask "Your name (optional, for graph ownership)" "")"
    [[ -n "$owner_name" ]] && set_env_val "MYCELIUM_OWNER__NAME" "$owner_name"

    # ── Tailscale ──
    echo
    info "Tailscale connects VPS to your laptop via WireGuard mesh."
    info "Get an auth key at: https://login.tailscale.com/admin/settings/keys"
    local ts_key
    ts_key="$(ask_secret "Tailscale auth key (or empty to skip)")"
    if [[ -n "$ts_key" ]]; then
        set_env_val "TAILSCALE_AUTHKEY" "$ts_key"
    else
        warn "Tailscale skipped — add TAILSCALE_AUTHKEY to .env later"
    fi

    # Store embeddings mode for compose profile
    echo "MYCELIUM_VPS_EMB_MODE=$emb_mode" >> "$ENV_FILE"
    success ".env configured"
}

# ── Deploy ──────────────────────────────────────────────────────────
deploy() {
    local emb_mode
    emb_mode="$(grep '^MYCELIUM_VPS_EMB_MODE=' "$ENV_FILE" | cut -d= -f2)"

    local compose_cmd="docker compose -f $COMPOSE_FILE"
    if [[ "$emb_mode" == "2" ]]; then
        compose_cmd="$compose_cmd --profile full"
    fi

    info "Pulling images..."
    $compose_cmd pull

    info "Building MYCELIUM..."
    $compose_cmd up -d --build

    info "Waiting for services..."
    bash scripts/wait-healthy.sh mycelium-neo4j mycelium-app
}

# ── Summary ─────────────────────────────────────────────────────────
show_summary() {
    local token
    token="$(grep '^MYCELIUM_MCP__AUTH_TOKEN=' "$ENV_FILE" | cut -d= -f2)"

    echo
    printf "  ${BOLD}${GREEN}MYCELIUM VPS is ready!${NC}\n"
    echo
    printf "  %-22s %s\n" "Service" "Access"
    printf "  %-22s %s\n" "──────────────────────" "──────────────────────────────"
    printf "  %-22s %s\n" "MCP (HTTP)"            "http://<tailscale-ip>:8000/mcp"
    printf "  %-22s %s\n" "Neo4j Browser"         "http://<tailscale-ip>:7474"
    printf "  %-22s %s\n" "Syncthing UI"          "http://<tailscale-ip>:8384"

    echo
    printf "  ${BOLD}On your laptop:${NC}\n"
    echo
    printf "  ${DIM}# 1. Install Tailscale (if not yet)${NC}\n"
    printf "  brew install tailscale  ${DIM}# or https://tailscale.com/download${NC}\n"
    echo
    printf "  ${DIM}# 2. Find your VPS Tailscale IP${NC}\n"
    printf "  tailscale status\n"
    echo
    printf "  ${DIM}# 3. Register remote MCP in Claude Code${NC}\n"
    printf "  claude mcp remove mycelium -s user 2>/dev/null\n"
    printf "  claude mcp add -t http -s user \\\\\n"
    printf "    --header \"Authorization: Bearer %s\" \\\\\n" "$token"
    printf "    mycelium http://<tailscale-ip>:8000/mcp\n"
    echo
    printf "  ${DIM}# 4. Set up Syncthing for vault sync (optional)${NC}\n"
    printf "  brew install syncthing\n"
    printf "  ${DIM}# Open http://<tailscale-ip>:8384 and pair devices${NC}\n"
    echo
}

# ── Main ────────────────────────────────────────────────────────────
main() {
    echo
    printf "  ${BOLD}${CYAN}MYCELIUM${NC} — VPS installer\n"
    printf "  ${DIM}Deploy Data Node for remote access${NC}\n"

    local root
    root="$(detect_project_root)"
    cd "$root"

    step "1/4" "Checking dependencies"
    check_deps
    success "All dependencies satisfied"

    step "2/4" "Configure environment"
    configure_env

    step "3/4" "Deploying services"
    deploy

    step "4/4" "Done!"
    show_summary
}

main "$@"
