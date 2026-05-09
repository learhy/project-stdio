# Deferred

## Phase 2

### Retry policy for failed nodes
The v1.1 spec describes a retry policy with backoff. The `RetryPolicy` model exists in models.py but no retry logic is wired in the executor. Deferred to a later bundle.

### RPC query gate (rpc_query predicate)
Gate nodes with `rpc_query` predicate auto-pass in `_dispatch_gate`. Real implementation requires: a target worker lookup, an RPC call to that worker, and timeout handling. Deferred — no bundle scheduled yet.

### Expansion capability subset check (required before Phase 2.6)
`handle_expansion_request` auto-approves all structurally valid expansions without checking capability manifests. Fragment nodes can request capabilities beyond the bundle grant. Fix before any real worker issues expansion requests: call `capability.is_subset()` for each fragment node manifest against the bundle grant. Deny if any node exceeds it.
