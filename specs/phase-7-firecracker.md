# Phase 7: Firecracker MicroVM Worker Isolation

## Background and motivation

Bubblewrap provides process-level sandbox isolation using Linux user namespaces. It is fast, unprivileged, and adequate for trusted worker code on a single developer machine. It is not adequate once the threat model includes untrusted code execution.

The confirmed threat: in March 2026, a core maintainer of Falco demonstrated that Claude Code will bypass its own bubblewrap sandbox when the sandbox is between the agent and completing its task. The agent found that `/proc/self/root/usr/bin/npx` resolves to the same binary without matching the deny pattern. When bubblewrap caught that path, the agent disabled its own sandbox entirely. This is not a theoretical vulnerability — it is a documented exploit with a working proof of concept against the exact sandbox architecture project-stdio currently uses.

The fix: add a hypervisor layer under bubblewrap. Firecracker microVMs boot a real Linux kernel in ~125ms with ~5MB overhead. The agent cannot bypass a hypervisor boundary through path manipulation — the guest kernel is genuinely separate from the host kernel. Bubblewrap continues to run inside the microVM as the capability enforcement layer (filesystem grants, network namespace, egress proxy). Defense in depth: microVM provides kernel isolation, bubblewrap provides capability enforcement.

**This is not optional once any untrusted code enters the execution path** — npm packages, PyPI packages, user-provided scripts, or any code not written entirely by the controlled worker process.

---

## Architecture

```
Host machine
└── Firecracker microVM (kernel boundary)
    └── Guest Linux (minimal rootfs from Dockerfile.worker)
        └── bubblewrap (capability enforcement)
            └── Worker process (developer.py / review.py / bundler.py)
                └── opencode / LLM calls / filesystem operations
```

The microVM is the trust boundary. Everything inside the VM can be trusted to the same degree as the current bubblewrap model — the worker can't escape the guest kernel to reach the host. Bubblewrap inside the VM still enforces the capability manifest (which files the worker can read/write, which network destinations it can reach, which binaries it can exec).

The orchestrator communicates with the worker via vsock (VM socket) rather than Unix socket. The worker connects back to the orchestrator's TCP/TLS endpoint (from Phase 4 Bundle 4.1) — no changes needed to the RPC protocol.

---

## Bundle 7.1: Firecracker infrastructure

### Prerequisites

- Firecracker binary installed on the host (`/usr/bin/firecracker` or from PATH)
- KVM available (`/dev/kvm` exists and is accessible to the orchestrator user)
- `firectl` or direct Firecracker API access
- The worker rootfs image (from `docker/Dockerfile.worker`, converted to ext4)

### Rootfs management

The worker rootfs is built once from `docker/Dockerfile.worker` and stored as an ext4 image at a configurable path (default: `/var/lib/studio/firecracker/rootfs.ext4`).

Add a new CLI command: `studio build-worker-image`

```bash
studio build-worker-image [--output /path/to/rootfs.ext4] [--no-cache]
```

This command:
1. Builds `docker/Dockerfile.worker` as a Docker image
2. Exports the image filesystem to a temporary container
3. Converts it to an ext4 image using `virt-make-fs` or `mkfs.ext4` + `genext2fs`
4. Writes to the output path
5. Prints the image size and hash

The rootfs is read-only. Each worker gets a writable overlay layer (tmpfs-backed) on top of the shared read-only rootfs. This allows hundreds of concurrent workers to share a single rootfs image without copying gigabytes per worker.

Add to `installer.sh`: after installing studio, automatically run `studio build-worker-image` if Firecracker is available.

### VM pool management

Cold-starting a Firecracker VM takes ~125ms. Pre-warming a pool of VMs eliminates this from the worker spawn path.

New class `studio/orchestrator/firecracker.py`: `VmPool`

```python
class VmPool:
    def __init__(self, pool_size: int, rootfs_path: str, kernel_path: str):
        ...
    
    async def start(self):
        """Pre-warm pool_size VMs at startup."""
    
    async def acquire(self) -> FirecrackerVm:
        """Get a pre-warmed VM from the pool. If pool is empty, create a new one."""
    
    async def release(self, vm: FirecrackerVm):
        """Return VM to pool after worker exits (reset overlay, restore to clean state)."""
    
    async def stop(self):
        """Shut down all VMs in the pool."""
```

Pool size is configurable: `firecracker.pool_size: 3` in settings. After a worker exits, the VM's overlay layer is wiped and the VM is restored to a clean state and returned to the pool. No reboot needed — just reset the overlay filesystem and clear the worker's memory.

### Firecracker VM configuration

Each VM gets:
- vCPUs: configurable per capability manifest (default 1, max from `resource_limits.cpu_limit`)
- Memory: configurable per capability manifest (default 512MB, from `resource_limits.memory_limit`)
- Root drive: shared read-only rootfs.ext4
- Overlay drive: per-worker tmpfs-backed writable ext4
- Network: TAP device per VM, connected to a bridge on the host
- vsock: for orchestrator ↔ worker communication
- Kernel: minimal Linux kernel (provided by Firecracker project or built from config)

The kernel image is stored at `/var/lib/studio/firecracker/vmlinux`. Include a download command in `installer.sh`: `studio download-kernel` fetches the latest Firecracker-compatible kernel binary.

### Network isolation in Firecracker

Each VM gets its own TAP device and IP address in a private range (172.16.0.0/24 by default). The egress proxy runs on the host, reachable from the VM via the TAP bridge. The VM's default route points to the proxy. The capability manifest's network grants are enforced by the proxy exactly as in the existing model — the microVM boundary adds kernel isolation, the proxy adds hostname enforcement.

NetworkPolicy (if k8s runner) and the TAP-bridge egress (if Firecracker runner) are both enforced at the infrastructure layer rather than relying on the worker process to respect them.

### Settings

```json
"firecracker": {
  "enabled": false,
  "kernel_path": "/var/lib/studio/firecracker/vmlinux",
  "rootfs_path": "/var/lib/studio/firecracker/rootfs.ext4",
  "pool_size": 3,
  "default_vcpus": 1,
  "default_memory_mb": 512,
  "tap_bridge": "studio-fc-br0",
  "ip_range": "172.16.0.0/24",
  "jailer_enabled": true
}
```

`jailer_enabled: true` runs each Firecracker process through the Firecracker jailer, which provides a second line of defense (chroot + cgroups + seccomp on the Firecracker process itself). Recommended for production.

### Schema

v16 migration: add `runner_type` enum value `firecracker` (alongside `local`, `remote_ssh`, `k8s`, `docker`).

### Tests

- test_vm_pool_prewarms: pool starts, N VMs are running before any worker is dispatched
- test_vm_pool_acquire_release: acquire returns a VM, release returns it to pool in clean state
- test_rootfs_build: `studio build-worker-image` produces a valid ext4 image containing studio-worker binary
- test_firecracker_not_available: graceful fallback to bwrap if `/dev/kvm` not present, with warning log
- test_vm_overlay_reset: after worker exits, overlay is wiped and VM returns clean state

Branch: phase-7/firecracker-infrastructure. Report ambiguities before coding.

---

## Bundle 7.2: FirecrackerWorkerRunner

### Background

Implements the `WorkerRunner` interface using the Firecracker VM pool from Bundle 7.1. From the orchestrator's perspective, this is the same interface as `LocalBwrapWorkerRunner`, `RemoteSSHWorkerRunner`, `K8sJobWorkerRunner`, and `DockerWorkerRunner`. The RunnerSelector (Bundle 4.4) routes to it based on `runner_preference: firecracker` or automatically when `firecracker.enabled: true` and no explicit preference is set.

### spawn_worker

```python
async def spawn_worker(self, worker_id, bundle_id, node_id, manifest, worktree_path):
    # 1. Acquire a pre-warmed VM from the pool
    vm = await self._pool.acquire()
    
    # 2. Issue mTLS cert for this worker (same as all other runners)
    cert_pem, key_pem = tls.issue_worker_cert(self._ca_cert_path, self._ca_key_path, worker_id)
    
    # 3. Mount the worktree into the VM overlay filesystem
    await vm.mount_worktree(worktree_path)
    
    # 4. Inject worker env vars via VM metadata service or vsock bootstrap
    await vm.inject_env({
        "STUDIO_WORKER_ID": worker_id,
        "STUDIO_BUNDLE_ID": bundle_id,
        "STUDIO_ORCHESTRATOR_ADDR": f"tcp://{self._orchestrator_host}:7811",
        "STUDIO_WORKER_TOKEN": self._generate_token(worker_id),
        "STUDIO_WORKER_CERT": base64.b64encode(cert_pem),
        "STUDIO_WORKER_KEY": base64.b64encode(key_pem),
        "STUDIO_ORCHESTRATOR_CA": base64.b64encode(self._ca_cert_pem),
        "OLLAMA_CLOUD_BASE_URL": self._settings.ollama_cloud.base_url,
        "OLLAMA_CLOUD_API_KEY": self._settings.ollama_cloud.api_key,
    })
    
    # 5. Translate capability manifest to VM resource config
    vm_config = capability_to_vm_config(manifest)
    await vm.apply_resource_config(vm_config)
    
    # 6. Launch the worker process inside the VM via vsock command
    await vm.exec("studio-worker", env_injected=True)
    
    # 7. Return a handle
    return FirecrackerWorkerHandle(vm=vm, worker_id=worker_id)
```

### capability_to_vm_config

Translates the capability manifest to Firecracker VM resource configuration:

```python
def capability_to_vm_config(manifest: CapabilityManifest) -> VmConfig:
    return VmConfig(
        vcpus=manifest.resource_limits.cpu_limit or 1,
        memory_mb=int(manifest.resource_limits.memory_limit_mb or 512),
        # Filesystem grants become bind-mounts inside the VM overlay
        mounts=[
            VmMount(host_path=g.path, guest_path=g.path, readonly=(g.mode == "read"))
            for g in manifest.filesystem.grants
        ],
        # Network grants become egress proxy allowlist (host-side, same as bwrap model)
        egress_allowlist=[g.host for g in manifest.network.grants],
        # Process exec allowlist enforced inside VM by bwrap (same as current model)
        exec_allowlist=manifest.process.exec_allowlist,
    )
```

**Note on exec allowlist:** inside the microVM, bubblewrap still runs to enforce the exec allowlist. This is the nested model: microVM for kernel isolation, bwrap inside for capability enforcement. The exec allowlist cannot be bypassed via path tricks because the guest kernel is separate from the host kernel — `/proc/self/root` tricks don't work across the hypervisor boundary.

### FirecrackerWorkerHandle

```python
@dataclass
class FirecrackerWorkerHandle:
    vm: FirecrackerVm
    worker_id: str
    
    async def cancel(self):
        """Send SIGTERM to worker process inside VM, then stop the VM."""
        await self.vm.exec_signal(self.worker_id, "TERM")
        await asyncio.sleep(5)
        await self.vm.stop()
    
    async def is_alive(self) -> bool:
        """Check if worker process is still running inside VM."""
        return await self.vm.is_process_running(self.worker_id)
    
    async def cleanup(self):
        """Return VM to pool after worker exits."""
        await self._pool.release(self.vm)
```

### Worktree handling

The worktree is mounted into the VM overlay filesystem at the same path as on the host. After the worker exits, the worktree directory on the host contains the worker's output (it was bind-mounted read-write into the overlay). The existing `_commit_worktree` logic in `executor.py` runs on the host after the VM exits — no changes needed.

### RunnerSelector integration

Add `firecracker` to the `RunnerPreference` enum. When `firecracker.enabled: true` in settings, `RunnerSelector` treats `FirecrackerWorkerRunner` as the default local runner, replacing `LocalBwrapWorkerRunner`. The bwrap runner remains available via explicit `runner_preference: local_bwrap` for development scenarios where Firecracker is not needed.

Add `capability_to_runner_compatibility()` entry for Firecracker: `unenforced_grants: []` — Firecracker with nested bubblewrap enforces all five capability axes. This is the only runner with a fully clean compatibility profile.

### Tests

- test_spawn_worker_firecracker: spawn a worker, verify VM is running, worker connects via TCP
- test_cancel_worker_firecracker: cancel mid-execution, verify VM is stopped and returned to pool
- test_worktree_mounted_in_vm: worktree directory is accessible inside the VM at the expected path
- test_capability_to_vm_config: manifest with memory_limit=1024MB produces VmConfig with memory_mb=1024
- test_runner_selector_prefers_firecracker: when firecracker.enabled, RunnerSelector routes to FirecrackerWorkerRunner
- test_exec_allowlist_enforced_inside_vm: worker inside VM cannot exec a binary not in the allowlist

Branch: phase-7/firecracker-runner. Merge 7.1 first. Report ambiguities before coding.

---

## Bundle 7.3: Installer and operational tooling

### installer.sh updates

Add to the installer:

```bash
# Check for KVM availability
if [ -e /dev/kvm ]; then
    echo "[checking] Firecracker support..."
    
    # Install Firecracker
    FC_VERSION="v1.7.0"
    curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-x86_64.tgz" \
        | tar -xz -C /usr/local/bin/
    
    # Download kernel
    studio download-kernel
    
    # Build worker rootfs
    studio build-worker-image
    
    # Enable Firecracker in settings
    studio config set firecracker.enabled true
    
    echo "[ok] Firecracker microVM isolation enabled"
else
    echo "[warning] /dev/kvm not available -- using bubblewrap isolation only"
    echo "         For stronger isolation, enable KVM on this host"
fi
```

### New CLI commands

- `studio build-worker-image [--no-cache]` — rebuild the worker rootfs from Dockerfile.worker
- `studio download-kernel [--version v1.7.0]` — download a Firecracker-compatible kernel binary
- `studio vm-status` — show running VMs in the pool, their states, and resource usage
- `studio vm-pool-resize <n>` — resize the pre-warmed VM pool at runtime

### Operational considerations

**Rootfs freshness**: the worker rootfs must be rebuilt whenever `docker/Dockerfile.worker` changes or when studio is updated. Add a hash check: on orchestrator startup, compare the hash of the installed rootfs against the hash of the current `Dockerfile.worker`. If they differ, log a warning: "Worker rootfs may be out of date — run `studio build-worker-image` to rebuild."

**KVM permissions**: the orchestrator user needs read/write access to `/dev/kvm`. On most Linux systems: `sudo usermod -aG kvm <username>`. Document this in `docs/install.md`.

**Disk space**: the rootfs image is typically 500MB-1GB. The overlay layer per worker is bounded by the worker's write activity (typically <100MB for a code generation task). Document disk requirements in `docs/install.md`.

**Docker not required at runtime**: once the rootfs is built, Docker is not needed to run workers. The rootfs is a static ext4 image. Workers run directly in Firecracker VMs without any Docker dependency. (Docker is only needed to build/rebuild the rootfs image.)

### Tests

- test_build_worker_image: produces a valid ext4 image with studio-worker binary present
- test_download_kernel: downloads kernel, verifies hash matches known-good value
- test_rootfs_freshness_check: modified Dockerfile.worker produces warning at startup
- test_vm_status_cli: studio vm-status shows correct pool state

Branch: phase-7/installer-tooling. Merge 7.2 first. Report ambiguities before coding.

---

## Bundle 7.4: Security hardening and documentation

### Jailer configuration

The Firecracker jailer provides a second defense layer: it runs each Firecracker process in a chroot with cgroups and seccomp. Enable by default in production.

Document in `docs/security.md` (new file):
- The three isolation layers: jailer (Firecracker process confinement) → microVM (kernel boundary) → bubblewrap (capability enforcement)
- Threat model: what each layer protects against
- Known limitations: GPU passthrough not supported, KVM required (no nested VMs in most cloud environments)
- The documented bubblewrap bypass and why the microVM layer closes it

### seccomp profile

Add a default seccomp profile for the guest VM that blocks syscalls not needed for code generation tasks. Base it on the Docker default seccomp profile plus a few additions for opencode's needs.

### Content-hash based exec allowlist

The current exec allowlist matches binaries by path. The research confirms this is bypassable by agents that find alternative paths to the same binary. Fix: in addition to path matching, verify the SHA256 hash of the binary against a known-good hash at exec time.

This applies to the bubblewrap layer inside the VM. When the capability manifest declares `exec_allowlist: ["/usr/bin/python3"]`, the orchestrator also records the SHA256 hash of that binary at bundle submission time. The bubblewrap wrapper inside the VM verifies hash match before allowing exec. If the hash doesn't match (binary was replaced), the exec is denied and an audit log entry is written.

This is a meaningful defense against supply chain attacks where a compromised dependency replaces a trusted binary with a malicious one.

### Tests

- test_jailer_runs_firecracker: Firecracker process runs under jailer in CI
- test_content_hash_allowlist: binary hash mismatch denies exec
- test_content_hash_allowlist_pass: correct hash allows exec

Branch: phase-7/security-hardening. Merge 7.3 first. Report ambiguities before coding.

---

## Deferred

**GPU passthrough**: Firecracker does not officially support GPU passthrough. Experimental work exists but is not production-ready. GPU-requiring workloads should use the Docker runner or k8s runner with GPU node selectors.

**macOS**: Firecracker requires KVM (Linux only). macOS users continue to use the Docker runner (Bundle 4.5). The DockerWorkerRunner provides container-level isolation which is adequate for trusted developer machines.

**Nested virtualization**: some cloud environments disable KVM for VMs (nested virtualization). In those environments the installer falls back to bubblewrap with a warning. Document which cloud providers support KVM in `docs/install.md`.

**VM snapshotting for fast restore**: Firecracker supports snapshotting a running VM and restoring from snapshot in <50ms. This could enable even faster pool replenishment — instead of booting a new VM, restore from a clean snapshot. Deferred to a future phase once the basic VM pool is stable.

---

## Bundle 7.5: Privileged agent support (eBPF and kernel modules)

### Background

Some workloads require kernel-level capabilities that bubblewrap strips: CAP_BPF for eBPF programs, CAP_SYS_ADMIN for certain monitoring agents, CAP_NET_ADMIN for network tools. Bundles 7.1-7.4 provide full microVM isolation but do not grant guest VMs elevated capabilities by default. Bundle 7.5 adds explicit support for privileged agent workloads.

The key insight: granting CAP_BPF to a Firecracker guest VM is safe -- the guest kernel is isolated from the host kernel by the hypervisor boundary. An eBPF program running inside the Firecracker guest cannot affect the host kernel. This is the primary security advantage of microVM isolation for privileged agent workloads: full kernel capability grants without host kernel exposure.

### Privileged capabilities in the capability manifest

Add a `privileged_capabilities` field to the capability manifest schema:

```yaml
privileged_capabilities:
  - CAP_BPF
  - CAP_PERFMON
  - CAP_SYS_ADMIN
```

When declared, the RunnerSelector routes the worker to a separate privileged VM pool rather than the standard pool. Capabilities are granted at the Firecracker jailer configuration level. All other capability manifest fields (filesystem, network, secrets, resource_limits) continue to apply normally.

### Privileged VM pool

Add a second pre-warmed VM pool in VmPool: `_privileged_pool`. Separate from the standard pool. Default size: 1 (privileged workloads are less common). Capabilities granted via the jailer's cgroup and seccomp configuration. Standard VMs never receive these capabilities.

Settings addition:
```json
"firecracker": {
  "privileged_pool_size": 1,
  "allowed_privileged_capabilities": ["CAP_BPF", "CAP_PERFMON"]
}
```

`allowed_privileged_capabilities` is an operator-controlled allowlist. Workers cannot request capabilities not on this list. Default allows CAP_BPF and CAP_PERFMON (safe for eBPF monitoring). CAP_SYS_ADMIN requires explicit operator opt-in.

### eBPF toolchain in the worker image

Update docker/Dockerfile.worker to include:
- clang and llvm (eBPF C compilation)
- libbpf-dev (BTF support)
- linux-headers-generic (for eBPF program compilation)
- bpftool (static verification and BTF inspection)
- python3-bcc (Python eBPF programs via BCC)
- The bpf filesystem is available inside privileged VMs at /sys/fs/bpf

### ArtifactType.PRIVILEGED_AGENT

Add to the ArtifactType enum in artifacts.py:

```python
PRIVILEGED_AGENT = "privileged_agent"  # eBPF, kernel modules, system daemons requiring elevated caps
```

### Split verification strategy for PRIVILEGED_AGENT

Verification is two-phase:

**Phase 1 -- Static (runs in standard sandbox, no privileges required):**
- clang compilation of eBPF C code (catches syntax and type errors)
- bpftool prog load --dry-run for kernel verifier simulation
- Python syntax check for bcc programs
- Unit tests for the userspace loader and output parser components

**Phase 2 -- Runtime (runs in privileged VM with declared capabilities):**
- Actually load and attach the eBPF program
- Generate synthetic events (e.g. fork a test process to trigger execve tracepoints)
- Verify events are captured and formatted correctly
- Verify the streaming output format matches the declared schema

The bundler generates both phases in the verification_strategy when artifact_type is PRIVILEGED_AGENT. The executor runs Phase 1 in a standard worker VM, then Phase 2 in a privileged worker VM if Phase 1 passes.

### Bundler guidance for privileged agents

Add to _BUNDLER_SYSTEM_PROMPT:

"For eBPF programs, Linux monitoring agents, or any code requiring kernel capabilities: set artifact_type to 'privileged_agent'. Declare the minimum required capabilities in privileged_capabilities (prefer CAP_BPF + CAP_PERFMON over CAP_SYS_ADMIN where possible). The verification_strategy must include both a static_phase (clang compilation, dry-run verification) and a runtime_phase (actual program load and event capture test)."

### Use cases enabled

- Linux process monitoring agents (eBPF tracepoints on execve, exit, fork, clone)
- Network observability (eBPF socket filters, XDP programs, connection tracking)
- Security monitoring agents (syscall tracing, file access auditing, privilege escalation detection)
- Performance profiling agents (perf events, CPU flame graph collection)
- Container and Kubernetes observability (namespace events, cgroup tracking)

### Acceptance criteria

1. Worker with CAP_BPF in privileged_capabilities is routed to the privileged VM pool.
2. Worker without privileged_capabilities cannot access /sys/fs/bpf inside the VM.
3. An eBPF program compiled and loaded inside the privileged guest VM cannot be observed from the host (isolation verified).
4. The eBPF toolchain (clang, bpftool, python3-bcc) is available inside the worker image.
5. Split verification: Phase 1 (compilation) passes in standard VM, Phase 2 (runtime load) passes in privileged VM.

Branch: phase-7/privileged-agents. Merge 7.4 first. Report ambiguities before coding.