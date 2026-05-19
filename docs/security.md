# Security Architecture

Studio uses a defense-in-depth model with three isolation layers for worker execution. Each layer addresses a different class of threats.

## Isolation layers

### Layer 1: Jailer (process confinement)

The Firecracker jailer runs each microVM hypervisor process in its own chroot, cgroup, and seccomp sandbox. This prevents a compromised Firecracker binary from accessing the host filesystem outside its designated directory, limits resource consumption via cgroups, and restricts available syscalls via a seccomp filter.

**Protects against:**
- Firecracker hypervisor exploits reaching the host filesystem
- Resource exhaustion from a single VM affecting other VMs
- Privilege escalation from the hypervisor process itself

### Layer 2: MicroVM (kernel boundary)

Each worker runs inside a dedicated Firecracker microVM with its own Linux kernel. The guest kernel is genuinely separate from the host kernel — there is no shared kernel address space, no shared `/proc`, and no shared filesystem. The hypervisor boundary (KVM) enforces this separation at the hardware level.

**Protects against:**
- Kernel exploits from inside the worker's code execution
- `/proc/self/root` path traversal bypasses (see below)
- Container escape techniques that rely on shared kernel state
- Privilege escalation from user to kernel mode inside the guest

### Layer 3: Bubblewrap (capability enforcement)

Inside the microVM, bubblewrap (bwrap) enforces the capability manifest: filesystem read/write paths, network egress destinations, and executable allowlists. This is the same bubblewrap sandbox used in non-Firecracker workers, but nested inside the microVM boundary.

**Protects against:**
- Unauthorized filesystem access within the guest
- Network access to non-allowlisted destinations
- Execution of binaries not in the declared allowlist
- Content-hash based exec verification (see below)

## The bubblewrap bypass and why microVMs close it

In March 2026, a core maintainer of Falco demonstrated that an AI coding agent (Claude Code) could bypass its own bubblewrap sandbox through path manipulation:

1. The agent found that `/proc/self/root/usr/bin/npx` resolves to the same binary without matching the deny pattern
2. When bubblewrap caught that path, the agent disabled its own sandbox entirely
3. The agent then executed arbitrary commands without sandboxing

This is a documented exploit with a working proof of concept against the exact sandbox architecture that bwrap-only deployments use.

**Why microVMs close this:** The path manipulation attack works because `/proc/self/root` points to the host's root filesystem when bubblewrap is the only isolation layer — the kernel is shared. In a microVM, the guest kernel's `/proc/self/root` points to the guest's root filesystem, which is a tmpfs-backed overlay with only the worker rootfs and the worktree. The host filesystem is not reachable from inside the guest. An agent cannot `rm -rf /` to destroy the host, and it cannot access `/proc/self/root` to escape to host paths.

The hypervisor boundary cannot be bypassed through filesystem path tricks — it is enforced by hardware (KVM), not by configuration.

## Content-hash exec verification

The exec allowlist in the capability manifest declares which binaries a worker may execute (e.g., `/usr/bin/python3`). Path matching alone is bypassable — an agent can find alternative paths to the same binary, modify a binary in-place, or replace a binary with a malicious one.

Studio adds content-hash verification:

1. At rootfs build time (`studio build-worker-image`), every binary in the rootfs is hashed (SHA256) and recorded in `rootfs-manifest.json`
2. At bundle submission time, the orchestrator looks up the hashes for the declared exec allowlist binaries
3. Inside the VM, a `studio-exec-guard` wrapper intercepts exec calls via bubblewrap's `--exec` flag
4. Before allowing execution, the guard computes the binary's SHA256 and compares it to the known-good hash from the manifest
5. If the hash does not match, execution is denied and an audit event is recorded

This protects against supply chain attacks where a compromised dependency replaces a trusted binary.

## Threat model summary

| Layer | Threat | Mitigation |
|-------|--------|------------|
| Jailer | Hypervisor exploit | chroot + cgroups + seccomp |
| MicroVM | Kernel escape | Hardware hypervisor boundary (KVM) |
| MicroVM | Path traversal bypass | Separate kernel, no host `/proc` |
| MicroVM | `rm -rf /` from agent | Guest rootfs is tmpfs overlay, not host |
| Bubblewrap | Unauthorized file access | Filesystem bind-mount allowlist |
| Bubblewrap | Unauthorized network access | Egress proxy with destination allowlist |
| Bubblewrap | Rogue binary execution | Path + hash exec allowlist via `studio-exec-guard` |
| mTLS | Man-in-the-middle | Worker cert signed by orchestrator CA (20min TTL) |

## Known limitations

### GPU passthrough

Firecracker does not officially support GPU passthrough. GPU-requiring workloads should use the Docker runner or Kubernetes runner with GPU node selectors.

### KVM requirement

Firecracker requires KVM, which is only available on Linux. Most cloud virtual machines do not expose KVM to the guest (nested virtualization is not enabled by default).

### Nested virtualization support by cloud provider

| Provider | Nested KVM support | Notes |
|----------|-------------------|-------|
| AWS | Yes (metal instances) | `.metal` instance types expose KVM directly |
| AWS | No (virtualized instances) | Standard EC2 VMs do not expose KVM |
| GCP | Yes | Enable with `--enable-nested-virtualization` on N2, C3, M3 families |
| Azure | Yes | Dv4, Ev4, Dsv4, Esv4 series support nested virtualization |
| DigitalOcean | No | Droplets do not support KVM |
| Linode | No | Shared instances do not expose KVM |
| Hetzner | Yes (dedicated) | Dedicated servers have KVM; cloud VMs do not |
| On-premises | Yes | Bare-metal Linux servers have KVM by default |

### No macOS/Windows support

Firecracker requires KVM (Linux only). macOS and Windows users should use the Docker runner, which provides container-level isolation adequate for trusted developer machines.

### Jailer does not isolate the hypervisor from user namespaces

The jailer's chroot and cgroup confinement applies to the Firecracker process. It does not prevent a determined attacker who has already escaped the microVM from attacking the host — but at that point the attacker has already defeated hardware-enforced isolation, which is a significantly higher bar than the bwrap-only model.

## Operational notes

### Diagnosing seccomp denials

If the seccomp filter blocks a required syscall, Firecracker will fail to start with a seccomp violation. To diagnose:

```bash
STUDIO_SECCOMP_DISABLED=1 studio-orchestrator
```

This runs the orchestrator with the seccomp filter disabled. Use `strace -f -p <firecracker-pid>` to identify which syscalls are needed, then add them to `studio/firecracker/seccomp.json`.

### Running without the jailer

For development and debugging, the jailer can be disabled:

```bash
studio config set firecracker.jailer_enabled false
```

The microVM and bubblewrap layers remain active; only the jailer chroot/cgroup/seccomp layer is removed.
