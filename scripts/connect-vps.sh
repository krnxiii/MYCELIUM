#!/usr/bin/env bash
set -euo pipefail
trap 'kill $(jobs -p) 2>/dev/null; printf "\033[?25h" >&2' EXIT INT TERM

# ── Colors & Constants ──────────────────────────────────────────────
CYAN='\033[0;36m'; BCYAN='\033[1;36m'; GREEN='\033[0;32m'; BGREEN='\033[1;32m'
YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

VAULT_DIR="$HOME/.mycelium/vault"
GITHUB_RAW="https://raw.githubusercontent.com/krnxiii/MYCELIUM/main"

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

BRAILLE=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')

spin() {
    local pid=$1 label="${2:-}"
    local i=0
    while kill -0 "$pid" 2>/dev/null; do
        printf "\r  ${DIM}%s${NC} %s" "${BRAILLE[$((i % ${#BRAILLE[@]}))]}" "$label" >&2
        sleep 0.1
        ((i++)) || true
    done
    printf "\r\033[K" >&2
    wait "$pid" || return $?
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
    MCP_TOKEN="$(ask_secret "Token")"
    hint "'Token' from the VPS installer summary"
    if [[ -z "$MCP_TOKEN" ]]; then
        error "Token is required"
        exit 1
    fi

    printf "\n"
    SYNCTHING_DEVICE_ID="$(ask "Sync ID (empty to skip vault sync)" "")"
    hint "'Sync ID' from the VPS installer summary"
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
    if tailscale ping "$VPS_HOST" -c 1 &>/dev/null 2>&1; then
        success "VPS reachable"
    else
        # Fallback: curl (any HTTP response = reachable, even 401)
        local code
        code="$(curl -s --connect-timeout 5 -o /dev/null -w '%{http_code}' "http://$VPS_HOST:9631/mcp" 2>/dev/null || echo "000")"
        if [[ "$code" != "000" ]]; then
            success "VPS reachable"
        else
            warn "Cannot reach $VPS_HOST"
            hint "Check that VPS is online and Tailscale is connected on both sides"
            local proceed
            proceed="$(ask "Continue anyway? [y/N]" "n")"
            [[ "$proceed" =~ ^[Yy] ]] || exit 1
            return
        fi
    fi

    # Test MCP endpoint
    local http_code
    http_code="$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 5 \
        -H "Authorization: Bearer $MCP_TOKEN" \
        "http://$VPS_HOST:9631/mcp" 2>/dev/null)" || http_code="000"

    case "$http_code" in
        200|406) success "MCP server reachable" ;;
        401|403) error "MCP auth failed -- check token"; exit 1 ;;
        000)     warn "MCP not responding -- VPS may still be starting" ;;
        *)       warn "MCP returned HTTP $http_code" ;;
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

    # Remove stale user-level registration
    claude mcp remove mycelium -s user 2>/dev/null || true

    # Register at user level
    claude mcp add -t http -s user \
        mycelium "http://$VPS_HOST:9631/mcp" \
        --header "Authorization: Bearer $MCP_TOKEN" >/dev/null 2>&1

    # Remove project-level .mcp.json override (user-level takes precedence)
    local root
    root="$(detect_project_root 2>/dev/null || true)"
    if [[ -n "$root" ]] && [[ -f "$root/.mcp.json" ]]; then
        # Remove mycelium from project config so user-level HTTP registration wins
        python3 -c "
import json, pathlib, sys
p = pathlib.Path('$root/.mcp.json')
cfg = json.loads(p.read_text())
if 'mycelium' in cfg.get('mcpServers', {}):
    del cfg['mcpServers']['mycelium']
    p.write_text(json.dumps(cfg, indent=2) + '\n')
    print('removed', file=sys.stderr)
" 2>/dev/null || true
    fi

    success "MCP registered (HTTP → $VPS_HOST:9631)"

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

    # Install skills

    for skill in "${skills[@]}"; do
        mkdir -p ~/.claude/skills/"$skill"
        if [[ -n "$root" ]] && [[ -f "$root/.claude/skills/$skill/SKILL.md" ]]; then
            cp "$root/.claude/skills/$skill/SKILL.md" ~/.claude/skills/"$skill"/SKILL.md
        else
            curl -fsSL "$GITHUB_RAW/.claude/skills/$skill/SKILL.md" \
                -o ~/.claude/skills/"$skill"/SKILL.md 2>/dev/null \
                || { warn "Failed to download skill: $skill"; return; }
        fi
    done

    success "Skills installed (${#skills[@]})"

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
        case "$os" in
            macos)
                if command -v brew &>/dev/null; then
                    brew install syncthing >/dev/null 2>&1 &
                    spin $! "Installing Syncthing..."
                else
                    error "brew not found -- install Syncthing manually: https://syncthing.net"
                    return
                fi
                ;;
            linux)
                if command -v apt-get &>/dev/null; then
                    sudo apt-get install -y syncthing >/dev/null 2>&1 &
                    spin $! "Installing Syncthing..."
                else
                    error "Install Syncthing manually: https://syncthing.net"
                    return
                fi
                ;;
        esac
        success "Syncthing installed"
    else
        success "Syncthing found"
    fi

    # Start Syncthing service
    case "$os" in
        macos)
            if ! brew services list 2>/dev/null | grep syncthing | grep -q started; then
                brew services start syncthing >/dev/null 2>&1 || true
                sleep 3
                success "Syncthing started"
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

    # ── Get local device ID ──
    local local_id
    local_id="$(curl -sf -H "$auth_header" "$st_api/system/status" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["myID"])' 2>/dev/null || echo "")"
    if [[ -z "$local_id" ]]; then
        warn "Cannot reach local Syncthing API"
        _show_manual_syncthing_instructions
        return
    fi

    # ── Configure LOCAL side ──

    local config
    config="$(curl -sf -H "$auth_header" "$st_api/config" 2>/dev/null || echo "")"
    if [[ -z "$config" ]]; then
        warn "Cannot read local Syncthing config"
        _show_manual_syncthing_instructions
        return
    fi

    # Add VPS device locally
    if echo "$config" | python3 -c "
import json,sys
cfg = json.load(sys.stdin)
ids = [d['deviceID'] for d in cfg.get('devices',[])]
sys.exit(0 if '$SYNCTHING_DEVICE_ID' in ids else 1)
" 2>/dev/null; then
        success "VPS device already added (local)"
    else
        curl -sf -X POST -H "$auth_header" -H "Content-Type: application/json" \
            "$st_api/config/devices" \
            -d "{\"deviceID\": \"$SYNCTHING_DEVICE_ID\", \"name\": \"mycelium-vps\", \"addresses\": [\"tcp://$VPS_HOST:22000\"], \"autoAcceptFolders\": true}" >/dev/null 2>&1 \
            && success "VPS device added (local)" \
            || { warn "Failed to add VPS device locally"; _show_manual_syncthing_instructions; return; }
    fi

    # Add vault folder locally
    config="$(curl -sf -H "$auth_header" "$st_api/config" 2>/dev/null)"
    if echo "$config" | python3 -c "
import json,sys
cfg = json.load(sys.stdin)
ids = [f['id'] for f in cfg.get('folders',[])]
sys.exit(0 if 'mycelium-vault' in ids else 1)
" 2>/dev/null; then
        success "Vault folder already configured (local)"
    else
        curl -sf -X POST -H "$auth_header" -H "Content-Type: application/json" \
            "$st_api/config/folders" \
            -d "{\"id\": \"mycelium-vault\", \"label\": \"MYCELIUM Vault\", \"path\": \"$VAULT_DIR\", \"type\": \"sendreceive\", \"rescanIntervalS\": 10, \"fsWatcherEnabled\": true, \"devices\": [{\"deviceID\": \"$local_id\"}, {\"deviceID\": \"$SYNCTHING_DEVICE_ID\"}]}" >/dev/null 2>&1 \
            && success "Vault folder configured (local)" \
            || { warn "Failed to add vault folder locally"; _show_manual_syncthing_instructions; return; }
    fi

    # ── Configure VPS side via Syncthing HTTP API ──

    hint "Configuring VPS Syncthing..."

    # Get VPS Syncthing API key via SSH
    local vps_api_key=""
    vps_api_key="$(ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$VPS_HOST" \
        'sed -n "s/.*<apikey>\(.*\)<\/apikey>.*/\1/p" ~/.mycelium/syncthing/config/config.xml 2>/dev/null' 2>/dev/null || true)"

    if [[ -z "$vps_api_key" ]]; then
        # Fallback: try HTTP API on VPS Syncthing directly (if no auth or default)
        warn "Cannot get VPS Syncthing API key via SSH"
        _show_manual_syncthing_instructions
        return
    fi

    local vps_st_api="http://$VPS_HOST:8384/rest"
    local vps_auth="X-API-Key: $vps_api_key"

    # Add laptop device on VPS
    curl -sf -X POST -H "$vps_auth" -H "Content-Type: application/json" \
        "$vps_st_api/config/devices" \
        -d "{\"deviceID\": \"$local_id\", \"name\": \"laptop\", \"autoAcceptFolders\": true}" >/dev/null 2>&1 \
        && success "Laptop device added (VPS)" \
        || warn "VPS already knows this device"

    # Get VPS own device ID for folder config
    local vps_own_id
    vps_own_id="$(curl -sf -H "$vps_auth" "$vps_st_api/system/status" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["myID"])' 2>/dev/null || echo "$SYNCTHING_DEVICE_ID")"

    # Add vault folder on VPS
    local vps_folders
    vps_folders="$(curl -sf -H "$vps_auth" "$vps_st_api/config/folders" 2>/dev/null || echo "[]")"
    if echo "$vps_folders" | python3 -c "
import json,sys
folders = json.load(sys.stdin)
ids = [f['id'] for f in folders]
sys.exit(0 if 'mycelium-vault' in ids else 1)
" 2>/dev/null; then
        success "Vault folder already configured (VPS)"
    else
        curl -sf -X POST -H "$vps_auth" -H "Content-Type: application/json" \
            "$vps_st_api/config/folders" \
            -d "{\"id\": \"mycelium-vault\", \"label\": \"MYCELIUM Vault\", \"path\": \"/var/syncthing/vault\", \"type\": \"sendreceive\", \"rescanIntervalS\": 10, \"fsWatcherEnabled\": true, \"devices\": [{\"deviceID\": \"$vps_own_id\"}, {\"deviceID\": \"$local_id\"}]}" >/dev/null 2>&1 \
            && success "Vault folder configured (VPS)" \
            || { warn "Failed to configure VPS vault folder"; _show_manual_syncthing_instructions; return; }
    fi

    success "Vault sync configured — both sides paired"
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
    # Dynamic width box (same as install-vps.sh)
    local svc1="MCP         http://$VPS_HOST:9631/mcp"
    local svc2="Neo4j       http://$VPS_HOST:7474"
    local svc3="Syncthing   http://$VPS_HOST:8384"
    local svc4="Vault       $VAULT_DIR"
    local W=40
    for s in "$svc1" "$svc2" "$svc3" "$svc4"; do
        (( ${#s} > W )) && W=${#s}
    done

    _row() { printf "  ${DIM}│${NC} %-${W}s ${DIM}│${NC}\n" "$1"; }
    _rul() { printf "  ${DIM}%s%s%s${NC}\n" "$1" "$(printf '─%.0s' $(seq 1 $((W+2))))" "$2"; }

    printf '\n'
    _rul "┌" "┐"
    _row "Connected to MYCELIUM VPS"
    _rul "├" "┤"
    _row "$svc1"
    _row "$svc2"
    _row "$svc3"
    _row "$svc4"
    _rul "└" "┘"

    printf '\n'
    printf "  ${BCYAN}Quick start${NC}\n"
    printf "  ${DIM}├─${NC} claude              ${DIM}MYCELIUM tools available${NC}\n"
    printf "  ${DIM}├─${NC} /mycelium-on        ${DIM}enable write access${NC}\n"
    printf "  ${DIM}└─${NC} /mycelium-recall X  ${DIM}search knowledge graph${NC}\n"
    printf '\n'
}

# ── Main ────────────────────────────────────────────────────────────
main() {
    printf '\n'
    printf "  ${BCYAN}MYCELIUM${NC}  ${DIM}connect to VPS${NC}\n"

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
