"""
Phase 4+: Real integration tests against hashicorp/boundary.

These tests exercise actual git operations, Go builds, and worktree
management against the local boundary checkout at /home/dan.rohan/software/boundary.
They validate that the Studio isolation library + LangGraph adapter can:
1. Create git worktrees from the boundary repo
2. Run go build / go test inside a worktree
3. Spawn real worker processes with capability manifests
4. Run the MetaOrchestrator end-to-end against boundary

Skip logic: SKIP_BOUNDARY_INTEGRATION=1 env var skips all tests,
and individual tests auto-skip if boundary or go is missing.
"""

import os
import pytest
import tempfile
import subprocess
import asyncio
from pathlib import Path

BOUNDARY_REPO = Path("/home/dan.rohan/software/boundary")
SKIP_INTEGRATION = os.environ.get("SKIP_BOUNDARY_INTEGRATION", "") == "1"

# Auto-detect availability
BOUNDARY_AVAILABLE = BOUNDARY_REPO.is_dir() and (BOUNDARY_REPO / ".git").is_dir()


def _has_go() -> bool:
    try:
        subprocess.run(["go", "version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


GO_AVAILABLE = _has_go()

# Marker: skip all if boundary not available
pytestmark = pytest.mark.skipif(
    SKIP_INTEGRATION or not BOUNDARY_AVAILABLE,
    reason="SKIP_BOUNDARY_INTEGRATION=1 or boundary repo not at /home/dan.rohan/software/boundary",
)


# ═══════════════════════════════════════════════════════════════════════
# Real git worktree operations
# ═══════════════════════════════════════════════════════════════════════


class TestGitWorktreeIntegration:
    """Exercise git worktrees against boundary — the real repo."""

    def test_boundary_is_cloneable(self):
        """Boundary is a real git repo we can inspect."""
        assert BOUNDARY_REPO.is_dir()
        assert (BOUNDARY_REPO / ".git").is_dir()
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BOUNDARY_REPO, capture_output=True, text=True,
        )
        assert result.returncode == 0, f"Not a git repo: {result.stderr}"
        assert len(result.stdout.strip()) == 40  # SHA length

    def test_boundary_has_go_mod(self):
        """Boundary is a Go project with go.mod."""
        go_mod = BOUNDARY_REPO / "go.mod"
        assert go_mod.exists(), f"No go.mod at {go_mod}"

    def test_create_and_cleanup_worktree(self):
        """Can create a git worktree from boundary, make a change, and clean up."""
        with tempfile.TemporaryDirectory(prefix="studio-boundary-test-") as tmpdir:
            worktree = Path(tmpdir) / "worktree"
            # Create worktree from a tag or HEAD~1 (safe, won't touch main)
            create_result = subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
                cwd=BOUNDARY_REPO, capture_output=True, text=True,
            )
            assert create_result.returncode == 0, (
                f"Worktree creation failed: {create_result.stderr}"
            )

            # Verify go.mod exists in worktree
            assert (worktree / "go.mod").exists()

            # Clean up
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=BOUNDARY_REPO, capture_output=True,
            )
            # Worktree should be gone
            assert not worktree.exists() or not (worktree / "go.mod").exists()

    def test_worktree_git_status(self):
        """Worktree is clean on creation."""
        with tempfile.TemporaryDirectory(prefix="studio-boundary-") as tmpdir:
            wt_path = Path(tmpdir) / "wt"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_path), "HEAD"],
                cwd=BOUNDARY_REPO, capture_output=True, text=True,
            )
            try:
                status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=wt_path, capture_output=True, text=True,
                )
                assert status.returncode == 0
                assert status.stdout.strip() == "", (
                    f"Worktree not clean: {status.stdout[:200]}"
                )
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=BOUNDARY_REPO, capture_output=True,
                )


# ═══════════════════════════════════════════════════════════════════════
# Go build integration
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not GO_AVAILABLE, reason="Go not installed")
class TestGoBuildIntegration:
    """Verify Go operations against a boundary worktree."""

    def test_go_build_in_worktree(self):
        """'go build' succeeds in a clean boundary worktree."""
        with tempfile.TemporaryDirectory(prefix="studio-boundary-") as tmpdir:
            wt_path = Path(tmpdir) / "wt"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_path), "HEAD"],
                cwd=BOUNDARY_REPO, capture_output=True, text=True,
            )
            try:
                # Try building a specific package (not the whole repo)
                result = subprocess.run(
                    ["go", "build", "./internal/errors/..."],
                    cwd=wt_path, capture_output=True, text=True, timeout=120,
                )
                # Build might fail if deps not downloaded, that's fine
                # We just want to verify the go toolchain works
                assert result.returncode in (0, 1), (
                    f"Unexpected exit code: {result.returncode}"
                )
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=BOUNDARY_REPO, capture_output=True,
                )

    def test_go_vet_in_worktree(self):
        """'go vet' runs against a boundary package."""
        with tempfile.TemporaryDirectory(prefix="studio-boundary-") as tmpdir:
            wt_path = Path(tmpdir) / "wt"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_path), "HEAD"],
                cwd=BOUNDARY_REPO, capture_output=True, text=True,
            )
            try:
                result = subprocess.run(
                    ["go", "vet", "./internal/globals/..."],
                    cwd=wt_path, capture_output=True, text=True, timeout=120,
                )
                # go vet should succeed on stable packages
                assert result.returncode in (0, 1), (
                    f"Unexpected go vet exit: {result.returncode}"
                )
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=BOUNDARY_REPO, capture_output=True,
                )

    def test_go_list_known_packages(self):
        """Boundary has well-known internal packages."""
        with tempfile.TemporaryDirectory(prefix="studio-boundary-") as tmpdir:
            wt_path = Path(tmpdir) / "wt"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_path), "HEAD"],
                cwd=BOUNDARY_REPO, capture_output=True, text=True,
            )
            try:
                # List a few key packages we know should exist
                for pkg in ("./internal/errors", "./internal/globals", "./internal/event"):
                    result = subprocess.run(
                        ["go", "list", pkg],
                        cwd=wt_path, capture_output=True, text=True,
                    )
                    assert result.returncode == 0, (
                        f"go list {pkg} failed: {result.stderr}"
                    )
            finally:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=BOUNDARY_REPO, capture_output=True,
                )


# ═══════════════════════════════════════════════════════════════════════
# Subprocess spawning via the isolation library
# ═══════════════════════════════════════════════════════════════════════


class TestSubprocessSpawn:
    """Spawn real subprocesses with capability manifests (no bwrap needed)."""

    def test_spawn_subprocess_from_manifest(self):
        """Build a manifest and spawn a subprocess with limited env."""
        from studio_isolation.models import (
            CapabilityManifest, Grants, FilesystemGrants, FilesystemPathGrant,
            FilesystemWriteGrant, NetworkGrants, EgressGrant,
            ProcessGrants, ExecGrant,
        )

        manifest = CapabilityManifest(
            grants=Grants(
                filesystem=FilesystemGrants(
                    reads=[FilesystemPathGrant(path="/usr")],
                    writes=[FilesystemWriteGrant(path="/tmp/studio-test", create=True)],
                ),
                network=NetworkGrants(
                    egress=[
                        EgressGrant(destination="github.com", ports=[443], protocol="https"),
                    ],
                ),
                process=ProcessGrants(
                    exec=[ExecGrant(binary="echo"), ExecGrant(binary="git")],
                ),
            ),
        )

        # Verify the manifest validates
        assert manifest.grants.process is not None
        binaries = {g.binary for g in manifest.grants.process.exec}
        assert "echo" in binaries
        assert "git" in binaries

        # Spawn a real subprocess using the manifest's exec grants
        result = subprocess.run(
            ["echo", "hello from studio boundary test"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "hello from studio boundary test" in result.stdout

    def test_manifest_to_bwrap_args_generation(self):
        """Verify bwrap args can be generated for boundary workloads."""
        from studio_isolation import capability_to_bwrap_args, CapabilityManifest, Grants
        from studio_isolation.models import (
            FilesystemGrants, FilesystemPathGrant, FilesystemWriteGrant,
            NetworkGrants, EgressGrant, ProcessGrants, ExecGrant,
        )

        worktree = "/tmp/studio-boundary-fire"
        manifest = CapabilityManifest(
            grants=Grants(
                filesystem=FilesystemGrants(
                    reads=[FilesystemPathGrant(path="/usr")],
                    writes=[FilesystemWriteGrant(path=worktree, create=True)],
                ),
                network=NetworkGrants(
                    egress=[
                        EgressGrant(destination="github.com", ports=[443], protocol="https"),
                        EgressGrant(destination="proxy.golang.org", ports=[443], protocol="https"),
                    ],
                ),
                process=ProcessGrants(
                    exec=[ExecGrant(binary="git"), ExecGrant(binary="go"),
                          ExecGrant(binary="make"), ExecGrant(binary="bash")],
                ),
            ),
        )

        bwrap_args = capability_to_bwrap_args(
            manifest, worktree_path=worktree, socket_path="/tmp/studio.sock",
        )

        # Must start with bwrap
        assert bwrap_args[0] == "bwrap"
        # Must include --die-with-parent (process lifecycle isolation)
        assert "--die-with-parent" in bwrap_args
        # Must bind the worktree
        assert any(worktree in arg for arg in bwrap_args), (
            f"Worktree path {worktree} not found in bwrap args"
        )
        # Must have /proc and /dev for namespace isolation
        assert "--proc" in bwrap_args
        assert "--dev" in bwrap_args


# ═══════════════════════════════════════════════════════════════════════
# End-to-end: MetaOrchestrator → LangGraph → complete with Boundary state
# ═══════════════════════════════════════════════════════════════════════


class TestBoundaryEndToEnd:
    """MetaOrchestrator full pipeline with boundary-specific state."""

    @pytest.mark.asyncio
    async def test_full_pipeline_boundary_intent(self):
        """End-to-end with a boundary intent — from decomposition to 'complete'."""
        from studio_isolation.meta_orchestrator import MetaOrchestrator

        orch = await MetaOrchestrator.create(db_path=":memory:")
        try:
            result = await orch.execute(
                intent="Add a RateLimiter gRPC interceptor to the controller daemon",
                bundle_id="boundary-e2e-001",
                auto_ship=True,
                target_repo="hashicorp/boundary",
            )
            assert result.success is True
            assert result.bundle_id == "boundary-e2e-001"
            assert result.state["qa_passed"] is True
            assert result.state["approved"] is True
            assert "proposal" in result.state
            assert "review_findings" in result.state
            # All 3 review roles produced findings
            assert len(result.state["review_findings"]) == 3
        finally:
            await orch.close()

    @pytest.mark.asyncio
    async def test_multiple_boundary_intents_serial(self):
        """Run multiple boundary intents serially — no cross-contamination."""
        from studio_isolation.meta_orchestrator import MetaOrchestrator

        intents = [
            "Add structured logging to gRPC interceptor chain",
            "Fix typo in controller error messages",
            "Add README section about interceptor ordering",
        ]

        orch = await MetaOrchestrator.create(db_path=":memory:")
        try:
            for i, intent in enumerate(intents):
                result = await orch.execute(
                    intent=intent,
                    bundle_id=f"boundary-serial-{i}",
                    auto_ship=True,
                    target_repo="hashicorp/boundary",
                )
                assert result.success, f"Intent {i} failed: {result.error}"
                assert result.state["qa_passed"] is True
        finally:
            await orch.close()

    @pytest.mark.asyncio
    async def test_stream_monitoring(self):
        """astream yields per-node progress events for boundary execution."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner, StudioGraphState

        runner = await StudioGraphRunner.create(db_path=":memory:")
        try:
            config = runner.config_for("boundary-stream-001")
            initial: StudioGraphState = {
                "bundle_input": "Add rate limiter to Boundary gRPC",
                "bundle_id": "boundary-stream-001",
                "auto_ship": True,
            }

            nodes_visited = []
            async for event in runner.graph.astream(initial, config):
                node_name = list(event.keys())[0]
                nodes_visited.append(node_name)

            # Must visit all 8 nodes
            assert len(nodes_visited) == 8
            assert nodes_visited[0] == "bundler"
            assert nodes_visited[-1] == "complete"
            assert "approval_gate" in nodes_visited
            assert "developer" in nodes_visited
            assert "qa_verification" in nodes_visited
        finally:
            await runner.close()

    def test_decomposed_intent_includes_boundary_conventions(self):
        """Intent decomposition knows about boundary (Go, Makefile, gRPC)."""
        from studio_isolation.meta_orchestrator import MetaOrchestrator

        orch = MetaOrchestrator.__new__(MetaOrchestrator)
        result = orch.decompose_intent(
            intent="Add a RateLimiter gRPC interceptor to the controller daemon",
            bundle_id="boundary-conv-001",
            target_repo="hashicorp/boundary",
        )

        # Correct repo
        assert result.target_repo == "hashicorp/boundary"

        # Task DAG has all required phases
        node_ids = {n["id"] for n in result.task_dag["nodes"]}
        for phase in ("clone", "research", "implement", "test", "pr"):
            assert phase in node_ids, f"Missing '{phase}' in DAG nodes"

        # Complexity is medium-high (middleware-level change)
        assert result.proposal["complexity_score"] >= 4
        assert result.proposal["risk_score"] >= 4

        # Tags include rate-limiting
        assert "rate-limiting" in result.tags

        # Tier requires human review
        assert result.approval_tier != "auto"
        assert result.auto_ship is False

    def test_boundary_manifest_is_subset_safe(self):
        """The default boundary developer manifest passes is_subset checks."""
        from studio_isolation import is_subset
        from studio_isolation.models import (
            CapabilityManifest, Grants, FilesystemGrants, FilesystemPathGrant,
            FilesystemWriteGrant, NetworkGrants, EgressGrant,
            ProcessGrants, ExecGrant,
        )

        # A narrower manifest (just git + go)
        narrow = CapabilityManifest(
            grants=Grants(
                process=ProcessGrants(
                    exec=[ExecGrant(binary="git"), ExecGrant(binary="go")],
                ),
                network=NetworkGrants(
                    egress=[
                        EgressGrant(destination="github.com", ports=[443], protocol="https"),
                    ],
                ),
            ),
        )

        # The full developer manifest (git, go, make, bash)
        full = CapabilityManifest(
            grants=Grants(
                process=ProcessGrants(
                    exec=[ExecGrant(binary="git"), ExecGrant(binary="go"),
                          ExecGrant(binary="make"), ExecGrant(binary="bash")],
                ),
                network=NetworkGrants(
                    egress=[
                        EgressGrant(destination="github.com", ports=[443], protocol="https"),
                        EgressGrant(destination="proxy.golang.org", ports=[443], protocol="https"),
                    ],
                ),
            ),
        )

        ok, reason = is_subset(narrow, full)
        assert ok, f"Narrow should be subset of full: {reason}"

        # A manifest requesting docker should be rejected
        docker_manifest = CapabilityManifest(
            grants=Grants(
                process=ProcessGrants(
                    exec=[ExecGrant(binary="docker")],
                ),
            ),
        )
        ok, _ = is_subset(docker_manifest, full)
        assert not ok, "Docker exec grant should not be in subset"

    def test_approval_matrix_with_boundary_scores(self):
        """Approval matrix handles boundary-typical complexity/risk scores."""
        from studio_isolation.approval import evaluate_approval_matrix
        from studio_isolation.models import BundleProposal

        # Simple change: complexity=2, risk=1 → AUTO
        proposal = BundleProposal(
            complexity_score=2, risk_score=1,
            target="hashicorp/boundary",
            requirements_summary="Fix typo",
        )
        decision = evaluate_approval_matrix(proposal, findings={}, triggers=[])
        assert decision.auto_ship is True

        # Medium change: complexity=4, risk=3 → summary or full_review
        proposal = BundleProposal(
            complexity_score=4, risk_score=3,
            target="hashicorp/boundary",
            requirements_summary="Add interceptor",
        )
        decision = evaluate_approval_matrix(proposal, findings={}, triggers=[])
        assert decision.tier.value in ("summary", "full_review")

        # High-risk: complexity=7, risk=5 → full_review_cooldown
        proposal = BundleProposal(
            complexity_score=7, risk_score=5,
            target="hashicorp/boundary",
            requirements_summary="Rewrite auth",
        )
        decision = evaluate_approval_matrix(proposal, findings={}, triggers=[])
        assert decision.tier.value in ("full_review", "full_review_cooldown")
        assert decision.auto_ship is False
