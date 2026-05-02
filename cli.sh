#!/bin/bash
# KERNOS CLI Launcher — interactive REPL against THIS Kernos folder.
#
# Double-click this file (or run from terminal: ./cli.sh).
#
# Companion to start.sh: where start.sh boots the Kernos server
# bound to Discord/SMS/Telegram adapters, cli.sh boots a stdin/
# stdout REPL bound to the SAME boot path (build_dev_handler in
# kernos/repl.py mirrors server.py's on_ready wiring) so you can
# soak-test the development clone without provisioning a second
# Discord/SMS/Telegram bot. The connectors stay pointed at the
# production folder; this REPL talks to the dev folder directly.
#
# Multi-folder safe: each Kernos clone runs its own REPL with its
# own data-dir + instance id, so this clone's REPL never touches
# the production folder's state.
#
# State-isolation DEFAULTS (only fire when neither shell-exported
# KERNOS_* env nor .env already sets them):
#   KERNOS_DATA_DIR    = ./data-dev      (separate from prod ./data)
#   KERNOS_INSTANCE_ID = repl:dev        (separate state keying)
#   KERNOS_SECRETS_DIR = ./secrets-dev   (separate credentials)
#   KERNOS_USE_DECOUPLED_TURN_RUNNER = 1 (the CCV1-shipped path)
#
# Precedence (highest to lowest):
#   1. shell-exported KERNOS_* values (`export KERNOS_INSTANCE_ID=...`)
#   2. .env file values (loaded by _load_kernos_env below)
#   3. The defaults above (only when neither 1 nor 2 set the var)
#
# **If your .env sets any of these (e.g. KERNOS_INSTANCE_ID), THAT
# value wins — the dev-isolation defaults below DO NOT override
# .env.** This is intentional: .env is the per-clone config
# surface. If you want guaranteed isolation, override at invocation
# time: ``KERNOS_INSTANCE_ID=repl:dev ./cli.sh``.
#
# Multi-user: when the instance has multiple members, the REPL
# prompts you to pick which member to "be" for the session. Set
# KERNOS_REPL_SENDER to bypass the prompt (useful for piped
# input):
#
#   echo "Hello" | KERNOS_REPL_SENDER=founder ./cli.sh
#
# Once a CLI subcommand layer ships (kernos repl --member ...),
# this launcher will wrap it; today it invokes ``python -m
# kernos.repl`` directly.

cd "$(dirname "$0")"
SCRIPT_DIR="$(pwd)"

# Load Kernos-specific values from .env. Same pattern as start.sh:
# only KERNOS_* lines are imported, never source the whole .env,
# existing shell-exported values win.
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

# CLI-specific defaults — keep dev state isolated from the prod
# folder's data/ and secrets/. The four KERNOS_* defaults below
# only fire when the variable wasn't already set (by export OR by
# .env).
export KERNOS_DATA_DIR="${KERNOS_DATA_DIR:-./data-dev}"
export KERNOS_INSTANCE_ID="${KERNOS_INSTANCE_ID:-repl:dev}"
export KERNOS_SECRETS_DIR="${KERNOS_SECRETS_DIR:-./secrets-dev}"
export KERNOS_USE_DECOUPLED_TURN_RUNNER="${KERNOS_USE_DECOUPLED_TURN_RUNNER:-1}"
export KERNOS_LOG_LEVEL="${KERNOS_LOG_LEVEL:-WARNING}"

# Activate the virtual environment.
source .venv/bin/activate

echo "Kernos CLI (dev REPL)"
echo "  folder      = $SCRIPT_DIR"
echo "  data_dir    = $KERNOS_DATA_DIR"
echo "  instance_id = $KERNOS_INSTANCE_ID"
echo "  decoupled   = $KERNOS_USE_DECOUPLED_TURN_RUNNER"
echo
echo "Tip: pipe input with"
echo "  echo 'your message' | KERNOS_REPL_SENDER=founder ./cli.sh"
echo

# Run the REPL. Today this is python -m kernos.repl; once a CLI
# subcommand layer ships, this becomes 'python -m kernos.cli repl'
# (or `kernos repl` via console_scripts) without changing the
# launcher's surface.
python -m kernos.repl
