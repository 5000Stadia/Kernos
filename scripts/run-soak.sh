#!/bin/bash
# Soak-test harness wrapper.
#
# Runs the substrate-fidelity soak harness against the Kernos
# development clone. Wraps `python -m kernos.soak` so the operator
# has one entry point.
#
# Usage:
#   ./scripts/run-soak.sh            # run automated scenarios end-to-end
#   ./scripts/run-soak.sh --list     # list available scenarios
#   ./scripts/run-soak.sh --auto-only --scenario probe_c_procedures
#
# Each run produces an artifact directory at
# data/soak-runs/<timestamp>/ with per-scenario log files, /dump
# snapshots, a JSON results blob, and a markdown summary report.
# The exit code is 0 if every automated scenario passed; non-zero
# if any failed. Operator-driven scenarios are listed but skipped
# in automation mode.

cd "$(dirname "$0")/.."

# Load .env for KERNOS_* values + LLM credentials. Mirrors cli.sh's
# precedence pattern.
_load_kernos_env() {
    local env_file="$(pwd)/.env"
    [ -f "$env_file" ] || return 0
    local line key val
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ''|'#'*) continue ;;
            KERNOS_*=*)
                key="${line%%=*}"
                val="${line#*=}"
                val="${val%\"}"; val="${val#\"}"
                val="${val%\'}"; val="${val#\'}"
                if [ -z "${!key+x}" ]; then
                    export "$key=$val"
                fi
                ;;
        esac
    done < "$env_file"
}
_load_kernos_env

source .venv/bin/activate

# Default to running all auto scenarios end-to-end, listing
# operator-driven ones for the operator to address separately.
if [ $# -eq 0 ]; then
    set -- --all
fi

python -m kernos.soak "$@"
