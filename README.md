# Studio

Studio lets you describe software you want built, review the plan, and come back to a pull request. It runs on your laptop for solo development or on a server for a team — autonomously, overnight, while you're in meetings.

Under the hood it's an orchestration system for AI coding agents: it plans work, reviews it before execution, runs workers in isolated environments, verifies the output, and tracks how accurate its estimates were over time. But mostly you'll interact with it through `studio submit`, `studio show`, and `studio approve`.

```bash
# Install
curl -fsSL https://raw.githubusercontent.com/learhy/project-stdio/main/installer.sh | bash

# Submit an idea
studio submit '{"bundle_input": {"idea": "Add rate limiting to the API gateway"}}'

# Review the plan, then approve
studio show <bundle-id>
studio approve <bundle-id>

# Your code lands in a GitHub PR
```

## What makes it different

Most agent tools hand a task to an LLM and hope for the best. Studio puts structure around the whole lifecycle:

**Typed capability manifest** — every worker declares upfront what it can read, write, execute, and reach on the network. These grants are enforced at every operation, not just suggested to the model.

**Five-tier approval matrix** — work is scored for complexity and risk before execution. Low-risk work auto-approves and runs immediately. High-risk work waits for human sign-off. The tiers (`auto`, `auto_notify`, `summary`, `full_review`, `full_review_cooldown`) give you control over when you want to be in the loop.

**Calibration loop** — after every bundle completes, estimated outcomes are compared to actual ones. Divergence above 50% triggers an automatic post-mortem. Over time the system gets better at predicting how long things take and how big the code will be.

**Mid-flight quality management** — workers can ask questions mid-execution rather than guessing. The orchestrator periodically checks in on running workers, can inject corrective context, and escalates to you when it isn't sure what to do.

**Self-healing inner loop** — before committing any code, the developer worker runs it against the verification strategy (smoke tests, pytest, `docker build`, or whatever the bundler planned), fixes failures autonomously up to 5 times, and only commits when it passes.

## Installation

### One-liner

```bash
# Safer: download + inspect before running
curl -fsSL https://raw.githubusercontent.com/learhy/project-stdio/main/installer.sh > /tmp/studio-installer.sh
less /tmp/studio-installer.sh
bash /tmp/studio-installer.sh

# One-liner (convenient, less secure):
curl -fsSL https://raw.githubusercontent.com/learhy/project-stdio/main/installer.sh | bash
```

The download-then-run pattern respects the script's shebang and lets you inspect before executing.

The installer checks prerequisites before touching anything, installs what's missing, prompts for your Ollama Cloud API key and optional GitHub App credentials, and starts the orchestrator under systemd.

For fully unattended installation:

```bash
OLLAMA_CLOUD_API_KEY=<your-key> \
STUDIO_GITHUB_APP_ID=<app-id> \
STUDIO_GITHUB_INSTALLATION_ID=<installation-id> \
STUDIO_GITHUB_KEY_PATH=/path/to/github-app.pem \
curl -fsSL https://raw.githubusercontent.com/learhy/project-stdio/main/installer.sh | bash
```

### What the installer needs

- **Linux** (x86_64). macOS and Windows users: use the Docker path — see [docs/install.md](docs/install.md).
- **Python 3.12+** — `sudo apt install python3.12` if missing.
- **Git** — `sudo apt install git` if missing.
- **Ollama Cloud account** — sign up at [ollama.com](https://ollama.com) to get an API key.
- **GitHub App** — optional, but required for the Issues and PR surfaces. See [Getting started with GitHub](#getting-started-with-github).

Everything else (uv, bubblewrap, opencode, Docker, Firecracker) is handled by the installer.

### Installation options

| Flag | Effect |
|------|--------|
| `--user` | Install to `~/.local/`. No root required. |
| `--prefix=PATH` | Custom install prefix. |
| `--docker` | Docker-based execution. Builds worker images, starts via docker compose. Recommended for macOS/Windows. |
| `--dry-run` | Show what would be installed without doing it. |
| `--uninstall` | Remove a previous installation. |

For Docker, Kubernetes, and VM-specific setups, see [docs/install.md](docs/install.md).

## Your first bundle

After installation, `studio` is on your PATH and the orchestrator is running.

### Submit an idea

```bash
studio submit '{"bundle_input": {"idea": "Build a Python Flask API with one endpoint GET /hello that returns {\"message\": \"hello world\"} as JSON. Include a requirements.txt and a test file."}}'
```

This prints a bundle ID. The bundler worker picks it up and starts planning.

### Watch it plan

```bash
studio show <bundle-id>
```

Run this every 30 seconds or so. When the state moves to `in_review`, the plan is ready.

### Review and approve

```bash
studio show <bundle-id>     # Read the proposal, DAG, capability grants, and review findings
studio approve <bundle-id>  # Start execution
```

### Watch it build

```bash
watch -n 10 'studio show <bundle-id>'
```

Workers run, commit code, and verify it. If a worker hits a question it can't answer from the spec, it appears under `Pending questions` in the show output. Answer it:

```bash
studio answer-question <question-id> "Use the existing auth helper at lib/auth/oauth.py"
```

### Find your code

When the bundle reaches `complete`, the code is either:

- **On GitHub**: a new repo or a pull request, depending on the `target_repo` in the bundle input. The PR URL appears in `studio show`.
- **Local worktree** (if no GitHub App configured): at `/tmp/studio-worktrees/<bundle-id>/`. Run `studio show <bundle-id> --verbose` to see the path.

## Worker isolation

Studio supports multiple isolation backends. The right one depends on your setup:

| Runner | Isolation | When to use |
|--------|-----------|-------------|
| `local` | bubblewrap (Linux namespaces) | Development on a Linux machine |
| `remote_ssh` | bubblewrap on remote hosts | Managed Linux fleet |
| `k8s` | Kubernetes Pod + NetworkPolicy | Cloud-native, autoscaling |
| `docker` | Docker containers | macOS, Windows, or CI |
| `firecracker` | Firecracker microVM | Production, untrusted code, eBPF |

The default is `local` on Linux, `docker` on macOS/Windows. You can set a preference per bundle:

```json
{"bundle_input": {"idea": "...", "runner_preference": "firecracker"}}
```

**On security**: bubblewrap is adequate for trusted worker code on a machine you control. Once untrusted code enters the picture — npm/pip packages installed by the agent, user-provided scripts — the Firecracker runner provides a real hypervisor boundary. eBPF agents and other privileged workloads run inside Firecracker VMs with explicitly-granted capabilities (`CAP_BPF`, `CAP_PERFMON`) without exposing the host kernel.

### Firecracker setup

```bash
studio download-kernel          # Fetch a compatible kernel (~40MB)
studio build-worker-image       # Build the worker rootfs from Dockerfile.worker (~800MB)
studio config set firecracker.enabled true
studio vm-status                # Verify the pool is pre-warmed
```

The orchestrator warns at startup if the worker rootfs is out of date relative to `Dockerfile.worker`. Rebuild it with `studio build-worker-image` when that happens.

## CLI reference

All commands require `STUDIO_SOCKET_PATH` if the socket is not at the default `/run/studio/orchestrator.sock`. The installer configures this automatically.

### Bundles

| Command | Description |
|---------|-------------|
| `studio submit '<json>'` | Submit a bundle from an inline JSON string or `@file.json` |
| `studio list` | List all bundles with current state |
| `studio show <id>` | Full bundle details: DAG, workers, review findings, outcome |
| `studio show <id> --verbose` | Adds full proposal JSON and artifact paths |
| `studio approve <id>` | Approve a bundle in `in_review` |
| `studio kill <id>` | Terminate workers and fail the bundle |

### Workers and escalations

| Command | Description |
|---------|-------------|
| `studio pending-escalations` | List all worker questions waiting for PM response |
| `studio answer-question <id> "<text>"` | Answer a worker's question |
| `studio resume-worker <id> [--context "<text>"]` | Resume a paused worker with optional guidance |
| `studio review-worker <id>` | Trigger an immediate quality check-in on a running worker |

### Fleet and infrastructure

| Command | Description |
|---------|-------------|
| `studio fleet-status` | SSH fleet hosts, health, active worker counts |
| `studio k8s-status` | Active Kubernetes Jobs in the `studio-workers` namespace |
| `studio docker-status` | Running worker containers with resource usage |
| `studio vm-status` | Firecracker VM pool state |
| `studio vm-pool-resize <n>` | Resize the pre-warmed Firecracker pool |

### Operations

| Command | Description |
|---------|-------------|
| `studio status` | Quick orchestrator status |
| `studio health` | Detailed health: DB, stalled bundles, recent errors |
| `studio calibration-report` | Estimation accuracy and code quality metrics |
| `studio config set <key> <value>` | Update a settings key (dot notation) |
| `studio config get <key>` | Read a settings value |
| `studio --version` | Installed version, and whether the running orchestrator is current |

## Configuration

Configuration lives at `memory/settings.json` in the project directory. The most important keys:

```json
{
  "ollama_cloud": {
    "base_url": "https://ollama.com/v1"
  },
  "github": {
    "enabled": false,
    "app_id": null,
    "installation_id": null,
    "private_key_path": null,
    "webhook_secret": null,
    "owner": null,
    "repo": null
  },
  "firecracker": {
    "enabled": false,
    "pool_size": 3
  },
  "developer": {
    "max_fix_attempts": 5
  },
  "ops": {
    "timeout_multiplier": 3.0
  }
}
```

`OLLAMA_CLOUD_API_KEY` can be set as an environment variable — it takes precedence over the config file.

> `memory/settings.json` is the current location and will move to `~/.config/studio/settings.json` in a future release.

## Bundle lifecycle

```
proposed → in_review → approved → in_progress → verifying → complete
                     ↘ rejected                ↘ failed
```

1. **proposed** — Bundle submitted. Bundler worker plans the work: DAG, complexity/risk scores, capability grants, acceptance criteria, verification strategy.
2. **in_review** — Plan ready. Review workers (adversarial, security, QA) evaluate in parallel. Approval tier assigned based on scores.
3. **approved** — Execution begins. Workers run in isolated environments.
4. **in_progress** — Workers executing. The orchestrator monitors quality mid-flight, answers questions, and escalates to PM when needed.
5. **verifying** — Workers done. QA worker verifies the output against acceptance criteria and the verification strategy.
6. **complete** — QA passed. Code on GitHub. Calibration data recorded.

## Capability manifest

Every bundle's workers operate within a declared capability manifest:

```yaml
filesystem:
  grants:
    - path: /workspace
      mode: read_write
network:
  grants:
    - host: api.github.com
      port: 443
process:
  exec_allowlist:
    - /usr/bin/python3
    - /usr/bin/git
resource_limits:
  memory_limit_mb: 512
  wall_time_limit: 3600
```

The bundler generates this from the idea. You can review it in `studio show` before approving. Every worker operation is checked against these grants — violations are denied and logged.

For privileged workloads:

```yaml
privileged_capabilities:
  - CAP_BPF
  - CAP_PERFMON
```

Workers with privileged capabilities are routed to Firecracker VMs with those capabilities granted. Standard workers cannot access them.

## Approval surfaces

**CLI** — `studio approve <bundle-id>`

**GitHub Issues** — with a GitHub App configured, bundles surface as issue comments. Post `/approve` to approve, `/reject <reason>` to reject, or `/answer:<question-id> your response` to answer a worker question.

**MCP (Claude Desktop)** — the orchestrator exposes an MCP server. Add it to Claude Desktop to review and approve bundles from a conversation. The connection details are shown at the end of the installer run.

## Getting started with GitHub

### 1. Create a GitHub App

At [github.com/settings/apps/new](https://github.com/settings/apps/new):

- **Webhook URL**: `https://your-host:7810/webhook`
- **Repository permissions**: Contents (R/W), Issues (R/W), Pull requests (R/W), Metadata (R)
- **Account permissions**: Administration (R/W) — required for creating new repos
- **Subscribe to events**: Issue comment, Issues, Pull request, Push

Generate and download the private key (`.pem` file). Install the app on your account with **All repositories** access.

### 2. Configure

```json
{
  "github": {
    "enabled": true,
    "app_id": 123456,
    "installation_id": 987654321,
    "private_key_path": "~/.config/studio/github-app.pem",
    "webhook_secret": "your-webhook-secret",
    "owner": "your-username",
    "repo": "project-stdio"
  }
}
```

### 3. Use GitHub Issues as bundle inputs

Create an issue describing what you want built. The orchestrator polls for new issues and converts them to bundles. The resulting PR links back to the issue and auto-closes it on completion.

## Operational runbook

### Monitoring

```bash
studio status                    # Quick check
studio health                    # Detailed — stalled bundles, recent errors, tier breakdown
watch -n 5 'studio list'        # Live bundle states
studio calibration-report        # Estimation drift over time
```

### Stalled bundles

A bundle stalls when its workers stop sending heartbeats. Worker timeouts are dynamic — `max(30min, estimate × 3)` — so a bundle estimated at 2 hours gets a 6-hour window before being flagged.

If a worker has commits but missed its final report, the orchestrator issues a synthetic completion rather than failing the bundle.

```bash
studio show <bundle-id>          # Check node states
studio kill <bundle-id>          # Terminate if genuinely stuck
```

### Database backup

```bash
# Safe to copy while orchestrator is running (WAL mode)
cp /var/lib/studio/state.db /backup/state-$(date -I).db
```

### Upgrading

```bash
git pull
systemctl stop studio-orchestrator
# Schema migrations run automatically on next start
systemctl start studio-orchestrator
```

Run `studio --version` to confirm the running orchestrator is on the updated code.

## Security

See [docs/security.md](docs/security.md) for the full threat model and architecture.

The short version: bubblewrap is fine for trusted code on a machine you control. For production or untrusted code, use Firecracker — it adds a real hypervisor boundary, seccomp filtering on the hypervisor process, and content-hash verification on worker binaries so a supply-chain-compromised dependency can't replace a trusted binary.

## License

Proprietary — all rights reserved.
