#!/bin/bash
# Manage the Kernos systemd-user service for THIS folder.
#
# Two opt-in/out paths:
#   1. Explicit subcommand:
#        ./scripts/manage-kernos-service.sh install
#        ./scripts/manage-kernos-service.sh uninstall
#        ./scripts/manage-kernos-service.sh status
#   2. Env var (apply current preference):
#        KERNOS_AUTO_RESTART_ON_BOOT=1 ./scripts/manage-kernos-service.sh apply
#        KERNOS_AUTO_RESTART_ON_BOOT=0 ./scripts/manage-kernos-service.sh apply
#
# The service is a systemd-user unit (no root required) installed to
# ~/.config/systemd/user/kernos.service. Once installed + enabled,
# Kernos auto-starts at login and restarts on crash.
#
# Multi-folder safe: each Kernos folder installs its own service
# under a folder-derived unit name (kernos-<dirhash>.service) so
# two folders' services do not collide.

set -e

# Resolve the Kernos folder this script ships from. We use the
# script's parent directory so symlinks / repo clones produce a
# stable WorkingDirectory.
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/scripts/kernos.service.template"

# Folder-scoped unit name so a dev clone and a production clone
# can each install independently without name collision.
DIR_HASH=$(echo -n "$SCRIPT_DIR" | sha256sum | cut -c1-8)
UNIT_NAME="kernos-${DIR_HASH}.service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/$UNIT_NAME"

_render_unit() {
    if [ ! -f "$TEMPLATE" ]; then
        echo "ERROR: template not found at $TEMPLATE" >&2
        exit 1
    fi
    mkdir -p "$UNIT_DIR"
    sed "s|__KERNOS_DIR__|$SCRIPT_DIR|g" "$TEMPLATE" > "$UNIT_PATH"
    echo "Wrote $UNIT_PATH"
}

_reload() {
    systemctl --user daemon-reload
}

_install() {
    echo "Installing Kernos service for: $SCRIPT_DIR"
    echo "  Unit name: $UNIT_NAME"
    _render_unit
    _reload
    systemctl --user enable "$UNIT_NAME"
    # If Kernos is already running from this folder via a manual
    # start.sh, don't double-start; just leave the service enabled
    # so it picks up on next login/reboot. Otherwise start it now.
    if pgrep -f "python kernos/server\.py" >/dev/null 2>&1 \
            && [ -n "$(_pids_in_this_dir 'python kernos/server\.py')" ]; then
        echo "Kernos is already running from this folder — leaving as-is."
        echo "Service is enabled and will take over on next boot/login."
    else
        systemctl --user start "$UNIT_NAME"
        echo "Service started."
    fi
    echo
    echo "Status:"
    systemctl --user --no-pager status "$UNIT_NAME" | head -8 || true
}

_uninstall() {
    echo "Uninstalling Kernos service for: $SCRIPT_DIR"
    echo "  Unit name: $UNIT_NAME"
    if systemctl --user is-active --quiet "$UNIT_NAME"; then
        systemctl --user stop "$UNIT_NAME" || true
    fi
    if systemctl --user is-enabled --quiet "$UNIT_NAME" 2>/dev/null; then
        systemctl --user disable "$UNIT_NAME" || true
    fi
    if [ -f "$UNIT_PATH" ]; then
        rm "$UNIT_PATH"
        echo "Removed $UNIT_PATH"
    fi
    _reload
    echo "Uninstalled."
}

_status() {
    echo "Kernos service for: $SCRIPT_DIR"
    echo "  Unit name: $UNIT_NAME"
    if [ -f "$UNIT_PATH" ]; then
        echo "  Installed: yes ($UNIT_PATH)"
    else
        echo "  Installed: no"
        return 0
    fi
    if systemctl --user is-enabled --quiet "$UNIT_NAME" 2>/dev/null; then
        echo "  Enabled (auto-start on login): yes"
    else
        echo "  Enabled (auto-start on login): no"
    fi
    if systemctl --user is-active --quiet "$UNIT_NAME"; then
        echo "  Currently active: yes"
    else
        echo "  Currently active: no"
    fi
    echo
    systemctl --user --no-pager status "$UNIT_NAME" | head -10 || true
}

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
    echo "${matches# }"
}

# Entry point.
ACTION="${1:-help}"

case "$ACTION" in
    install)
        _install
        ;;
    uninstall|remove)
        _uninstall
        ;;
    status)
        _status
        ;;
    apply)
        # Honor env var: 1/yes/true → install; 0/no/false/unset → uninstall.
        case "${KERNOS_AUTO_RESTART_ON_BOOT:-0}" in
            1|yes|true|on)
                _install
                ;;
            *)
                _uninstall
                ;;
        esac
        ;;
    help|*)
        cat <<EOF
Usage: $0 <command>

Commands:
  install    Install + enable + start the systemd-user service for this folder.
  uninstall  Stop, disable, and remove the service.
  status     Show install/enable/active state.
  apply      Install if KERNOS_AUTO_RESTART_ON_BOOT is set to 1/yes/true/on,
             otherwise uninstall. Lets you toggle via env var.
  help       Show this message.

Each Kernos folder gets its own service unit (folder-hash-derived name)
so multiple clones can coexist without collision.
EOF
        ;;
esac
