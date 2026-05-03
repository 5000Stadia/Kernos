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
                # Strip CR (Windows line endings) and trailing whitespace.
                val="${val%$'\r'}"
                val="${val%"${val##*[![:space:]]}"}"
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

# --- Graceful-crash handler ----------------------------------
# When start.sh is launched by double-click, the terminal window
# is owned by the script's shell. If python (or any earlier step)
# exits with an error, the script returns and the window closes
# instantly — the user never sees the traceback. This trap keeps
# the window open on actual crashes so the error is readable.
#
# Skip-pause exit codes:
#   0   — clean exit
#   130 — SIGINT (Ctrl+C, user-initiated stop)
#   143 — SIGTERM (kill signal, e.g. self-restart logic above)
#
# Set KERNOS_START_NO_PAUSE=1 to suppress the pause unconditionally
# (e.g. when chaining start.sh from another script or systemd).
_kernos_exit_handler() {
    local rc=$?
    if [ $rc -ne 0 ] && [ $rc -ne 130 ] && [ $rc -ne 143 ]; then
        echo ""
        echo "============================================================"
        echo "Kernos exited with error code $rc"
        echo "Scroll up in this window to see the traceback or error."
        echo "============================================================"
        if [ "${KERNOS_START_NO_PAUSE:-0}" != "1" ] && [ -t 0 ] && [ -t 1 ]; then
            echo ""
            read -r -p "Press Enter to close this window... " _
        fi
    fi
}
trap _kernos_exit_handler EXIT

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
# threads request context.
#
# Default flipped to 0 (legacy path) 2026-05-02 after live soak proved
# the C7 thin path is anti-capability without WORKSHOP-BINDING:
# IntegrationService picks render-only ActionKinds (RESPOND_ONLY,
# CONSTRAINED_RESPONSE, PROPOSE_TOOL — all of which forbid tool calls
# in their kind prompts) for tool-needing requests because
# IntegrationInputs.surfaced_tools is empty on this path. Result:
# agent says "no calendar tool available" while calendar tool IS in
# the request body — agent is faithfully obeying its instructions.
# Until the resolution spec lands, legacy path remains the operator
# default. Override to 1 explicitly when soak-testing thin path.
export KERNOS_USE_DECOUPLED_TURN_RUNNER=${KERNOS_USE_DECOUPLED_TURN_RUNNER:-0}

# Start the bot
echo "Starting Kernos..."
echo "Decoupled turn runner: ${KERNOS_USE_DECOUPLED_TURN_RUNNER:-OFF}"
echo "Press Ctrl+C to stop."
echo ""
python kernos/server.py
