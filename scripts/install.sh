#!/usr/bin/env bash
set -euo pipefail

# ── Colors & Constants ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'
DIM='\033[2m'; NC='\033[0m'

MIN_PYTHON="3.12"
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

# Replace value for key in .env file (safe for any characters — no sed escaping)
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

# ── Project Root Detection ──────────────────────────────────────────
detect_project_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    # If run from scripts/, go up one level
    if [[ "$(basename "$dir")" == "scripts" ]]; then
        dir="$(dirname "$dir")"
    fi
    # Verify it's actually the project root
    if [[ ! -f "$dir/Makefile" ]] || [[ ! -f "$dir/$ENV_EXAMPLE" ]]; then
        error "Cannot find project root (no Makefile or $ENV_EXAMPLE)"
        error "Run from the project root or from scripts/"
        exit 1
    fi
    echo "$dir"
}

# ── OS Detection ────────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Darwin) echo "macos"  ;;
        Linux)  echo "linux"  ;;
        *)      echo "unknown" ;;
    esac
}

pkg_manager() {
    if command -v brew &>/dev/null; then echo "brew"
    elif command -v apt-get &>/dev/null; then echo "apt"
    elif command -v dnf &>/dev/null; then echo "dnf"
    else echo "unknown"
    fi
}

# ── Dependency Checks ──────────────────────────────────────────────
check_python() {
    if ! command -v python3 &>/dev/null; then
        return 1
    fi
    local ver
    ver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    # Compare: required >= MIN_PYTHON
    python3 -c "
import sys
cur = tuple(map(int, '$ver'.split('.')))
req = tuple(map(int, '$MIN_PYTHON'.split('.')))
sys.exit(0 if cur >= req else 1)
"
}

check_uv() { command -v uv &>/dev/null; }

check_docker() { command -v docker &>/dev/null; }

check_docker_daemon() {
    docker info &>/dev/null
}

check_docker_compose() {
    docker compose version &>/dev/null
}

check_make() { command -v make &>/dev/null; }

# ── Dependency Table ────────────────────────────────────────────────
check_deps() {
    local scenario="$1"
    local all_ok=true
    local os
    os="$(detect_os)"
    local pkg
    pkg="$(pkg_manager)"

    printf "\n  %-22s %s\n" "Dependency" "Status"
    printf "  %-22s %s\n" "──────────────────────" "──────"

    # make — always needed
    if check_make; then
        printf "  %-22s ${GREEN}✓ found${NC}\n" "make"
    else
        printf "  %-22s ${RED}✗ missing${NC}\n" "make"
        all_ok=false
    fi

    # Docker — always needed (Neo4j runs in Docker)
    if check_docker; then
        printf "  %-22s ${GREEN}✓ found${NC}\n" "docker"
        if check_docker_daemon; then
            printf "  %-22s ${GREEN}✓ running${NC}\n" "docker daemon"
        else
            printf "  %-22s ${RED}✗ not running${NC}\n" "docker daemon"
            all_ok=false
        fi
    else
        printf "  %-22s ${RED}✗ missing${NC}\n" "docker"
        all_ok=false
    fi

    # Docker Compose — always needed
    if check_docker_compose; then
        printf "  %-22s ${GREEN}✓ found${NC}\n" "docker compose"
    else
        printf "  %-22s ${RED}✗ missing${NC}\n" "docker compose"
        all_ok=false
    fi

    # Python & uv — only for scenario 1 (local dev)
    if [[ "$scenario" == "1" ]]; then
        if check_python; then
            local pyver
            pyver="$(python3 --version 2>&1)"
            printf "  %-22s ${GREEN}✓ %s${NC}\n" "python ≥$MIN_PYTHON" "$pyver"
        else
            printf "  %-22s ${RED}✗ missing or <$MIN_PYTHON${NC}\n" "python ≥$MIN_PYTHON"
            all_ok=false
        fi

        if check_uv; then
            printf "  %-22s ${GREEN}✓ found${NC}\n" "uv"
        else
            printf "  %-22s ${YELLOW}○ will install${NC}\n" "uv"
        fi
    fi

    echo

    if [[ "$all_ok" == false ]]; then
        warn "Some dependencies are missing. Install hints:"
        echo

        if ! check_make; then
            case "$os" in
                macos) info "  make:    xcode-select --install" ;;
                linux)
                    case "$pkg" in
                        apt) info "  make:    sudo apt install build-essential" ;;
                        dnf) info "  make:    sudo dnf install make" ;;
                        *)   info "  make:    install via your package manager" ;;
                    esac ;;
            esac
        fi

        if ! check_docker; then
            case "$os" in
                macos) info "  docker:  brew install --cask docker  (then open Docker.app)" ;;
                linux) info "  docker:  https://docs.docker.com/engine/install/" ;;
            esac
        elif ! check_docker_daemon; then
            case "$os" in
                macos) info "  docker:  open Docker.app (daemon not running)" ;;
                linux) info "  docker:  sudo systemctl start docker" ;;
            esac
        fi

        if ! check_docker_compose && check_docker; then
            info "  compose: included in Docker Desktop; or: docker compose plugin"
        fi

        if [[ "$scenario" == "1" ]]; then
            if ! check_python; then
                case "$os" in
                    macos) info "  python:  brew install python@3.12" ;;
                    linux)
                        case "$pkg" in
                            apt) info "  python:  sudo apt install python3.12 python3.12-venv" ;;
                            dnf) info "  python:  sudo dnf install python3.12" ;;
                            *)   info "  python:  install Python ≥$MIN_PYTHON" ;;
                        esac ;;
                esac
            fi
        fi

        echo
        error "Fix missing dependencies and re-run this script."
        exit 1
    fi
}

# ── Scenario Selection ──────────────────────────────────────────────
select_scenario() {
    echo >&2
    printf "  ${BOLD}1)${NC}  Local dev       — Python on host, Neo4j in Docker, embeddings via API\n" >&2
    printf "  ${BOLD}2)${NC}  Docker + API    — everything in Docker, embeddings via DeepInfra API\n" >&2
    printf "  ${BOLD}3)${NC}  Full Docker     — everything local, no external APIs (downloads ~2 GB model)\n" >&2
    printf "  ${BOLD}4)${NC}  Connect to VPS  — link this machine to an existing VPS deployment\n" >&2
    echo >&2

    while true; do
        local choice
        choice="$(ask "Choose scenario [1/2/3/4]" "1")"
        case "$choice" in
            1|2|3) echo "$choice"; return ;;
            4) echo "4"; return ;;
            *) warn "Enter 1, 2, 3, or 4" >&2 ;;
        esac
    done
}

# ── API Key Validation ──────────────────────────────────────────────
validate_api_key() {
    local key="$1"
    [[ "$key" =~ ^[a-zA-Z0-9_-]{10,}$ ]]
}

# ── .env Generation ─────────────────────────────────────────────────
generate_env() {
    local scenario="$1"

    # Backup existing .env
    if [[ -f "$ENV_FILE" ]]; then
        local backup
        backup="$ENV_FILE.backup.$(date +%Y%m%d_%H%M%S)"
        local overwrite
        overwrite="$(ask "Existing .env found. Overwrite? (backup → $backup)" "y")"
        case "$overwrite" in
            [yY]*) cp "$ENV_FILE" "$backup"; success "Backup saved: $backup" ;;
            *)     info "Keeping existing .env"; return 0 ;;
        esac
    fi

    # Start from template
    cp "$ENV_EXAMPLE" "$ENV_FILE"

    # ── API key (scenarios 1 & 2 only) ──
    if [[ "$scenario" != "3" ]]; then
        echo
        info "Scenario $scenario uses DeepInfra API for embeddings."
        info "Get a free key at: https://deepinfra.com/dash/api_keys"
        echo

        local api_key
        while true; do
            api_key="$(ask_secret "DeepInfra API key")"
            if [[ -z "$api_key" ]]; then
                warn "API key is required for this scenario"
            elif validate_api_key "$api_key"; then
                break
            else
                warn "Key must be ≥10 alphanumeric/dash/underscore characters"
            fi
        done

        set_env_val "MYCELIUM_SEMANTIC__API_KEY" "$api_key"
    else
        # Scenario 3: local embeddings, no API key needed
        set_env_val "MYCELIUM_SEMANTIC__PROVIDER" "api"
        set_env_val "MYCELIUM_SEMANTIC__API_BASE_URL" "http://localhost:9632"
        set_env_val "MYCELIUM_SEMANTIC__API_KEY" ""
    fi

    # ── Owner name (optional) ──
    echo
    local owner_name
    owner_name="$(ask "What's your name? (optional, for first-person linking)" "")"
    if [[ -n "$owner_name" ]]; then
        set_env_val "MYCELIUM_OWNER__NAME" "$owner_name"
    fi

    # ── Obsidian layer ──
    echo
    info "Obsidian layer adds YAML frontmatter to vault files for Graph View."
    info "Requires Obsidian (https://obsidian.md) pointed at ~/.mycelium/vault/"
    local obsidian
    obsidian="$(ask "Enable Obsidian visualization layer?" "y")"
    case "$obsidian" in
        [nN]*) set_env_val "MYCELIUM_OBSIDIAN__ENABLED" "false" ;;
        *)
            set_env_val "MYCELIUM_OBSIDIAN__ENABLED" "true"
            local project_neurons
            project_neurons="$(ask "Project neurons as .md files in vault/neurons/? (experimental)" "y")"
            case "$project_neurons" in
                [yY]*) set_env_val "MYCELIUM_OBSIDIAN__PROJECT_NEURONS" "true" ;;
                *)     set_env_val "MYCELIUM_OBSIDIAN__PROJECT_NEURONS" "false" ;;
            esac
            ;;
    esac

    # ── Sigma.js render (optional) ──
    echo
    info "Sigma.js render opens the knowledge graph in a browser (localhost:9633)."
    info "Alternative to Obsidian Graph View — interactive, with ForceAtlas2 layout."
    local render_enabled
    render_enabled="$(ask "Enable Sigma.js graph viewer?" "y")"
    case "$render_enabled" in
        [yY]*) set_env_val "MYCELIUM_RENDER__ENABLED" "true" ;;
        *)     set_env_val "MYCELIUM_RENDER__ENABLED" "false" ;;
    esac

    # ── Neo4j password ──
    echo
    local neo4j_pass
    neo4j_pass="$(ask_secret "Neo4j password" "password")"
    if [[ "${#neo4j_pass}" -lt 4 ]]; then
        warn "Password too short (min 4 chars), using default"
        neo4j_pass="password"
    fi
    if [[ "$neo4j_pass" != "password" ]]; then
        set_env_val "MYCELIUM_NEO4J__PASSWORD" "$neo4j_pass"
    fi

    success ".env generated"
}

# ── Install uv (if missing, scenario 1 only) ───────────────────────
ensure_uv() {
    if check_uv; then return 0; fi
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the env to pick up uv in current session
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if check_uv; then
        success "uv installed"
    else
        error "uv installation failed. Install manually: https://docs.astral.sh/uv/"
        exit 1
    fi
}

# ── Run Scenario ────────────────────────────────────────────────────
run_scenario() {
    local scenario="$1"

    case "$scenario" in
        1)
            ensure_uv
            info "Running: make quickstart"
            make quickstart
            ;;
        2)
            info "Running: make quickstart-app"
            make quickstart-app
            ;;
        3)
            info "Running: make quickstart-docker"
            warn "First run downloads BGE-M3 model for local embeddings. This may take a while."
            make quickstart-docker
            ;;
    esac
}

# ── Register MCP Server in Claude Code ─────────────────────────────
register_mcp() {
    local scenario="$1"

    if ! command -v claude &>/dev/null; then
        warn "claude CLI not found — skipping MCP registration"
        info "Install: https://docs.anthropic.com/en/docs/claude-code"
        info "Then run: make mcp-install"
        return 0
    fi

    local root
    root="$(pwd)"

    if [[ "$scenario" != "1" ]]; then
        # Docker: MCP via HTTP (container exposes :8000)
        claude mcp remove mycelium -s user 2>/dev/null || true
        claude mcp add -t http -s user mycelium http://localhost:9631/mcp
        success "MCP server registered (HTTP → localhost:8000)"
    else
        # Local dev: MCP via stdio
        claude mcp remove mycelium -s user 2>/dev/null || true
        claude mcp add -t stdio -s user mycelium -- uv run --project "$root" --extra mcp python -m mycelium.mcp.server
        success "MCP server registered globally in Claude Code"
        info "Available from any directory. Verify: claude mcp list"
    fi

    # Gate init: default read=on, write=off
    mkdir -p ~/.mycelium
    touch ~/.mycelium/.read_enabled
    success "Gate init: read=on, write=off"

    # Install skills globally
    local skills=(mycelium-on mycelium-off mycelium-ingest mycelium-recall mycelium-reflect mycelium-distill mycelium-discover mycelium-domain)
    for skill in "${skills[@]}"; do
        mkdir -p ~/.claude/skills/"$skill"
        cp "$root/.claude/skills/$skill/SKILL.md" ~/.claude/skills/"$skill"/SKILL.md
    done
    success "Skills installed: ${skills[*]/#//}"

    # Offer global access rules
    install_global_rules "$root"
}

# ── Global CLAUDE.md rules (optional) ────────────────────────────
install_global_rules() {
    local root="$1"
    local marker="## MYCELIUM MCP Access Control"
    local target="$HOME/.claude/CLAUDE.md"

    if [[ -f "$target" ]] && grep -qF "$marker" "$target"; then
        success "Access rules already in $target"
        return 0
    fi

    echo
    info "Global access rules prevent the agent from toggling MCP access on its own."
    info "Without them, the agent may enable write access without asking."
    local answer
    answer="$(ask "Add MYCELIUM access rules to ~/.claude/CLAUDE.md?" "y")"
    case "$answer" in
        [yY]*)
            mkdir -p "$(dirname "$target")"
            cat >> "$target" <<'RULES'

## MYCELIUM MCP Access Control
- NEVER create `~/.mycelium/.write_enabled` yourself
- NEVER delete `~/.mycelium/.read_enabled` yourself
- Use `/mycelium-on` and `/mycelium-off` skills to toggle access
- If a tool returns "disabled", tell the user to run the skill
RULES
            success "Access rules added to $target" ;;
        *)
            warn "Skipped. Run later: make mcp-rules-install" ;;
    esac
}

# ── CLI Wrapper (scenario 1 only) ──────────────────────────────────
install_cli() {
    local root="$1"
    local bin_dir="$HOME/.local/bin"
    local wrapper="$bin_dir/mycelium"

    mkdir -p "$bin_dir"

    cat > "$wrapper" <<WRAPPER
#!/usr/bin/env bash
exec uv run --project "$root" mycelium "\$@"
WRAPPER
    chmod +x "$wrapper"
    success "CLI installed: $wrapper"

    # Check PATH
    if ! echo "$PATH" | tr ':' '\n' | grep -qx "$bin_dir"; then
        warn "\$HOME/.local/bin is not in your PATH"
        local shell_rc
        case "$(basename "$SHELL")" in
            zsh)  shell_rc="~/.zshrc" ;;
            bash) shell_rc="~/.bashrc" ;;
            *)    shell_rc="your shell config" ;;
        esac
        info "Add to $shell_rc:"
        info "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
}

# ── Summary ─────────────────────────────────────────────────────────
show_summary() {
    local scenario="$1"

    echo
    printf "  ${BOLD}${GREEN}MYCELIUM is ready!${NC}\n"
    echo
    printf "  %-20s %s\n" "Service" "URL"
    printf "  %-20s %s\n" "────────────────────" "──────────────────────────"
    printf "  %-20s %s\n" "Neo4j Browser"       "http://localhost:7474"
    printf "  %-20s %s\n" "Neo4j Bolt"          "bolt://localhost:7687"

    if [[ "$scenario" == "2" || "$scenario" == "3" ]]; then
        printf "  %-20s %s\n" "MCP (HTTP)" "http://localhost:9631/mcp"
    fi

    if [[ "$scenario" == "3" ]]; then
        printf "  %-20s %s\n" "TEI Embeddings" "http://localhost:9632"
    fi

    # Check if render is enabled in .env
    if grep -q '^MYCELIUM_RENDER__ENABLED=true' "$ENV_FILE" 2>/dev/null; then
        printf "  %-20s %s\n" "Graph Viewer" "http://localhost:9633 (make render)"
    fi

    echo
    printf "  ${BOLD}Next steps:${NC}\n"
    case "$scenario" in
        1)
            printf "    make serve        — start the MCP server\n"
            if grep -q '^MYCELIUM_RENDER__ENABLED=true' "$ENV_FILE" 2>/dev/null; then
                printf "    make render       — open Sigma.js graph viewer\n"
            fi
            printf "    make test         — run tests\n"
            printf "    claude            — use MYCELIUM tools from anywhere\n"
            ;;
        2|3)
            printf "    claude            — use MYCELIUM tools via HTTP MCP\n"
            printf "    docker compose logs -f  — watch logs\n"
            printf "    make down               — stop services\n"
            ;;
    esac
    echo
}

# ── Main ────────────────────────────────────────────────────────────
main() {
    echo
    printf "  ${BOLD}${CYAN}MYCELIUM${NC} — installer\n"
    printf "  ${DIM}Mind Wide Web — distributed consciousness OS${NC}\n"

    local root
    root="$(detect_project_root)"
    cd "$root"

    # Step 1: Scenario selection
    local total=5
    step "1/$total" "Select installation scenario"
    local scenario
    scenario="$(select_scenario)"
    local labels=( [1]="Local dev" [2]="Docker + API" [3]="Full Docker" [4]="Connect to VPS" )
    success "Scenario $scenario: ${labels[$scenario]}"

    # Scenario 4: connect laptop to existing VPS (exec, no return)
    if [[ "$scenario" == "4" ]]; then
        exec bash scripts/connect-vps.sh
    fi

    # Scenario 1 has extra step (CLI wrapper)
    [[ "$scenario" == "1" ]] && total=6

    # Step 2: Check dependencies
    step "2/$total" "Checking dependencies"
    check_deps "$scenario"
    success "All dependencies satisfied"

    # Step 3: Generate .env
    step "3/$total" "Configure environment"
    generate_env "$scenario"

    # Step 4: Install
    step "4/$total" "Installing MYCELIUM"
    run_scenario "$scenario"

    # Step 5: Register MCP
    step "5/$total" "Registering MCP server"
    register_mcp "$scenario"

    # Step 6: Install CLI wrapper (scenario 1 only)
    if [[ "$scenario" == "1" ]]; then
        step "6/$total" "Installing CLI"
        install_cli "$root"
    fi

    # Done
    step "Done" "Ready!"
    show_summary "$scenario"
}

main "$@"
