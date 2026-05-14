#!/usr/bin/env bash
#
# Studio uninstaller — removes all installed files
#
# Usage:
#   bash uninstall.sh
#   sudo bash uninstall.sh      # if installed system-wide
#   bash uninstall.sh --user    # force user-local removal
#   bash uninstall.sh --dry-run # preview
#
set -euo pipefail

RED=''
GREEN=''
YELLOW=''
BOLD=''
NC=''

setup_colors() {
    if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
        RED='\033[0;31m'
        GREEN='\033[0;32m'
        YELLOW='\033[0;33m'
        BOLD='\033[1m'
        NC='\033[0m'
    fi
}

color_ok()   { echo -e "${GREEN}[ok]${NC} $*"; }
color_warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
color_err()  { echo -e "${RED}[error]${NC} $*"; }
step()       { echo -e "\n${BOLD}[step]${NC} $*"; }
info()       { echo "       $*"; }

die() {
    color_err "$@"
    exit 1
}

yn_prompt() {
    local prompt="$1"
    local default="${2:-n}"
    local yn

    if [[ ! -t 0 ]]; then
        [[ "$default" == "y" ]] && return 0 || return 1
    fi

    if [[ "$default" == "y" ]]; then
        read -r -p "       ${prompt} [Y/n]: " yn
        [[ "${yn:-}" =~ ^[Nn]$ ]] && return 1 || return 0
    else
        read -r -p "       ${prompt} [y/N]: " yn
        [[ "${yn:-}" =~ ^[Yy]$ ]] && return 0 || return 1
    fi
}

cmd_exists() { command -v "$1" &>/dev/null; }

# ── Flags ────────────────────────────────────────────────────────────────────

DRY_RUN=false
FORCE_USER=false
PREFIX=""

usage() {
    cat <<EOF
Studio uninstaller

Usage: uninstall.sh [FLAGS]

Flags:
  --help        Show this message
  --dry-run     Print what would be removed, do nothing
  --user        Remove user-local install (even if run as root)
  --prefix=PATH Remove install under custom prefix
  --no-color    Disable colored output
EOF
    exit 0
}

parse_flags() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help)    usage ;;
            --dry-run) DRY_RUN=true ;;
            --user)    FORCE_USER=true ;;
            --prefix=*) PREFIX="${1#*=}" ;;
            --prefix)  PREFIX="$2"; shift ;;
            --no-color) NO_COLOR=1 ;;
            *) die "Unknown flag: $1 (try --help)" ;;
        esac
        shift
    done
}

# ── Detection ────────────────────────────────────────────────────────────────

detect_mode() {
    if [[ -n "$PREFIX" ]]; then
        BIN_DIR="${PREFIX}/bin"
        CONFIG_DIR="${PREFIX}/etc/studio"
        DATA_DIR="${PREFIX}/var/lib/studio"
        LOG_DIR="${PREFIX}/var/log/studio"
        STUDIO_SRC="${PREFIX}/opt/studio"
        SYSTEMD_DIR=""
        return
    fi

    if [[ "$FORCE_USER" == "true" ]]; then
        BIN_DIR="${HOME}/.local/bin"
        CONFIG_DIR="${HOME}/.config/studio"
        DATA_DIR="${HOME}/.local/share/studio"
        LOG_DIR="${DATA_DIR}/logs"
        STUDIO_SRC="${DATA_DIR}/src"
        if cmd_exists systemctl && [[ -d /run/systemd/system ]]; then
            SYSTEMD_DIR="${HOME}/.config/systemd/user"
        else
            SYSTEMD_DIR=""
        fi
        return
    fi

    if [[ "$(id -u)" -eq 0 ]]; then
        BIN_DIR="/usr/local/bin"
        CONFIG_DIR="/etc/studio"
        DATA_DIR="/var/lib/studio"
        LOG_DIR="/var/log/studio"
        STUDIO_SRC="/opt/studio"
        if cmd_exists systemctl && [[ -d /run/systemd/system ]]; then
            SYSTEMD_DIR="/etc/systemd/system"
        else
            SYSTEMD_DIR=""
        fi
    else
        BIN_DIR="${HOME}/.local/bin"
        CONFIG_DIR="${HOME}/.config/studio"
        DATA_DIR="${HOME}/.local/share/studio"
        LOG_DIR="${DATA_DIR}/logs"
        STUDIO_SRC="${DATA_DIR}/src"
        if cmd_exists systemctl && [[ -d /run/systemd/system ]]; then
            SYSTEMD_DIR="${HOME}/.config/systemd/user"
        else
            SYSTEMD_DIR=""
        fi
    fi
}

should_skip() {
    if [[ "$DRY_RUN" == "true" ]]; then
        info "(dry-run) would: $*"
        return 0
    fi
    return 1
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    setup_colors
    parse_flags "$@"
    detect_mode

    step "Uninstalling Studio"

    echo ""
    echo "       This will remove:"
    echo "       - Binaries in $BIN_DIR"
    echo "       - Config in  $CONFIG_DIR"
    echo "       - Data in    $DATA_DIR"
    echo "       - Logs in    $LOG_DIR"
    echo "       - Source in  $STUDIO_SRC"
    if [[ -n "${SYSTEMD_DIR:-}" ]]; then
        echo "       - Systemd units in $SYSTEMD_DIR"
    fi
    if [[ "$(id -u)" -eq 0 ]] && [[ -z "$PREFIX" ]] && [[ "$FORCE_USER" != "true" ]]; then
        echo "       - studio user/group"
        echo "       - /run/studio"
    fi
    echo ""

    if ! yn_prompt "Proceed with uninstall?" "n"; then
        info "Uninstall cancelled."
        exit 0
    fi

    # Stop and remove systemd units
    if [[ -n "${SYSTEMD_DIR:-}" ]]; then
        step "Stopping services"
        local is_user=false
        [[ "$SYSTEMD_DIR" == *"user"* ]] && is_user=true

        for svc in studio-orchestrator.service studio-mcp.service; do
            if should_skip "stop $svc"; then continue; fi
            if [[ "$is_user" == "true" ]]; then
                systemctl --user stop "$svc" 2>/dev/null || true
                systemctl --user disable "$svc" 2>/dev/null || true
            else
                systemctl stop "$svc" 2>/dev/null || true
                systemctl disable "$svc" 2>/dev/null || true
            fi
        done

        if ! should_skip "remove unit files"; then
            rm -f "$SYSTEMD_DIR/studio-orchestrator.service"
            rm -f "$SYSTEMD_DIR/studio-mcp.service"
        fi

        if [[ "$is_user" == "true" ]]; then
            systemctl --user daemon-reload 2>/dev/null || true
            systemctl --user reset-failed 2>/dev/null || true
        else
            systemctl daemon-reload 2>/dev/null || true
            systemctl reset-failed 2>/dev/null || true
        fi
        color_ok "Services stopped and removed"
    fi

    # Remove binaries
    step "Removing binaries"
    for ep in studio studio-orchestrator studio-worker studio-bundler studio-review studio-mcp studio-qa studio-proxy; do
        if should_skip "remove $BIN_DIR/$ep"; then continue; fi
        rm -f "$BIN_DIR/$ep"
        info "  removed $ep"
    done

    # Remove config
    step "Removing configuration"
    if should_skip "remove $CONFIG_DIR"; then true; else
        rm -rf "$CONFIG_DIR"
        color_ok "Removed $CONFIG_DIR"
    fi

    # Remove data (includes logs if user-local)
    step "Removing data"
    if should_skip "remove $DATA_DIR"; then true; else
        rm -rf "$DATA_DIR"
        color_ok "Removed $DATA_DIR"
    fi

    # Remove external log dir (system/prefix modes)
    if [[ "$LOG_DIR" != "$DATA_DIR"/* ]] && [[ -d "$LOG_DIR" ]]; then
        step "Removing logs"
        if should_skip "remove $LOG_DIR"; then true; else
            rm -rf "$LOG_DIR"
            color_ok "Removed $LOG_DIR"
        fi
    fi

    # Remove source
    step "Removing source"
    if should_skip "remove $STUDIO_SRC"; then true; else
        rm -rf "$STUDIO_SRC"
        color_ok "Removed $STUDIO_SRC"
    fi

    # Cleanup system-level artifacts
    if [[ "$(id -u)" -eq 0 ]] && [[ -z "$PREFIX" ]] && [[ "$FORCE_USER" != "true" ]]; then
        step "Cleaning system artifacts"
        if should_skip "remove /run/studio"; then true; else
            rm -rf /run/studio
        fi
        if should_skip "remove studio user/group"; then true; else
            userdel studio 2>/dev/null || true
            groupdel studio 2>/dev/null || true
        fi
    fi

    echo ""
    if [[ "$DRY_RUN" == "true" ]]; then
        color_warn "This was a dry run — no changes were made."
    else
        color_ok "Studio has been uninstalled."
    fi
}

main "$@"
