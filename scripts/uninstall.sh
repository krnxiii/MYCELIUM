#!/usr/bin/env bash
set -euo pipefail

# ── Colors & Constants ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'
DIM='\033[2m'; NC='\033[0m'

MYCELIUM_DIR="$HOME/.mycelium"

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

# ── Project Root Detection ──────────────────────────────────────────
detect_project_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ "$(basename "$dir")" == "scripts" ]]; then
        dir="$(dirname "$dir")"
    fi
    if [[ ! -f "$dir/Makefile" ]]; then
        error "Cannot find project root (no Makefile)"
        error "Run from the project root or from scripts/"
        exit 1
    fi
    echo "$dir"
}

# ── Step 1: Stop services ──────────────────────────────────────────
stop_services() {
    step "1/5" "Stopping services"

    local root="$1"
    if command -v docker &>/dev/null && docker compose -f "$root/docker-compose.yml" ps -q 2>/dev/null | grep -q .; then
        docker compose -f "$root/docker-compose.yml" down 2>/dev/null || true
        success "Containers stopped"
    else
        info "No running containers found"
    fi
}

# ── Step 2: Remove MCP integration ─────────────────────────────────
remove_mcp() {
    step "2/5" "Removing MCP integration"

    # MCP server registration
    if command -v claude &>/dev/null; then
        if claude mcp remove mycelium -s user 2>/dev/null; then
            success "MCP server unregistered"
        else
            info "MCP server was not registered"
        fi
    else
        info "claude CLI not found — skipping MCP removal"
    fi

    # Skills
    local removed=false
    for skill in mycelium-on mycelium-off mycelium-ingest mycelium-recall mycelium-reflect mycelium-distill mycelium-discover; do
        if [[ -d "$HOME/.claude/skills/$skill" ]]; then
            rm -rf "$HOME/.claude/skills/$skill"
            removed=true
        fi
    done
    if [[ "$removed" == true ]]; then
        success "Skills removed"
    else
        info "No skills found"
    fi

    # Global CLAUDE.md rules
    local target="$HOME/.claude/CLAUDE.md"
    if [[ -f "$target" ]] && grep -qF "## MYCELIUM MCP Access Control" "$target"; then
        # Remove the marker block (header + 4 rule lines + preceding blank line)
        local tmp="${target}.tmp"
        awk '
            /^$/ { blank = blank "\n"; next }
            /^## MYCELIUM MCP Access Control/ { skip = 1; blank = ""; next }
            skip && /^- / { next }
            skip && !/^- / { skip = 0 }
            { printf "%s", blank; blank = ""; print }
        ' "$target" > "$tmp"
        mv "$tmp" "$target"
        success "Access rules removed from ~/.claude/CLAUDE.md"
    else
        info "No access rules in ~/.claude/CLAUDE.md"
    fi
}

# ── Step 3: Remove CLI wrapper ─────────────────────────────────────
remove_cli() {
    step "3/6" "Removing CLI wrapper"

    local wrapper="$HOME/.local/bin/mycelium"
    if [[ -f "$wrapper" ]]; then
        rm -f "$wrapper"
        success "CLI removed ($wrapper)"
    else
        info "No CLI wrapper found"
    fi
}

# ── Step 4: Remove data ────────────────────────────────────────────
remove_data() {
    step "4/6" "Removing data"

    if [[ ! -d "$MYCELIUM_DIR" ]]; then
        info "$MYCELIUM_DIR does not exist"
        return 0
    fi

    local answer
    answer="$(ask "Delete $MYCELIUM_DIR (graph data, vault, logs, gate flags)?" "n")"
    case "$answer" in
        [yY]*)
            rm -rf "$MYCELIUM_DIR"
            success "Data removed ($MYCELIUM_DIR)" ;;
        *)
            warn "Kept $MYCELIUM_DIR" ;;
    esac
}

# ── Step 5: Clean project ──────────────────────────────────────────
clean_project() {
    step "5/6" "Cleaning project"

    local root="$1"
    local has_env=false has_venv=false

    [[ -f "$root/.env" ]]  && has_env=true
    [[ -d "$root/.venv" ]] && has_venv=true

    if [[ "$has_env" == false && "$has_venv" == false ]]; then
        info "No .env or .venv found"
        return 0
    fi

    local items=""
    $has_env  && items=".env"
    $has_venv && items="${items:+$items, }.venv"

    local answer
    answer="$(ask "Remove $items?" "n")"
    case "$answer" in
        [yY]*)
            $has_env  && rm -f "$root/.env"
            $has_venv && rm -rf "$root/.venv"
            success "Project cleaned ($items)" ;;
        *)
            warn "Kept $items" ;;
    esac
}

# ── Step 6: Summary ────────────────────────────────────────────────
show_done() {
    step "6/6" "Done!"
    success "MYCELIUM uninstalled"
    info "To reinstall: make quickstart"
    echo
}

# ── Main ────────────────────────────────────────────────────────────
main() {
    echo
    printf "  ${BOLD}${CYAN}MYCELIUM${NC} — uninstaller\n"
    echo

    local root
    root="$(detect_project_root)"

    stop_services "$root"
    remove_mcp
    remove_cli
    remove_data
    clean_project "$root"
    show_done
}

main "$@"
