# Studio Installation

## Prerequisites

- **Linux** (kernel 5.x+). macOS and Windows are not supported.
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

**Worker isolation not working.**
Verify bubblewrap is installed: `bwrap --version`. Install it with your package manager if missing.
