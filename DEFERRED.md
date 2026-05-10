# Deferred

## Phase 2

### Retry policy for failed nodes
The v1.1 spec describes a retry policy with backoff. The `RetryPolicy` model exists in models.py but no retry logic is wired in the executor. Deferred to a later bundle.

### RPC query gate (rpc_query predicate)
Gate nodes with `rpc_query` predicate auto-pass in `_dispatch_gate`. Real implementation requires: a target worker lookup, an RPC call to that worker, and timeout handling. Deferred — no bundle scheduled yet.

### Expansion capability subset check (required before Phase 2.6)
`handle_expansion_request` auto-approves all structurally valid expansions without checking capability manifests. Fragment nodes can request capabilities beyond the bundle grant. Fix before any real worker issues expansion requests: call `capability.is_subset()` for each fragment node manifest against the bundle grant. Deny if any node exceeds it.

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
