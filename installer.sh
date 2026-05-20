#!/usr/bin/env bash
#
# Studio installer — Agent Orchestration System
#
# Usage:
#   # Safer: download + inspect before running
#   curl -fsSL https://raw.githubusercontent.com/learhy/project-stdio/main/installer.sh > install.sh
#   less install.sh
#   bash install.sh
#
#   # One-liner (convenient, less secure):
#   curl -fsSL https://raw.githubusercontent.com/learhy/project-stdio/main/installer.sh | bash
#
#   # Local clone:
#   bash installer.sh
#   sudo bash installer.sh          # system-wide
#   bash installer.sh --user        # user-local
#   bash installer.sh --dry-run     # preview
#   bash installer.sh --uninstall   # remove
#   bash installer.sh --prefix=/opt/studio  # custom prefix
#
set -euo pipefail

# ── Globals ──────────────────────────────────────────────────────────────────

STUDIO_REPO="https://github.com/learhy/project-stdio.git"
STUDIO_REPO_RAW="https://raw.githubusercontent.com/learhy/project-stdio/main"
SCRIPT_NAME="${0##*/}"
VERSION="0.1.0"

# Will be set by detect step
IS_ROOT=false
IS_TTY=false
HAS_SYSTEMD=false
INSTALL_MODE=""       # system | user | prefix
PREFIX=""             # only set in prefix mode
DRY_RUN=false
UNINSTALL=false

# Derived paths — set by resolve_paths()
BIN_DIR=""
CONFIG_DIR=""
CONFIG_FILE=""
DATA_DIR=""
LOG_DIR=""
SYSTEMD_DIR=""        # empty if no systemd
STUDIO_SRC=""         # where the source tree lives
VENV_DIR=""           # venv inside STUDIO_SRC

# ── Helpers ──────────────────────────────────────────────────────────────────

RED=''
GREEN=''
YELLOW=''
BOLD=''
NC=''

setup_colors() {
    if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]]; then
        IS_TTY=true
        RED='\033[0;31m'
        GREEN='\033[0;32m'
        YELLOW='\033[0;33m'
        BOLD='\033[1m'
        NC='\033[0m'
    fi
}

color_ok()    { echo -e "${GREEN}[ok]${NC} $*"; }
color_warn()  { echo -e "${YELLOW}[warn]${NC} $*"; }
color_err()   { echo -e "${RED}[error]${NC} $*"; }
color_info()  { echo -e "${BOLD}[info]${NC} $*"; }
step()        { echo -e "\n${BOLD}[step]${NC} $*"; }
info()        { echo "       $*"; }
log_ok()      { echo -e "  ${GREEN}[ok]${NC} $*"; }
log_warning() { echo -e "  ${YELLOW}[warn]${NC} $*"; }
log_error()   { echo -e "  ${RED}[error]${NC} $*"; }
log_installing() { echo -e "  ${YELLOW}[installing]${NC} $*"; }
print_header() { echo -e "\n${BOLD}── $* ──${NC}"; }

die() {
    color_err "$@"
    exit 1
}

yn_prompt() {
    local prompt="$1"
    local default="${2:-n}"
    local yn

    if [[ "$IS_TTY" != "true" ]]; then
        # Non-interactive: accept default
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

prompt_value() {
    local prompt="$1"
    local default="${2:-}"
    local val

    if [[ "$IS_TTY" != "true" ]]; then
        echo "$default"
        return
    fi

    if [[ -n "$default" ]]; then
        read -r -p "       ${prompt} [${default}]: " val
        echo "${val:-$default}"
    else
        read -r -p "       ${prompt}: " val
        echo "$val"
    fi
}

cmd_exists() { command -v "$1" &>/dev/null; }

should_skip() { [[ "$DRY_RUN" == "true" ]] && { info "(dry-run) would: $*"; return 0; }; return 1; }

check_cmd() {
    if ! cmd_exists "$1"; then
        color_err "$1 not found in PATH"
        return 1
    fi
    return 0
}

# ── Flag parsing ─────────────────────────────────────────────────────────────

usage() {
    cat <<EOF
Studio installer v${VERSION}

Usage: ${SCRIPT_NAME} [FLAGS]

Flags:
  --help          Show this message
  --version       Print version
  --dry-run       Print what would be done, do nothing
  --uninstall     Remove Studio (same as running uninstall.sh)
  --user          Force user-local install (even if run as root)
  --prefix=PATH   Install under custom prefix (e.g. /opt/studio)
  --no-color      Disable colored output

Install modes:
  Run as root (default):     system-wide under /usr/local, /etc, /var
  Run as user (default):     user-local under ~/.local, ~/.config
  --user flag:               force user-local regardless of privileges
  --prefix=PATH:             all paths relative to PATH

Examples:
  sudo bash installer.sh                  # system-wide install
  bash installer.sh                       # user-local install
  bash installer.sh --prefix=/opt/studio  # custom prefix
  bash installer.sh --uninstall           # remove Studio
  curl -fsSL .../installer.sh | bash     # remote one-liner (user-local)
EOF
    exit 0
}

parse_flags() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --help)    usage ;;
            --version) echo "studio-installer v${VERSION}"; exit 0 ;;
            --dry-run) DRY_RUN=true ;;
            --uninstall) UNINSTALL=true ;;
            --user)    INSTALL_MODE="user" ;;
            --prefix=*) INSTALL_MODE="prefix"; PREFIX="${1#*=}" ;;
            --prefix)  INSTALL_MODE="prefix"; PREFIX="$2"; shift ;;
            --no-color) NO_COLOR=1 ;;
            *) die "Unknown flag: $1 (try --help)" ;;
        esac
        shift
    done
}

# ── Step 1: Detect ───────────────────────────────────────────────────────────

detect_os() {
    step "Step 1: Detecting system"

    local os
    os="$(uname -s)"
    if [[ "$os" != "Linux" ]]; then
        die "Studio requires Linux. Detected: $os"
    fi
    color_ok "OS: Linux"

    local arch
    arch="$(uname -m)"
    case "$arch" in
        x86_64)  color_ok "Architecture: x86_64" ;;
        aarch64) color_ok "Architecture: arm64 (aarch64)" ;;
        arm64)   color_ok "Architecture: arm64" ;;
        *)       color_warn "Architecture: $arch (untested, may work)" ;;
    esac
}

detect_privileges() {
    if [[ "$(id -u)" -eq 0 ]]; then
        IS_ROOT=true
    fi

    # Determine install mode
    if [[ "$INSTALL_MODE" == "user" ]]; then
        info "User-local install (--user flag)"
    elif [[ "$INSTALL_MODE" == "prefix" ]]; then
        info "Custom prefix install: $PREFIX"
    elif [[ "$IS_ROOT" == "true" ]]; then
        INSTALL_MODE="system"
        info "System-wide install (running as root)"
    else
        # Check write access to /usr/local/bin
        if [[ -w /usr/local/bin ]]; then
            INSTALL_MODE="system"
            info "System-wide install (write access to /usr/local/bin)"
        else
            INSTALL_MODE="user"
            color_warn "No write access to /usr/local/bin — falling back to user-local install"
            info "Use --user to suppress this warning, or sudo for system-wide"
        fi
    fi
}

detect_systemd() {
    if cmd_exists systemctl && [[ -d /run/systemd/system ]]; then
        HAS_SYSTEMD=true
        color_ok "systemd: available"
    else
        HAS_SYSTEMD=false
        color_warn "systemd not detected — services will not be installed"
    fi
}

resolve_paths() {
    case "$INSTALL_MODE" in
        system)
            BIN_DIR="/usr/local/bin"
            CONFIG_DIR="/etc/studio"
            CONFIG_FILE="$CONFIG_DIR/settings.json"
            DATA_DIR="/var/lib/studio"
            LOG_DIR="/var/log/studio"
            SYSTEMD_DIR="/etc/systemd/system"
            STUDIO_SRC="/opt/studio"
            ;;
        user)
            BIN_DIR="${HOME}/.local/bin"
            CONFIG_DIR="${HOME}/.config/studio"
            CONFIG_FILE="$CONFIG_DIR/settings.json"
            DATA_DIR="${HOME}/.local/share/studio"
            LOG_DIR="${DATA_DIR}/logs"
            STUDIO_SRC="${DATA_DIR}/src"
            if [[ "$HAS_SYSTEMD" == "true" ]]; then
                SYSTEMD_DIR="${HOME}/.config/systemd/user"
            else
                SYSTEMD_DIR=""
            fi
            ;;
        prefix)
            BIN_DIR="${PREFIX}/bin"
            CONFIG_DIR="${PREFIX}/etc/studio"
            CONFIG_FILE="$CONFIG_DIR/settings.json"
            DATA_DIR="${PREFIX}/var/lib/studio"
            LOG_DIR="${PREFIX}/var/log/studio"
            STUDIO_SRC="${PREFIX}/opt/studio"
            SYSTEMD_DIR=""  # No systemd for prefix installs
            ;;
    esac
    VENV_DIR="${STUDIO_SRC}/.venv"
}

show_paths() {
    info "Install paths:"
    info "  Binaries:    $BIN_DIR"
    info "  Config:      $CONFIG_FILE"
    info "  Data:        $DATA_DIR"
    info "  Logs:        $LOG_DIR"
    info "  Source:      $STUDIO_SRC"
    if [[ -n "$SYSTEMD_DIR" ]]; then
        info "  Systemd:     $SYSTEMD_DIR"
    fi
}

# ── Step 2: Dependencies ─────────────────────────────────────────────────────

dep_status() {
    local name="$1"
    echo -n "       [checking] ${name}... "
}

dep_ok()      { echo -e "${GREEN}ok${NC}"; }
dep_install() { echo -e "${YELLOW}installing${NC}"; }
dep_missing() { echo -e "${RED}missing - manual install required${NC}"; }

check_python() {
    dep_status "python3 >= 3.12"
    if ! cmd_exists python3; then
        dep_missing
        die "python3 not found. Install Python 3.12+ and retry:
  Debian/Ubuntu:  apt install python3.12
  Fedora:         dnf install python3.12
  Arch:           pacman -S python"
    fi

    local pyver
    pyver="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
    local major minor
    major="${pyver%%.*}"
    minor="${pyver##*.}"

    if (( major < 3 )) || (( major == 3 && minor < 12 )); then
        dep_missing
        die "Python $pyver detected, but 3.12+ is required.
  Install Python 3.12+ and ensure 'python3' points to it.
  Debian/Ubuntu:  apt install python3.12
  Fedora:         dnf install python3.12"
    fi

    dep_ok
    info "  version: $pyver"
}

check_uv() {
    dep_status "uv"
    if cmd_exists uv; then
        dep_ok
        info "  $(uv --version 2>/dev/null || true)"
    else
        dep_install
        if ! should_skip "install uv via astral.sh"; then
            curl -LsSf https://astral.sh/uv/install.sh | sh
            # Ensure uv is on PATH for the rest of this script
            export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
            if ! cmd_exists uv; then
                die "uv installed but not found on PATH. Add ~/.local/bin or ~/.cargo/bin to your PATH and retry."
            fi
        fi
        color_ok "uv: installed"
    fi
}

check_git() {
    dep_status "git"
    if cmd_exists git; then
        dep_ok
        info "  $(git --version 2>/dev/null || true)"
    else
        dep_missing
        die "git not found. Install git and retry:
  Debian/Ubuntu:  apt install git
  Fedora:         dnf install git"
    fi
}

detect_distro() {
    if cmd_exists apt-get; then
        echo "debian"
    elif cmd_exists dnf; then
        echo "fedora"
    elif cmd_exists pacman; then
        echo "arch"
    else
        echo "unknown"
    fi
}

check_bubblewrap() {
    dep_status "bubblewrap (bwrap)"
    if cmd_exists bwrap; then
        dep_ok
        info "  $(bwrap --version 2>/dev/null || true)"
        return
    fi

    dep_install
    if should_skip "install bubblewrap"; then
        color_warn "bubblewrap not installed — worker isolation will not be available"
        return
    fi

    local distro
    distro="$(detect_distro)"

    local install_ok=false
    case "$distro" in
        debian)
            if [[ "$IS_ROOT" == "true" ]]; then
                apt-get update -qq && apt-get install -y -qq bubblewrap && install_ok=true
            elif sudo -n true 2>/dev/null; then
                sudo apt-get update -qq && sudo apt-get install -y -qq bubblewrap && install_ok=true
            else
                color_err "bubblewrap requires root. Ask your sysadmin:"
                color_err "  sudo apt-get install bubblewrap"
            fi ;;
        fedora)
            if [[ "$IS_ROOT" == "true" ]]; then
                dnf install -y bubblewrap && install_ok=true
            elif sudo -n true 2>/dev/null; then
                sudo dnf install -y bubblewrap && install_ok=true
            else
                color_err "bubblewrap requires root. Ask your sysadmin:"
                color_err "  sudo dnf install bubblewrap"
            fi ;;
        arch)
            if [[ "$IS_ROOT" == "true" ]]; then
                pacman -S --noconfirm bubblewrap && install_ok=true
            elif sudo -n true 2>/dev/null; then
                sudo pacman -S --noconfirm bubblewrap && install_ok=true
            else
                color_err "bubblewrap requires root. Ask your sysadmin:"
                color_err "  sudo pacman -S bubblewrap"
            fi ;;
        *)
            color_warn "Unknown package manager. Install bubblewrap manually:"
            color_warn "  Debian/Ubuntu:  apt install bubblewrap"
            color_warn "  Fedora:         dnf install bubblewrap"
            color_warn "  Arch:           pacman -S bubblewrap"
            ;;
    esac

    if cmd_exists bwrap; then
        color_ok "bubblewrap: installed"
    elif [[ "$install_ok" == "true" && "$DRY_RUN" != "true" ]]; then
        color_warn "bubblewrap installed but not found on PATH — worker isolation will not work"
    else
        color_warn "bubblewrap not available — worker isolation will not be available"
    fi
}

check_opencode() {
    dep_status "opencode"
    if cmd_exists opencode; then
        dep_ok
        info "  $(opencode --version 2>/dev/null || true)"
        return
    fi

    dep_install
    if should_skip "install opencode"; then
        return
    fi

    # Must pipe to bash, not sh — the install script uses [[ bashisms
    # that fail silently under dash (Debian/Ubuntu default /bin/sh).
    if curl -fsSL https://opencode.ai/install | bash; then
        export PATH="${HOME}/.local/bin:${PATH}"
        if cmd_exists opencode; then
            color_ok "opencode: installed"
        else
            color_warn "opencode install completed but binary not found on PATH"
            color_warn "Look in ~/.local/bin or ~/.cargo/bin and ensure it's on your PATH"
        fi
    else
        color_warn "opencode install failed — developer worker features will not be available"
        color_warn "Install manually: https://opencode.ai/install"
    fi
}

check_docker() {
    dep_status "Docker Engine"
    if command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
        dep_ok
        info "  Docker $(docker --version | cut -d' ' -f3 | tr -d ',')"
        return 0
    fi

    dep_install
    if should_skip "install Docker Engine"; then
        return 0
    fi

    if command -v apt-get &>/dev/null; then
        curl -fsSL https://get.docker.com | sh
        usermod -aG docker "$USER" 2>/dev/null || true
        systemctl enable --now docker 2>/dev/null || true
    elif command -v dnf &>/dev/null; then
        dnf install -y docker docker-compose-plugin
        systemctl enable --now docker
    else
        color_err "Cannot auto-install Docker on this system. Please install Docker Engine manually: https://docs.docker.com/engine/install/"
        return 1
    fi

    if command -v docker &>/dev/null; then
        color_ok "Docker: installed"
        color_info "NOTE: You may need to log out and back in for Docker group membership to take effect."
        info "Run 'newgrp docker' to activate immediately."
    else
        color_err "Docker installation failed"
        return 1
    fi
}

# ── Step 2.5: Firecracker microVM isolation (optional, requires KVM) ──────────

_FC_KVM_AVAILABLE=false

check_firecracker() {
    step "Step 2.5: Firecracker microVM isolation (optional, requires KVM)"

    if [[ ! -e /dev/kvm ]]; then
        color_warn "/dev/kvm not available -- using bubblewrap isolation only"
        echo "         For stronger isolation, enable KVM on this host"
        return
    fi

    local arch
    arch="$(uname -m)"
    if [[ "$arch" != "x86_64" ]]; then
        color_warn "Firecracker only available for x86_64 (detected: $arch). Skipping."
        return
    fi

    _FC_KVM_AVAILABLE=true

    # Check if firecracker binary already exists
    local fc_path
    fc_path="$(command -v firecracker 2>/dev/null || true)"
    if [[ -n "$fc_path" ]]; then
        color_ok "Firecracker already installed at $fc_path, skipping download"
    else
        dep_status "firecracker"
        dep_install
        local FC_VERSION="v1.7.0"
        if ! should_skip "download firecracker ${FC_VERSION}"; then
            curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-x86_64.tgz" \
                | tar -xz -C /usr/local/bin/
        fi

        if [[ -x /usr/local/bin/jailer ]]; then
            dep_ok
            info "  firecracker + jailer installed"
        elif cmd_exists firecracker; then
            color_ok "firecracker: installed"
        else
            color_warn "firecracker may not have installed correctly"
            _FC_KVM_AVAILABLE=false
        fi
    fi
}

setup_firecracker_assets() {
    # Download kernel and build rootfs (requires studio CLI - runs post-install)
    if [[ "$_FC_KVM_AVAILABLE" != "true" ]]; then
        return
    fi

    local kernel_path="${DATA_DIR}/firecracker/vmlinux"
    local rootfs_path="${DATA_DIR}/firecracker/rootfs.ext4"

    step "Step 4.5: Firecracker kernel and rootfs setup"

    if [[ -f "$kernel_path" ]]; then
        color_ok "Kernel already present at $kernel_path, skipping download"
    else
        info "Downloading Firecracker kernel..."
        if ! should_skip "download kernel"; then
            "$BIN_DIR/studio" download-kernel --output "$kernel_path"
        fi
        color_ok "Kernel: downloaded"
    fi

    if [[ -f "$rootfs_path" ]]; then
        color_ok "Rootfs already present at $rootfs_path, skipping build"
    else
        info "Building worker rootfs image..."
        if ! should_skip "build rootfs"; then
            "$BIN_DIR/studio" build-worker-image --output "$rootfs_path"
        fi
        color_ok "Rootfs: built"
    fi

    # Enable Firecracker in settings
    info "Enabling Firecracker in settings..."
    if ! should_skip "configure firecracker.enabled"; then
        write_json_field "$CONFIG_FILE" "firecracker.enabled" "true"
        write_json_field "$CONFIG_FILE" "firecracker.kernel_path" "$kernel_path"
        write_json_field "$CONFIG_FILE" "firecracker.rootfs_path" "$rootfs_path"
        # Enable jailer for system installs (defense-in-depth for production)
        if [[ "$INSTALL_MODE" == "system" ]]; then
            write_json_field "$CONFIG_FILE" "firecracker.jailer_enabled" "true"
            write_json_field "$CONFIG_FILE" "firecracker.jailer_chroot_base" "${DATA_DIR}/firecracker/jailer"
            color_ok "Jailer: enabled (system install)"
        else
            color_info "Jailer: disabled by default (use studio config set firecracker.jailer_enabled true)"
        fi
    fi
    color_ok "Firecracker microVM isolation enabled"
}

# ── Step 3: Repo ─────────────────────────────────────────────────────────────

resolve_source() {
    step "Step 3: Resolving source"

    # Check if we're inside a project-stdio clone
    local in_clone=false
    if [[ -d .git ]] && [[ -f pyproject.toml ]]; then
        local remote
        remote="$(git config --get remote.origin.url 2>/dev/null || true)"
        if [[ "$remote" == *project-stdio* ]]; then
            in_clone=true
            STUDIO_SRC="$(pwd)"
            VENV_DIR="${STUDIO_SRC}/.venv"
            color_ok "Running from existing clone: $STUDIO_SRC"
        fi
    fi

    if [[ "$in_clone" == "true" ]]; then
        return
    fi

    color_ok "Source: cloning from $STUDIO_REPO"

    if [[ -d "$STUDIO_SRC" ]]; then
        color_warn "$STUDIO_SRC already exists — updating"
        if ! should_skip "git pull in $STUDIO_SRC"; then
            git -C "$STUDIO_SRC" pull --ff-only
        fi
    else
        if ! should_skip "git clone to $STUDIO_SRC"; then
            mkdir -p "$(dirname "$STUDIO_SRC")"
            git clone "$STUDIO_REPO" "$STUDIO_SRC"
        fi
    fi
}

# ── Step 4: Install ──────────────────────────────────────────────────────────

install_python_package() {
    step "Step 4: Installing Studio"

    color_ok "Setting up virtual environment and installing package"
    info "  source: $STUDIO_SRC"
    info "  venv:   $VENV_DIR"

    if ! should_skip "uv venv && uv pip install"; then
        cd "$STUDIO_SRC"
        uv venv --python "$(which python3)"
        uv pip install -e .
        cd - > /dev/null
    fi

    color_ok "Package installed"
}

create_bin_wrappers() {
    info "Creating binary wrappers in $BIN_DIR"

    if ! should_skip "create bin wrappers"; then
        mkdir -p "$BIN_DIR"

        local entrypoints=(
            studio
            studio-orchestrator
            studio-worker
            studio-bundler
            studio-review
            studio-mcp
            studio-qa
            studio-proxy
        )

        for ep in "${entrypoints[@]}"; do
            local wrapper="${BIN_DIR}/${ep}"
            cat > "$wrapper" <<WRAPPER
#!/usr/bin/env bash
exec "${VENV_DIR}/bin/${ep}" "\$@"
WRAPPER
            chmod 755 "$wrapper"
            info "  $wrapper"
        done
    fi

    # Ensure bin dir is on PATH for this session
    export PATH="${BIN_DIR}:${PATH}"
}

create_directories() {
    info "Creating directories"

    if ! should_skip "create directories"; then
        mkdir -p "$CONFIG_DIR"
        mkdir -p "$DATA_DIR"
        mkdir -p "$LOG_DIR"

        if [[ "$INSTALL_MODE" == "system" ]]; then
            mkdir -p /run/studio
            # Create studio user/group if they don't exist
            if ! getent group studio &>/dev/null; then
                groupadd --system studio
                info "  created studio group"
            fi
            if ! getent passwd studio &>/dev/null; then
                useradd --system --no-create-home --home-dir "$DATA_DIR" \
                    --gid studio --shell /usr/sbin/nologin studio
                info "  created studio user"
            fi
            chown -R studio:studio "$CONFIG_DIR"
            chown -R studio:studio "$DATA_DIR"
            chown -R studio:studio "$LOG_DIR"
            chown -R studio:studio /run/studio
            chmod 750 "$CONFIG_DIR" "$DATA_DIR" "$LOG_DIR"
        fi
    fi

    color_ok "Directories created"
}

generate_tls_cert() {
    local tls_dir
    tls_dir="$(dirname "$CONFIG_FILE")/tls"
    local ca_cert="${tls_dir}/ca.crt"
    local ca_key="${tls_dir}/ca.key"
    local server_cert="${tls_dir}/server.crt"
    local server_key="${tls_dir}/server.key"

    if [[ -f "$ca_cert" ]] && [[ -f "$ca_key" ]] && [[ -f "$server_cert" ]] && [[ -f "$server_key" ]]; then
        color_warn "mTLS certs already exist at $tls_dir — leaving in place"
        return
    fi

    info "Generating mTLS certificates (CA + server cert)"

    if ! should_skip "generate mTLS certificates"; then
        mkdir -p "$tls_dir"

        # Generate CA private key
        openssl genrsa -out "$ca_key" 4096 2>/dev/null
        chmod 600 "$ca_key"

        # Generate self-signed CA certificate (10 years)
        openssl req -x509 -new -nodes -key "$ca_key" -sha256 -days 3650 \
            -out "$ca_cert" \
            -subj "/CN=Studio Orchestrator CA/O=Studio" 2>/dev/null
        chmod 644 "$ca_cert"

        # Generate server private key
        openssl genrsa -out "$server_key" 4096 2>/dev/null
        chmod 600 "$server_key"

        # Generate server CSR
        local server_csr="${tls_dir}/server.csr"
        openssl req -new -key "$server_key" -out "$server_csr" \
            -subj "/CN=studio-orchestrator/O=Studio" 2>/dev/null

        # Sign server cert with CA
        openssl x509 -req -in "$server_csr" -CA "$ca_cert" -CAkey "$ca_key" \
            -CAcreateserial -out "$server_cert" -days 365 -sha256 2>/dev/null
        chmod 644 "$server_cert"

        # Clean up CSR
        rm -f "$server_csr"

        if [[ "$INSTALL_MODE" == "system" ]]; then
            chown studio:studio "$ca_cert" "$ca_key" "$server_cert" "$server_key" 2>/dev/null || true
        fi

        color_ok "mTLS certificates generated:"
        info "  CA cert:  $ca_cert"
        info "  CA key:   $ca_key"
        info "  Server cert: $server_cert"
        info "  Server key:  $server_key"
        info "  Worker certs are issued automatically at spawn time (20 min validity)."
    fi
}

install_default_config() {
    info "Installing default configuration"

    if [[ -f "$CONFIG_FILE" ]]; then
        color_warn "Config already exists at $CONFIG_FILE — leaving in place"
        return
    fi

    if ! should_skip "copy default config"; then
        if [[ -f "${STUDIO_SRC}/settings.json.example" ]]; then
            cp "${STUDIO_SRC}/settings.json.example" "$CONFIG_FILE"
            chmod 640 "$CONFIG_FILE"
            if [[ "$INSTALL_MODE" == "system" ]]; then
                chown studio:studio "$CONFIG_FILE"
            fi
            color_ok "Default config: $CONFIG_FILE"
        else
            color_warn "No settings.json.example found — skipping config"
        fi
    fi
}

# Render a systemd unit file substituting paths for the target install mode.
# Usage: render_unit <source-unit-path> <output-path>
render_unit() {
    local src="$1"
    local dst="$2"

    local exec_prefix="${VENV_DIR}/bin"

    if [[ "$INSTALL_MODE" == "system" ]]; then
        # System install: paths are standard
        sed \
            -e "s|/usr/bin/studio|${exec_prefix}/studio|g" \
            -e "s|/var/lib/studio|${DATA_DIR}|g" \
            -e "s|/run/studio|/run/studio|g" \
            -e "s|/etc/studio|${CONFIG_DIR}|g" \
            "$src" > "$dst"
    elif [[ "$INSTALL_MODE" == "user" ]]; then
        # User install: remove User=/Group=, adjust paths, add XDG env vars
        sed \
            -e "s|/usr/bin/studio|${exec_prefix}/studio|g" \
            -e "s|/var/lib/studio|${DATA_DIR}|g" \
            -e "s|/run/studio|${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/studio|g" \
            -e "s|/etc/studio|${CONFIG_DIR}|g" \
            -e '/^User=/d' \
            -e '/^Group=/d' \
            "$src" > "$dst"
    fi
}

install_systemd_units() {
    if [[ -z "$SYSTEMD_DIR" ]]; then
        color_warn "No systemd support — skipping service installation"
        return
    fi

    info "Installing systemd units to $SYSTEMD_DIR"

    if ! should_skip "install systemd units"; then
        mkdir -p "$SYSTEMD_DIR"

        local src_dir="${STUDIO_SRC}/studio/systemd"
        local units=("studio-orchestrator.service" "studio-mcp.service")

        for unit in "${units[@]}"; do
            local src="${src_dir}/${unit}"
            local dst="${SYSTEMD_DIR}/${unit}"

            if [[ -f "$src" ]]; then
                render_unit "$src" "$dst"
                chmod 644 "$dst"
                info "  $dst"
            else
                color_warn "Unit file not found: $src"
            fi
        done
    fi

    # Reload and enable
    if ! should_skip "reload systemd and enable units"; then
        if [[ "$INSTALL_MODE" == "user" ]]; then
            systemctl --user daemon-reload
            systemctl --user enable --now studio-orchestrator.service
            systemctl --user enable --now studio-mcp.service
        else
            systemctl daemon-reload
            systemctl enable --now studio-orchestrator.service
            systemctl enable --now studio-mcp.service
        fi
    fi

    color_ok "Systemd units installed and enabled"
}

# ── Step 5: Configure ────────────────────────────────────────────────────────

write_json_field() {
    local file="$1"
    local key="$2"
    local value="$3"

    # Use python3 for reliable JSON editing
    python3 -c "
import json, sys
try:
    with open('$file') as f:
        data = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    data = {}
parts = '$key'.split('.')
d = data
for p in parts[:-1]:
    d = d.setdefault(p, {})
d[parts[-1]] = '$value'
with open('$file', 'w') as f:
    json.dump(data, f, indent=2)
" 2>/dev/null || color_warn "Could not set $key in $file"
}

configure_api_key() {
    step "Step 5: Configuration"

    # Check environment first
    if [[ -n "${OLLAMA_CLOUD_API_KEY:-}" ]]; then
        color_ok "OLLAMA_CLOUD_API_KEY found in environment"
        write_json_field "$CONFIG_FILE" "ollama_cloud.api_key" "$OLLAMA_CLOUD_API_KEY"
        return
    fi

    if [[ "$IS_TTY" == "true" ]]; then
        info "Enter your Ollama Cloud API key (or set OLLAMA_CLOUD_API_KEY env var)"
        local api_key
        read -r -p "       API Key: " api_key
        if [[ -n "$api_key" ]]; then
            write_json_field "$CONFIG_FILE" "ollama_cloud.api_key" "$api_key"
            color_ok "API key written to config"
        else
            color_warn "No API key provided — add ollama_cloud.api_key to $CONFIG_FILE manually"
        fi
    else
        color_warn "No OLLAMA_CLOUD_API_KEY in environment and no TTY — skipping interactive prompt"
        info "Set OLLAMA_CLOUD_API_KEY before running the installer, or edit $CONFIG_FILE"
    fi
}

configure_github() {
    info "GitHub App integration (optional — press Enter to skip)"

    # Check for environment variables first (non-interactive mode)
    local env_app_id="${STUDIO_GITHUB_APP_ID:-}"
    local env_install_id="${STUDIO_GITHUB_INSTALLATION_ID:-}"
    local env_key_path="${STUDIO_GITHUB_KEY_PATH:-}"

    if [[ -n "$env_app_id" && -n "$env_install_id" && -n "$env_key_path" ]]; then
        write_json_field "$CONFIG_FILE" "github.app_id" "$env_app_id"
        write_json_field "$CONFIG_FILE" "github.installation_id" "$env_install_id"
        write_json_field "$CONFIG_FILE" "github.private_key_path" "$env_key_path"
        write_json_field "$CONFIG_FILE" "github.enabled" "true"
        color_ok "GitHub App configured from environment"
        return
    fi

    if [[ "$IS_TTY" != "true" ]]; then
        if [[ -n "$env_app_id" || -n "$env_install_id" || -n "$env_key_path" ]]; then
            color_warn "GitHub App env vars partially set — all three required (STUDIO_GITHUB_APP_ID, STUDIO_GITHUB_INSTALLATION_ID, STUDIO_GITHUB_KEY_PATH). Skipping GitHub setup."
        else
            color_warn "Skipping GitHub setup (non-interactive, no env vars set)"
        fi
        return
    fi

    local app_id
    read -r -p "       GitHub App ID: " app_id
    if [[ -z "$app_id" ]]; then
        color_warn "GitHub App skipped"
        return
    fi

    write_json_field "$CONFIG_FILE" "github.app_id" "$app_id"
    write_json_field "$CONFIG_FILE" "github.enabled" "true"

    local install_id
    read -r -p "       GitHub Installation ID: " install_id
    write_json_field "$CONFIG_FILE" "github.installation_id" "$install_id"

    local key_path
    read -r -p "       GitHub App private key path (.pem): " key_path
    write_json_field "$CONFIG_FILE" "github.private_key_path" "$key_path"

    color_ok "GitHub App configured"
}

configure_mcp_token() {
    local token
    if should_skip "generate MCP token"; then
        token="(dry-run)"
    else
        token="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
        write_json_field "$CONFIG_FILE" "mcp.bearer_token" "$token"
    fi

    echo ""
    echo "   ┌──────────────────────────────────────────────────────────────┐"
    echo "   │  MCP Bearer Token (copy this — it is shown only once):       │"
    echo "   │  ${BOLD}${token}${NC}                               │"
    echo "   └──────────────────────────────────────────────────────────────┘"
    echo ""

    color_ok "MCP bearer token generated and written to $CONFIG_FILE"
}

# ── Step 6: Verify ───────────────────────────────────────────────────────────

verify_install() {
    step "Step 6: Verification"

    local failed=0

    info "Checking studio --help..."
    if should_skip "verify studio"; then
        info "(dry-run) skip"
    elif "$BIN_DIR/studio" --help &>/dev/null; then
        color_ok "studio CLI: OK"
    else
        color_err "studio CLI: FAILED"
        failed=1
    fi

    # Check services if systemd is in use
    if [[ -n "$SYSTEMD_DIR" ]] && ! should_skip "check systemd services"; then
        info "Checking systemd services..."
        sleep 2  # let services settle

        if [[ "$INSTALL_MODE" == "user" ]]; then
            systemctl --user --no-pager status studio-orchestrator.service --no-pager 2>/dev/null || true
            systemctl --user --no-pager status studio-mcp.service --no-pager 2>/dev/null || true
        else
            systemctl --no-pager status studio-orchestrator.service 2>/dev/null || true
            systemctl --no-pager status studio-mcp.service 2>/dev/null || true
        fi
    fi

    if [[ "$failed" -eq 0 ]]; then
        color_ok "Verification: PASSED"
    else
        color_warn "Verification: some checks failed"
    fi
}

# ── Step 7: Next Steps ───────────────────────────────────────────────────────

print_next_steps() {
    step "Step 7: Next Steps"

    local cmd_prefix=""
    if [[ "$INSTALL_MODE" == "user" ]] && ! echo "$PATH" | grep -q "${HOME}/.local/bin"; then
        cmd_prefix="PATH=\"\$HOME/.local/bin:\$PATH\" "
    fi

    cat <<STEPS

  Submit your first bundle:
    ${cmd_prefix}studio submit --prompt "Add a hello-world endpoint"

  Monitor progress:
    ${cmd_prefix}studio health
    ${cmd_prefix}studio tasks

  Connect Claude Desktop to Studio MCP:
    Add this to your Claude Desktop config:
      {
        "mcpServers": {
          "studio": {
            "command": "${BIN_DIR}/studio-mcp",
            "env": {
              "STUDIO_MCP_BEARER_TOKEN": "<your-mcp-bearer-token>"
            }
          }
        }
      }
    (Get your bearer token from: ${CONFIG_FILE} → mcp.bearer_token)

  Documentation:
    ${STUDIO_REPO_RAW}/docs/install.md

  Uninstall:
    bash ${STUDIO_SRC}/uninstall.sh
    # or: bash installer.sh --uninstall

STEPS

    if [[ "$DRY_RUN" == "true" ]]; then
        color_warn "This was a dry run — no changes were made."
    else
        color_ok "Studio installation complete!"
    fi
}

# ── Uninstall ────────────────────────────────────────────────────────────────

do_uninstall() {
    step "Uninstalling Studio"

    # Re-detect mode for cleanup
    detect_os
    detect_privileges
    resolve_paths

    echo ""
    echo "       This will remove:"
    echo "       - Binaries in $BIN_DIR"
    echo "       - Config in $CONFIG_DIR"
    echo "       - Data in $DATA_DIR"
    echo "       - Logs in $LOG_DIR"
    echo "       - Source in $STUDIO_SRC"
    if [[ -n "$SYSTEMD_DIR" ]]; then
        echo "       - Systemd units in $SYSTEMD_DIR"
    fi
    echo ""

    if ! yn_prompt "Proceed with uninstall?" "n"; then
        info "Uninstall cancelled."
        exit 0
    fi

    if should_skip "uninstall"; then
        return
    fi

    # Systemd units
    if [[ -n "$SYSTEMD_DIR" ]]; then
        info "Stopping and disabling services..."
        if [[ "$INSTALL_MODE" == "user" ]]; then
            systemctl --user stop studio-orchestrator.service studio-mcp.service 2>/dev/null || true
            systemctl --user disable studio-orchestrator.service studio-mcp.service 2>/dev/null || true
            rm -f "$SYSTEMD_DIR/studio-orchestrator.service"
            rm -f "$SYSTEMD_DIR/studio-mcp.service"
            systemctl --user daemon-reload 2>/dev/null || true
        else
            systemctl stop studio-orchestrator.service studio-mcp.service 2>/dev/null || true
            systemctl disable studio-orchestrator.service studio-mcp.service 2>/dev/null || true
            rm -f "$SYSTEMD_DIR/studio-orchestrator.service"
            rm -f "$SYSTEMD_DIR/studio-mcp.service"
            systemctl daemon-reload 2>/dev/null || true
        fi
    fi

    # Bin wrappers
    info "Removing binaries..."
    for ep in studio studio-orchestrator studio-worker studio-bundler studio-review studio-mcp studio-qa studio-proxy; do
        rm -f "$BIN_DIR/$ep"
    done

    # Data, config, logs, source
    info "Removing config..."
    rm -rf "$CONFIG_DIR"
    info "Removing data..."
    rm -rf "$DATA_DIR"

    if [[ "$LOG_DIR" != "$DATA_DIR/logs" ]]; then
        info "Removing logs..."
        rm -rf "$LOG_DIR"
    fi

    rm -rf "$STUDIO_SRC"

    # Cleanup studio user/group (system install)
    if [[ "$INSTALL_MODE" == "system" ]]; then
        rm -rf /run/studio
        userdel studio 2>/dev/null || true
        groupdel studio 2>/dev/null || true
    fi

    color_ok "Studio has been uninstalled."
    exit 0
}

# ── Pre-flight checks ────────────────────────────────────────────────────────

check_prerequisites() {
    local failed=0

    print_header "Pre-flight checks"

    # OS check
    if [[ "$(uname -s)" != "Linux" ]]; then
        log_error "project-stdio requires Linux. macOS/Windows: use Docker (see docs/install.md)"
        ((failed++))
    else
        log_ok "Operating system: Linux"
    fi

    # Architecture check
    local arch
    arch=$(uname -m)
    if [[ "$arch" != "x86_64" ]]; then
        log_warning "Architecture $arch: Firecracker isolation not available (x86_64 only). Workers will use bubblewrap."
    else
        log_ok "Architecture: x86_64"
    fi

    # Python 3.12+ check
    local python_cmd=""
    for cmd in python3.12 python3.13 python3; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$($cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
            local major minor
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [[ "$major" -ge 3 && "$minor" -ge 12 ]]; then
                python_cmd="$cmd"
                log_ok "Python $ver ($cmd)"
                break
            fi
        fi
    done
    if [[ -z "$python_cmd" ]]; then
        log_error "Python 3.12+ required but not found."
        log_error "Install: sudo apt install python3.12 python3.12-venv  (Debian/Ubuntu)"
        log_error "         sudo dnf install python3.12                   (RHEL/Fedora)"
        ((failed++))
    fi

    # Git check
    if ! command -v git &>/dev/null; then
        log_error "git required but not found."
        log_error "Install: sudo apt install git  (Debian/Ubuntu)"
        log_error "         sudo dnf install git  (RHEL/Fedora)"
        ((failed++))
    else
        log_ok "git $(git --version | cut -d' ' -f3)"
    fi

    # curl check
    if ! command -v curl &>/dev/null; then
        log_error "curl required but not found."
        log_error "Install: sudo apt install curl"
        ((failed++))
    else
        log_ok "curl $(curl --version | head -1 | cut -d' ' -f2)"
    fi

    # Disk space check (minimum 5GB free in install prefix)
    local free_gb
    free_gb=$(df -BG "${PREFIX:-/usr/local}" 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G')
    if [[ -n "$free_gb" && "$free_gb" -lt 5 ]]; then
        log_error "Insufficient disk space: ${free_gb}GB free, 5GB required (rootfs image ~1GB, worker overlays ~10GB each)"
        ((failed++))
    else
        log_ok "Disk space: ${free_gb:-?}GB free"
    fi

    # KVM check (non-fatal, just inform)
    if [[ -e /dev/kvm ]]; then
        log_ok "KVM available: Firecracker microVM isolation will be enabled"
    else
        log_warning "KVM not available: Firecracker isolation disabled. Workers will use bubblewrap."
        log_warning "For KVM on cloud VMs: AWS use .metal instances, GCP use --enable-nested-virtualization"
    fi

    # Internet connectivity check
    if ! curl -sf --max-time 10 https://github.com &>/dev/null; then
        log_error "No internet connectivity (cannot reach github.com)"
        ((failed++))
    else
        log_ok "Internet connectivity"
    fi

    # Summary
    if [[ "$failed" -gt 0 ]]; then
        echo ""
        log_error "Pre-flight check failed: $failed prerequisite(s) missing."
        log_error "Please fix the issues above and re-run the installer."
        log_error "The installer has NOT modified your system."
        exit 1
    fi

    log_ok "All pre-flight checks passed"
    echo ""
}

# ── Main ─────────────────────────────────────────────────────────────────────

main() {
    setup_colors
    parse_flags "$@"

    echo -e "${BOLD}Studio Installer v${VERSION}${NC}"

    if [[ "$UNINSTALL" == "true" ]]; then
        do_uninstall
    fi

    # ── Pre-flight: check prerequisites before touching anything ──
    check_prerequisites

    # ── Step 1: Detect ──
    detect_os
    detect_privileges
    detect_systemd
    resolve_paths
    show_paths

    # ── Step 2: Dependencies ──
    step "Step 2: Checking dependencies"
    check_python
    check_uv
    check_git
    check_bubblewrap
    check_opencode
    check_docker

    # ── Step 2.5: Firecracker (system dep only -- binary download) ──
    check_firecracker

    # ── Step 3: Repo ──
    resolve_source

    # ── Step 4: Install ──
    install_python_package
    create_bin_wrappers
    create_directories
    generate_tls_cert
    install_default_config
    install_systemd_units

    # ── Step 4.5: Firecracker kernel and rootfs (requires studio CLI) ──
    setup_firecracker_assets

    # ── Step 5: Configure ──
    configure_api_key
    configure_github
    configure_mcp_token

    # ── Step 6: Verify ──
    verify_install

    # ── Step 7: Next Steps ──
    print_next_steps
}

main "$@"
