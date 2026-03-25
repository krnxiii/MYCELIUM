#!/usr/bin/env bash
# Usage: wait-healthy.sh <container> [container2 ...]
# Waits for Docker containers to become healthy with a progress spinner.
set -euo pipefail

TIMEOUT=${WAIT_TIMEOUT:-120}
SPIN='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'

wait_one() {
    local ctr=$1 i=0 status="starting"
    while true; do
        if (( i % 10 == 0 )); then
            status=$(docker inspect --format='{{.State.Health.Status}}' "$ctr" 2>/dev/null || echo "not found")
        fi
        local secs=$(( i / 10 )) tenths=$(( i % 10 ))
        case "$status" in
            healthy)
                printf "\r\033[K\033[32m✓\033[0m  %s healthy (%d.%ds)\n" "$ctr" "$secs" "$tenths"
                return 0 ;;
            unhealthy)
                printf "\r\033[K\033[31m✗\033[0m  %s unhealthy after %d.%ds\n" "$ctr" "$secs" "$tenths"
                docker logs "$ctr" --tail 5 2>&1 | sed 's/^/   /'
                return 1 ;;
        esac
        if (( secs >= TIMEOUT )); then
            printf "\r\033[K\033[31m✗\033[0m  %s timeout after %ds (status: %s)\n" "$ctr" "$TIMEOUT" "$status"
            return 1
        fi
        printf "\r\033[K%s  %s %s %d.%ds" "${SPIN:i%${#SPIN}:1}" "$ctr" "$status" "$secs" "$tenths"
        sleep 0.1
        (( i++ )) || true
    done
}

for ctr in "$@"; do
    wait_one "$ctr" || exit 1
done
