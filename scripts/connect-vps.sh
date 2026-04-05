#!/usr/bin/env bash
set -euo pipefail
trap 'kill $(jobs -p) 2>/dev/null; printf "\033[?25h" >&2' EXIT INT TERM

# ── Colors & Constants ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'
DIM='\033[2m'; NC='\033[0m'

VAULT_DIR="$HOME/.mycelium/vault"
GITHUB_RAW="https://raw.githubusercontent.com/krnxiii/MYCELIUM/main"

# ── Helpers ─────────────────────────────────────────────────────────
success() { printf "  ${GREEN}✓${NC}  %s\n" "$1"; }
warn()    { printf "  ${YELLOW}!${NC}  %s\n" "$1"; }
error()   { printf "  ${RED}✗${NC}  %s\n" "$1" >&2; }
hint()    { printf "     ${DIM}%s${NC}\n" "$1"; }
sep()     { printf "\n  ${DIM}─────────────────────────────────────────────${NC}\n"; }

step() {
    printf "\n  ${BOLD}${CYAN}[%s]${NC} ${BOLD}%s${NC}\n" "$1" "$2"
    printf "  ${DIM}─────────────────────────────────────────────${NC}\n"
}

ask() {
    local prompt="$1" default="${2:-}"
    if [[ -n "$default" ]]; then
        printf "  ${BOLD}?${NC}  %s ${DIM}[%s]${NC}: " "$prompt" "$default" >&2
    else
        printf "  ${BOLD}?${NC}  %s: " "$prompt" >&2
    fi
    read -r answer
    echo "${answer:-$default}"
}

ask_secret() {
    local prompt="$1" default="${2:-}"
    if [[ -n "$default" ]]; then
        printf "  ${BOLD}?${NC}  %s ${DIM}[%s]${NC}: " "$prompt" "$default" >&2
    else
        printf "  ${BOLD}?${NC}  %s: " "$prompt" >&2
    fi
    read -rs answer
    echo >&2
    echo "${answer:-$default}"
}

# ── Braille Spinner ─────────────────────────────────────────────────
_spinner_pid=""

spin_start() {
    local msg="${1:-Working...}"
    local frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')
    (
        while true; do
            for f in "${frames[@]}"; do
                printf "\r  ${CYAN}%s${NC}  ${DIM}%s${NC}" "$f" "$msg" >&2
                sleep 0.08
            done
        done
    ) &
    _spinner_pid=$!
}

spin_stop() {
    local ok="${1:-true}" msg="${2:-}"
    if [[ -n "$_spinner_pid" ]]; then
        kill "$_spinner_pid" 2>/dev/null
        wait "$_spinner_pid" 2>/dev/null || true
        _spinner_pid=""
    fi
    printf "\r\033[K" >&2
    if [[ "$ok" == "true" ]] && [[ -n "$msg" ]]; then
        success "$msg"
    elif [[ "$ok" == "false" ]] && [[ -n "$msg" ]]; then
        warn "$msg"
    fi
}

detect_os() {
    case "$(uname -s)" in
        Darwin) echo "macos"  ;;
        Linux)  echo "linux"  ;;
        *)      echo "unknown" ;;
    esac
}

# ── Project Root (optional -- script works without repo) ────────────
detect_project_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [[ "$(basename "$dir")" == "scripts" ]] && dir="$(dirname "$dir")"
    if [[ -f "$dir/Makefile" ]] && [[ -d "$dir/.claude/skills" ]]; then
        echo "$dir"
    else
        return 1
    fi
}

# ── Step 1: Check Dependencies ──────────────────────────────────────
check_deps() {
    local all_ok=true os
    os="$(detect_os)"

    printf "\n"

    # tailscale
    if command -v tailscale &>/dev/null; then
        printf "  ${GREEN}✓${NC}  ${DIM}├─${NC} tailscale\n"
    else
        printf "  ${RED}✗${NC}  ${DIM}├─${NC} tailscale\n"
        all_ok=false
    fi

    # claude CLI
    if command -v claude &>/dev/null; then
        printf "  ${GREEN}✓${NC}  ${DIM}├─${NC} claude CLI\n"
    else
        printf "  ${YELLOW}○${NC}  ${DIM}├─${NC} claude CLI ${DIM}(optional)${NC}\n"
    fi

    # curl
    if command -v curl &>/dev/null; then
        printf "  ${GREEN}✓${NC}  ${DIM}└─${NC} curl\n"
    else
        printf "  ${RED}✗${NC}  ${DIM}└─${NC} curl\n"
        all_ok=false
    fi

    printf "\n"
    if [[ "$all_ok" == false ]]; then
        case "$os" in
            macos) hint "brew install tailscale" ;;
            linux) hint "https://tailscale.com/download/linux" ;;
        esac
        error "Fix missing dependencies and re-run."
        exit 1
    fi
}

# ── Step 2: Collect VPS Info ────────────────────────────────────────
collect_vps_info() {
    printf "\n"

    VPS_HOST="$(ask "VPS Tailscale hostname or IP")"
    hint "The hostname shown in your Tailscale admin console"
    if [[ -z "$VPS_HOST" ]]; then
        error "VPS host is required"
        exit 1
    fi

    printf "\n"
    MCP_TOKEN="$(ask_secret "MCP auth token")"
    hint "From the VPS installer output"
    if [[ -z "$MCP_TOKEN" ]]; then
        error "MCP token is required"
        exit 1
    fi

    printf "\n"
    SYNCTHING_DEVICE_ID="$(ask "VPS Syncthing Device ID (empty to skip vault sync)" "")"
    hint "Long alphanumeric string from VPS installer output"
}

# ── Step 3: Ensure Tailscale + Test Connectivity ─────────────────────
test_connectivity() {
    local os
    os="$(detect_os)"

    # Ensure Tailscale is running
    if ! tailscale status &>/dev/null; then
        warn "Tailscale not running"
        hint "Starting Tailscale..."
        case "$os" in
            macos)
                brew services start tailscale 2>/dev/null || true
                sleep 2
                ;;
            linux)
                sudo systemctl start tailscaled 2>/dev/null || true
                sleep 2
                ;;
        esac

        # Check if connected or needs login
        if ! tailscale status &>/dev/null; then
            warn "Tailscale needs authentication"
            hint "Opening login in browser..."
            if [[ "$os" == "macos" ]]; then
                tailscale login 2>/dev/null || sudo tailscale up 2>/dev/null || true
            else
                sudo tailscale up 2>/dev/null || true
            fi
            # Wait for connection
            local attempts=0
            while ! tailscale status &>/dev/null && (( attempts < 30 )); do
                sleep 1
                ((attempts++)) || true
            done
        fi

        if tailscale status &>/dev/null; then
            success "Tailscale connected"
        else
            warn "Tailscale still not connected"
        fi
    else
        success "Tailscale running"
    fi

    # Test VPS reachability
    spin_start "Reaching $VPS_HOST..."

    if ! tailscale ping "$VPS_HOST" --timeout=5s &>/dev/null 2>&1; then
        if ! curl -sf --connect-timeout 5 "http://$VPS_HOST:9631/mcp" -o /dev/null 2>/dev/null; then
            spin_stop false "Cannot reach $VPS_HOST"
            hint "Check that VPS is online and Tailscale is connected on both sides"
            local proceed
            proceed="$(ask "Continue anyway? [y/N]" "n")"
            [[ "$proceed" =~ ^[Yy] ]] || exit 1
            return
        fi
    fi

    spin_stop true "VPS reachable"

    # Test MCP endpoint
    spin_start "Checking MCP server..."
    local http_code
    http_code="$(curl -sf -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $MCP_TOKEN" \
        "http://$VPS_HOST:9631/mcp" 2>/dev/null || echo "000")"

    case "$http_code" in
        200|406) spin_stop true  "MCP server reachable" ;;
        401|403) spin_stop false; error "MCP auth failed -- check token"; exit 1 ;;
        000)     spin_stop false "MCP not responding -- VPS may still be starting" ;;
        *)       spin_stop false "MCP returned HTTP $http_code" ;;
    esac
}

# ── Step 4: Register MCP in Claude Code ─────────────────────────────
register_mcp() {
    if ! command -v claude &>/dev/null; then
        warn "Claude CLI not found -- skipping MCP registration"
        hint "Install: npm install -g @anthropic-ai/claude-code"
        hint "Then run:"
        printf "     ${DIM}claude mcp add -t http -s user \\\\${NC}\n"
        printf "     ${DIM}  --header \"Authorization: Bearer %s\" \\\\${NC}\n" "$MCP_TOKEN"
        printf "     ${DIM}  mycelium http://%s:9631/mcp${NC}\n" "$VPS_HOST"
        return
    fi

    # Remove stale registration
    claude mcp remove mycelium -s user 2>/dev/null || true

    # Register
    claude mcp add -t http -s user \
        --header "Authorization: Bearer $MCP_TOKEN" \
        mycelium "http://$VPS_HOST:9631/mcp"

    success "MCP server registered in Claude Code"

    # Gate init
    mkdir -p ~/.mycelium
    touch ~/.mycelium/.read_enabled
    success "Gate init: read=on, write=off"
}

# ── Step 4b: Install Skills ─────────────────────────────────────────
install_skills() {
    local skills=(mycelium-on mycelium-off mycelium-ingest mycelium-recall
                  mycelium-reflect mycelium-distill mycelium-discover mycelium-domain)

    # Try local repo first, fallback to GitHub download
    local root
    root="$(detect_project_root 2>/dev/null || echo "")"

    spin_start "Installing ${#skills[@]} skills..."

    for skill in "${skills[@]}"; do
        mkdir -p ~/.claude/skills/"$skill"
        if [[ -n "$root" ]] && [[ -f "$root/.claude/skills/$skill/SKILL.md" ]]; then
            cp "$root/.claude/skills/$skill/SKILL.md" ~/.claude/skills/"$skill"/SKILL.md
        else
            curl -fsSL "$GITHUB_RAW/.claude/skills/$skill/SKILL.md" \
                -o ~/.claude/skills/"$skill"/SKILL.md 2>/dev/null \
                || { spin_stop false "Failed to download skill: $skill"; return; }
        fi
    done

    spin_stop true "Skills installed (${#skills[@]})"

    # Access rules
    local marker="## MYCELIUM MCP Access Control"
    local target="$HOME/.claude/CLAUDE.md"
    if [[ -f "$target" ]] && grep -qF "$marker" "$target"; then
        success "Access rules already present"
    else
        mkdir -p "$(dirname "$target")"
        cat >> "$target" <<'RULES'

## MYCELIUM MCP Access Control
- NEVER create `~/.mycelium/.write_enabled` yourself
- NEVER delete `~/.mycelium/.read_enabled` yourself
- Use `/mycelium-on` and `/mycelium-off` skills to toggle access
- If a tool returns "disabled", tell the user to run the skill
RULES
        success "Access rules added to ~/.claude/CLAUDE.md"
    fi
}

# ── Step 5: Setup Syncthing Vault Sync ──────────────────────────────
setup_syncthing() {
    if [[ -z "$SYNCTHING_DEVICE_ID" ]]; then
        hint "Vault sync skipped (no Device ID provided)"
        return
    fi

    local os
    os="$(detect_os)"

    # Install Syncthing
    if ! command -v syncthing &>/dev/null; then
        spin_start "Installing Syncthing..."
        case "$os" in
            macos)
                if command -v brew &>/dev/null; then
                    brew install syncthing >/dev/null 2>&1
                else
                    spin_stop false
                    error "brew not found -- install Syncthing manually: https://syncthing.net"
                    return
                fi
                ;;
            linux)
                if command -v apt-get &>/dev/null; then
                    sudo apt-get install -y syncthing >/dev/null 2>&1
                else
                    spin_stop false
                    error "Install Syncthing manually: https://syncthing.net"
                    return
                fi
                ;;
        esac
        spin_stop true "Syncthing installed"
    else
        success "Syncthing found"
    fi

    # Start Syncthing service
    case "$os" in
        macos)
            if ! brew services list 2>/dev/null | grep syncthing | grep -q started; then
                brew services start syncthing 2>/dev/null || true
                spin_start "Waiting for Syncthing to start..."
                sleep 3
                spin_stop true "Syncthing started"
            fi
            ;;
        linux)
            if ! systemctl --user is-active syncthing &>/dev/null; then
                systemctl --user enable --now syncthing 2>/dev/null || true
                sleep 3
            fi
            ;;
    esac

    # Create vault directory
    mkdir -p "$VAULT_DIR"

    # Find Syncthing API key
    local config_file=""
    case "$os" in
        macos)  config_file="$HOME/Library/Application Support/Syncthing/config.xml" ;;
        linux)  config_file="$HOME/.local/state/syncthing/config.xml"
                [[ -f "$config_file" ]] || config_file="$HOME/.config/syncthing/config.xml" ;;
    esac

    if [[ ! -f "$config_file" ]]; then
        warn "Syncthing config not found"
        hint "Syncthing may still be starting. Manual pairing needed."
        _show_manual_syncthing_instructions
        return
    fi

    local api_key
    api_key="$(sed -n 's/.*<apikey>\(.*\)<\/apikey>.*/\1/p' "$config_file" 2>/dev/null \
        || echo "")"

    if [[ -z "$api_key" ]]; then
        warn "Could not extract Syncthing API key"
        _show_manual_syncthing_instructions
        return
    fi

    local st_api="http://localhost:8384/rest"
    local auth_header="X-API-Key: $api_key"

    # Add VPS device
    spin_start "Adding VPS device to Syncthing..."
    local device_config
    device_config=$(cat <<DEVICE_JSON
{
    "deviceID": "$SYNCTHING_DEVICE_ID",
    "name": "mycelium-vps",
    "addresses": ["tcp://$VPS_HOST:22000"],
    "autoAcceptFolders": true
}
DEVICE_JSON
    )

    # Get current config
    local config
    config="$(curl -sf -H "$auth_header" "$st_api/config" 2>/dev/null || echo "")"
    if [[ -z "$config" ]]; then
        spin_stop false "Cannot reach Syncthing API"
        _show_manual_syncthing_instructions
        return
    fi

    # Check if device already exists
    if echo "$config" | python3 -c "
import json,sys
cfg = json.load(sys.stdin)
ids = [d['deviceID'] for d in cfg.get('devices',[])]
sys.exit(0 if '$SYNCTHING_DEVICE_ID' in ids else 1)
" 2>/dev/null; then
        spin_stop true "VPS device already added"
    else
        # Add device via config patch
        echo "$config" | python3 -c "
import json,sys
cfg = json.load(sys.stdin)
cfg['devices'].append({
    'deviceID': '$SYNCTHING_DEVICE_ID',
    'name': 'mycelium-vps',
    'addresses': ['tcp://$VPS_HOST:22000'],
    'autoAcceptFolders': True,
    'compression': 'metadata',
})
json.dump(cfg, sys.stdout)
" 2>/dev/null | curl -sf -X PUT -H "$auth_header" \
            -H "Content-Type: application/json" \
            -d @- "$st_api/config" >/dev/null 2>&1 \
        && spin_stop true "VPS device added" \
        || { spin_stop false "Failed to add device via API"; _show_manual_syncthing_instructions; return; }
    fi

    # Add vault folder
    spin_start "Configuring vault folder..."

    # Re-read config (may have changed)
    config="$(curl -sf -H "$auth_header" "$st_api/config" 2>/dev/null)"

    if echo "$config" | python3 -c "
import json,sys
cfg = json.load(sys.stdin)
ids = [f['id'] for f in cfg.get('folders',[])]
sys.exit(0 if 'mycelium-vault' in ids else 1)
" 2>/dev/null; then
        spin_stop true "Vault folder already configured"
    else
        # Get local device ID
        local local_id
        local_id="$(curl -sf -H "$auth_header" "$st_api/system/status" 2>/dev/null \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["myID"])' 2>/dev/null || echo "")"

        echo "$config" | python3 -c "
import json,sys
cfg = json.load(sys.stdin)
cfg['folders'].append({
    'id': 'mycelium-vault',
    'label': 'MYCELIUM Vault',
    'path': '$VAULT_DIR',
    'type': 'sendreceive',
    'devices': [
        {'deviceID': '$local_id'},
        {'deviceID': '$SYNCTHING_DEVICE_ID'},
    ],
    'rescanIntervalS': 60,
    'fsWatcherEnabled': True,
})
json.dump(cfg, sys.stdout)
" 2>/dev/null | curl -sf -X PUT -H "$auth_header" \
            -H "Content-Type: application/json" \
            -d @- "$st_api/config" >/dev/null 2>&1 \
        && spin_stop true "Vault folder configured: $VAULT_DIR" \
        || { spin_stop false "Failed to add folder via API"; _show_manual_syncthing_instructions; return; }
    fi

    printf "\n"
    hint "Vault sync will start once VPS accepts the connection."
    hint "If auto-accept is off on VPS, open http://$VPS_HOST:8384 to approve."
}

_show_manual_syncthing_instructions() {
    printf "\n"
    hint "Manual Syncthing setup:"
    hint "  1. Open http://localhost:8384 (local Syncthing UI)"
    hint "  2. Add Remote Device -> paste VPS Device ID"
    hint "  3. Set address: tcp://$VPS_HOST:22000"
    hint "  4. Add Folder -> ID: mycelium-vault -> Path: $VAULT_DIR"
    hint "  5. Share folder with the VPS device"
    printf "\n"
}

# ── Summary ─────────────────────────────────────────────────────────
show_summary() {
    local w=51

    printf "\n"
    printf "  ${CYAN}%s${NC}\n" "$(printf '%.0s─' $(seq 1 $w))"
    printf "  ${CYAN}│${NC}  ${BOLD}${GREEN}Connected to MYCELIUM VPS${NC}%-$((w - 29))s${CYAN}│${NC}\n" ""
    printf "  ${CYAN}%s${NC}\n" "$(printf '%.0s─' $(seq 1 $w))"

    printf "\n"
    printf "  ${BOLD}${CYAN}Services${NC}\n"
    printf "  ${DIM}├─${NC} MCP Server     ${DIM}http://%s:9631/mcp${NC}\n" "$VPS_HOST"
    printf "  ${DIM}├─${NC} Neo4j Browser  ${DIM}http://%s:7474${NC}\n" "$VPS_HOST"
    printf "  ${DIM}├─${NC} Syncthing UI   ${DIM}http://%s:8384${NC}\n" "$VPS_HOST"
    printf "  ${DIM}└─${NC} Vault (local)  ${DIM}%s${NC}\n" "$VAULT_DIR"

    printf "\n"
    printf "  ${BOLD}${CYAN}Quick start${NC}\n"
    printf "  ${DIM}├─${NC} claude              ${DIM}MYCELIUM tools available${NC}\n"
    printf "  ${DIM}├─${NC} /mycelium-on        ${DIM}enable write access${NC}\n"
    printf "  ${DIM}└─${NC} /mycelium-recall X  ${DIM}search knowledge graph${NC}\n"

    printf "\n"
    printf "  ${CYAN}%s${NC}\n" "$(printf '%.0s─' $(seq 1 $w))"
    printf "\n"
}

# ── Main ────────────────────────────────────────────────────────────
main() {
    printf "\n"
    printf "  ${BOLD}${CYAN}MYCELIUM${NC} ${DIM}/${NC} connect to VPS\n"
    printf "  ${DIM}Set up laptop to use remote Data Node${NC}\n"

    step "1/5" "Checking dependencies"
    check_deps

    step "2/5" "VPS connection info"
    collect_vps_info

    step "3/5" "Testing connectivity"
    test_connectivity

    step "4/5" "Setting up Claude Code"
    register_mcp
    install_skills

    step "5/5" "Setting up vault sync"
    setup_syncthing

    show_summary
}

main "$@"
