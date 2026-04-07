#!/usr/bin/env bash
set -euo pipefail

# ── Colors & Constants ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

# Resolve real user home even under sudo
if [[ -n "${SUDO_USER:-}" ]]; then
    _REAL_HOME="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || eval echo "~$SUDO_USER")"
else
    _REAL_HOME="$HOME"
fi
MYCELIUM_DIR="$_REAL_HOME/.mycelium"

# ── Helpers ─────────────────────────────────────────────────────────
success() { printf "  ${GREEN}✓${NC}  %s\n" "$1"; }
info()    { printf "  ${DIM}ℹ${NC}  %s\n" "$1"; }
warn()    { printf "  ${YELLOW}!${NC}  %s\n" "$1"; }
error()   { printf "  ${RED}✗${NC}  %s\n" "$1" >&2; }

step() { printf "\n${BOLD}${CYAN}[%s]${NC} ${BOLD}%s${NC}\n" "$1" "$2"; }

ask() {
    local prompt="$1" default="${2:-}"
    if [[ -n "$default" ]]; then
        printf "  ${BOLD}>${NC} %s ${DIM}[%s]${NC}: " "$prompt" "$default" >&2
    else
        printf "  ${BOLD}>${NC} %s: " "$prompt" >&2
    fi
    read -r answer
    echo "${answer:-$default}"
}

# ── Project Root Detection ──────────────────────────────────────────
detect_project_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [[ "$(basename "$dir")" == "scripts" ]] && dir="$(dirname "$dir")"
    if [[ ! -f "$dir/Makefile" ]]; then
        error "Cannot find project root (no Makefile)"
        exit 1
    fi
    echo "$dir"
}

# ── Step 1: Stop services ──────────────────────────────────────────
stop_services() {
    step "1/6" "Stopping services"

    local root="$1" stopped=false
    if command -v docker &>/dev/null; then
        for cf in docker-compose.vps.yml docker-compose.yml; do
            if [[ -f "$root/$cf" ]] && docker compose -f "$root/$cf" ps -q 2>/dev/null | grep -q .; then
                docker compose -f "$root/$cf" \
                    --profile telegram --profile voice-whisper \
                    --profile app --profile full \
                    down --remove-orphans 2>/dev/null || true
                stopped=true
                success "Containers stopped ($cf)"
            fi
        done
    fi
    $stopped || info "No running containers found"
}

# ── Step 2: Remove MCP integration ─────────────────────────────────
remove_mcp() {
    step "2/6" "Removing MCP integration"

    if command -v claude &>/dev/null; then
        claude mcp remove mycelium -s user 2>/dev/null \
            && success "MCP server unregistered" \
            || info "MCP server was not registered"
    else
        info "claude CLI not found — skipping"
    fi

    # Skills
    local removed=false
    for skill in mycelium-on mycelium-off mycelium-ingest mycelium-recall \
                 mycelium-reflect mycelium-distill mycelium-discover mycelium-domain; do
        if [[ -d "$_REAL_HOME/.claude/skills/$skill" ]]; then
            rm -rf "$_REAL_HOME/.claude/skills/$skill"
            removed=true
        fi
    done
    $removed && success "Skills removed" || info "No skills found"

    # Global CLAUDE.md rules
    local target="$_REAL_HOME/.claude/CLAUDE.md"
    if [[ -f "$target" ]] && grep -qF "## MYCELIUM MCP Access Control" "$target"; then
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
    fi
}

# ── Step 3: Remove CLI wrapper ─────────────────────────────────────
remove_cli() {
    step "3/6" "Removing CLI wrapper"

    local wrapper="$_REAL_HOME/.local/bin/mycelium"
    if [[ -f "$wrapper" ]]; then
        rm -f "$wrapper"
        success "CLI removed"
    else
        info "No CLI wrapper found"
    fi
}

# ── Step 4: Remove data ────────────────────────────────────────────
remove_data() {
    step "4/6" "Removing data ($MYCELIUM_DIR)"

    if [[ ! -d "$MYCELIUM_DIR" ]]; then
        info "$MYCELIUM_DIR does not exist"
        return 0
    fi

    # Show what exists
    local has_neo4j=false has_vault=false has_syncthing=false has_other=false
    [[ -d "$MYCELIUM_DIR/neo4j" ]]    && has_neo4j=true
    [[ -d "$MYCELIUM_DIR/vault" ]]     && has_vault=true
    [[ -d "$MYCELIUM_DIR/syncthing" ]] && has_syncthing=true

    $has_neo4j    && info "  neo4j/     — graph database"
    $has_vault    && info "  vault/     — knowledge files (Obsidian notes)"
    $has_syncthing && info "  syncthing/ — sync configuration"

    echo
    local answer
    answer="$(ask "Delete ALL data in $MYCELIUM_DIR?" "n")"
    case "$answer" in
        [yY]*)
            rm -rf "$MYCELIUM_DIR"
            success "All data removed"
            return 0
            ;;
    esac

    # Granular deletion
    if $has_neo4j; then
        answer="$(ask "Delete graph database (neo4j/)?" "n")"
        case "$answer" in
            [yY]*) rm -rf "$MYCELIUM_DIR/neo4j"; success "Graph database removed" ;;
            *)     warn "Kept graph database" ;;
        esac
    fi

    if $has_vault; then
        answer="$(ask "Delete vault (knowledge files, Obsidian notes)?" "n")"
        case "$answer" in
            [yY]*) rm -rf "$MYCELIUM_DIR/vault"; success "Vault removed" ;;
            *)     warn "Kept vault" ;;
        esac
    fi

    if $has_syncthing; then
        answer="$(ask "Delete Syncthing config?" "y")"
        case "$answer" in
            [yY]*) rm -rf "$MYCELIUM_DIR/syncthing"; success "Syncthing config removed" ;;
            *)     warn "Kept Syncthing config" ;;
        esac
    fi

    # Remaining files (gate flags, logs, etc.)
    local remaining
    remaining="$(ls -A "$MYCELIUM_DIR" 2>/dev/null | grep -vE '^(neo4j|vault|syncthing)$' || true)"
    if [[ -n "$remaining" ]]; then
        for item in $remaining; do
            rm -rf "${MYCELIUM_DIR:?}/$item"
        done
        success "Cleaned up remaining files"
    fi

    # Remove dir if empty
    rmdir "$MYCELIUM_DIR" 2>/dev/null && success "Removed empty $MYCELIUM_DIR" || true
}

# ── Step 5: Clean project ──────────────────────────────────────────
clean_project() {
    step "5/6" "Cleaning project"

    local root="$1"
    local has_env=false has_venv=false

    [[ -f "$root/.env" ]]  && has_env=true
    [[ -d "$root/.venv" ]] && has_venv=true

    if ! $has_env && ! $has_venv; then
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
        *)  warn "Kept $items" ;;
    esac

    # Remove Docker images
    if command -v docker &>/dev/null; then
        answer="$(ask "Remove Docker images (mycelium-vps-*)?" "y")"
        case "$answer" in
            [yY]*)
                docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
                    | grep -E '^mycelium' \
                    | xargs -r docker rmi 2>/dev/null || true
                success "Docker images removed" ;;
            *)  warn "Kept Docker images" ;;
        esac
    fi
}

# ── Step 6: Summary ────────────────────────────────────────────────
show_done() {
    step "6/6" "Done"
    success "MYCELIUM uninstalled"
    info "To reinstall: make quickstart"
    echo
}

# ── Main ────────────────────────────────────────────────────────────
main() {
    echo
    printf "  ${BOLD}${CYAN}MYCELIUM${NC}  ${DIM}uninstaller${NC}\n"
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
