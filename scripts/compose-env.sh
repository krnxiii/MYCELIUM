#!/usr/bin/env bash
# Resolve COMPOSE_FILE + PROFILES for the mycelium stack. Source, then call:
#   . scripts/compose-env.sh && resolve_compose_env
# Priority: MYCELIUM_COMPOSE_FILE env var > .env marker > existing containers
# (docker compose ps -a — stopped containers count too) > loud failure.
# Never guess from runtime state alone: with the stack fully down that once
# deployed the DEV compose on prod. Prod pins the file in .env.

resolve_compose_env() {
    local marker="${MYCELIUM_COMPOSE_FILE:-}"
    [ -z "$marker" ] && [ -f .env ] && marker="$(sed -n 's/^MYCELIUM_COMPOSE_FILE=//p' .env | tail -1)"

    if [ -n "$marker" ]; then
        if [ ! -f "$marker" ]; then
            echo "MYCELIUM_COMPOSE_FILE points to a missing file: $marker" >&2
            return 1
        fi
        COMPOSE_FILE="$marker"
    elif [ -f docker-compose.vps.yml ] && docker compose -f docker-compose.vps.yml ps -a --format '{{.Name}}' 2>/dev/null | grep -q mycelium; then
        COMPOSE_FILE="docker-compose.vps.yml"
    elif docker compose -f docker-compose.yml ps -a --format '{{.Name}}' 2>/dev/null | grep -q mycelium; then
        COMPOSE_FILE="docker-compose.yml"
    else
        echo "Cannot determine environment: no mycelium containers exist and no marker set." >&2
        echo "Add MYCELIUM_COMPOSE_FILE=docker-compose.vps.yml (prod) or =docker-compose.yml (dev) to .env and re-run." >&2
        return 1
    fi

    local profs="${MYCELIUM_COMPOSE_PROFILES:-}"
    [ -z "$profs" ] && [ -f .env ] && profs="$(sed -n 's/^MYCELIUM_COMPOSE_PROFILES=//p' .env | tail -1)"

    PROFILES=()
    if [ -n "$profs" ]; then
        local p
        for p in ${profs//,/ }; do PROFILES+=(--profile "$p"); done
    else
        local existing
        existing="$(docker compose -f "$COMPOSE_FILE" ps -a --format '{{.Name}}' 2>/dev/null)"
        case "$existing" in *telegram*)     PROFILES+=(--profile telegram) ;; esac
        case "$existing" in *whisper*)      PROFILES+=(--profile voice-whisper) ;; esac
        case "$existing" in *mycelium-app*) PROFILES+=(--profile app) ;; esac
    fi
}
