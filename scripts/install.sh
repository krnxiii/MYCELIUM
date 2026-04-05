#!/usr/bin/env bash
set -euo pipefail
trap 'kill $(jobs -p) 2>/dev/null; printf "\033[?25h" >&2' EXIT INT TERM

# ── Colors & Constants ──────────────────────────────────────────────
CYAN='\033[0;36m'; BCYAN='\033[1;36m'; GREEN='\033[0;32m'
YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'
DIM='\033[2m'; NC='\033[0m'

MIN_PYTHON="3.12"
ENV_FILE=".env"
ENV_EXAMPLE=".env.example"

BRAILLE=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')

# ── Helpers ─────────────────────────────────────────────────────────
success() { printf "  ${GREEN}✓${NC}  %s\n" "$1"; }
warn()    { printf "  ${YELLOW}!${NC}  %s\n" "$1"; }
error()   { printf "  ${RED}✗${NC}  %s\n" "$1" >&2; }
hint()    { printf "     ${DIM}%s${NC}\n" "$1"; }
sep()     { printf "  ${DIM}─────────────────────────────────────────────${NC}\n"; }

step() {
    printf "\n${BOLD}${BCYAN}[%s]${NC} ${BOLD}%s${NC}\n" "$1" "$2"
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

# ── Project Root Detection ──────────────────────────────────────────
detect_project_root() {
    local dir
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [[ "$(basename "$dir")" == "scripts" ]] && dir="$(dirname "$dir")"
    if [[ ! -f "$dir/Makefile" ]] || [[ ! -f "$dir/$ENV_EXAMPLE" ]]; then
        error "Cannot find project root (no Makefile or $ENV_EXAMPLE)"
        exit 1
    fi
    printf '%s' "$dir"
}

# ── OS Detection ────────────────────────────────────────────────────
detect_os() {
    case "$(uname -s)" in
        Darwin) printf 'macos'  ;;
        Linux)  printf 'linux'  ;;
        *)      printf 'unknown' ;;
    esac
}

pkg_manager() {
    if command -v brew &>/dev/null; then printf 'brew'
    elif command -v apt-get &>/dev/null; then printf 'apt'
    elif command -v dnf &>/dev/null; then printf 'dnf'
    else printf 'unknown'
    fi
}

# ── Dependency Checks ──────────────────────────────────────────────
check_python() {
    if ! command -v python3 &>/dev/null; then return 1; fi
    local ver
    ver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    python3 -c "
import sys
cur = tuple(map(int, '$ver'.split('.')))
req = tuple(map(int, '$MIN_PYTHON'.split('.')))
sys.exit(0 if cur >= req else 1)
"
}

check_uv()             { command -v uv &>/dev/null; }
check_docker()         { command -v docker &>/dev/null; }
check_docker_daemon()  { docker info &>/dev/null; }
check_docker_compose() { docker compose version &>/dev/null; }
check_make()           { command -v make &>/dev/null; }

# ── Dependency Table ────────────────────────────────────────────────
check_deps() {
    local scenario="$1"
    local all_ok=true
    local os pkg last_dep
    os="$(detect_os)"
    pkg="$(pkg_manager)"

    printf '\n'

    # Determine last dep for └─
    if [[ "$scenario" == "1" ]]; then last_dep="uv"; else last_dep="docker compose"; fi

    # make
    local prefix="${DIM}├─${NC}"
    if check_make; then
        printf "  $prefix %-18s ${GREEN}found${NC}\n" "make"
    else
        printf "  $prefix %-18s ${RED}missing${NC}\n" "make"
        all_ok=false
    fi

    # docker
    if check_docker; then
        printf "  $prefix %-18s ${GREEN}found${NC}\n" "docker"
        if check_docker_daemon; then
            printf "  $prefix %-18s ${GREEN}running${NC}\n" "docker daemon"
        else
            printf "  $prefix %-18s ${RED}not running${NC}\n" "docker daemon"
            all_ok=false
        fi
    else
        printf "  $prefix %-18s ${RED}missing${NC}\n" "docker"
        all_ok=false
    fi

    # docker compose
    if [[ "$last_dep" == "docker compose" ]]; then prefix="${DIM}└─${NC}"; fi
    if check_docker_compose; then
        printf "  $prefix %-18s ${GREEN}found${NC}\n" "docker compose"
    else
        printf "  $prefix %-18s ${RED}missing${NC}\n" "docker compose"
        all_ok=false
    fi

    # python & uv (scenario 1 only)
    if [[ "$scenario" == "1" ]]; then
        prefix="${DIM}├─${NC}"
        if check_python; then
            local pyver
            pyver="$(python3 --version 2>&1)"
            printf "  $prefix %-18s ${GREEN}%s${NC}\n" "python ≥$MIN_PYTHON" "$pyver"
        else
            printf "  $prefix %-18s ${RED}missing${NC}\n" "python ≥$MIN_PYTHON"
            all_ok=false
        fi

        prefix="${DIM}└─${NC}"
        if check_uv; then
            printf "  $prefix %-18s ${GREEN}found${NC}\n" "uv"
        else
            printf "  $prefix %-18s ${YELLOW}will install${NC}\n" "uv"
        fi
    fi

    printf '\n'

    if [[ "$all_ok" == false ]]; then
        warn "Missing dependencies:"
        if ! check_make; then
            case "$os" in
                macos) hint "make:    xcode-select --install" ;;
                linux)
                    case "$pkg" in
                        apt) hint "make:    sudo apt install build-essential" ;;
                        dnf) hint "make:    sudo dnf install make" ;;
                        *)   hint "make:    install via your package manager" ;;
                    esac ;;
            esac
        fi
        if ! check_docker; then
            case "$os" in
                macos) hint "docker:  brew install --cask docker  (then open Docker.app)" ;;
                linux) hint "docker:  https://docs.docker.com/engine/install/" ;;
            esac
        elif ! check_docker_daemon; then
            case "$os" in
                macos) hint "docker:  open Docker.app (daemon not running)" ;;
                linux) hint "docker:  sudo systemctl start docker" ;;
            esac
        fi
        if ! check_docker_compose && check_docker; then
            hint "compose: included in Docker Desktop; or: docker compose plugin"
        fi
        if [[ "$scenario" == "1" ]] && ! check_python; then
            case "$os" in
                macos) hint "python:  brew install python@3.12" ;;
                linux)
                    case "$pkg" in
                        apt) hint "python:  sudo apt install python3.12 python3.12-venv" ;;
                        dnf) hint "python:  sudo dnf install python3.12" ;;
                        *)   hint "python:  install Python ≥$MIN_PYTHON" ;;
                    esac ;;
            esac
        fi
        printf '\n'
        error "Fix missing dependencies and re-run."
        exit 1
    fi
}

# ── Scenario Selection ──────────────────────────────────────────────
select_scenario() {
    printf '\n' >&2
    printf "  ${BOLD}1)${NC}  Local dev       ${DIM}— Python on host, Neo4j in Docker${NC}\n" >&2
    printf "  ${BOLD}2)${NC}  Docker + API    ${DIM}— everything in Docker, embeddings via API${NC}\n" >&2
    printf "  ${BOLD}3)${NC}  Full Docker     ${DIM}— everything local, no external APIs (~2 GB)${NC}\n" >&2
    printf "  ${BOLD}4)${NC}  Connect to VPS  ${DIM}— link to an existing VPS deployment${NC}\n" >&2
    printf '\n' >&2

    while true; do
        local choice
        choice="$(ask "Choose scenario [1/2/3/4]" "1")"
        case "$choice" in
            1|2|3|4) printf '%s' "$choice"; return ;;
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
            [yY]*) cp "$ENV_FILE" "$backup"; success "Backup: $backup" ;;
            *)     hint "Keeping existing .env"; return 0 ;;
        esac
    fi

    cp "$ENV_EXAMPLE" "$ENV_FILE"

    # ── API key (scenarios 1 & 2) ──
    if [[ "$scenario" != "3" ]]; then
        sep
        local api_key
        while true; do
            api_key="$(ask_secret "DeepInfra API key")"
            hint "Free key: https://deepinfra.com/dash/api_keys"
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
        set_env_val "MYCELIUM_SEMANTIC__PROVIDER" "api"
        set_env_val "MYCELIUM_SEMANTIC__API_BASE_URL" "http://localhost:9632"
        set_env_val "MYCELIUM_SEMANTIC__API_KEY" ""
    fi

    # ── Owner ──
    sep
    local owner_name
    owner_name="$(ask "Your name" "")"
    hint "Optional. Used for graph ownership metadata."
    [[ -n "$owner_name" ]] && set_env_val "MYCELIUM_OWNER__NAME" "$owner_name"

    # ── Obsidian ──
    sep
    printf "  ${BCYAN}Obsidian${NC}  ${DIM}visualization layer for vault files${NC}\n"
    local obsidian
    obsidian="$(ask "Enable Obsidian layer?" "y")"
    hint "Adds YAML frontmatter for Graph View. Point Obsidian at ~/.mycelium/vault/"
    case "$obsidian" in
        [nN]*) set_env_val "MYCELIUM_OBSIDIAN__ENABLED" "false" ;;
        *)
            set_env_val "MYCELIUM_OBSIDIAN__ENABLED" "true"
            local project_neurons
            project_neurons="$(ask "Project neurons as .md files? (experimental)" "y")"
            case "$project_neurons" in
                [yY]*) set_env_val "MYCELIUM_OBSIDIAN__PROJECT_NEURONS" "true" ;;
                *)     set_env_val "MYCELIUM_OBSIDIAN__PROJECT_NEURONS" "false" ;;
            esac
            ;;
    esac

    # ── Sigma.js render ──
    sep
    printf "  ${BCYAN}Sigma.js${NC}  ${DIM}interactive graph viewer in browser${NC}\n"
    local render_enabled
    render_enabled="$(ask "Enable graph viewer?" "y")"
    hint "Opens at localhost:9633 via 'make render'"
    case "$render_enabled" in
        [yY]*) set_env_val "MYCELIUM_RENDER__ENABLED" "true" ;;
        *)     set_env_val "MYCELIUM_RENDER__ENABLED" "false" ;;
    esac

    # ── Neo4j password ──
    sep
    local neo4j_pass
    neo4j_pass="$(ask_secret "Neo4j password" "password")"
    if [[ "${#neo4j_pass}" -lt 4 ]]; then
        warn "Too short (min 4 chars), using default"
        neo4j_pass="password"
    fi
    [[ "$neo4j_pass" != "password" ]] && set_env_val "MYCELIUM_NEO4J__PASSWORD" "$neo4j_pass"

    printf '\n'
    success ".env generated"
}

# ── Install uv (if missing, scenario 1 only) ───────────────────────
ensure_uv() {
    if check_uv; then return 0; fi
    (curl -LsSf https://astral.sh/uv/install.sh | sh) >/dev/null 2>&1 &
    if ! spin $! "Installing uv..."; then
        error "uv installation failed. Install manually: https://docs.astral.sh/uv/"
        exit 1
    fi
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
            make quickstart
            ;;
        2)
            make quickstart-app
            ;;
        3)
            warn "First run downloads BGE-M3 model (~2 GB). This may take a while."
            make quickstart-docker
            ;;
    esac
}

# ── Register MCP Server in Claude Code ─────────────────────────────
register_mcp() {
    local scenario="$1"

    if ! command -v claude &>/dev/null; then
        warn "claude CLI not found — skipping MCP registration"
        hint "Install: curl -fsSL https://claude.ai/install.sh | bash"
        hint "Then run: make mcp-install"
        return 0
    fi

    local root
    root="$(pwd)"

    if [[ "$scenario" != "1" ]]; then
        claude mcp remove mycelium -s user 2>/dev/null || true
        claude mcp add -t http -s user mycelium http://localhost:9631/mcp
        success "MCP registered (HTTP → localhost:9631)"
    else
        claude mcp remove mycelium -s user 2>/dev/null || true
        claude mcp add -t stdio -s user mycelium -- uv run --project "$root" --extra mcp python -m mycelium.mcp.server
        success "MCP registered (stdio)"
        hint "Available from any directory. Verify: claude mcp list"
    fi

    # Gate init
    mkdir -p ~/.mycelium
    touch ~/.mycelium/.read_enabled
    success "Gate init: read=on, write=off"

    # Skills
    local skills=(mycelium-on mycelium-off mycelium-ingest mycelium-recall
                  mycelium-reflect mycelium-distill mycelium-discover mycelium-domain)
    for skill in "${skills[@]}"; do
        mkdir -p ~/.claude/skills/"$skill"
        cp "$root/.claude/skills/$skill/SKILL.md" ~/.claude/skills/"$skill"/SKILL.md
    done
    success "Skills installed (${#skills[@]})"

    # Access rules
    install_global_rules "$root"
}

# ── Global CLAUDE.md rules ──────────────────────────────────────────
install_global_rules() {
    local root="$1"
    local marker="## MYCELIUM MCP Access Control"
    local target="$HOME/.claude/CLAUDE.md"

    if [[ -f "$target" ]] && grep -qF "$marker" "$target"; then
        success "Access rules already present"
        return 0
    fi

    printf '\n'
    local answer
    answer="$(ask "Add MYCELIUM access rules to ~/.claude/CLAUDE.md?" "y")"
    hint "Prevents agent from toggling MCP access without asking"
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
            success "Access rules added" ;;
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

    if ! echo "$PATH" | tr ':' '\n' | grep -qx "$bin_dir"; then
        warn "\$HOME/.local/bin is not in your PATH"
        local shell_rc
        case "$(basename "$SHELL")" in
            zsh)  shell_rc="~/.zshrc" ;;
            bash) shell_rc="~/.bashrc" ;;
            *)    shell_rc="your shell config" ;;
        esac
        hint "Add to $shell_rc:  export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
}

# ── Summary ─────────────────────────────────────────────────────────
show_summary() {
    local scenario="$1"

    printf '\n'
    printf "  ${DIM}┌──────────────────────────────────────────────┐${NC}\n"
    printf "  ${DIM}│${NC}  ${BOLD}${GREEN}MYCELIUM is ready${NC}                            ${DIM}│${NC}\n"
    printf "  ${DIM}├──────────────────────────────────────────────┤${NC}\n"
    printf "  ${DIM}│${NC}  ${BCYAN}Neo4j${NC}     http://localhost:7474             ${DIM}│${NC}\n"
    printf "  ${DIM}│${NC}  ${BCYAN}Bolt${NC}      bolt://localhost:7687             ${DIM}│${NC}\n"

    if [[ "$scenario" == "2" || "$scenario" == "3" ]]; then
        printf "  ${DIM}│${NC}  ${BCYAN}MCP${NC}       http://localhost:9631/mcp        ${DIM}│${NC}\n"
    fi
    if [[ "$scenario" == "3" ]]; then
        printf "  ${DIM}│${NC}  ${BCYAN}TEI${NC}       http://localhost:9632             ${DIM}│${NC}\n"
    fi
    if grep -q '^MYCELIUM_RENDER__ENABLED=true' "$ENV_FILE" 2>/dev/null; then
        printf "  ${DIM}│${NC}  ${BCYAN}Graph${NC}     http://localhost:9633  ${DIM}make render${NC} ${DIM}│${NC}\n"
    fi

    printf "  ${DIM}└──────────────────────────────────────────────┘${NC}\n"

    printf '\n'
    printf "  ${BOLD}${BCYAN}Quick start${NC}\n"
    case "$scenario" in
        1)
            printf "  ${DIM}├─${NC} make serve        ${DIM}start MCP server${NC}\n"
            if grep -q '^MYCELIUM_RENDER__ENABLED=true' "$ENV_FILE" 2>/dev/null; then
                printf "  ${DIM}├─${NC} make render       ${DIM}open graph viewer${NC}\n"
            fi
            printf "  ${DIM}├─${NC} make test         ${DIM}run tests${NC}\n"
            printf "  ${DIM}└─${NC} claude            ${DIM}use MYCELIUM tools${NC}\n"
            ;;
        2|3)
            printf "  ${DIM}├─${NC} claude                  ${DIM}use MYCELIUM tools${NC}\n"
            printf "  ${DIM}├─${NC} docker compose logs -f  ${DIM}watch logs${NC}\n"
            printf "  ${DIM}└─${NC} make down               ${DIM}stop services${NC}\n"
            ;;
    esac
    printf '\n'
}

# ── Main ────────────────────────────────────────────────────────────
main() {
    printf '\n'
    printf "  ${BCYAN}MYCELIUM${NC}  ${DIM}installer${NC}\n"

    local root
    root="$(detect_project_root)"
    cd "$root"

    # Step 1: Scenario
    local total=5
    step "1/$total" "Select installation scenario"
    local scenario
    scenario="$(select_scenario)"
    local labels=( [1]="Local dev" [2]="Docker + API" [3]="Full Docker" [4]="Connect to VPS" )
    success "Scenario $scenario: ${labels[$scenario]}"

    # Scenario 4 → connect-vps.sh
    if [[ "$scenario" == "4" ]]; then
        exec bash scripts/connect-vps.sh
    fi

    [[ "$scenario" == "1" ]] && total=6

    # Step 2: Dependencies
    step "2/$total" "Checking dependencies"
    check_deps "$scenario"
    success "All dependencies satisfied"

    # Step 3: Environment
    step "3/$total" "Configure environment"
    generate_env "$scenario"

    # Step 4: Install
    step "4/$total" "Installing MYCELIUM"
    run_scenario "$scenario"

    # Step 5: MCP
    step "5/$total" "Registering MCP server"
    register_mcp "$scenario"

    # Step 6: CLI (scenario 1 only)
    if [[ "$scenario" == "1" ]]; then
        step "6/$total" "Installing CLI"
        install_cli "$root"
    fi

    # Done
    step "Done" "Ready!"
    show_summary "$scenario"
}

main "$@"
