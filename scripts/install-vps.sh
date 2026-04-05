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

# ── Claude Code CLI (for LLM extraction) ─────────────────────────
setup_claude_cli() {
    local claude_bin=""

    # Find claude CLI
    for p in claude ~/.local/bin/claude /usr/local/bin/claude; do
        if command -v "$p" &>/dev/null || [[ -x "$p" ]]; then
            claude_bin="$p"
            break
        fi
    done

    if [[ -z "$claude_bin" ]]; then
        echo
        info "Claude Code CLI is needed for knowledge extraction."
        info "Without it, /capture saves signals but won't extract neurons."
        echo
        local install_choice
        install_choice="$(ask "Install Claude Code CLI? [y/N]" "n")"
        if [[ "$install_choice" =~ ^[Yy] ]]; then
            if ! command -v npm &>/dev/null; then
                warn "npm not found. Install Node.js first: https://nodejs.org/"
                warn "Then run: npm install -g @anthropic-ai/claude-code && claude login"
                return
            fi
            info "Installing Claude Code CLI..."
            npm install -g @anthropic-ai/claude-code
            claude_bin="claude"
        else
            warn "Skipped — extraction will be unavailable. Install later:"
            printf "  ${DIM}npm install -g @anthropic-ai/claude-code && claude login${NC}\n"
            return
        fi
    else
        success "Claude Code CLI found: $claude_bin"
    fi

    # Check if authenticated
    if [[ ! -d "$HOME/.claude" ]] || ! "$claude_bin" -p "echo ok" &>/dev/null 2>&1; then
        echo
        info "Claude Code needs authentication (opens URL in browser)."
        local login_choice
        login_choice="$(ask "Login now? [Y/n]" "y")"
        if [[ "$login_choice" =~ ^[Yy]?$ ]]; then
            "$claude_bin" login
            if [[ $? -eq 0 ]]; then
                success "Claude Code authenticated"
            else
                warn "Login failed or cancelled. Run '$claude_bin login' later."
            fi
        else
            warn "Skipped — run '$claude_bin login' before using extraction."
        fi
    else
        success "Claude Code authenticated"
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

    # ── Telegram bot ──
    echo
    info "Telegram bot gives you mobile access to the graph."
    info "Create a bot: https://t.me/BotFather → /newbot"
    local tg_token
    tg_token="$(ask_secret "Telegram bot token (or empty to skip)")"
    if [[ -n "$tg_token" ]]; then
        set_env_val "MYCELIUM_TELEGRAM__BOT_TOKEN" "$tg_token"
        local tg_chat_id
        info "To find your chat_id: send /start to @userinfobot on Telegram"
        tg_chat_id="$(ask "Your Telegram chat_id" "0")"
        set_env_val "MYCELIUM_TELEGRAM__OWNER_CHAT_ID" "$tg_chat_id"
    else
        warn "Telegram skipped — add MYCELIUM_TELEGRAM__BOT_TOKEN to .env later"
    fi

    # ── STT (voice input) ──
    if [[ -n "$tg_token" ]]; then
        echo
        info "Voice input: transcribe voice messages in Telegram."
        printf "  ${BOLD}1)${NC}  Deepgram API   — cloud, fast, accurate (needs API key)\n" >&2
        printf "  ${BOLD}2)${NC}  Whisper local  — runs on device, no external API (downloads ~1 GB model)\n" >&2
        printf "  ${BOLD}3)${NC}  None           — no voice input\n" >&2
        echo >&2
        local stt_choice
        stt_choice="$(ask "STT provider [1/2/3]" "3")"
        case "$stt_choice" in
            1)
                set_env_val "MYCELIUM_TELEGRAM__STT_PROVIDER" "deepgram"
                info "Get API key at: https://console.deepgram.com"
                local stt_key
                stt_key="$(ask_secret "Deepgram API key")"
                if [[ -n "$stt_key" ]]; then
                    set_env_val "MYCELIUM_TELEGRAM__STT_API_KEY" "$stt_key"
                    success "Deepgram configured"
                else
                    warn "No key — set MYCELIUM_TELEGRAM__STT_API_KEY in .env later"
                fi
                ;;
            2)
                set_env_val "MYCELIUM_TELEGRAM__STT_PROVIDER" "whisper-local"
                success "Whisper local configured"
                ;;
            *)
                set_env_val "MYCELIUM_TELEGRAM__STT_PROVIDER" "none"
                info "Voice input disabled"
                ;;
        esac
    fi

    # Store flags for compose profiles
    echo "MYCELIUM_VPS_EMB_MODE=$emb_mode" >> "$ENV_FILE"
    [[ -n "$tg_token" ]] && echo "MYCELIUM_VPS_TELEGRAM=1" >> "$ENV_FILE"
    [[ "${stt_choice:-}" == "2" ]] && echo "MYCELIUM_VPS_WHISPER=1" >> "$ENV_FILE"
    success ".env configured"
}

# ── Deploy ──────────────────────────────────────────────────────────
deploy() {
    local emb_mode tg_mode whisper_mode
    emb_mode="$(grep '^MYCELIUM_VPS_EMB_MODE=' "$ENV_FILE" | cut -d= -f2)"
    tg_mode="$(grep '^MYCELIUM_VPS_TELEGRAM=' "$ENV_FILE" | cut -d= -f2)"
    whisper_mode="$(grep '^MYCELIUM_VPS_WHISPER=' "$ENV_FILE" | cut -d= -f2)"

    # Create directories for bind mounts
    local data_dir="${MYCELIUM_DATA_DIR:-$HOME/.mycelium}"
    mkdir -p "$data_dir/syncthing" "$data_dir/vault"

    local compose_cmd="docker compose -f $COMPOSE_FILE"
    [[ "$emb_mode" == "2" ]]    && compose_cmd="$compose_cmd --profile full"
    [[ "$tg_mode" == "1" ]]     && compose_cmd="$compose_cmd --profile telegram"
    [[ "$whisper_mode" == "1" ]] && compose_cmd="$compose_cmd --profile voice-whisper"

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

    # Try to get Syncthing device ID
    local st_id=""
    for i in 1 2 3; do
        st_id="$(curl -sf http://localhost:8384/rest/system/status 2>/dev/null \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["myID"])' 2>/dev/null || echo "")"
        [[ -n "$st_id" ]] && break
        sleep 2
    done

    echo
    printf "  ${BOLD}${GREEN}MYCELIUM VPS is ready!${NC}\n"
    echo
    printf "  %-22s %s\n" "Service" "Access"
    printf "  %-22s %s\n" "──────────────────────" "──────────────────────────────"
    printf "  %-22s %s\n" "MCP (HTTP)"            "http://<tailscale-ip>:9631/mcp"
    printf "  %-22s %s\n" "Neo4j Browser"         "http://<tailscale-ip>:7474"
    printf "  %-22s %s\n" "Syncthing UI"          "http://<tailscale-ip>:8384"

    if [[ -n "$st_id" ]]; then
        echo
        printf "  ${BOLD}Syncthing Device ID (for pairing):${NC}\n"
        printf "  ${CYAN}%s${NC}\n" "$st_id"
    fi

    echo
    printf "  ${BOLD}On your laptop — one command:${NC}\n"
    echo
    printf "  ${CYAN}bash <(curl -fsSL https://raw.githubusercontent.com/krnxiii/MYCELIUM/main/scripts/connect-vps.sh)${NC}\n"
    echo
    printf "  ${DIM}It will ask for:${NC}\n"
    printf "  ${DIM}  - VPS hostname/IP (Tailscale)${NC}\n"
    printf "  ${DIM}  - MCP token: %s${NC}\n" "$token"
    if [[ -n "$st_id" ]]; then
        printf "  ${DIM}  - Syncthing ID: %s${NC}\n" "$st_id"
    fi
    echo
    printf "  ${DIM}Handles: MCP registration, skills, vault sync — no repo needed.${NC}\n"
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

    step "1/5" "Checking dependencies"
    check_deps
    success "All dependencies satisfied"

    step "2/5" "Claude Code CLI"
    setup_claude_cli

    step "3/5" "Configure environment"
    configure_env

    step "4/5" "Deploying services"
    deploy

    step "5/5" "Done!"
    show_summary
}

main "$@"
