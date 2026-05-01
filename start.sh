#!/bin/bash
# KERNOS Discord Bot Launcher
# Double-click this file (or run from terminal: ./start.sh)
#
# Self-restart behavior: if a prior Kernos instance from THIS
# folder is already running (e.g. an orphaned start.sh adopted
# by systemd --user after the launching terminal closed), this
# script terminates it before booting a fresh instance.
#
# Multi-folder safe: the kill is scoped to processes whose CWD
# matches this script's directory, so running start.sh from a
# dev clone (e.g. ~/Kernos-dev) won't disturb a Kernos server
# running from a different clone (e.g. ~/Kernos).

cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd)"

# Load Kernos-specific values from .env so the user can toggle
# behavior by editing .env instead of remembering to export shell
# vars. Only KERNOS_* lines are imported, and we never source the
# whole .env (avoids executing shell metacharacters in API keys
# or tokens). Existing shell-exported KERNOS_* values win — set -a
# does not overwrite already-set vars when used with the conditional
# pattern below.
_load_kernos_env() {
    local env_file="$SCRIPT_DIR/.env"
    [ -f "$env_file" ] || return 0
    local line key val
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|'#'*) continue ;;
            KERNOS_*=*)
                key="${line%%=*}"
                val="${line#*=}"
                # Strip surrounding quotes (single or double) if present.
                val="${val%\"}"; val="${val#\"}"
                val="${val%\'}"; val="${val#\'}"
                # Only set if not already exported — shell wins over .env.
                if [ -z "${!key+x}" ]; then
                    export "$key=$val"
                fi
                ;;
        esac
    done < "$env_file"
}
_load_kernos_env

# Helper: emit space-separated PIDs of processes matching $1
# whose /proc/PID/cwd resolves to SCRIPT_DIR.
_pids_in_this_dir() {
    local pattern="$1"
    local pid_list
    pid_list=$(pgrep -f "$pattern" 2>/dev/null || true)
    local matches=""
    for pid in $pid_list; do
        local cwd
        cwd=$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)
        if [ "$cwd" = "$SCRIPT_DIR" ]; then
            matches="$matches $pid"
        fi
    done
    # Trim leading whitespace.
    echo "${matches# }"
}

# --- Kill any prior Kernos instance from THIS folder ----------
# Opt-out: set KERNOS_START_KILL_PRIOR=0 to disable. Default on.
SELF_PID=$$
EXISTING_SERVERS=""
if [ "${KERNOS_START_KILL_PRIOR:-1}" = "1" ]; then
    EXISTING_SERVERS=$(_pids_in_this_dir "python kernos/server\.py")
fi
if [ -n "$EXISTING_SERVERS" ]; then
    echo "Found running Kernos in $SCRIPT_DIR: PIDs $EXISTING_SERVERS — terminating before restart..."
    # Graceful first.
    kill $EXISTING_SERVERS 2>/dev/null || true
    # Also nudge any prior bash start.sh wrappers from this dir
    # (excluding ourselves) so they don't sit waiting for a child
    # that's about to die.
    EXISTING_LAUNCHERS=$(_pids_in_this_dir "bash .*start\.sh" \
        | tr ' ' '\n' | grep -v "^${SELF_PID}$" | tr '\n' ' ' || true)
    EXISTING_LAUNCHERS="${EXISTING_LAUNCHERS%% }"
    if [ -n "$EXISTING_LAUNCHERS" ]; then
        kill $EXISTING_LAUNCHERS 2>/dev/null || true
    fi
    # Give SIGTERM up to 5 seconds to land cleanly.
    for i in 1 2 3 4 5; do
        STILL=$(_pids_in_this_dir "python kernos/server\.py")
        [ -z "$STILL" ] && break
        sleep 1
    done
    # Anything still alive gets SIGKILL.
    STILL=$(_pids_in_this_dir "python kernos/server\.py")
    if [ -n "$STILL" ]; then
        echo "Force-killing stragglers: $STILL"
        kill -9 $STILL 2>/dev/null || true
        sleep 1
    fi
    echo "Prior instance terminated."
fi

# Activate the virtual environment
source .venv/bin/activate

# IWL v3 thin-path soak: route turns through the decoupled-cognition
# path (TurnRunner + IntegrationService + EnactmentService).
# Conversational kinds flow end-to-end with per-turn
# ProductionResponseDelivery + telemetry binding + synthetic
# reasoning.* aggregation. Full-machinery dispatch is gated behind
# _UnwiredDescriptorLookup until INTEGRATION-WIRE-LIVE-WORKSHOP-BINDING
# threads request context. Unset this var to revert to legacy path.
export KERNOS_USE_DECOUPLED_TURN_RUNNER=1

# Start the bot
echo "Starting Kernos..."
echo "Decoupled turn runner: ${KERNOS_USE_DECOUPLED_TURN_RUNNER:-OFF}"
echo "Press Ctrl+C to stop."
echo ""
python kernos/server.py
