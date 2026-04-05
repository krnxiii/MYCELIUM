#!/usr/bin/env bash
set -euo pipefail
trap 'kill $(jobs -p) 2>/dev/null; printf "\033[?25h" >&2' EXIT INT TERM

# ── Colors & Constants ──────────────────────────────────────────────
CYAN='\033[0;36m'; BCYAN='\033[1;36m'; GREEN='\033[0;32m'; BGREEN='\033[1;32m'
YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

COMPOSE_FILE="docker-compose.vps.yml"
ENV_FILE=".env"
ENV_EXAMPLE=".env.example"

BRAILLE=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')

# ── Helpers ─────────────────────────────────────────────────────────
success() { printf "  ${GREEN}✓${NC}  %s\n" "$1"; }
warn()    { printf "  ${YELLOW}!${NC}  %s\n" "$1"; }
error()   { printf "  ${RED}✗${NC}  %s\n" "$1" >&2; }
hint()    { printf "    ${DIM}%s${NC}\n" "$1" >&2; }

step() {
    printf "\n${BOLD}${BCYAN}[%s]${NC} ${BOLD}%s${NC}\n" "$1" "$2"
}

sep() {
    printf "${DIM}  ─────────────────────────────────────────────${NC}\n"
}

ask() {
    local prompt="$1" default="${2:-}"
    if [[ -n "$default" ]]; then
        printf "  ${BOLD}>${NC} %s ${DIM}[%s]${NC}: " "$prompt" "$default" >&2
    else
        printf "  ${BOLD}>${NC} %s: " "$prompt" >&2
    fi
    read -r answer
    printf '%s' "${answer:-$default}"
}

ask_secret() {
    local prompt="$1" default="${2:-}"
    if [[ -n "$default" ]]; then
        printf "  ${BOLD}>${NC} %s ${DIM}[%s]${NC}: " "$prompt" "$default" >&2
    else
        printf "  ${BOLD}>${NC} %s: " "$prompt" >&2
    fi
    read -rs answer
    printf '\n' >&2
    printf '%s' "${answer:-$default}"
}

spin() {
    local pid=$1 label="${2:-}"
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r  ${DIM}%s${NC} %s" "${BRAILLE[$((i % ${#BRAILLE[@]}))]}" "$label" >&2
        sleep 0.1
        ((i++)) || true  # ((0)) returns 1 in bash 5.x, must not trigger set -e
    done
    printf "\r\033[K" >&2
    wait "$pid" || return $?  # propagate exit code without set -e killing us
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
    printf '%s' "$dir"
}

# ── Dependency Checks ──────────────────────────────────────────────
check_deps() {
    local all_ok=true

    for cmd in docker make curl; do
        if command -v "$cmd" &>/dev/null; then
            printf "  ${DIM}├─${NC} %-18s ${GREEN}found${NC}\n" "$cmd"
        else
            printf "  ${DIM}├─${NC} %-18s ${RED}missing${NC}\n" "$cmd"
            all_ok=false
        fi
    done

    if docker compose version &>/dev/null; then
        printf "  ${DIM}├─${NC} %-18s ${GREEN}found${NC}\n" "docker compose"
    else
        printf "  ${DIM}├─${NC} %-18s ${RED}missing${NC}\n" "docker compose"
        all_ok=false
    fi

    if docker info &>/dev/null; then
        printf "  ${DIM}└─${NC} %-18s ${GREEN}running${NC}\n" "docker daemon"
    else
        printf "  ${DIM}└─${NC} %-18s ${RED}not running${NC}\n" "docker daemon"
        all_ok=false
    fi

    if [[ "$all_ok" == false ]]; then
        printf '\n'
        error "Fix missing dependencies and re-run."
        exit 1
    fi
}

# ── Claude Code CLI (for LLM extraction) ─────────────────────────
setup_claude_cli() {
    # Check if already installed (any method: curl, npm, brew)
    if command -v claude &>/dev/null; then
        success "Claude Code CLI found: $(command -v claude)"
    else
        printf '\n'
        warn "Claude Code CLI not found"
        hint "Needed for knowledge extraction. Without it, signals are saved but neurons won't be extracted."
        printf '\n'
        local install_choice
        install_choice="$(ask "Install Claude Code CLI? [Y/n]" "y")"
        if [[ "$install_choice" =~ ^[Yy]?$ ]]; then
            printf '\n'
            (curl -fsSL https://claude.ai/install.sh | bash) >/dev/null 2>&1 &
            if ! spin $! "Installing Claude Code CLI..."; then
                warn "Installation failed. Install later: curl -fsSL https://claude.ai/install.sh | bash"
                return
            fi
            # Pick up new binary in current session
            export PATH="$HOME/.local/bin:$HOME/.claude/bin:$PATH"
            if ! command -v claude &>/dev/null; then
                warn "Installation finished but 'claude' not found in PATH."
                warn "Restart your shell and run 'claude login'."
                return
            fi
            success "Claude Code CLI installed"
        else
            warn "Skipped — install later: curl -fsSL https://claude.ai/install.sh | bash"
            return
        fi
    fi

    # Check auth (auth status is instant, unlike claude -p which spawns a full process)
    if claude auth status 2>/dev/null | grep -q '"loggedIn": true'; then
        success "Claude Code authenticated"
    else
        printf '\n'
        warn "Claude Code needs authentication"
        local login_choice
        login_choice="$(ask "Login now? [Y/n]" "y")"
        if [[ "$login_choice" =~ ^[Yy]?$ ]]; then
            if claude auth login; then
                success "Claude Code authenticated"
            else
                warn "Login failed or cancelled. Run 'claude auth login' later."
            fi
        else
            warn "Skipped — run 'claude auth login' before using extraction."
        fi
    fi
}

# ── Generate Auth Token ────────────────────────────────────────────
generate_token() {
    python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null \
        || openssl rand -base64 32 | tr -d '/+=' | head -c 43
}

# ── Embeddings Mode ────────────────────────────────────────────────
select_embeddings() {
    printf '\n' >&2
    printf "  ${BOLD}1)${NC}  DeepInfra API  ${DIM}— no local GPU needed${NC}\n" >&2
    printf "  ${BOLD}2)${NC}  Local TEI      ${DIM}— BGE-M3 on VPS CPU (~2 GB download)${NC}\n" >&2
    printf '\n' >&2
    while true; do
        local choice
        choice="$(ask "Embeddings mode [1/2]" "1")"
        case "$choice" in
            1|2) printf '%s' "$choice"; return ;;
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
    printf "\n  ${DIM}Save this — you'll need it on your laptop:${NC}\n"
    printf "  ${BOLD}${CYAN}%s${NC}\n" "$token"

    # ── Neo4j password ──
    sep
    local neo4j_pass
    neo4j_pass="$(ask_secret "Neo4j password" "password")"
    [[ "${#neo4j_pass}" -lt 4 ]] && neo4j_pass="password"
    set_env_val "MYCELIUM_NEO4J__PASSWORD" "$neo4j_pass"

    # ── Embeddings ──
    sep
    local emb_mode
    emb_mode="$(select_embeddings)"
    if [[ "$emb_mode" == "1" ]]; then
        local api_key
        api_key="$(ask_secret "DeepInfra API key")"
        hint "Free key: https://deepinfra.com/dash/api_keys"
        if [[ -z "$api_key" ]]; then
            warn "No key — set MYCELIUM_SEMANTIC__API_KEY in .env later"
        else
            set_env_val "MYCELIUM_SEMANTIC__API_KEY" "$api_key"
        fi
    else
        set_env_val "MYCELIUM_SEMANTIC__API_BASE_URL" "http://embeddings:8080"
        set_env_val "MYCELIUM_SEMANTIC__API_KEY" ""
    fi

    # ── Owner ──
    sep
    local owner_name
    owner_name="$(ask "Your name" "")"
    hint "Optional. Used for graph ownership metadata."
    [[ -n "$owner_name" ]] && set_env_val "MYCELIUM_OWNER__NAME" "$owner_name"

    # ── Tailscale ──
    sep
    printf "  ${BCYAN}Tailscale${NC}  ${DIM}secure tunnel between VPS and laptop${NC}\n"
    local ts_key
    while true; do
        ts_key="$(ask_secret "Tailscale auth key")"
        hint "login.tailscale.com -> Settings -> Keys -> Generate auth key"
        if [[ -n "$ts_key" ]]; then
            set_env_val "TAILSCALE_AUTHKEY" "$ts_key"
            break
        fi
        warn "Tailscale is required. Your laptop needs it to connect."
        local skip
        skip="$(ask "Skip anyway? (system won't be reachable) [y/N]" "n")"
        if [[ "$skip" =~ ^[Yy] ]]; then
            warn "Skipped — add TAILSCALE_AUTHKEY to .env and restart later"
            break
        fi
    done

    # ── Telegram block (bot + chat_id + STT grouped) ──
    sep
    printf "  ${BCYAN}Telegram${NC}  ${DIM}mobile access to the graph${NC}\n"
    local tg_token
    tg_token="$(ask_secret "Bot token (or empty to skip)")"
    hint "Create via @BotFather -> /newbot"
    local stt_choice=""
    if [[ -n "$tg_token" ]]; then
        set_env_val "MYCELIUM_TELEGRAM__BOT_TOKEN" "$tg_token"
        local tg_chat_id
        tg_chat_id="$(ask "Your chat_id" "0")"
        hint "Send /start to @userinfobot to find it"
        set_env_val "MYCELIUM_TELEGRAM__OWNER_CHAT_ID" "$tg_chat_id"

        printf '\n'
        printf "    ${DIM}Voice input:${NC}\n"
        printf "    ${BOLD}1)${NC}  Deepgram   ${DIM}— cloud, fast, accurate${NC}\n"
        printf "    ${BOLD}2)${NC}  Whisper    ${DIM}— local, no API (~1 GB model)${NC}\n"
        printf "    ${BOLD}3)${NC}  None\n"
        stt_choice="$(ask "STT provider [1/2/3]" "3")"
        case "$stt_choice" in
            1)
                set_env_val "MYCELIUM_TELEGRAM__STT_PROVIDER" "deepgram"
                local stt_key
                stt_key="$(ask_secret "Deepgram API key")"
                hint "https://console.deepgram.com"
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
                ;;
        esac
    else
        warn "Telegram skipped — add MYCELIUM_TELEGRAM__BOT_TOKEN to .env later"
    fi

    # Store flags for compose profiles
    printf 'MYCELIUM_VPS_EMB_MODE=%s\n' "$emb_mode" >> "$ENV_FILE"
    [[ -n "$tg_token" ]] && printf 'MYCELIUM_VPS_TELEGRAM=1\n' >> "$ENV_FILE"
    [[ "${stt_choice:-}" == "2" ]] && printf 'MYCELIUM_VPS_WHISPER=1\n' >> "$ENV_FILE"
    printf '\n'
    success ".env configured"
}

# ── Deploy ──────────────────────────────────────────────────────────
deploy() {
    local emb_mode tg_mode whisper_mode
    emb_mode="$(grep '^MYCELIUM_VPS_EMB_MODE=' "$ENV_FILE" | cut -d= -f2 || true)"
    tg_mode="$(grep '^MYCELIUM_VPS_TELEGRAM=' "$ENV_FILE" | cut -d= -f2 || true)"
    whisper_mode="$(grep '^MYCELIUM_VPS_WHISPER=' "$ENV_FILE" | cut -d= -f2 || true)"

    # Create directories for bind mounts
    local data_dir="${MYCELIUM_DATA_DIR:-$HOME/.mycelium}"
    mkdir -p "$data_dir/syncthing" "$data_dir/vault"

    local compose_cmd="docker compose -f $COMPOSE_FILE"
    [[ "$emb_mode" == "2" ]]    && compose_cmd="$compose_cmd --profile full"
    [[ "$tg_mode" == "1" ]]     && compose_cmd="$compose_cmd --profile telegram"
    [[ "$whisper_mode" == "1" ]] && compose_cmd="$compose_cmd --profile voice-whisper"

    local logfile
    logfile="$(mktemp)"

    $compose_cmd pull >>"$logfile" 2>&1 &
    if ! spin $! "Pulling images..."; then
        error "docker compose pull failed:"; tail -5 "$logfile" >&2; rm -f "$logfile"; exit 1
    fi
    success "Images pulled"

    $compose_cmd up -d --build >>"$logfile" 2>&1 &
    if ! spin $! "Building & starting MYCELIUM..."; then
        error "docker compose up failed:"; tail -10 "$logfile" >&2; rm -f "$logfile"; exit 1
    fi
    success "Containers started"

    bash scripts/wait-healthy.sh mycelium-neo4j mycelium-app >>"$logfile" 2>&1 &
    if ! spin $! "Waiting for healthy services..."; then
        error "Health check failed:"; tail -5 "$logfile" >&2; rm -f "$logfile"; exit 1
    fi
    success "All services healthy"
    rm -f "$logfile"
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

    # Compute box width from longest content
    local token_line="Token       $token"
    local sync_line=""
    [[ -n "$st_id" ]] && sync_line="Sync ID     $st_id"
    local W=44  # min: "MCP         http://<tailscale-ip>:9631/mcp"
    (( ${#token_line} > W )) && W=${#token_line}
    (( ${#sync_line}  > W )) && W=${#sync_line}

    _row() { printf "  ${DIM}│${NC} %-${W}s ${DIM}│${NC}\n" "$1"; }
    _rul() { printf "  ${DIM}%s%s%s${NC}\n" "$1" "$(printf '─%.0s' $(seq 1 $((W+2))))" "$2"; }

    printf '\n'
    _rul "┌" "┐"
    _row "MYCELIUM VPS is ready"
    _rul "├" "┤"
    _row "MCP         http://<tailscale-ip>:9631/mcp"
    _row "Neo4j       http://<tailscale-ip>:7474"
    _row "Syncthing   http://<tailscale-ip>:8384"
    _rul "├" "┤"
    _row "$token_line"
    [[ -n "$st_id" ]] && _row "$sync_line"
    _rul "└" "┘"

    printf '\n'
    printf "  ${BOLD}On your laptop:${NC}\n"
    printf "  ${CYAN}git clone https://github.com/krnxiii/MYCELIUM && cd MYCELIUM${NC}\n"
    printf "  ${CYAN}bash scripts/install.sh${NC}  ${DIM}-> choose \"4) Connect to VPS\"${NC}\n"
    printf '\n'
    printf "  ${DIM}Or without cloning:${NC}\n"
    printf "  ${DIM}bash <(curl -fsSL https://raw.githubusercontent.com/krnxiii/MYCELIUM/main/scripts/connect-vps.sh)${NC}\n"
    printf '\n'
}

# ── Main ────────────────────────────────────────────────────────────
main() {
    printf '\n'
    printf "  ${BCYAN}MYCELIUM${NC}  ${DIM}VPS installer${NC}\n"

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

    step "5/5" "Done"
    show_summary
}

main "$@"
