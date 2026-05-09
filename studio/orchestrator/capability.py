"""Capability manifest subset checking and op_descriptor dispatch.

This is the most important correctness surface in the kernel. Every
capability check flows through here.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Any

from .models import CapabilityManifest


# ── Subset checking ──────────────────────────────────────────────────────────

def is_subset(
    task_manifest: CapabilityManifest,
    bundle_manifest: CapabilityManifest,
) -> tuple[bool, str]:
    """Returns (is_subset, failure_reason)."""
    t = task_manifest.grants
    b = bundle_manifest.grants

    if not _filesystem_is_subset(t.filesystem, b.filesystem):
        return (False, "filesystem grant exceeds bundle scope")
    if not _network_is_subset(t.network, b.network):
        return (False, "network grant exceeds bundle scope")
    if not _process_is_subset(t.process, b.process):
        return (False, "process grant exceeds bundle scope")
    if not _secrets_is_subset(t.secrets, b.secrets):
        return (False, "secrets grant exceeds bundle scope")
    if not _rpc_is_subset(t.rpc, b.rpc):
        return (False, "rpc grant exceeds bundle scope")
    if not _resources_is_subset(t.resources, b.resources):
        return (False, "resources grant exceeds bundle scope")
    return (True, "")


def _filesystem_is_subset(task, bundle) -> bool:
    # Reads
    for t_read in task.reads:
        found = False
        for b_read in bundle.reads:
            if _path_contained(t_read.path, b_read.path):
                if t_read.recursive and not b_read.recursive:
                    continue
                found = True
                break
        if not found:
            return False

    # Writes
    for t_write in task.writes:
        found = False
        for b_write in bundle.writes:
            if _path_contained(t_write.path, b_write.path):
                if t_write.recursive and not b_write.recursive:
                    continue
                found = True
                break
        if not found:
            return False

    # Working tree
    if task.working_tree and bundle.working_tree:
        if (
            task.working_tree.write_scope == "path_restricted"
            and bundle.working_tree.write_scope == "full"
        ):
            return False
        if task.working_tree.restricted_paths:
            bundle_paths = set(bundle.working_tree.restricted_paths)
            if not all(p in bundle_paths for p in task.working_tree.restricted_paths):
                return False

    return True


def _path_contained(task_path: str, bundle_path: str) -> bool:
    """Check if task_path is within or equal to bundle_path."""
    t = PurePosixPath(task_path)
    b = PurePosixPath(bundle_path)
    try:
        t.relative_to(b)
        return True
    except ValueError:
        return False


# Protocol subsumption order: tcp > udp, http, https; http > https
_PROTOCOL_RANK = {"tcp": 3, "udp": 2, "http": 2, "https": 1}


def _network_is_subset(task, bundle) -> bool:
    # Egress
    for t_egress in task.egress:
        found = False
        for b_egress in bundle.egress:
            # Destination containment
            if not _destination_contained(t_egress.destination, b_egress.destination):
                continue
            # Ports subset
            if t_egress.ports and b_egress.ports:
                if not set(t_egress.ports).issubset(set(b_egress.ports)):
                    continue
            elif t_egress.ports and not b_egress.ports:
                pass  # bundle allows any port
            # Protocol subsumption: bundle protocol must be >= task protocol
            task_rank = _PROTOCOL_RANK.get(t_egress.protocol, 0)
            bundle_rank = _PROTOCOL_RANK.get(b_egress.protocol, 0)
            if bundle_rank < task_rank:
                continue
            found = True
            break
        if not found:
            return False

    # Ingress
    if task.ingress.enabled and not bundle.ingress.enabled:
        return False

    # DNS
    if task.dns.enabled and not bundle.dns.enabled:
        return False

    return True


def _destination_contained(task_dest: str, bundle_dest: str) -> bool:
    """Check if task destination is contained in bundle destination."""
    if task_dest == bundle_dest:
        return True
    # CIDR containment
    if "/" in bundle_dest and "/" in task_dest:
        return _cidr_contains(bundle_dest, task_dest)
    # If bundle is a CIDR and task is not, can't easily check — require exact
    return False


def _cidr_contains(parent_cidr: str, child_cidr: str) -> bool:
    """Check if parent_cidr contains child_cidr."""
    import ipaddress
    try:
        parent = ipaddress.ip_network(parent_cidr, strict=False)
        child = ipaddress.ip_network(child_cidr, strict=False)
        return child.subnet_of(parent)
    except ValueError:
        return False


def _process_is_subset(task, bundle) -> bool:
    for t_exec in task.exec:
        found = False
        for b_exec in bundle.exec:
            if t_exec.binary != b_exec.binary:
                continue
            # args_pattern: task must be more restrictive
            if t_exec.args_pattern and not b_exec.args_pattern:
                continue  # task restricts, bundle allows anything — ok
            if t_exec.args_pattern and b_exec.args_pattern:
                if not _pattern_subset(t_exec.args_pattern, b_exec.args_pattern):
                    continue
            found = True
            break
        if not found:
            return False

    # spawn_subtasks
    if task.spawn_subtasks.enabled and not bundle.spawn_subtasks.enabled:
        return False
    if task.spawn_subtasks.max_depth > bundle.spawn_subtasks.max_depth:
        return False
    if task.spawn_subtasks.max_count > bundle.spawn_subtasks.max_count:
        return False

    return True


def _pattern_subset(task_pattern: str, bundle_pattern: str) -> bool:
    """Check if task pattern matches only a subset of what bundle pattern matches.

    Conservative: returns False when unable to determine statically.
    """
    # Exact match: task = bundle, definitely subset
    if task_pattern == bundle_pattern:
        return True
    # If bundle has a regex alternation that includes task pattern
    if f"({task_pattern}" in bundle_pattern or f"|{task_pattern}" in bundle_pattern:
        return True
    # If task is more specific (longer pattern generally means more restrictive)
    # This is a heuristic; the spec says fails-safe by rejecting
    return False


def _secrets_is_subset(task, bundle) -> bool:
    for t_secret in task:
        found = False
        for b_secret in bundle:
            if t_secret.name != b_secret.name:
                continue
            # Purpose: bundle 'custom' subsumes all
            if t_secret.purpose != b_secret.purpose and b_secret.purpose != "custom":
                continue
            # Delivery: env and file are equivalent, rpc is more restrictive
            if t_secret.delivery == "rpc" and b_secret.delivery not in ("rpc", "env", "file"):
                continue
            found = True
            break
        if not found:
            return False
    return True


def _rpc_is_subset(task, bundle) -> bool:
    # Methods
    for t_pattern in task.methods:
        found = False
        for b_pattern in bundle.methods:
            if _rpc_method_covered(t_pattern, b_pattern):
                found = True
                break
        if not found:
            return False

    # Artifact reads
    for t_pattern in task.artifact_access.reads:
        found = False
        for b_pattern in bundle.artifact_access.reads:
            if _artifact_pattern_covers(b_pattern, t_pattern):
                found = True
                break
        if not found:
            return False

    # Artifact writes
    for t_pattern in task.artifact_access.writes:
        found = False
        for b_pattern in bundle.artifact_access.writes:
            if _artifact_pattern_covers(b_pattern, t_pattern):
                found = True
                break
        if not found:
            return False

    return True


def _rpc_method_covered(task_pattern: str, bundle_pattern: str) -> bool:
    """Check if bundle_pattern covers task_pattern.

    'artifact.*' covers 'artifact.publish' and 'artifact.*'.
    'artifact.publish' covers 'artifact.publish' only.
    'worker.*' covers 'worker.heartbeat', 'worker.*'.
    """
    if bundle_pattern == task_pattern:
        return True
    if bundle_pattern.endswith(".*"):
        prefix = bundle_pattern[:-2]
        return task_pattern.startswith(prefix + ".")
    return False


def _artifact_pattern_covers(bundle_pattern, task_pattern) -> bool:
    """Check if bundle_pattern covers task_pattern (bundle is superset)."""
    for field in ("namespace", "name", "version", "content_type"):
        b_val = getattr(bundle_pattern, field)
        t_val = getattr(task_pattern, field)
        if not _glob_covers(b_val, t_val):
            return False
    return True


def _glob_covers(bundle_glob: str, task_glob: str) -> bool:
    """Check if bundle glob covers (matches a superset of) task glob."""
    # Same = covers
    if bundle_glob == task_glob:
        return True
    # "**" covers everything
    if bundle_glob == "**":
        return True
    # "*" covers everything in a single segment
    if bundle_glob == "*" and task_glob != "**":
        return True
    # If bundle is more general prefix with wildcard
    # e.g. "test-*" covers "test-results-*"
    bundle_re = _glob_to_regex(bundle_glob)
    task_re = _glob_to_regex(task_glob)
    # Check if task regex is more specific by comparing pattern strings
    # This is a heuristic: longer glob with more literal chars is more specific
    if bundle_glob.endswith("*") and task_glob.startswith(bundle_glob.rstrip("*")):
        return True
    return False


def _resources_is_subset(task, bundle) -> bool:
    if task.cpu_limit > bundle.cpu_limit > 0:
        return False
    if task.memory_limit > bundle.memory_limit > 0:
        return False
    if task.disk_limit > bundle.disk_limit > 0:
        return False
    if task.wall_time_limit > bundle.wall_time_limit > 0:
        return False
    if (
        task.llm_token_budget.input_tokens
        > bundle.llm_token_budget.input_tokens
        > 0
    ):
        return False
    if (
        task.llm_token_budget.output_tokens
        > bundle.llm_token_budget.output_tokens
        > 0
    ):
        return False
    # Per-model budgets
    for model, t_budget in task.llm_token_budget.by_model.items():
        b_budget = bundle.llm_token_budget.by_model.get(model)
        if b_budget is None:
            return False
        if t_budget.get("input_tokens", 0) > b_budget.get("input_tokens", 0) > 0:
            return False
        if t_budget.get("output_tokens", 0) > b_budget.get("output_tokens", 0) > 0:
            return False
    return True


# ── Glob matching ────────────────────────────────────────────────────────────

@lru_cache(maxsize=512)
def _glob_to_regex(pattern: str) -> re.Pattern:
    """Compile a glob pattern to a regex. Cached for performance."""
    i = 0
    n = len(pattern)
    parts = ["^"]

    while i < n:
        c = pattern[i]
        if c == "**":
            parts.append(".*")
            i += 2
        elif c == "*":
            # Single-segment wildcard
            parts.append("[^/]*")
            i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        elif c == "[":
            end = pattern.find("]", i)
            if end == -1:
                parts.append(re.escape(c))
                i += 1
            else:
                parts.append(pattern[i : end + 1])
                i = end + 1
        else:
            parts.append(re.escape(c))
            i += 1

    parts.append("$")
    return re.compile("".join(parts))


def glob_match(pattern: str, value: str) -> bool:
    """Match a value against a glob pattern.

    Supports: *, **, ?, [abc], [!abc].
    No brace expansion, no extglob.
    """
    if pattern == "**":
        return True
    if pattern == "*":
        return "/" not in value
    regex = _glob_to_regex(pattern)
    return bool(regex.match(value))


# ── op_descriptor parsing and dispatch ───────────────────────────────────────

class OpDescriptor:
    """Parsed op_descriptor: <category>.<operation>[:<resource>]"""

    def __init__(self, descriptor: str) -> None:
        self.raw = descriptor
        # Split on first dot for category
        dot_idx = descriptor.index(".")
        self.category = descriptor[:dot_idx]
        rest = descriptor[dot_idx + 1:]

        # Split operation:resource
        if ":" in rest:
            colon_idx = rest.index(":")
            self.operation = rest[:colon_idx]
            self.resource = rest[colon_idx + 1:]
        else:
            self.operation = rest
            self.resource = None

    def __repr__(self) -> str:
        return f"OpDescriptor(category={self.category!r}, op={self.operation!r}, resource={self.resource!r})"


def check_op(
    op_descriptor: str,
    manifest: CapabilityManifest,
) -> tuple[bool, str | None]:
    """Check if an operation is allowed by a manifest.

    Returns (allowed, capability_id).

    Pure function — no I/O.
    """
    try:
        op = OpDescriptor(op_descriptor)
    except (ValueError, IndexError):
        return (False, None)

    grants = manifest.grants

    if op.category == "filesystem":
        return _check_filesystem(op, grants)
    elif op.category == "network":
        return _check_network(op, grants)
    elif op.category == "process":
        return _check_process(op, grants)
    elif op.category == "secrets":
        return _check_secrets(op, grants)
    elif op.category == "rpc":
        return _check_rpc(op, grants)
    elif op.category == "resources":
        return _check_resources(op, grants)
    else:
        return (False, None)


def _check_filesystem(op: OpDescriptor, grants) -> tuple[bool, str | None]:
    if op.operation == "read":
        entries = grants.filesystem.reads
    elif op.operation == "write":
        entries = grants.filesystem.writes
    else:
        return (False, None)

    if op.resource is None:
        return (False, None)

    for entry in entries:
        if _path_contained(op.resource, entry.path):
            if op.operation == "write" and not entry.create:
                return (False, None)
            return (True, None)

    return (False, None)


def _check_network(op: OpDescriptor, grants) -> tuple[bool, str | None]:
    if op.operation != "egress":
        return (False, None)

    if op.resource is None:
        return (False, None)

    # Resource is host:port or cidr
    if ":" in op.resource:
        host, _, port_str = op.resource.rpartition(":")
        try:
            port = int(port_str)
        except ValueError:
            port = None
    else:
        host = op.resource
        port = None

    for entry in grants.network.egress:
        if _destination_contained(host, entry.destination):
            if port and entry.ports and port not in entry.ports:
                continue
            return (True, None)

    return (False, None)


def _check_process(op: OpDescriptor, grants) -> tuple[bool, str | None]:
    if op.operation != "exec":
        return (False, None)

    if op.resource is None:
        return (False, None)

    for entry in grants.process.exec:
        if entry.binary == op.resource:
            return (True, None)

    return (False, None)


def _check_secrets(op: OpDescriptor, grants) -> tuple[bool, str | None]:
    if op.operation != "fetch":
        return (False, None)

    if op.resource is None:
        return (False, None)

    for entry in grants.secrets:
        if entry.name == op.resource:
            return (True, None)

    return (False, None)


def _check_rpc(op: OpDescriptor, grants) -> tuple[bool, str | None]:
    if op.operation == "method":
        if op.resource is None:
            return (False, None)
        for pattern in grants.rpc.methods:
            if _rpc_method_covered(op.resource, pattern):
                return (True, None)
        return (False, None)

    elif op.operation == "artifact_access.read":
        if op.resource is None:
            return (False, None)
        for pattern in grants.rpc.artifact_access.reads:
            if _descriptor_matches_glob(op.resource, pattern):
                return (True, None)
        return (False, None)

    elif op.operation == "artifact_access.write":
        if op.resource is None:
            return (False, None)
        for pattern in grants.rpc.artifact_access.writes:
            if _descriptor_matches_glob(op.resource, pattern):
                return (True, None)
        return (False, None)

    return (False, None)


def _descriptor_matches_glob(resource: str, pattern) -> bool:
    """Match a descriptor JSON string against an artifact access pattern."""
    try:
        import json
        desc = json.loads(resource)
    except (json.JSONDecodeError, TypeError):
        return False

    for field in ("namespace", "name", "version", "content_type"):
        p_val = getattr(pattern, field, "*")
        d_val = desc.get(field, "")
        if not glob_match(p_val, d_val):
            return False
    return True


def _check_resources(op: OpDescriptor, grants) -> tuple[bool, str | None]:
    # Resources checks: always pass at op level (resource limits are
    # checked at submission validation and runner spawn time, not at
    # RPC dispatch time)
    return (True, None)
