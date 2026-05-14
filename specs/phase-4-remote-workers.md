# Phase 4: Remote Worker Scale-Out

## Scope and motivation

Phase 3 delivered a production-hardened single-host orchestrator. Phase 4 extends worker execution to remote hosts and Kubernetes clusters while keeping the orchestrator on a single machine. This is the designed scale path.

The constraint the reviewer identified is accurate: single SQLite writer with WAL is permanently single-host for the orchestrator itself. That constraint is accepted and the orchestrator stays on one machine. What Phase 4 changes is where workers run. Workers are already fire-and-forget subprocesses from the orchestrator's perspective; making them remote subprocesses is a runner implementation, not an architecture change.

**What does not change in Phase 4:**

- The orchestrator process, the state machine, SQLite, the event pump, the DAG executor, the capability manifest schema, the approval matrix, the calibration loop. All unchanged.
- The WorkerRunner interface. `LocalBwrapWorkerRunner` continues to work as-is for development and small workloads.
- The RPC protocol. JSON-RPC 2.0 framing is already transport-agnostic. The protocol does not change; only the connection setup changes.

**What changes in Phase 4:**

- The orchestrator's JSON-RPC listener adds a TCP/TLS endpoint alongside the existing Unix socket.
- Two new WorkerRunner implementations: `K8sJobWorkerRunner` (workers as Kubernetes Jobs) and `RemoteSSHWorkerRunner` (workers on a managed fleet of Linux hosts via SSH plus bubblewrap).
- The per-worker egress proxy becomes a sidecar pattern for remote workers rather than a host-side process.
- A fleet registry in `settings.json` describing available remote hosts and their capabilities.

The deliverables are ordered so that `RemoteSSHWorkerRunner` ships first (lower dependencies, faster to validate the transport change) and `K8sJobWorkerRunner` ships second.

---

## Bundle 4.1: TCP/TLS transport for the orchestrator RPC listener

### Background

Workers currently connect to the orchestrator via `/run/studio/orchestrator.sock`. This is a Unix domain socket, which is fast and has implicit auth (socket file permissions), but is local-only. Remote workers need a network endpoint.

The orchestrator needs to expose a second listener on a configurable TCP/TLS endpoint. Both listeners serve the same JSON-RPC 2.0 dispatcher; the transport layer is the only difference.

### Deliverables

**Dual-listener orchestrator.** `main.py` starts two listeners when remote workers are enabled in `settings.json`:

```
orchestrator.remote_workers.enabled: true
orchestrator.remote_workers.listen_addr: "0.0.0.0:7811"
orchestrator.remote_workers.tls_cert_path: "/etc/studio/tls/server.crt"
orchestrator.remote_workers.tls_key_path: "/etc/studio/tls/server.key"
```

The TCP listener uses the same `asyncio`-based connection manager as the Unix socket listener. The only difference is connection setup: TCP/TLS instead of Unix domain socket. The dispatcher, capability checks, heartbeat tracking, and all RPC method handlers are shared.

**Token auth on TCP.** Unix socket workers are implicitly scoped to the local machine; TCP workers are not. For TCP connections, the orchestrator validates the worker token strictly before accepting any further messages. The token is already single-use and 256-bit random; no protocol change is needed. The orchestrator logs the source IP of every TCP connection alongside the worker ID in the audit log.

**Worker connection string.** Workers need to know how to connect. Two env vars:

- `STUDIO_ORCHESTRATOR_ADDR`: set to either `unix:/run/studio/orchestrator.sock` (local) or `tcp://host:port` (remote). The worker connection bootstrap reads this and branches accordingly.
- `STUDIO_WORKER_TOKEN`: unchanged.

Update `base.py` in the worker bootstrap to parse `STUDIO_ORCHESTRATOR_ADDR` and open the appropriate connection type.

**TLS certificate management.** For development, a self-signed cert is acceptable. For production, the cert should be provisioned via Let's Encrypt or an internal CA. The spec does not mandate a provisioning mechanism; the operator provides paths to cert and key files. Document both the self-signed development path and the Let's Encrypt production path in README.md.

**Settings migration.** Schema version bumps to v6. Migration adds `remote_workers_enabled BOOLEAN NOT NULL DEFAULT 0` to the `settings` table (not actually used for query purposes but useful as a schema-level audit trail of when remote workers were enabled).

**Tests.** The existing RPC tests use the Unix socket path. Add parallel test variants that use a loopback TCP connection with a self-signed test cert. All 14 RPC methods must pass on both transports.

### Acceptance criteria

1. Orchestrator starts and listens on both Unix socket and TCP/TLS when remote workers enabled.
2. A worker connecting via TCP authenticates, heartbeats, and completes a bundle end-to-end.
3. `studio status` shows both listener addresses.
4. A connection attempt with a wrong token on TCP produces an audit log entry and closes the connection.
5. All existing Unix-socket acceptance tests still pass.

---

## Bundle 4.2: RemoteSSHWorkerRunner

### Background

The simplest remote runner: SSH to a target host, copy the worker binary and task spec, run bubblewrap there, stream RPC back to the orchestrator via TCP. This gives remote execution with the same isolation model as local workers and no Kubernetes dependency.

The target is a managed fleet: a set of Linux hosts (VMs, bare-metal, cloud instances) that the operator has configured with bubblewrap, the studio-worker binary, and the appropriate language caches. The orchestrator selects a host from the fleet using a simple scheduler, SSHes to it, and runs the worker.

### Fleet registry

Add to `settings.json`:

```json
"remote_fleet": {
  "enabled": true,
  "hosts": [
    {
      "name": "worker-1",
      "addr": "worker-1.internal",
      "ssh_user": "studio",
      "ssh_key_path": "/etc/studio/ssh/worker-1.key",
      "capabilities": ["python", "node", "go"],
      "max_concurrent_workers": 4,
      "arch": "x86_64"
    }
  ],
  "selection_policy": "least_loaded"
}
```

The `capabilities` field lists worker classes the host can run. The orchestrator checks this before assigning a worker. `max_concurrent_workers` is enforced by the orchestrator's per-host semaphore. `selection_policy` is `least_loaded` (fewest active workers) or `round_robin` for v1.

### Deliverables

**`RemoteSSHWorkerRunner` class.** Implements the `WorkerRunner` interface. `spawn()` does the following:

1. Select a host from the fleet registry per the selection policy.
2. SSH to the host and verify it is reachable and has capacity.
3. Create a temporary working directory on the remote host.
4. Copy the task spec JSON to the remote host via `scp` or SFTP.
5. Copy the capability manifest to the remote host.
6. Launch the worker via SSH: `bwrap [flags derived from capability manifest] studio-worker`. The worker binary is assumed to be pre-installed on the remote host (see setup documentation).
7. Return a `RemoteWorkerHandle` that tracks the SSH session and the remote PID.

The bwrap flags are generated by the same `capability_to_bwrap_args()` function used by `LocalBwrapWorkerRunner`. No new capability translation logic.

**Per-worker egress proxy on remote hosts.** The local egress proxy runs as a host-side asyncio process. For remote workers, it runs as an SSH-tunneled process: the orchestrator SSHes to the remote host, starts the proxy process there with the same manifest-derived allowlist, and passes the proxy socket path to the worker as `STUDIO_PROXY_SOCKET`. The proxy process exits when the SSH connection closes. This preserves identical egress enforcement semantics regardless of where the worker runs.

**`RemoteWorkerHandle`.** Tracks:
- The SSH connection object
- The remote host and PID
- The remote working directory path (for cleanup)

`cancel()` sends SIGTERM to the remote PID via SSH, waits for the grace period, then SIGKILL. `is_alive()` polls the remote process via SSH. On handle drop (worker completed or cancelled), the temporary working directory is removed from the remote host.

**Git worktree on remote hosts.** Workers need access to the repo. Two options, configurable per host:

- `worktree_mode: clone`: the orchestrator does a `git clone --single-branch` of the bundle's feature branch to the remote host before spawning. This is the default. Slower but self-contained.
- `worktree_mode: nfs_mount`: the control-plane repo is NFS-mounted on the remote host and the orchestrator creates a git worktree there directly. Faster but requires NFS setup.

The `worktree_mode` is set per host in the fleet registry.

**Host health monitoring.** A background task in the orchestrator pings each fleet host every 60 seconds. Hosts that fail three consecutive pings are marked `degraded` and removed from selection until they recover. A `studio fleet-status` CLI command shows the current state of all fleet hosts.

**New CLI commands.** Add to `cli.py`:

- `studio fleet-status`: shows each host, its current worker count, its status (healthy/degraded), and the last successful ping time.
- `studio fleet-add <name> <addr>`: adds a host to the fleet registry (writes `settings.json`).
- `studio fleet-remove <name>`: removes a host from the fleet registry.

**Settings migration.** Schema version bumps to v7. No new tables; fleet state is in `settings.json` and the health of individual hosts is tracked in memory only (non-persistent, re-evaluated on each orchestrator start).

### Acceptance criteria

1. Submit a bundle, approve it. The orchestrator selects a remote fleet host, SSHes to it, runs the worker with bubblewrap, and the worker heartbeats and completes.
2. `studio fleet-status` shows the remote host as healthy with 1 active worker during execution.
3. A `studio kill <bundle-id>` during remote execution correctly sends SIGTERM to the remote PID and the worker process exits cleanly on the remote host.
4. The per-worker egress proxy on the remote host enforces the same allowlist as local execution; an attempt by the worker to reach an unlisted host is blocked.
5. A remote host that goes offline mid-execution triggers worker failure and bundle reconciliation within 3 ping intervals (3 minutes).
6. Git worktree is created on the remote host and cleaned up after bundle completion.

---

## Bundle 4.3: K8sJobWorkerRunner

### Background

Workers as Kubernetes Jobs. Each worker spawns as a Pod in a configured namespace. The capability manifest translates to Pod spec fields. The orchestrator communicates with workers via the TCP/TLS listener from Bundle 4.1.

This is the right choice for large-scale parallel workloads where the worker fleet needs to grow and shrink dynamically and where per-worker resource limits need Kubernetes enforcement rather than bwrap-level enforcement.

### Prerequisites

- Bundle 4.1 (TCP/TLS listener) must be merged.
- A Kubernetes cluster accessible from the orchestrator host.
- The `kubernetes` Python client installed (`uv add kubernetes`).
- A namespace `studio-workers` with appropriate RBAC (service account, role, role binding). A Helm chart is included in `deploy/helm/studio-workers/` as part of this bundle.

### Capability manifest to Pod spec translation

The `capability_to_pod_spec()` function in `runner.py` translates a capability manifest into a Kubernetes Pod spec. The translation rules:

**Filesystem grants** become `volumeMounts` and `volumes`. The working tree is a `emptyDir` or a PVC depending on `worktree_mode`. Read-only mounts use `readOnly: true`. The restricted-paths enforcement is coarser on k8s than with bwrap (bwrap enforces at the bind-mount level; k8s enforces at the volume level) -- this is a known limitation documented in the Pod spec comment.

**Network grants** become a `NetworkPolicy` applied to the worker Pod's label. Default-deny egress. Each allowed destination in the manifest becomes an egress rule. DNS is allowed if `dns.enabled: true`. The `NetworkPolicy` is created before the Pod and deleted after the Pod terminates.

**Process grants (exec allowlist)** cannot be enforced at the k8s level without a custom admission controller. The exec allowlist is passed to the worker as an env var and enforced by the worker's own `cap.check` calls against the orchestrator's RPC dispatcher. This is weaker than bwrap-level enforcement and is documented as a known limitation for k8s workers. A future admission controller or seccomp profile could close this gap.

**Secrets grants** become projected volumes or env vars from Kubernetes Secrets. The orchestrator creates a short-lived Kubernetes Secret before Pod creation, mounts it into the Pod, and deletes it after the Pod terminates.

**Resource grants** become `resources.limits` and `resources.requests` in the Pod spec. `cpu_limit`, `memory_limit`, and `wall_time_limit` map directly. `wall_time_limit` also sets `activeDeadlineSeconds` on the Job.

**Security context** applied uniformly to all worker Pods:

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 10000
  runAsGroup: 10000
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  seccompProfile:
    type: RuntimeDefault
```

### Deliverables

**`K8sJobWorkerRunner` class.** Implements the `WorkerRunner` interface. `spawn()` does:

1. Translate capability manifest to Pod spec.
2. Create `NetworkPolicy` in `studio-workers` namespace.
3. Create Kubernetes Secret for any declared secrets.
4. Create Kubernetes Job with the translated Pod spec. The Job's pod template includes:
   - `STUDIO_ORCHESTRATOR_ADDR` set to the orchestrator's TCP/TLS endpoint.
   - `STUDIO_WORKER_TOKEN` set to the single-use token.
   - `STUDIO_WORKTREE_PATH` set to the mount path of the working volume.
5. Watch the Job for Pod creation and capture the Pod name for monitoring.
6. Return a `K8sWorkerHandle`.

**`K8sWorkerHandle`.** Tracks Job name, Pod name, namespace. `cancel()` deletes the Job (which terminates the Pod). `is_alive()` checks Job status. On handle drop, the Job, NetworkPolicy, and Secret are deleted.

**Git worktree on k8s.** Three options, configurable per cluster:

- `worktree_mode: init_container`: an init container clones the bundle's feature branch into a shared `emptyDir` volume before the worker container starts. This is the default.
- `worktree_mode: pvc`: a pre-provisioned PVC containing the repo is mounted. Requires the operator to keep the PVC in sync. Faster for large repos.
- `worktree_mode: nfs_mount`: as with the SSH runner, NFS mount of the control-plane repo.

**Per-worker egress proxy on k8s.** The egress proxy runs as a sidecar container in the worker Pod. The sidecar is built from `studio-agent-proxy` image (a new minimal image added to the base image set). The capability manifest's network grants are passed to the sidecar as env vars. The worker container connects to the proxy via the Pod's localhost interface. The sidecar and worker share a network namespace within the Pod, which is the Kubernetes equivalent of the bwrap network namespace.

**Pod event watching.** The orchestrator watches Pod events from the Kubernetes API server (via `watch.Watch()` on the Pod resource). On eviction, OOMKill, or node failure, the orchestrator receives the event promptly rather than waiting for the RPC connection to time out. This is the `pod-eviction event watching` item from the original deferred list.

**Helm chart.** `deploy/helm/studio-workers/` contains:
- `ServiceAccount` named `studio-worker` in `studio-workers` namespace.
- `Role` and `RoleBinding` giving the orchestrator permission to create/delete Jobs, Pods, NetworkPolicies, and Secrets in `studio-workers`.
- `ClusterRole` and `ClusterRoleBinding` for Pod event watching.
- Default `LimitRange` for the namespace.
- Default `NetworkPolicy` (deny all, overridden per-worker by the runner).

**`studio k8s-status` CLI command.** Shows active Jobs in the `studio-workers` namespace with their Pod status, age, and associated bundle ID.

**Settings for k8s runner:**

```json
"k8s_runner": {
  "enabled": false,
  "kubeconfig_path": null,
  "namespace": "studio-workers",
  "orchestrator_tcp_addr": "orchestrator.internal:7811",
  "image_pull_policy": "IfNotPresent",
  "worktree_mode": "init_container",
  "default_storage_class": null
}
```

`kubeconfig_path: null` means use the in-cluster service account (for when the orchestrator itself runs in k8s) or the default kubeconfig at `~/.kube/config`.

### Acceptance criteria

1. Submit a bundle, approve it with `k8s_runner.enabled: true`. A Kubernetes Job is created in `studio-workers`. The worker heartbeats back to the orchestrator over TCP/TLS and the bundle completes.
2. `studio k8s-status` shows the active Job during execution and no Jobs after completion.
3. The NetworkPolicy is created before Pod start and deleted after Pod termination.
4. A Pod eviction (simulated by `kubectl delete pod`) triggers worker failure detection within 30 seconds without waiting for RPC timeout.
5. A worker that exceeds `wall_time_limit` is killed by Kubernetes (`activeDeadlineSeconds`) and the orchestrator detects the failure via the Pod event watch.
6. Secrets are deleted from the namespace after the Job terminates.
7. The Helm chart installs cleanly on a vanilla k8s cluster and the RBAC is sufficient for the runner to operate.

---

## Bundle 4.4: Runner selection and mixed-fleet operation

### Background

With three runner implementations (`LocalBwrapWorkerRunner`, `RemoteSSHWorkerRunner`, `K8sJobWorkerRunner`), the orchestrator needs a way to select the right runner for each worker based on the task's requirements and the available capacity.

### Runner selection policy

Each task spec can declare a `runner_preference` in its `spec.params`:

```yaml
runner_preference: local | remote_ssh | k8s | any
```

`any` (the default) lets the orchestrator choose based on capacity and the task's capability requirements. The selection logic:

1. If `runner_preference` is set, use that runner (fail if unavailable).
2. If the task's resource grant exceeds what the local host can provide (e.g., `memory_limit > local_available_ram`), prefer remote.
3. If the local worker semaphore is full and a remote runner has capacity, use remote.
4. Otherwise, use local.

This policy is implemented in a `RunnerSelector` class that takes the current semaphore states and fleet health and returns a runner instance.

### Capability requirements for remote runners

Some capability grants that are locally enforced by bwrap cannot be enforced on all remote runners. The `capability_to_runner_compatibility()` function checks whether a given manifest is compatible with a given runner and returns a list of unenforced grants. The orchestrator logs unenforced grants to the audit log and requires explicit opt-in via `runner.allow_unenforced_grants: true` in `settings.json` before dispatching to an incompatible runner.

### Deliverables

**`RunnerSelector` class.** Selects a runner instance for a given task spec and capability manifest. Respects `runner_preference`, semaphore states, fleet health, and compatibility checks.

**`capability_to_runner_compatibility()` function.** Returns `{runner: str, unenforced_grants: list[str], compatible: bool}` for each available runner. Used by `RunnerSelector` and surfaced in `studio show <bundle-id>` output (so the operator can see which grants are unenforced on remote runners).

**Mixed-fleet acceptance test.** A bundle with three parallel worker tasks where one task has `runner_preference: local`, one has `runner_preference: remote_ssh`, and one has `runner_preference: k8s`. All three run concurrently and the bundle completes.

**`studio show <bundle-id>` update.** The worker section of `show` output now includes the runner type that was used for each worker node.

### Acceptance criteria

1. Local semaphore full, remote fleet available: orchestrator automatically routes new workers to the remote fleet.
2. Task with `runner_preference: k8s` runs on k8s even when local and SSH capacity is available.
3. Mixed-fleet bundle (local + SSH + k8s workers in parallel) completes end-to-end.
4. A manifest with an exec grant that k8s cannot enforce produces an audit log entry noting the unenforced grant.

---

## Bundle 4.5: DockerWorkerRunner

### Background

The three runners in Bundles 4.2-4.3 all require Linux kernel namespaces for worker isolation -- bubblewrap directly (local and SSH), or Kubernetes (which itself requires Linux nodes). None of them work on macOS or Windows hosts.

`DockerWorkerRunner` replaces bwrap with Docker as the isolation layer. Each worker runs as its own container. The orchestrator runs in its own container (or natively on Linux). Workers connect back to the orchestrator over TCP/TLS using the endpoint from Bundle 4.1. Network isolation, resource limits, and filesystem isolation are all enforced at the Docker layer rather than the bwrap layer.

This is not a monolith pattern. The orchestrator is one container. Each worker is a separate container with its own lifecycle, resource limits, network namespace, and filesystem. The capability manifest translates to `docker run` flags the same way it translates to bwrap flags or a Pod spec. Standard container tooling (docker stats, docker logs, docker inspect) works on each worker independently.

### Prerequisites

- Bundle 4.1 (TCP/TLS listener) must be merged. Workers connect to the orchestrator over TCP, not Unix socket.
- Docker Engine installed and accessible. The orchestrator process must be able to call the Docker API -- either via the Docker socket (/var/run/docker.sock) mounted into the orchestrator container, or via a remote Docker daemon configured in settings.json.
- The studio-worker base image must be built and available locally or in a registry. See Worker image below.

### Capability manifest to docker run translation

The `capability_to_docker_args()` function in `runner.py` translates a capability manifest into `docker run` flags:

**Filesystem grants** become `--volume` mounts. Read-only mounts use `:ro`. The working tree is a named Docker volume created per worker and deleted after the worker exits. The restricted-paths enforcement uses `--volume src:dst:ro` to mount only the declared paths rather than the full working tree.

**Network grants** become Docker network configuration. Workers are attached to a per-worker Docker network (created before container start, deleted after). The network is created with `--internal` (no external routing by default). Allowed egress destinations are enforced by the per-worker egress proxy running as a sidecar container on the same network (see Egress proxy below). DNS is controlled via `--dns` flags.

**Process grants (exec allowlist)** are enforced by the worker's own `cap.check` RPC calls against the orchestrator, identical to the k8s runner. Docker does not provide binary-level exec allowlisting without a custom seccomp profile; the installer generates a base seccomp profile that blocks the most dangerous syscalls but cannot enforce the per-manifest exec list at the kernel level. This is a known limitation, documented in the container startup logs.

**Secrets grants** become environment variables passed via `--env` or Docker secrets via `--secret`. Short-lived: the orchestrator creates the secret, passes it to the container, and removes it after the container exits.

**Resource grants** become `--memory`, `--cpus`, and `--pids-limit` flags. `wall_time_limit` becomes a timeout wrapper around `docker run` that sends SIGTERM then SIGKILL.

**Security defaults applied to every worker container:**

```
--read-only                          # read-only root filesystem
--tmpfs /tmp                         # writable /tmp
--no-new-privileges                  # no privilege escalation
--cap-drop ALL                       # drop all capabilities
--cap-add <only what manifest needs> # add back only declared caps
--security-opt no-new-privileges
--user 10000:10000                   # non-root user
```

These defaults match the Kubernetes securityContext from Bundle 4.3, preserving consistent security semantics across runners.

### Worker image

Add `docker/Dockerfile.worker` to the repo:

```dockerfile
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    python3.12 python3-pip git curl bubblewrap \
    && rm -rf /var/lib/apt/lists/*

# Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:$PATH"

# Install opencode
RUN curl -fsSL https://opencode.ai/install | bash

# Install studio-worker only (not the full orchestrator)
COPY . /build
RUN cd /build && uv pip install --system -e ".[worker]"

# Non-root user
RUN useradd -u 10000 -m studio
USER studio
WORKDIR /home/studio

ENTRYPOINT ["studio-worker"]
```

Add a `worker` extras group to `pyproject.toml` that installs only the worker dependencies (not the orchestrator, MCP server, or bundler). This keeps the worker image small.

Add `docker/Dockerfile.orchestrator` for running the orchestrator itself in Docker:

```dockerfile
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y \
    python3.12 python3-pip git curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.cargo/bin:$PATH"

COPY . /build
RUN cd /build && uv pip install --system -e .

EXPOSE 7810 7811

ENTRYPOINT ["studio-orchestrator"]
```

Add `docker-compose.yml` at the repo root:

```yaml
services:
  orchestrator:
    build:
      context: .
      dockerfile: docker/Dockerfile.orchestrator
    restart: unless-stopped
    volumes:
      - ./data:/var/lib/studio
      - ./config/settings.json:/etc/studio/settings.json:ro
      - /var/run/docker.sock:/var/run/docker.sock  # Docker socket for spawning worker containers
    ports:
      - "7810:7810"
      - "7811:7811"
    environment:
      - OLLAMA_CLOUD_API_KEY=${OLLAMA_CLOUD_API_KEY}
      - STUDIO_SOCKET_PATH=/var/lib/studio/orchestrator.sock
    networks:
      - studio-internal

networks:
  studio-internal:
    driver: bridge
```

Note: `docker-compose.yml` does not define a worker service. Workers are spawned dynamically by the orchestrator via the Docker API, not declared statically in compose.

### Egress proxy as sidecar container

The per-worker egress proxy runs as a sidecar container on the same Docker network as the worker. The orchestrator starts the proxy container first, then starts the worker container with `--network container:<proxy-container-id>` to share the proxy's network namespace.

The proxy container is built from `docker/Dockerfile.proxy`:

```dockerfile
FROM debian:bookworm-slim
COPY . /build
RUN cd /build && uv pip install --system -e ".[proxy]"
ENTRYPOINT ["studio-proxy"]
```

The capability manifest's network grants are passed to the proxy container as a JSON env var. The proxy enforces the allowlist exactly as it does in the local and SSH runners. When the worker container exits, the orchestrator stops and removes the proxy container.

This preserves identical egress enforcement semantics across all four runner implementations.

### DockerWorkerRunner class

Implements the WorkerRunner interface. `spawn()` does:

1. Pull or verify the worker image is available locally.
2. Create a per-worker Docker network (internal, no external routing).
3. Start the egress proxy container on the worker network with the manifest's egress allowlist.
4. Create a named Docker volume for the working tree.
5. Clone the bundle's feature branch into the volume via a short-lived init container.
6. Start the worker container with the translated docker run flags, connected to the worker network.
7. Return a `DockerWorkerHandle`.

`DockerWorkerHandle` tracks container IDs for the worker and proxy, the volume name, and the network name. `cancel()` stops the worker container, then removes the proxy container, volume, and network. `is_alive()` checks container status via the Docker API.

On handle drop (worker completed or cancelled), all resources are cleaned up: worker container removed, proxy container removed, volume removed, network removed. No orphaned containers or volumes.

### Docker socket security

Mounting `/var/run/docker.sock` into the orchestrator container gives the orchestrator full Docker API access on the host, which is effectively root-equivalent. This is a standard pattern (used by Portainer, Watchtower, CI runners) but it is a meaningful privilege.

Document this clearly in docs/install.md and in the docker-compose.yml comments:

```yaml
# WARNING: Mounting the Docker socket gives the orchestrator root-equivalent
# access to the host. This is required for the DockerWorkerRunner and is the
# standard pattern for container-spawning orchestrators. If this is a concern,
# use the K8sJobWorkerRunner instead, which scopes permissions to a namespace.
```

An alternative is Docker-in-Docker (dind), which gives the orchestrator its own isolated Docker daemon. This avoids the socket privilege issue but adds complexity and performance overhead. Add it to DEFERRED.md as a future hardening option.

### Settings

```json
"docker_runner": {
  "enabled": false,
  "socket_path": "/var/run/docker.sock",
  "worker_image": "project-stdio-worker:latest",
  "proxy_image": "project-stdio-proxy:latest",
  "network_prefix": "studio-worker",
  "volume_prefix": "studio-worktree",
  "registry": null,
  "pull_policy": "if_not_present"
}
```

`registry: null` means use local images only. Set to a registry URL to pull from a remote registry.

### macOS and Windows support

With `DockerWorkerRunner`, the orchestrator and all workers run inside Linux containers on any host OS. Docker Desktop on macOS and Windows provides a Linux VM; the containers run in that VM with full Linux kernel namespaces available, including user namespaces for bwrap compatibility.

Note: the orchestrator container itself does not use bwrap. Workers use Docker isolation instead of bwrap isolation. The security model is equivalent: each worker has its own network namespace, filesystem namespace, and resource limits. The enforcement mechanism is Docker rather than bwrap, which is more observable (docker stats, docker events) but slightly coarser for exec allowlisting.

### Update installer.sh

Add a `--docker` flag to installer.sh that:
1. Skips the bwrap and opencode installation checks (not needed on the host)
2. Checks for Docker Engine and docker compose
3. Builds the worker and proxy images: `docker compose build`
4. Sets `docker_runner.enabled: true` in settings.json
5. Starts the orchestrator via `docker compose up -d`
6. Verifies with `docker compose ps`

### New CLI commands

- `studio docker-status`: shows running worker containers, their bundle IDs, resource usage (from docker stats), and uptime.
- `studio docker-images`: shows the worker and proxy image versions currently in use. Flags if the images are out of date relative to the installed studio version.

### Acceptance criteria

1. On macOS with Docker Desktop: `docker compose up -d` starts the orchestrator. Submit a bundle, approve it. A worker container appears (`docker ps`), heartbeats to the orchestrator over TCP, and the bundle completes.
2. `studio docker-status` shows the worker container during execution and no containers after completion.
3. The egress proxy sidecar container blocks a worker attempting to reach an unlisted host.
4. `docker stats` shows per-worker resource usage during execution.
5. `studio kill <bundle-id>` stops and removes the worker container cleanly.
6. All worker containers, volumes, and networks are removed after bundle completion (no orphans after `docker ps -a` and `docker volume ls`).
7. The installer --docker flag builds images, starts the orchestrator, and passes verification on a fresh macOS machine with only Docker Desktop installed.

---

## Security notes

**Trust boundary extension.** Phase 4 extends the trust boundary beyond a single machine. The orchestrator's TCP/TLS endpoint is a new attack surface. Workers connecting via TCP present a token, but the token is transmitted over the wire (TLS protects it in transit). Compared to Unix socket workers where the token never leaves the host, TCP workers have a slightly wider token exposure window. Mitigations: TLS with certificate pinning (configurable), token expiry already at 15 minutes (from Bundle 3.4), single-use tokens.

**Remote host compromise.** If a remote fleet host is compromised, an attacker who can observe the host's memory or network can extract the worker token and impersonate the worker toward the orchestrator. This is the same threat as a compromised local host. The mitigation is the same: the token is single-use, short-lived, and worker actions are capability-checked server-side.

**k8s cluster access.** The orchestrator's k8s credentials (the `studio-worker` service account) need create/delete permissions on Jobs, Pods, NetworkPolicies, and Secrets in the `studio-workers` namespace. This is a meaningful privilege. The Helm chart scopes it to the namespace; the orchestrator does not need cluster-admin. Operator responsibility: protect the kubeconfig or service account token with the same care as the orchestrator's other credentials.

**Per-worker egress proxy on remote hosts.** The proxy process on remote fleet hosts runs as the `studio` user on that host. If the host is compromised, the proxy can be bypassed at the OS level. This is unchanged from local execution; the proxy provides defense-in-depth, not a hard guarantee against a compromised host.

---

## Deferred

**Firecracker/microVM tier under bubblewrap.** The reviewer correctly identified that bubblewrap puts the trust boundary at the host kernel, which is insufficient for untrusted worker payloads. A Firecracker or Kata Containers tier would close this gap. It is out of scope for Phase 4 but should be on the roadmap before Phase 5 if any untrusted code enters scope. The `WorkerRunner` interface accommodates it: `FirecrackerWorkerRunner` would be a new implementation.

**Worker binary distribution.** Phase 4 assumes the studio-worker binary is pre-installed on remote fleet hosts. A proper distribution mechanism (apt package, container image pull, automated provisioning via Ansible or Terraform) is out of scope for Phase 4 but necessary for production fleet management.

**Autoscaling.** The k8s runner creates Jobs but does not autoscale the cluster. Node autoscaling is the cluster operator's responsibility. A future bundle could integrate with the Cluster Autoscaler or Karpenter to provision nodes based on pending worker demand.

**Multi-region fleet.** All fleet hosts are assumed to be on the same network as the orchestrator. Cross-region workers would need the TCP/TLS endpoint to be publicly reachable or tunneled, with additional latency and reliability considerations for the RPC channel.

**Orchestrator HA.** Single orchestrator, single SQLite writer. Out of scope as discussed. If the orchestrator machine fails, in-flight bundles fail and must be re-submitted after restart. Crash recovery handles this cleanly; the LoE for true HA (Postgres + distributed locking) is a separate workstream.
