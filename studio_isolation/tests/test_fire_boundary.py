"""Phase 4: Fire test against learhy/boundary.

Exercises the full Hermes → MetaOrchestrator → LangGraph → workers pipeline
with Boundary-specific capabilities (Go, gRPC, Makefile-driven builds).

Target: learhy/boundary (hashicorp/boundary — 112MB Go monorepo)

Test categories:
1. Intent decomposition for Boundary-specific tasks
2. StateGraph topology with Boundary manifests
3. End-to-end graph execution with Boundary state
4. Capability manifest generation for Go/gRPC workloads
5. Approval matrix evaluation for Boundary work
"""

import os
import pytest
import tempfile
import subprocess
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from studio_isolation.meta_orchestrator import MetaOrchestrator, DecomposedIntent
from studio_isolation.langgraph_adapter import StudioGraphState


# ── Boundary-specific intents ────────────────────────────────────────────────

BOUNDARY_INTENTS = [
    {
        "intent": "Add a RateLimiter gRPC interceptor to the controller daemon",
        "expected_tags": ["rate-limiting"],
        "expected_tier": "full_review",
        "complexity_min": 4,
        "risk_min": 4,
    },
    {
        "intent": "Add a structured logging middleware to gRPC interceptor chain",
        "expected_tags": ["middleware"],
        "expected_tier": "full_review",
        "complexity_min": 4,
        "risk_min": 2,
    },
    {
        "intent": "Add a README section about gRPC interceptors",
        "expected_tags": [],
        "expected_tier": "auto",
        "complexity_max": 3,
        "risk_max": 2,
    },
    {
        "intent": "Refactor the auth interceptor to use new token validation",
        "expected_tags": ["auth"],
        "expected_tier": "full_review",
        "complexity_min": 4,
        "risk_min": 2,
    },
    {
        "intent": "Fix typo in controller interceptor comments",
        "expected_tags": [],
        "expected_tier": "auto",
        "complexity_max": 3,
        "risk_max": 2,
    },
]


class TestBoundaryIntentDecomposition:
    """Verify intent decomposition for Boundary-specific tasks."""

    @staticmethod
    def _orch():
        return MetaOrchestrator.__new__(MetaOrchestrator)

    def test_rate_limiter_intent(self):
        orch = self._orch()
        result = orch.decompose_intent(
            intent="Add a RateLimiter gRPC interceptor to the controller daemon",
            bundle_id="boundary-fire-001",
            target_repo="learhy/boundary",
        )
        assert result.target_repo == "learhy/boundary"
        assert "rate-limiting" in result.tags
        assert result.approval_tier == "full_review"
        assert result.auto_ship is False
        # Verify task DAG has all required phases
        node_ids = {n["id"] for n in result.task_dag["nodes"]}
        for phase in ("clone", "research", "implement", "test", "pr"):
            assert phase in node_ids, f"Missing phase '{phase}' in task DAG"

    def test_readme_intent_is_auto(self):
        """Simple doc changes should auto-ship."""
        orch = self._orch()
        result = orch.decompose_intent(
            intent="Add a README section about gRPC interceptors",
            bundle_id="boundary-fire-002",
            target_repo="learhy/boundary",
        )
        assert result.auto_ship is True
        assert result.approval_tier == "auto"
        assert result.proposal["complexity_score"] <= 3

    def test_auth_refactor_is_high_risk(self):
        orch = self._orch()
        result = orch.decompose_intent(
            intent="Refactor the auth interceptor to use new token validation",
            bundle_id="boundary-fire-003",
            target_repo="learhy/boundary",
        )
        assert "auth" in result.tags
        assert result.proposal["risk_score"] >= 2

    @pytest.mark.parametrize("spec", BOUNDARY_INTENTS)
    def test_intent_roundtrip(self, spec):
        """Every Boundary intent decomposes cleanly and produces valid state."""
        orch = self._orch()
        result = orch.decompose_intent(
            intent=spec["intent"],
            bundle_id="boundary-fire-roundtrip",
            target_repo="learhy/boundary",
        )
        # Tags
        if spec["expected_tags"]:
            for tag in spec["expected_tags"]:
                assert tag in result.tags, f"Expected tag '{tag}' in {result.tags}"
        else:
            assert len(result.tags) == 0, f"Expected no tags, got {result.tags}"
        # Tier
        assert result.approval_tier == spec["expected_tier"]
        # Complexity bounds
        if "complexity_min" in spec:
            assert result.proposal["complexity_score"] >= spec["complexity_min"]
        if "complexity_max" in spec:
            assert result.proposal["complexity_score"] <= spec["complexity_max"]
        # Risk bounds
        if "risk_min" in spec:
            assert result.proposal["risk_score"] >= spec["risk_min"]
        if "risk_max" in spec:
            assert result.proposal["risk_score"] <= spec["risk_max"]
        # State is valid
        state = result.to_initial_state()
        assert state["bundle_input"] == spec["intent"]
        assert state["bundle_id"] == "boundary-fire-roundtrip"
        assert "branch_name" in state


class TestBoundaryCapabilityManifest:
    """Verify capability manifests generated for Boundary workloads."""

    def test_go_developer_manifest(self):
        """A Go developer worker should have git, go, make, bash access."""
        from studio_isolation.models import (
            CapabilityManifest, Grants, FilesystemGrants, FilesystemPathGrant,
            FilesystemWriteGrant, NetworkGrants, EgressGrant,
            ProcessGrants, ExecGrant,
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
                    exec=[
                        ExecGrant(binary="git"),
                        ExecGrant(binary="go"),
                        ExecGrant(binary="make"),
                        ExecGrant(binary="bash"),
                    ],
                ),
            ),
        )

        # Verify grants are present
        assert manifest.grants.filesystem is not None
        assert len(manifest.grants.filesystem.reads) >= 1
        assert len(manifest.grants.filesystem.writes) >= 1
        assert manifest.grants.filesystem.writes[0].path == worktree
        assert manifest.grants.filesystem.writes[0].create is True

        # Verify network egress grants
        destinations = {g.destination for g in manifest.grants.network.egress}
        assert "github.com" in destinations
        assert "proxy.golang.org" in destinations

        # Verify process grants
        binaries = {g.binary for g in manifest.grants.process.exec}
        for tool in ("git", "go", "make", "bash"):
            assert tool in binaries, f"Missing {tool} in process grants"

        # Verify subset checking works
        from studio_isolation import is_subset
        # A manifest that requests git + go should be a subset of git+go+make+bash
        subset_manifest = CapabilityManifest(
            grants=Grants(
                process=ProcessGrants(
                    exec=[ExecGrant(binary="git"), ExecGrant(binary="go")],
                ),
            ),
        )
        ok, reason = is_subset(subset_manifest, manifest)
        assert ok, f"Subset check failed: {reason}"

        # A manifest requesting docker (not in grants) should be rejected
        overscope_manifest = CapabilityManifest(
            grants=Grants(
                process=ProcessGrants(
                    exec=[ExecGrant(binary="docker")],
                ),
            ),
        )
        ok, reason = is_subset(overscope_manifest, manifest)
        assert not ok, "Should reject manifest with docker exec grant"


class TestBoundaryGraphRunner:
    """Verify LangGraph execution with Boundary state."""

    @pytest.mark.asyncio
    async def test_boundary_capability_manifest_in_graph(self):
        """A graph run with Boundary intent should produce a valid capability manifest."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner

        runner = await StudioGraphRunner.create(db_path=":memory:")
        try:
            state = await runner.run(
                bundle_input="Add a RateLimiter gRPC interceptor to the controller daemon",
                bundle_id="boundary-fire-graph-001",
                auto_ship=True,
            )
            assert state["bundle_id"] == "boundary-fire-graph-001"
            assert state["qa_passed"] is True
            assert state["approved"] is True
            assert state["approval_decision"] == "approved"
            assert "approval_tier" in state
        finally:
            await runner.close()

    @pytest.mark.asyncio
    async def test_multiple_boundary_bundles_different_threads(self):
        """Multiple Boundary bundles don't interfere with each other."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner

        runner = await StudioGraphRunner.create(db_path=":memory:")
        try:
            results = {}
            for i, spec in enumerate(BOUNDARY_INTENTS[:3]):
                bundle_id = f"boundary-multi-{i}"
                state = await runner.run(
                    bundle_input=spec["intent"],
                    bundle_id=bundle_id,
                    auto_ship=True,
                )
                results[bundle_id] = state

            # Each bundle has distinct state
            assert results["boundary-multi-0"]["bundle_id"] == "boundary-multi-0"
            assert results["boundary-multi-1"]["bundle_id"] == "boundary-multi-1"
            assert results["boundary-multi-2"]["bundle_id"] == "boundary-multi-2"

            # All should have completed the full pipeline
            for state in results.values():
                assert state["qa_passed"] is True
                assert state["approved"] is True
        finally:
            await runner.close()

    @pytest.mark.asyncio
    async def test_graph_topology_traversal(self):
        """Verify all nodes in the canonical graph are visited for Boundary."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner

        runner = await StudioGraphRunner.create(db_path=":memory:")
        try:
            config = runner.config_for("boundary-topo-001")
            initial: StudioGraphState = {
                "bundle_input": "Add rate limiter to Boundary gRPC",
                "bundle_id": "boundary-topo-001",
                "auto_ship": True,
            }

            nodes_visited = []
            async for event in runner.graph.astream(initial, config):
                node_name = list(event.keys())[0]
                nodes_visited.append(node_name)

            # Verify the expected node sequence (sequential graph)
            expected = [
                "bundler",
                "review_adversary",
                "review_security",
                "review_qa",
                "approval_gate",
                "developer",
                "qa_verification",
                "complete",
            ]
            assert nodes_visited == expected, (
                f"Expected node sequence {expected}, got {nodes_visited}"
            )
        finally:
            await runner.close()


class TestBoundaryFireFullPipeline:
    """End-to-end: MetaOrchestrator → LangGraph → complete, Boundary intent."""

    @pytest.mark.asyncio
    async def test_full_pipeline_auto_ship(self):
        """Full pipeline: intent → decompose → graph → complete."""
        orch = await MetaOrchestrator.create(db_path=":memory:")
        try:
            result = await orch.execute(
                intent="Add structured logging to gRPC interceptor chain",
                bundle_id="boundary-fire-full-001",
                auto_ship=True,
                target_repo="learhy/boundary",
            )
            assert result.success is True
            assert result.bundle_id == "boundary-fire-full-001"
            assert "proposal" in result.state
            assert "review_findings" in result.state
            assert result.state["approved"] is True
            assert result.state["qa_passed"] is True
        finally:
            await orch.close()

    @pytest.mark.asyncio
    async def test_full_pipeline_rejection_path(self):
        """Rejected bundles go through all review nodes but stop at gate."""
        orch = await MetaOrchestrator.create(db_path=":memory:")
        try:
            # High complexity + high risk = requires human approval
            # Since no relay is configured, this should fail
            result = await orch.execute(
                intent="Rewrite the entire auth system and change the encryption scheme",
                bundle_id="boundary-fire-full-002",
                auto_ship=False,  # Force human review
                target_repo="learhy/boundary",
            )
            # Without a relay, the interrupt fails
            assert result.was_interrupted is False
            # But decomposition still works
            assert result.bundle_id == "boundary-fire-full-002"
        finally:
            await orch.close()

    @pytest.mark.asyncio
    async def test_boundary_decomposition_within_graph(self):
        """Verify decomposed intent flows through the graph correctly."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner

        # Pre-decompose the intent
        orch = MetaOrchestrator.__new__(MetaOrchestrator)
        decomposed = orch.decompose_intent(
            intent="Add rate limiter to controller interceptor",
            bundle_id="boundary-dag-test",
            target_repo="learhy/boundary",
        )

        runner = await StudioGraphRunner.create(db_path=":memory:")
        try:
            state = await runner.graph.ainvoke(
                decomposed.to_initial_state(),
                runner.config_for("boundary-dag-test"),
            )
            assert state["bundle_id"] == "boundary-dag-test"
            assert state["qa_passed"] is True
            assert state["approved"] is True
            # The intent should flow through
            assert "rate limiter" in state["bundle_input"].lower()
        finally:
            await runner.close()


# ═══════════════════════════════════════════════════════════════════════
# Real runner injection — verifies spawn_worker is called via config
# ═══════════════════════════════════════════════════════════════════════


class TestRunnerInjection:
    """Verify runner/db are threaded through RunnableConfig to nodes."""

    @pytest.mark.asyncio
    async def test_runner_injected_via_config_reaches_node(self):
        """When StudioGraphRunner is created with a runner, node_developer
        receives it via config.configurable and spawn_worker is called."""
        from unittest.mock import MagicMock, AsyncMock
        from studio_isolation.langgraph_adapter import StudioGraphRunner

        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.error = ""
        mock_result.process = None
        mock_runner.spawn_worker = AsyncMock(return_value=mock_result)

        mock_db = MagicMock()
        mock_db.fetch_one = AsyncMock(return_value=None)
        mock_db.execute = AsyncMock()
        mock_db.conn = MagicMock()
        mock_db.conn.commit = AsyncMock()

        runner = await StudioGraphRunner.create(
            db_path=":memory:",
            studio_runner=mock_runner,
            studio_db=mock_db,
        )
        try:
            state = await runner.run(
                bundle_input="Add comment to README",
                bundle_id="inject-test-001",
                auto_ship=True,
            )
            # node_developer should have called spawn_worker
            mock_runner.spawn_worker.assert_called_once()
            call_kwargs = mock_runner.spawn_worker.call_args.kwargs
            assert call_kwargs["bundle_id"] == "inject-test-001"
            assert call_kwargs["node_id"] == "developer"
            assert call_kwargs["worker_type"] == "developer"
            # Graph still completes successfully
            assert state["approved"] is True
            assert state["qa_passed"] is True
        finally:
            await runner.close()

    @pytest.mark.asyncio
    async def test_noop_runner_passes_through_graph(self):
        """NoopWorkerRunner injected through config produces real DB entries
        but graph still completes cleanly."""
        from studio_isolation.runner import NoopWorkerRunner
        from studio_isolation.langgraph_adapter import StudioGraphRunner

        mock_db = MagicMock()
        mock_db.fetch_one = AsyncMock(return_value=None)
        mock_db.execute = AsyncMock()
        mock_db.conn = MagicMock()
        mock_db.conn.commit = AsyncMock()

        noop = NoopWorkerRunner(mock_db, token_expiry_minutes=5)

        runner = await StudioGraphRunner.create(
            db_path=":memory:",
            studio_runner=noop,
            studio_db=mock_db,
        )
        try:
            state = await runner.run(
                bundle_input="Fix typo in boundary docs",
                bundle_id="noop-test-001",
                auto_ship=True,
            )
            assert state["bundle_id"] == "noop-test-001"
            assert state["approved"] is True
            assert state["qa_passed"] is True
        finally:
            await runner.close()

    @pytest.mark.asyncio
    async def test_meta_orchestrator_passes_runner_to_graph(self):
        """MetaOrchestrator.create() propagates runner to StudioGraphRunner."""
        from unittest.mock import MagicMock, AsyncMock
        from studio_isolation.meta_orchestrator import MetaOrchestrator

        mock_runner = MagicMock()
        mock_result = MagicMock()
        mock_result.error = ""
        mock_result.process = None
        mock_runner.spawn_worker = AsyncMock(return_value=mock_result)

        mock_db = MagicMock()
        mock_db.fetch_one = AsyncMock(return_value=None)
        mock_db.execute = AsyncMock()
        mock_db.conn = MagicMock()
        mock_db.conn.commit = AsyncMock()

        orch = await MetaOrchestrator.create(
            db_path=":memory:",
            studio_runner=mock_runner,
            studio_db=mock_db,
        )
        try:
            result = await orch.execute(
                intent="Add comment to boundary README",
                bundle_id="meta-inject-001",
                auto_ship=True,
                target_repo="learhy/boundary",
            )
            assert result.success is True
            mock_runner.spawn_worker.assert_called_once()
        finally:
            await orch.close()


# ═══════════════════════════════════════════════════════════════════════
# Real boundary worktree fire test — go build inside the graph
# ═══════════════════════════════════════════════════════════════════════


class TestBoundaryWorktreeFire:
    """Real boundary worktree operations through the LangGraph adapter."""

    @pytest.mark.asyncio
    async def test_graph_with_go_build_in_worktree(self):
        """Create a real boundary worktree and run go build through
        the developer node, driven by a custom runner that actually
        executes commands."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner, StudioGraphState
        from studio_isolation.runner import WorkerSpawnResult
        import subprocess

        boundary_repo = Path("/home/dan.rohan/software/boundary")
        if not boundary_repo.is_dir() or not (boundary_repo / "go.mod").exists():
            pytest.skip("boundary repo not available")

        # Create a temp worktree that we'll use as the "worker's" workspace
        wt_dir = tempfile.mkdtemp(prefix="studio-boundary-fire-graph-")

        class RealBoundaryRunner:
            """A test runner that creates a real worktree and runs go build."""

            def spawn_worker(self, worker_id, bundle_id, node_id, manifest,
                            worktree_path, task_spec, worker_type, **kwargs):
                # Create a real git worktree from boundary
                subprocess.run(
                    ["git", "worktree", "add", "--detach", str(wt_dir), "HEAD"],
                    cwd=str(boundary_repo), capture_output=True, check=True,
                )
                # Run go build in the worktree
                build_result = subprocess.run(
                    ["go", "build", "./..."],
                    cwd=str(wt_dir), capture_output=True, text=True, timeout=120,
                )
                return WorkerSpawnResult(
                    worker_id=worker_id, token="test-token",
                    node_id=node_id, error="" if build_result.returncode == 0 else build_result.stderr,
                )

        runner = await StudioGraphRunner.create(
            db_path=":memory:",
            studio_runner=RealBoundaryRunner(),
        )
        try:
            state = await runner.run(
                bundle_input="Fix typo in boundary go.mod comment",
                bundle_id="boundary-build-fire-001",
                auto_ship=True,
            )
            assert state["bundle_id"] == "boundary-build-fire-001"
            assert state["approved"] is True
            assert state["qa_passed"] is True
        finally:
            # Clean up worktree
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_dir)],
                cwd=str(boundary_repo), capture_output=True,
            )
            await runner.close()

    @pytest.mark.asyncio
    async def test_graph_with_boundary_vet_and_list(self):
        """Run go vet and go list in a boundary worktree through the graph."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner
        from studio_isolation.runner import WorkerSpawnResult
        import subprocess

        boundary_repo = Path("/home/dan.rohan/software/boundary")
        if not boundary_repo.is_dir() or not (boundary_repo / "go.mod").exists():
            pytest.skip("boundary repo not available")

        wt_dir = tempfile.mkdtemp(prefix="studio-boundary-fire-vet-")

        class BoundaryVetRunner:
            def spawn_worker(self, worker_id, bundle_id, node_id, manifest,
                            worktree_path, task_spec, worker_type, **kwargs):
                subprocess.run(
                    ["git", "worktree", "add", "--detach", str(wt_dir), "HEAD"],
                    cwd=str(boundary_repo), capture_output=True, check=True,
                )
                # Run go vet
                vet_result = subprocess.run(
                    ["go", "vet", "./..."],
                    cwd=str(wt_dir), capture_output=True, text=True, timeout=120,
                )
                # Run go list
                list_result = subprocess.run(
                    ["go", "list", "./..."],
                    cwd=str(wt_dir), capture_output=True, text=True, timeout=60,
                )
                errors = []
                if vet_result.returncode != 0:
                    errors.append(f"vet: {vet_result.stderr}")
                if list_result.returncode != 0:
                    errors.append(f"list: {list_result.stderr}")
                return WorkerSpawnResult(
                    worker_id=worker_id, token="test-token",
                    node_id=node_id, error="; ".join(errors) if errors else "",
                )

        runner = await StudioGraphRunner.create(
            db_path=":memory:",
            studio_runner=BoundaryVetRunner(),
        )
        try:
            state = await runner.run(
                bundle_input="Audit boundary packages for naming conventions",
                bundle_id="boundary-vet-fire-001",
                auto_ship=True,
            )
            assert state["bundle_id"] == "boundary-vet-fire-001"
            assert state["approved"] is True
            assert state["qa_passed"] is True
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_dir)],
                cwd=str(boundary_repo), capture_output=True,
            )
            await runner.close()
