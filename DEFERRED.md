# Deferred

## Phase 2

### Retry policy for failed nodes
The v1.1 spec describes a retry policy with backoff. The `RetryPolicy` model exists in models.py but no retry logic is wired in the executor. Deferred to a later bundle.

### RPC query gate (rpc_query predicate)
Gate nodes with `rpc_query` predicate auto-pass in `_dispatch_gate`. Real implementation requires: a target worker lookup, an RPC call to that worker, and timeout handling. Deferred — no bundle scheduled yet.

### Artifact streaming (stream_put/stream_get)
Streaming for artifact data transfer is deferred until artifact sizes routinely exceed 100 MB or base64 overhead becomes a bottleneck. The design sketch is in the v1.1 spec (lines 1788-1794). Binary side channel alongside the JSON-RPC control channel is the intended path.

### Version immutability enforcement
Making non-`"latest"` versions reject re-publishes would prevent accidental overwrites. Flagged in the v1.1 spec (lines 1818) for a v1.2 design pass. Currently all versions accept overwrites (upsert semantics).

### Cross-bundle artifact sharing semantics
The `namespace: global` pathway exists but cross-bundle read grants, namespace collision policies, and global artifact lifecycle when multiple bundles reference the same artifact are not specified. Deferred per spec (line 1864).

### artifact.list pagination
Single unpaginated response is fine at v1.1 scale. If dynamic expansion produces thousands of artifacts, add `limit` and `cursor` parameters. Deferred per spec (line 1820).

### Credential-use audit aggregation for secrets.fetch
Workers refreshing short-lived tokens every hour produce one audit line per hour per worker. Aggregation is a future refinement. Deferred per spec (line 1824).

### Global artifact default TTL
Global artifacts currently live forever until cap-evicted or explicitly deleted. A configurable default TTL (e.g., 90 days) would prevent unbounded accumulation. Deferred per spec (line 1816).

### MCP capability grant/revoke tools (Bundle 2.7)
`grant_capability` and `revoke_capability` MCP tools return a `not_implemented` stub. The full capability request flow requires capability_requests table mechanics and state machine transitions not yet built. Deferred to Phase 3.

### MCP stdio-over-SSH transport (Bundle 2.7)
Only streamable HTTP transport is implemented in v1 (port 8080, reverse-proxied by Caddy). stdio-over-SSH is deferred to Phase 3.

### GitHub webhook signature validation (Bundle 2.8)
Webhook endpoint at `/github/webhook` accepts POST requests without HMAC-SHA256 signature verification. A webhook secret should be configured and signatures validated before exposing the endpoint beyond localhost. Deferred as bonus feature.

### Per-bundle polling time tracking (Bundle 2.8)
GitHub issue polling uses a single global `_last_poll_time` for all bundles. Per-bundle `last_polled_at` would avoid redundant API calls for bundles with no recent activity. Deferred until polling overhead becomes measurable.

### GitHub API rate-limit handling (Bundle 2.8)
No rate-limit awareness — the client does not inspect `X-RateLimit-Remaining` headers or back off when near limits. At current polling cadence (60s) and bundle volume this is safe, but rate-limit awareness should be added before production use with a busy repo.
### Auto-rollback on verification failure (Bundle 2.9)
Transitions 20 (verification_failed_auto_rollback) and 21 (verification_failed_manual_rollback) are stubbed with TODO comments in state_machine.py. When QA verification fails, the bundle always goes to Transition 19 (VERIFYING → FAILED). Auto-rollback requires: machine-executable rollback plan detection, stakes assessment, rollback DAG spawning, and the full Rollback mid-flight steering mechanics. Deferred to Phase 3.

### QA worker automated check runners (Bundle 2.9)
The QA worker's `_run_automated_checks()` shells out to `pytest`, `ruff`, and `acceptance.sh` as subprocesses. Coverage threshold checking and pre-merge gate enforcement are limited to what the shell commands return. A more structured test-runner interface (with per-test-case granularity, flaky detection, and structured coverage reports) would improve the Verification Report's fidelity but is deferred.

## Phase 3

### Worker inject_context push on secret rotation (Bundle 3.4)
Secret rotation records affected workers in audit_log but does not actively push new secret values to running workers. Workers that fetched the old secret continue using it until they re-fetch. Implement `worker.inject_context` push notification so rotated secrets are delivered to affected workers without restart. Deferred — `worker.inject_context` is a stub method.

### Egress proxy TLS-in-CONNECT SNI enforcement (Bundle 3.1)
The proxy peeks at the first TLS ClientHello after CONNECT and extracts the SNI. If the SNI doesn't match the CONNECT target hostname, the tunnel is blocked. However, TLS 1.3 Encrypted ClientHello (ECH) will defeat SNI sniffing entirely. When ECH gains real-world adoption, the proxy will need an alternative enforcement path (likely: enforce at CONNECT time with DNS pinning, accept the residual risk of post-CONNECT hostname mismatches, and rely on audit logging for incident response). Deferred until ECH appears in worker traffic.

### Egress proxy HTTP method/path constraints (Bundle 3.1)
The capability manifest schema has separate `http` and `https` protocols with a note that "the schema can be extended later with method or path constraints without breaking compatibility." The current proxy enforces only hostname:port. Per-method restrictions (allow GET but not POST) and per-path restrictions (allow /api/* but not /admin/*) are deferred.

### Egress proxy caching layer (Bundle 3.1)
No response caching in the proxy. Repeated requests to the same endpoint result in repeated upstream connections. A caching layer with manifest-aware cache keys would reduce latency for common package manager and API calls, but adds cache invalidation complexity and potential staleness bugs. Deferred until worker network latency is a measured bottleneck.

### Egress proxy connection pooling (Bundle 3.1)
Each HTTP request opens a new upstream connection. Connection pooling (keep-alive across multiple requests from the same worker to the same host) would reduce TCP handshake overhead. Deferred until worker traffic patterns justify the complexity.

### K8sJobWorkerRunner network policy integration (Bundle 3.1)
The `K8sJobWorkerRunner` is future work; when implemented, the egress proxy model maps to `NetworkPolicy` egress rules with the proxy as a sidecar. The translation logic (capability manifest → NetworkPolicy + sidecar config) is not specified, and the proxy's Unix socket model may need a TCP listener variant for sidecar communication. Deferred until the k8s runner is implemented.
