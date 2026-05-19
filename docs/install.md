# Studio Installation

## Prerequisites

- **Linux** (kernel 5.x+) for VM/bare-metal install. macOS and Windows are supported via the Docker installation path.
- **Python 3.12+** (`python3 --version`)
- **git**
- **bubblewrap** (`bwrap --version`) — for worker sandboxing
- **opencode** — AI coding agent CLI used by workers
- **uv** — Python package manager

All dependencies except Python and git are auto-installed by the installer when using a supported package manager (apt, dnf).

## Quick install (one-liner)

```bash
curl -fsSL https://raw.githubusercontent.com/learhy/project-stdio/main/installer.sh | bash
```

This runs a user-local install. It does not need root, installs to `~/.local/bin`, and enables systemd user services.

**Safer alternative** — download first and inspect:

```bash
curl -fsSL https://raw.githubusercontent.com/learhy/project-stdio/main/installer.sh > install.sh
less install.sh
bash install.sh
```

## System-wide install (requires root)

```bash
sudo bash installer.sh
```

Or if you cloned the repo:

```bash
git clone https://github.com/learhy/project-stdio.git
cd project-stdio
sudo bash installer.sh
```

Installs to:
- Binaries: `/usr/local/bin/`
- Config: `/etc/studio/settings.json`
- Data: `/var/lib/studio/`
- Logs: `/var/log/studio/`
- Services: `/etc/systemd/system/` (studio-orchestrator.service, studio-mcp.service)
- Source: `/opt/studio/`

Creates a `studio` system user and group.

## User-local install

```bash
bash installer.sh --user
```

Installs to:
- Binaries: `~/.local/bin/`
- Config: `~/.config/studio/settings.json`
- Data: `~/.local/share/studio/`
- Logs: `~/.local/share/studio/logs/`
- Services: `~/.config/systemd/user/`
- Source: `~/.local/share/studio/src/`

## Custom prefix install

```bash
bash installer.sh --prefix=/opt/studio
```

All paths are rooted under the prefix. Systemd services are not installed in prefix mode.

## Flags

| Flag | Description |
|------|-------------|
| `--help` | Show usage |
| `--version` | Print installer version |
| `--dry-run` | Preview actions without making changes |
| `--uninstall` | Remove Studio |
| `--user` | Force user-local install |
| `--prefix=PATH` | Install under custom prefix |
| `--no-color` | Disable colored output |

## What gets installed

### Binaries

| Binary | Purpose |
|--------|---------|
| `studio` | CLI for bundle submission, health, task inspection |
| `studio-orchestrator` | Main orchestrator daemon |
| `studio-worker` | Developer worker (implements tasks) |
| `studio-bundler` | Bundler worker (idea → proposal + DAG) |
| `studio-review` | Security/QA review worker |
| `studio-qa` | Post-execution verification worker |
| `studio-mcp` | Model Context Protocol server |
| `studio-proxy` | Egress proxy for worker isolation |

### Services (systemd)

- `studio-orchestrator.service` — main orchestration kernel
- `studio-mcp.service` — MCP endpoint for Claude Desktop integration

### Directories

| Path (system) | Path (user) | Purpose |
|---------------|-------------|---------|
| `/etc/studio/` | `~/.config/studio/` | Configuration |
| `/var/lib/studio/` | `~/.local/share/studio/` | SQLite state DB, memory |
| `/var/log/studio/` | `~/.local/share/studio/logs/` | Worker and orchestrator logs |
| `/run/studio/` | `$XDG_RUNTIME_DIR/studio/` | Unix domain sockets |
| `/opt/studio/` | `~/.local/share/studio/src/` | Source tree and venv |

## Post-install configuration

### Ollama Cloud API key

If `OLLAMA_CLOUD_API_KEY` is set in the environment before running the installer, it is written to `settings.json` automatically. Otherwise you are prompted interactively.

To set it after install, edit the config file and add:

```json
"ollama_cloud": {
  "api_key": "your-key-here"
}
```

### GitHub App (optional)

The installer prompts for GitHub App credentials. To configure after install, set these fields in `settings.json`:

```json
"github": {
  "enabled": true,
  "app_id": "123456",
  "installation_id": "12345678",
  "private_key_path": "/etc/studio/github.pem"
}
```

### MCP bearer token

A random 32-byte hex token is generated during install. It is printed once to the terminal and written to `settings.json` under `mcp.bearer_token`.

To regenerate:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Then update `mcp.bearer_token` in your config file.

## Connecting Claude Desktop

Add this to your Claude Desktop configuration:

```json
{
  "mcpServers": {
    "studio": {
      "command": "/usr/local/bin/studio-mcp",
      "env": {
        "STUDIO_MCP_BEARER_TOKEN": "<token-from-settings.json>"
      }
    }
  }
}
```

For user-local install, change the command to `~/.local/bin/studio-mcp`.

Claude Desktop config locations:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

## Verifying the install

```bash
studio --help
studio health
```

Check service status:

```bash
# System install
systemctl status studio-orchestrator
systemctl status studio-mcp

# User install
systemctl --user status studio-orchestrator
systemctl --user status studio-mcp
```

View logs:

```bash
# System install
journalctl -u studio-orchestrator -f
journalctl -u studio-mcp -f

# User install
journalctl --user -u studio-orchestrator -f
journalctl --user -u studio-mcp -f
```

## VM / Linux server installation

For installing on a fresh Linux VM or server for production use.

### 1. Prerequisites

```bash
apt update && apt install -y python3.12 python3-pip git bubblewrap
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://opencode.ai/install | bash
```

### 2. Clone and run installer

```bash
git clone https://github.com/learhy/project-stdio.git
cd project-stdio
sudo bash installer.sh
```

### 3. Configure settings.json

Edit `/etc/studio/settings.json` to set production paths and enable remote workers:

```json
{
  "orchestrator": {
    "socket_path": "/run/studio/orchestrator.sock",
    "db_path": "/var/lib/studio/state.db",
    "memory_root": "/var/lib/studio/memory/"
  },
  "remote_workers": {
    "enabled": true,
    "listen_addr": "0.0.0.0:7811"
  }
}
```

### 4. Enable and start via systemd

```bash
systemctl enable --now studio-orchestrator
systemctl enable --now studio-mcp
```

### 5. Open firewall port 7811 for worker connections

```bash
# ufw
ufw allow 7811/tcp

# firewalld
firewall-cmd --add-port=7811/tcp --permanent && firewall-cmd --reload
```

### 6. Verify

```bash
studio health
studio status
```

## Docker installation (recommended for macOS/Windows)

Run the orchestrator in a Docker container with workers as sibling containers via DockerWorkerRunner. This path supports Linux, macOS, and Windows hosts.

### 1. Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed and running
- git

### 2. Clone and configure

```bash
git clone https://github.com/learhy/project-stdio.git
cd project-stdio
mkdir -p config data
cp settings.json.example config/settings.json
```

Edit `config/settings.json` to set your `OLLAMA_CLOUD_API_KEY` and any other required fields. The `docker_runner` section is pre-configured for the Docker path.

### 3. Start the orchestrator

```bash
OLLAMA_CLOUD_API_KEY="your-key" docker compose up -d
```

### 4. Verify

```bash
# Check containers are running
docker compose ps

# View orchestrator logs
docker logs studio-orchestrator-1

# Check health
docker exec studio-orchestrator-1 studio health
```

### 5. Submit a bundle

```bash
docker exec studio-orchestrator-1 studio submit /build/studio/tests/fixtures/hello-world.json
```

Workers run as sibling containers automatically via DockerWorkerRunner. Use `studio docker-status` and `studio docker-images` inside the container to inspect them.

### 6. Docker socket security

Mounting `/var/run/docker.sock` into the orchestrator container gives it root-equivalent access to the host. This is a standard pattern for container-spawning orchestrators (used by Portainer, Watchtower, CI runners) but it is a meaningful privilege. If this is a concern, use the K8sJobWorkerRunner instead, which scopes permissions to a namespace. See the warning comment in `docker-compose.yml`.

## Kubernetes installation

For running workers on a Kubernetes cluster. The orchestrator itself can run anywhere (VM, Docker, or bare-metal) — only workers are scheduled on the cluster.

### 1. Prerequisites

- `kubectl` configured with access to the target cluster
- Helm 3 installed
- Orchestrator already running (VM or Docker path above) with `remote_workers.enabled: true`

### 2. Install the Helm chart

```bash
helm install studio-workers deploy/helm/studio-workers/
```

This creates the `studio-workers` namespace, ServiceAccount, Role, RoleBinding, and default NetworkPolicy.

### 3. Verify RBAC

```bash
kubectl get pods -n studio-workers
kubectl get sa,role,rolebinding -n studio-workers
```

### 4. Configure settings.json

On the orchestrator host, edit `settings.json` to enable the k8s runner:

```json
{
  "k8s_runner": {
    "enabled": true,
    "orchestrator_tcp_addr": "<your-orchestrator-host>:7811"
  },
  "runner_selector": {
    "allow_unenforced_grants": true
  }
}
```

`orchestrator_tcp_addr` must be reachable from the Kubernetes cluster. `allow_unenforced_grants: true` is required because k8s cannot enforce exec_allowlist at the kernel level (the same grant is enforced by the worker via RPC).

### 5. Verify connectivity

```bash
studio k8s-status
```

If the orchestrator can reach the cluster, this shows no active Jobs (none running yet).

### 6. Test with a bundle

Submit a bundle with `runner_preference: k8s` in the task spec:

```bash
echo '{
  "bundle_input": {"idea": "K8s test: add a hello-world endpoint"},
  "task_dag": {
    "nodes": [{"id": "n1", "kind": "worker", "spec": {
      "objective": "Create hello endpoint",
      "runner_preference": "k8s"
    }}],
    "edges": [], "entry_nodes": ["n1"], "exit_nodes": ["n1"]
  }
}' | studio submit -
```

Watch workers spin up:

```bash
kubectl get jobs -n studio-workers -w
```

## Uninstalling

From within the source tree:

```bash
bash uninstall.sh
sudo bash uninstall.sh          # if installed system-wide
bash uninstall.sh --user        # force user-local removal
```

Or via the installer:

```bash
bash installer.sh --uninstall
```

The uninstaller stops services, removes binaries, config, data, logs, and the source tree. For system installs it also removes the `studio` user and group.

Use `--dry-run` to preview what will be removed.

## Troubleshooting

**`studio` command not found.**
Ensure the binary directory is on your PATH. For user-local install, add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile.

**Services fail to start.**
Check `journalctl -u studio-orchestrator` for errors. Common issues: missing Python dependencies, wrong file permissions, port conflicts.

**`Permission denied` on sockets.**
Ensure `/run/studio/` exists and is owned by `studio:studio`. Run `sudo mkdir -p /run/studio && sudo chown studio:studio /run/studio`.

## Firecracker microVM isolation (optional)

For stronger worker isolation, Studio can run workers inside Firecracker microVMs. This adds a hypervisor boundary (KVM) under the existing bubblewrap sandbox — defense in depth.

### Requirements

- **Linux x86_64** (KVM required; aarch64 not yet supported)
- **KVM** available at `/dev/kvm`
- **Docker** (only needed to build the rootfs image, not at runtime)
- **~1GB disk space** for the rootfs image
- **~100MB per concurrent worker** for overlay filesystems

### KVM permissions

The orchestrator user needs read/write access to `/dev/kvm`. On most Linux systems:

```bash
sudo usermod -aG kvm $USER
```

Log out and back in for the group change to take effect.

### Disk space

The worker rootfs image is typically 500MB–1GB. Each concurrent worker gets a tmpfs-backed overlay layer, typically <100MB for code generation tasks. Plan for:

| Component | Approximate size |
|-----------|-----------------|
| Rootfs ext4 image | 500 MB – 1 GB |
| Kernel image | ~15 MB |
| Per-worker overlay | <100 MB |
| Firecracker binary | ~10 MB |

Docker is **not required at runtime** — once the rootfs image is built, workers run directly in Firecracker VMs without any Docker dependency.

### Enabling Firecracker

The installer enables Firecracker automatically if KVM is detected. To enable manually:

```bash
# Install Firecracker binary
curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/v1.7.0/firecracker-v1.7.0-x86_64.tgz" \
    | tar -xz -C /usr/local/bin/

# Download kernel
studio download-kernel

# Build worker rootfs
studio build-worker-image

# Enable in settings
studio config set firecracker.enabled true
```

Check status:

```bash
studio vm-status
studio check-rootfs
```

To disable Firecracker and fall back to bubblewrap:

```bash
studio config set firecracker.enabled false
```

**Worker isolation not working.**
Verify bubblewrap is installed: `bwrap --version`. Install it with your package manager if missing.
