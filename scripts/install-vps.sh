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

spin_log() {
    # Spinner that shows live progress from a log file.
    # Usage: spin_log <pid> <logfile> <label>
    # Parses docker build steps [N/M] and shows elapsed time.
    local pid=$1 logfile="$2" label="${3:-}"
    local i=0 start cols phase line last_step=""
    start=$SECONDS
    cols=$(tput cols 2>/dev/null || echo 80)

    while kill -0 "$pid" 2>/dev/null; do
        local elapsed=$(( SECONDS - start ))
        local mins=$(( elapsed / 60 ))
        local secs=$(( elapsed % 60 ))
        local time_str
        if (( mins > 0 )); then
            time_str="${mins}m ${secs}s"
        else
            time_str="${secs}s"
        fi

        # Parse last docker build step from logfile
        phase="$label"
        line="$(grep -oE '\[([0-9]+/[0-9]+)\] [A-Z]+' "$logfile" 2>/dev/null | tail -1 || true)"
        if [[ -n "$line" ]]; then
            local step_num="${line%%]*}"
            step_num="${step_num#[}"  # "3/9"
            local cmd="${line#*] }"   # "RUN", "COPY", etc.
            case "$cmd" in
                RUN)  phase="Building [$step_num]" ;;
                COPY) phase="Copying  [$step_num]" ;;
                *)    phase="Building [$step_num]" ;;
            esac
        elif grep -q 'Creating\|Starting\|Recreating' "$logfile" 2>/dev/null; then
            local svc
            svc="$(grep -oE '(Creating|Starting|Recreating) [a-z_-]+' "$logfile" | tail -1 || true)"
            [[ -n "$svc" ]] && phase="$svc"
        fi

        # Truncate to terminal width: "  ⠋ phase... (Xs)"
        local avail=$(( cols - 12 - ${#time_str} ))
        if (( ${#phase} > avail )); then
            phase="${phase:0:$((avail-1))}…"
        fi

        printf "\r  ${DIM}%s${NC} %-${avail}s ${DIM}(%s)${NC}" \
            "${BRAILLE[$((i % ${#BRAILLE[@]}))]}" "$phase" "$time_str" >&2
        sleep 0.15
        ((i++)) || true
    done
    printf "\r\033[K" >&2
    wait "$pid" || return $?
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

# Read existing value from .env (returns empty if not found)
# Read existing value, strip inline comments (space+#) and whitespace.
# Preserves # inside values like "secret#" (no space before #).
_prev() {
    grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- \
        | sed 's/[[:space:]][[:space:]]*#.*//; s/^[[:space:]]*//; s/[[:space:]]*$//' || true
}

# Mask secret for display: show first 4 chars + "..."
_mask() { local v="$1"; [[ ${#v} -gt 4 ]] && echo "${v:0:4}..." || echo "$v"; }

configure_env() {
    # Backup existing .env, then merge: keep old values, add new keys from example
    local had_env=false
    if [[ -f "$ENV_FILE" ]]; then
        had_env=true
        local backup="$ENV_FILE.backup.$(date +%Y%m%d_%H%M%S)"
        cp "$ENV_FILE" "$backup"
        success "Existing .env backed up: $backup"
        # Merge: start from example, overlay existing values
        local tmp="${ENV_FILE}.merge"
        cp "$ENV_EXAMPLE" "$tmp"
        while IFS='=' read -r key val; do
            [[ -z "$key" || "$key" == \#* ]] && continue
            set_env_val "$key" "$val" "$tmp"
        done < "$ENV_FILE"
        mv "$tmp" "$ENV_FILE"
    else
        cp "$ENV_EXAMPLE" "$ENV_FILE"
    fi
    chmod 600 "$ENV_FILE"

    # ── Auth token ──
    local prev_token
    prev_token="$(_prev MYCELIUM_MCP__AUTH_TOKEN)"
    local token
    if [[ -n "$prev_token" ]]; then
        token="$prev_token"
        success "Auth token preserved"
    else
        token="$(generate_token)"
        set_env_val "MYCELIUM_MCP__AUTH_TOKEN" "$token"
        success "Auth token generated"
    fi
    printf "\n  ${DIM}Save this — you'll need it on your laptop:${NC}\n"
    printf "  ${BOLD}${CYAN}%s${NC}\n" "$token"

    # ── Neo4j password ──
    sep
    local prev_neo4j
    prev_neo4j="$(_prev MYCELIUM_NEO4J__PASSWORD)"
    local neo4j_pass
    neo4j_pass="$(ask_secret "Neo4j password" "${prev_neo4j:-password}")"
    [[ "${#neo4j_pass}" -lt 4 ]] && neo4j_pass="password"
    set_env_val "MYCELIUM_NEO4J__PASSWORD" "$neo4j_pass"

    # ── Embeddings ──
    sep
    local emb_mode
    emb_mode="$(select_embeddings)"
    if [[ "$emb_mode" == "1" ]]; then
        local prev_di_key api_key
        prev_di_key="$(_prev MYCELIUM_SEMANTIC__API_KEY)"
        api_key="$(ask_secret "DeepInfra API key" "${prev_di_key:+$(_mask "$prev_di_key")}")"
        # If user accepted the masked default, use the real previous key
        [[ "$api_key" == "$(_mask "$prev_di_key")" ]] && api_key="$prev_di_key"
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
    local prev_owner owner_name
    prev_owner="$(_prev MYCELIUM_OWNER__NAME)"
    owner_name="$(ask "Your name" "${prev_owner:-}")"
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
    local prev_tg_token tg_token
    prev_tg_token="$(_prev MYCELIUM_TELEGRAM__BOT_TOKEN)"
    tg_token="$(ask_secret "Bot token (or empty to skip)" "${prev_tg_token:+$(_mask "$prev_tg_token")}")"
    [[ "$tg_token" == "$(_mask "$prev_tg_token")" ]] && tg_token="$prev_tg_token"
    hint "Create via @BotFather -> /newbot"
    local stt_choice=""
    if [[ -n "$tg_token" ]]; then
        set_env_val "MYCELIUM_TELEGRAM__BOT_TOKEN" "$tg_token"
        local prev_chat_id tg_chat_id
        prev_chat_id="$(_prev MYCELIUM_TELEGRAM__OWNER_CHAT_ID)"
        tg_chat_id="$(ask "Your chat_id" "${prev_chat_id:-0}")"
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
                local prev_stt_key stt_key
                prev_stt_key="$(_prev MYCELIUM_TELEGRAM__STT_API_KEY)"
                stt_key="$(ask_secret "Deepgram API key" "${prev_stt_key:+$(_mask "$prev_stt_key")}")"
                [[ "$stt_key" == "$(_mask "$prev_stt_key")" ]] && stt_key="$prev_stt_key"
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

    # ── Graph Viewer (Sigma.js) ──
    sep
    printf "  ${BOLD}Graph viewer${NC} ${DIM}(interactive graph in browser)${NC}\n"
    local render_choice
    render_choice="$(ask "Enable graph viewer?" "y")"
    case "$render_choice" in
        [yY]*)
            set_env_val "MYCELIUM_RENDER__ENABLED" "true"
            success "Graph viewer enabled (port 9633)"
            ;;
        *)
            set_env_val "MYCELIUM_RENDER__ENABLED" "false"
            info "Graph viewer disabled"
            ;;
    esac

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

    # Build with --progress=plain so we can parse [N/M] steps
    BUILDKIT_PROGRESS=plain $compose_cmd build >>"$logfile" 2>&1 &
    if ! spin_log $! "$logfile" "Building images..."; then
        error "docker compose build failed:"
        tail -20 "$logfile" >&2; rm -f "$logfile"; exit 1
    fi
    success "Images built"

    $compose_cmd up -d >>"$logfile" 2>&1 &
    if ! spin_log $! "$logfile" "Starting containers..."; then
        error "docker compose up failed:"
        tail -10 "$logfile" >&2; rm -f "$logfile"; exit 1
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

    # Try to get Syncthing device ID (API requires auth)
    local st_id="" st_api_key=""
    local data_dir="${MYCELIUM_DATA_DIR:-$HOME/.mycelium}"
    st_api_key="$(sed -n 's/.*<apikey>\(.*\)<\/apikey>.*/\1/p' "$data_dir/syncthing/config/config.xml" 2>/dev/null || true)"
    for i in 1 2 3; do
        st_id="$(curl -sf -H "X-API-Key: $st_api_key" http://localhost:8384/rest/system/status 2>/dev/null \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["myID"])' 2>/dev/null || echo "")"
        [[ -n "$st_id" ]] && break
        sleep 2
    done

    # Get Tailscale address
    local ts_ip="" ts_host="" ts_addr
    ts_ip="$(tailscale ip -4 2>/dev/null || true)"
    ts_host="$(tailscale status --self --json 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
    ts_addr="${ts_ip:-<tailscale-ip>}"

    # Compute box width from longest content
    local token_line="Token       $token"
    local sync_line=""
    [[ -n "$st_id" ]] && sync_line="Sync ID     $st_id"
    local sync_key_line=""
    [[ -n "$st_api_key" ]] && sync_key_line="Sync Key    $st_api_key"
    local ts_line=""
    [[ -n "$ts_ip" ]] && ts_line="Tailscale   ${ts_ip}${ts_host:+  ($ts_host)}"
    local mcp_line="MCP         http://${ts_addr}:9631/mcp"

    local W=44
    for _l in "$mcp_line" "$token_line" "$sync_line" "$sync_key_line" "$ts_line"; do
        (( ${#_l} > W )) && W=${#_l}
    done

    _row() { printf "  ${DIM}│${NC} %-${W}s ${DIM}│${NC}\n" "$1"; }
    _rul() { printf "  ${DIM}%s%s%s${NC}\n" "$1" "$(printf '─%.0s' $(seq 1 $((W+2))))" "$2"; }

    printf '\n'
    _rul "┌" "┐"
    _row "MYCELIUM VPS is ready"
    _rul "├" "┤"
    _row "MCP         http://${ts_addr}:9631/mcp"
    _row "Neo4j       http://${ts_addr}:7474"
    _row "Syncthing   http://${ts_addr}:8384"
    local render_enabled
    render_enabled="$(grep '^MYCELIUM_RENDER__ENABLED=' "$ENV_FILE" | cut -d= -f2 || true)"
    [[ "$render_enabled" == "true" ]] && _row "Graph       http://${ts_addr}:9633"
    _rul "├" "┤"
    [[ -n "$ts_line" ]] && _row "$ts_line"
    _row "$token_line"
    [[ -n "$st_id" ]] && _row "$sync_line"
    [[ -n "$st_api_key" ]] && _row "$sync_key_line"
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
