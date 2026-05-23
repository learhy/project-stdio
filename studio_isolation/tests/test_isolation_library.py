"""Smoke tests for the standalone studio_isolation library.

These tests verify the extracted library works independently of the
orchestrator runtime (no executor, no scheduler, no main module).
"""

import pytest
from studio_isolation import (
    is_subset,
    capability_to_bwrap_args,
    CapabilityManifest,
    Grants,
    FilesystemGrants,
    FilesystemPathGrant,
    FilesystemWriteGrant,
    NetworkGrants,
    EgressGrant,
    ProcessGrants,
    ExecGrant,
    RpcGrants,
    ResourceGrants,
    SecretGrant,
    glob_match,
)


class TestCapabilitySubset:
    """Verify is_subset() works standalone (no orchestrator)."""

    def test_empty_manifests(self):
        a = CapabilityManifest()
        b = CapabilityManifest()
        ok, reason = is_subset(a, b)
        assert ok, f"empty should be subset of empty: {reason}"

    def test_filesystem_subset(self):
        bundle = CapabilityManifest(
            grants=Grants(
                filesystem=FilesystemGrants(
                    reads=[FilesystemPathGrant(path="/home")],
                    writes=[FilesystemWriteGrant(path="/home/output")],
                )
            )
        )
        task = CapabilityManifest(
            grants=Grants(
                filesystem=FilesystemGrants(
                    reads=[FilesystemPathGrant(path="/home/project")],
                    writes=[FilesystemWriteGrant(path="/home/output/dir")],
                )
            )
        )
        ok, reason = is_subset(task, bundle)
        assert ok, f"task read/write within bundle scope: {reason}"

    def test_filesystem_exceeds_scope(self):
        bundle = CapabilityManifest(
            grants=Grants(
                filesystem=FilesystemGrants(
                    reads=[FilesystemPathGrant(path="/home")],
                )
            )
        )
        task = CapabilityManifest(
            grants=Grants(
                filesystem=FilesystemGrants(
                    reads=[FilesystemPathGrant(path="/etc")],
                )
            )
        )
        ok, reason = is_subset(task, bundle)
        assert not ok, "task reading /etc exceeds bundle scope"

    def test_network_subset(self):
        # Network subset requires exact destination match (hostname or CIDR).
        # api.github.com is not contained by github.com in exact match.
        bundle = CapabilityManifest(
            grants=Grants(
                network=NetworkGrants(
                    egress=[
                        EgressGrant(destination="github.com", ports=[443], protocol="https"),
                        EgressGrant(destination="api.example.com", ports=[443], protocol="https"),
                    ]
                )
            )
        )
        # Task uses github.com exactly — within bundle scope
        task = CapabilityManifest(
            grants=Grants(
                network=NetworkGrants(
                    egress=[
                        EgressGrant(destination="github.com", ports=[443], protocol="https"),
                    ]
                )
            )
        )
        ok, reason = is_subset(task, bundle)
        assert ok, f"task network exact match should be within bundle scope: {reason}"

    def test_network_exact_match_only(self):
        """Network destination containment requires exact match or CIDR."""
        bundle = CapabilityManifest(
            grants=Grants(
                network=NetworkGrants(
                    egress=[EgressGrant(destination="github.com", ports=[443], protocol="https")]
                )
            )
        )
        task = CapabilityManifest(
            grants=Grants(
                network=NetworkGrants(
                    egress=[EgressGrant(destination="api.github.com", ports=[443], protocol="https")]
                )
            )
        )
        ok, reason = is_subset(task, bundle)
        assert not ok, f"api.github.com != github.com should not be subset: {reason}"

    def test_process_subset(self):
        bundle = CapabilityManifest(
            grants=Grants(
                process=ProcessGrants(
                    exec=[ExecGrant(binary="git"), ExecGrant(binary="python")]
                )
            )
        )
        task = CapabilityManifest(
            grants=Grants(
                process=ProcessGrants(
                    exec=[ExecGrant(binary="git")]
                )
            )
        )
        ok, reason = is_subset(task, bundle)
        assert ok, f"task process within bundle scope: {reason}"

    def test_process_exceeds_scope(self):
        bundle = CapabilityManifest(
            grants=Grants(
                process=ProcessGrants(
                    exec=[ExecGrant(binary="git")]
                )
            )
        )
        task = CapabilityManifest(
            grants=Grants(
                process=ProcessGrants(
                    exec=[ExecGrant(binary="curl")]
                )
            )
        )
        ok, reason = is_subset(task, bundle)
        assert not ok, "task using curl exceeds bundle scope"


class TestBwrapArgs:
    """Verify capability_to_bwrap_args() generates valid arguments."""

    def test_basic_bwrap(self):
        manifest = CapabilityManifest(
            grants=Grants(
                filesystem=FilesystemGrants(
                    reads=[FilesystemPathGrant(path="/tmp")],
                ),
                network=NetworkGrants(
                    egress=[EgressGrant(destination="github.com", ports=[443], protocol="https")],
                ),
                process=ProcessGrants(
                    exec=[ExecGrant(binary="git")],
                ),
            )
        )
        args = capability_to_bwrap_args(manifest, worktree_path="/tmp/wt", socket_path="/run/studio/orch.sock")
        assert args[0] == "bwrap"
        assert "--die-with-parent" in args
        assert any("--bind" in a or a == "--bind" for a in args)

    def test_bwrap_includes_proxy_socket(self):
        manifest = CapabilityManifest(
            grants=Grants(
                network=NetworkGrants(
                    egress=[EgressGrant(destination="github.com", ports=[443], protocol="https")],
                )
            )
        )
        # Proxy socket path: capability_to_bwrap_args binds the parent directory if it exists
        args = capability_to_bwrap_args(
            manifest, worktree_path="/tmp/wt",
            socket_path="/run/studio/orch.sock",
            proxy_socket="/tmp/studio-proxy-test.sock",
        )
        # With a proxy socket, --unshare-net should be added
        assert "--unshare-net" in args


class TestGlobMatch:
    """Verify glob_match() from artifact module."""

    def test_exact(self):
        assert glob_match("hello", "hello")
        assert not glob_match("hello", "world")

    def test_star(self):
        assert glob_match("*.py", "test.py")
        assert not glob_match("*.py", "test.txt")

    def test_double_star(self):
        assert glob_match("**", "a/b/c")
        assert glob_match("a/**/z", "a/b/c/z")

    def test_question(self):
        assert glob_match("file.???", "file.txt")
        assert not glob_match("file.???", "file.py")  # 2 chars, doesn't match ???


class TestManifestConstruction:
    """Verify CapabilityManifest can be constructed standalone."""

    def test_default_manifest(self):
        m = CapabilityManifest()
        assert m.schema_version == "1.0"
        assert m.grants.filesystem.reads == []
        assert m.grants.filesystem.writes == []
        assert m.grants.network.egress == []

    def test_full_manifest(self):
        m = CapabilityManifest(
            grants=Grants(
                filesystem=FilesystemGrants(
                    reads=[FilesystemPathGrant(path="/src")],
                    writes=[FilesystemWriteGrant(path="/out")],
                ),
                network=NetworkGrants(
                    egress=[EgressGrant(destination="api.example.com", ports=[443], protocol="https")],
                ),
                process=ProcessGrants(
                    exec=[ExecGrant(binary="python"), ExecGrant(binary="git")],
                ),
                rpc=RpcGrants(methods=["heartbeat", "log", "artifact.publish"]),
                resources=ResourceGrants(cpu_limit=2, memory_limit=1024),
                secrets=[SecretGrant(name="GITHUB_TOKEN", purpose="github_auth")],
            )
        )
        assert len(m.grants.filesystem.reads) == 1
        assert m.grants.resources.cpu_limit == 2
        assert m.grants.resources.memory_limit == 1024
        assert len(m.grants.secrets) == 1
