#!/bin/bash
# Kernos development REPL launcher.
#
# Spins up a stdin/stdout REPL against the development Kernos folder
# (this clone) with a separate data directory + instance id so dev
# state never collides with whatever's running in the production
# Kernos folder. Use this for soak runs against the CCV1 substrate-
# fidelity arc (data/diagnostics/live-tests/COGNITIVE-CONTEXT-V1-live-test.md)
# without setting up a second Discord/SMS/Telegram bot.
#
# Defaults (only fire when neither shell-exported KERNOS_* env nor
# .env already sets them — .env wins over these defaults):
#   KERNOS_DATA_DIR        = ./data-dev   (isolated from prod)
#   KERNOS_INSTANCE_ID     = repl:dev     (state keying)
#   KERNOS_USE_DECOUPLED_TURN_RUNNER = 1  (the CCV1 path)
#   KERNOS_SECRETS_DIR     = ./secrets-dev (isolated credentials)
#
# Precedence: shell-exported KERNOS_* > .env > the defaults above.
# **If your .env sets these, the .env values are used; the
# isolation defaults DO NOT override.** Override at invocation
# time for guaranteed isolation:
#   KERNOS_INSTANCE_ID=repl:dev ./scripts/dev-repl.sh

cd "$(dirname "$0")/.."
SCRIPT_DIR="$(pwd)"

# Load KERNOS_* env values from .env, mirroring start.sh's handling.
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

# REPL-specific defaults — keep dev state isolated from the prod
# Kernos folder's data/ and secrets/.
export KERNOS_DATA_DIR="${KERNOS_DATA_DIR:-./data-dev}"
export KERNOS_INSTANCE_ID="${KERNOS_INSTANCE_ID:-repl:dev}"
export KERNOS_SECRETS_DIR="${KERNOS_SECRETS_DIR:-./secrets-dev}"
export KERNOS_USE_DECOUPLED_TURN_RUNNER="${KERNOS_USE_DECOUPLED_TURN_RUNNER:-1}"
export KERNOS_LOG_LEVEL="${KERNOS_LOG_LEVEL:-WARNING}"

source .venv/bin/activate

echo "Kernos dev REPL"
echo "  data_dir   = $KERNOS_DATA_DIR"
echo "  instance   = $KERNOS_INSTANCE_ID"
echo "  decoupled  = $KERNOS_USE_DECOUPLED_TURN_RUNNER"
echo

python -m kernos.repl
