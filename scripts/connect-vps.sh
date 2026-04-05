#!/usr/bin/env bash
set -euo pipefail

# ── Colors & Constants ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'
DIM='\033[2m'; NC='\033[0m'

VAULT_DIR="$HOME/.mycelium/vault"

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

detect_os() {
    case "$(uname -s)" in
        Darwin) echo "macos"  ;;
        Linux)  echo "linux"  ;;
        *)      echo "unknown" ;;
    esac
}

# ── Project Root ────────────────────────────────────────────────────
detect_project_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [[ "$(basename "$dir")" == "scripts" ]] && dir="$(dirname "$dir")"
    echo "$dir"
}

# ── Step 1: Check Dependencies ──────────────────────────────────────
check_deps() {
    local all_ok=true os
    os="$(detect_os)"

    printf "\n  %-22s %s\n" "Dependency" "Status"
    printf "  %-22s %s\n" "──────────────────────" "──────"

    if command -v tailscale &>/dev/null; then
        printf "  %-22s ${GREEN}✓ found${NC}\n" "tailscale"
    else
        printf "  %-22s ${RED}✗ missing${NC}\n" "tailscale"
        all_ok=false
    fi

    if command -v claude &>/dev/null; then
        printf "  %-22s ${GREEN}✓ found${NC}\n" "claude CLI"
    else
        printf "  %-22s ${YELLOW}○ optional${NC}\n" "claude CLI"
    fi

    if command -v curl &>/dev/null; then
        printf "  %-22s ${GREEN}✓ found${NC}\n" "curl"
    else
        printf "  %-22s ${RED}✗ missing${NC}\n" "curl"
        all_ok=false
    fi

    echo
    if [[ "$all_ok" == false ]]; then
        case "$os" in
            macos)
                info "Install: brew install tailscale"
                ;;
            linux)
                info "Install: https://tailscale.com/download/linux"
                ;;
        esac
        error "Fix missing dependencies and re-run."
        exit 1
    fi
}

# ── Step 2: Collect VPS Info ─────────────────────────────────────────
collect_vps_info() {
    echo
    info "You'll need these values from the VPS installer output:"
    info "  - VPS Tailscale hostname or IP"
    info "  - MCP auth token"
    info "  - Syncthing Device ID (for vault sync)"
    echo

    VPS_HOST="$(ask "VPS Tailscale hostname or IP")"
    if [[ -z "$VPS_HOST" ]]; then
        error "VPS host is required"
        exit 1
    fi

    MCP_TOKEN="$(ask_secret "MCP auth token")"
    if [[ -z "$MCP_TOKEN" ]]; then
        error "MCP token is required"
        exit 1
    fi

    SYNCTHING_DEVICE_ID="$(ask "VPS Syncthing Device ID (or empty to skip vault sync)" "")"
}

# ── Step 3: Test Connectivity ────────────────────────────────────────
test_connectivity() {
    info "Testing connection to $VPS_HOST..."

    # Ping via Tailscale
    if ! tailscale ping "$VPS_HOST" --timeout=5s &>/dev/null 2>&1; then
        # Fallback: try direct curl
        if ! curl -sf --connect-timeout 5 "http://$VPS_HOST:9631/mcp" -o /dev/null 2>/dev/null; then
            warn "Cannot reach $VPS_HOST — check Tailscale is connected"
            local proceed
            proceed="$(ask "Continue anyway? [y/N]" "n")"
            [[ "$proceed" =~ ^[Yy] ]] || exit 1
            return
        fi
    fi

    # Test MCP endpoint
    local http_code
    http_code="$(curl -sf -o /dev/null -w '%{http_code}' \
        -H "Authorization: Bearer $MCP_TOKEN" \
        "http://$VPS_HOST:9631/mcp" 2>/dev/null || echo "000")"

    case "$http_code" in
        200|406) success "MCP server reachable (HTTP $http_code)" ;;
        401|403) error "MCP auth failed — check token"; exit 1 ;;
        000)     warn "MCP server not responding — may not be started yet" ;;
        *)       warn "MCP returned HTTP $http_code" ;;
    esac
}

# ── Step 4: Register MCP in Claude Code ──────────────────────────────
register_mcp() {
    if ! command -v claude &>/dev/null; then
        warn "Claude CLI not found — skipping MCP registration"
        info "Install: npm install -g @anthropic-ai/claude-code"
        info "Then run:"
        printf "  claude mcp add -t http -s user \\\\\n"
        printf "    --header \"Authorization: Bearer %s\" \\\\\n" "$MCP_TOKEN"
        printf "    mycelium http://%s:9631/mcp\n" "$VPS_HOST"
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

# ── Step 5: Install Skills ───────────────────────────────────────────
install_skills() {
    local root="$1"

    if [[ ! -d "$root/.claude/skills" ]]; then
        warn "Skills directory not found — skipping"
        return
    fi

    local skills=(mycelium-on mycelium-off mycelium-ingest mycelium-recall
                  mycelium-reflect mycelium-distill mycelium-discover)
    for skill in "${skills[@]}"; do
        if [[ -f "$root/.claude/skills/$skill/SKILL.md" ]]; then
            mkdir -p ~/.claude/skills/"$skill"
            cp "$root/.claude/skills/$skill/SKILL.md" ~/.claude/skills/"$skill"/SKILL.md
        fi
    done
    success "Skills installed"

    # Access rules
    local marker="## MYCELIUM MCP Access Control"
    local target="$HOME/.claude/CLAUDE.md"
    if [[ -f "$target" ]] && grep -qF "$marker" "$target"; then
        success "Access rules already in $target"
    else
        mkdir -p "$(dirname "$target")"
        cat >> "$target" <<'RULES'

## MYCELIUM MCP Access Control
- NEVER create `~/.mycelium/.write_enabled` yourself
- NEVER delete `~/.mycelium/.read_enabled` yourself
- Use `/mycelium-on` and `/mycelium-off` skills to toggle access
- If a tool returns "disabled", tell the user to run the skill
RULES
        success "Access rules added to $target"
    fi
}

# ── Step 6: Setup Syncthing Vault Sync ───────────────────────────────
setup_syncthing() {
    if [[ -z "$SYNCTHING_DEVICE_ID" ]]; then
        info "Vault sync skipped (no Device ID provided)"
        return
    fi

    local os
    os="$(detect_os)"

    # Install Syncthing
    if ! command -v syncthing &>/dev/null; then
        info "Installing Syncthing..."
        case "$os" in
            macos)
                if command -v brew &>/dev/null; then
                    brew install syncthing
                else
                    error "brew not found — install Syncthing manually: https://syncthing.net"
                    return
                fi
                ;;
            linux)
                if command -v apt-get &>/dev/null; then
                    sudo apt-get install -y syncthing
                else
                    error "Install Syncthing manually: https://syncthing.net"
                    return
                fi
                ;;
        esac
    fi
    success "Syncthing installed"

    # Start Syncthing service
    case "$os" in
        macos)
            if ! brew services list 2>/dev/null | grep syncthing | grep -q started; then
                brew services start syncthing 2>/dev/null || true
                info "Waiting for Syncthing to start..."
                sleep 3
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
        warn "Syncthing config not found at $config_file"
        warn "Syncthing may still be starting. Manual pairing needed."
        _show_manual_syncthing_instructions
        return
    fi

    local api_key
    api_key="$(grep -oP '(?<=<apikey>)[^<]+' "$config_file" 2>/dev/null \
        || sed -n 's/.*<apikey>\(.*\)<\/apikey>.*/\1/p' "$config_file" 2>/dev/null \
        || echo "")"

    if [[ -z "$api_key" ]]; then
        warn "Could not extract Syncthing API key"
        _show_manual_syncthing_instructions
        return
    fi

    local st_api="http://localhost:8384/rest"
    local auth_header="X-API-Key: $api_key"

    # Add VPS device
    info "Adding VPS device to Syncthing..."
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
        warn "Cannot reach Syncthing API"
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
        success "VPS device already added"
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
        && success "VPS device added" \
        || { warn "Failed to add device via API"; _show_manual_syncthing_instructions; return; }
    fi

    # Add vault folder
    if echo "$config" | python3 -c "
import json,sys
cfg = json.load(sys.stdin)
ids = [f['id'] for f in cfg.get('folders',[])]
sys.exit(0 if 'mycelium-vault' in ids else 1)
" 2>/dev/null; then
        success "Vault folder already configured"
    else
        # Get local device ID
        local local_id
        local_id="$(curl -sf -H "$auth_header" "$st_api/system/status" 2>/dev/null \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["myID"])' 2>/dev/null || echo "")"

        # Re-read config (may have changed from device add)
        config="$(curl -sf -H "$auth_header" "$st_api/config" 2>/dev/null)"

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
        && success "Vault folder configured: $VAULT_DIR" \
        || { warn "Failed to add folder via API"; _show_manual_syncthing_instructions; return; }
    fi

    info "Vault sync will start once VPS accepts the connection."
    info "If auto-accept is off on VPS, open http://$VPS_HOST:8384 to approve."
}

_show_manual_syncthing_instructions() {
    echo
    info "Manual Syncthing setup:"
    printf "  ${DIM}1. Open http://localhost:8384 (local Syncthing UI)${NC}\n"
    printf "  ${DIM}2. Add Remote Device → paste VPS Device ID${NC}\n"
    printf "  ${DIM}3. Set address: tcp://%s:22000${NC}\n" "$VPS_HOST"
    printf "  ${DIM}4. Add Folder → ID: mycelium-vault → Path: %s${NC}\n" "$VAULT_DIR"
    printf "  ${DIM}5. Share folder with the VPS device${NC}\n"
    echo
}

# ── Summary ──────────────────────────────────────────────────────────
show_summary() {
    echo
    printf "  ${BOLD}${GREEN}Connected to MYCELIUM VPS!${NC}\n"
    echo
    printf "  %-22s %s\n" "Service" "Access"
    printf "  %-22s %s\n" "──────────────────────" "──────────────────────────────"
    printf "  %-22s %s\n" "MCP Server"     "http://$VPS_HOST:9631/mcp"
    printf "  %-22s %s\n" "Neo4j Browser"  "http://$VPS_HOST:7474"
    printf "  %-22s %s\n" "Syncthing UI"   "http://$VPS_HOST:8384"
    printf "  %-22s %s\n" "Vault (local)"  "$VAULT_DIR"
    echo
    printf "  ${BOLD}Usage:${NC}\n"
    printf "    claude              — MYCELIUM tools available from anywhere\n"
    printf "    ${DIM}/mycelium-on${NC}        — enable write access\n"
    printf "    ${DIM}/mycelium-recall X${NC}  — search knowledge graph\n"
    echo
}

# ── Main ─────────────────────────────────────────────────────────────
main() {
    echo
    printf "  ${BOLD}${CYAN}MYCELIUM${NC} — connect to VPS\n"
    printf "  ${DIM}Set up laptop to use remote MYCELIUM Data Node${NC}\n"

    local root
    root="$(detect_project_root)"

    step "1/6" "Checking dependencies"
    check_deps

    step "2/6" "VPS connection info"
    collect_vps_info

    step "3/6" "Testing connectivity"
    test_connectivity

    step "4/6" "Registering MCP server"
    register_mcp

    step "5/6" "Installing skills"
    install_skills "$root"

    step "6/6" "Setting up vault sync"
    setup_syncthing

    show_summary
}

main "$@"
