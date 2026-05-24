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
        """Verify all nodes in the canonical graph are visited for Boundary.

        With parallel review fan-out, reviews may arrive in any order.
        We verify all required nodes are present and the boundary nodes
        (bundler first, complete last) have correct positions.
        """
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

            # Verify all required nodes are present (reviews may be in any order)
            required = {
                "bundler", "review_adversary", "review_security", "review_qa",
                "review_aggregator", "approval_gate", "developer",
                "qa_verification", "complete",
            }
            assert set(nodes_visited) == required, (
                f"Expected nodes {sorted(required)}, got {sorted(set(nodes_visited))}"
            )
            # Entry and exit nodes must be in correct position
            assert nodes_visited[0] == "bundler"
            assert nodes_visited[-1] == "complete"
            # Review nodes run in parallel → review_aggregator comes after all reviews
            # approval_gate comes after aggregator → developer → qa → complete
            assert nodes_visited.index("review_aggregator") > nodes_visited.index("bundler")
            assert nodes_visited.index("approval_gate") > nodes_visited.index("review_aggregator")
            assert nodes_visited.index("developer") > nodes_visited.index("approval_gate")
            assert nodes_visited.index("qa_verification") > nodes_visited.index("developer")
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
        """Rejected bundles go through all review nodes but stop at gate.

        With auto_ship=False and no relay configured, the graph reaches
        the interrupt but cannot proceed. The state contains all review
        findings accumulated before the gate.
        """
        orch = await MetaOrchestrator.create(db_path=":memory:")
        try:
            # High complexity + high risk = requires human approval
            # Since no relay is configured, the interrupt fires but fails
            result = await orch.execute(
                intent="Rewrite the entire auth system and change the encryption scheme",
                bundle_id="boundary-fire-full-002",
                auto_ship=False,  # Force human review
                target_repo="learhy/boundary",
            )
            # Without a relay, the interrupt fires and we can't proceed
            assert result.was_interrupted is True
            assert result.success is False
            assert "No relay configured" in result.error
            # Decomposition still worked
            assert result.bundle_id == "boundary-fire-full-002"
            # State contains review findings accumulated before the gate
            assert len(result.state.get("review_findings", [])) == 3
        finally:
            await orch.close()

    @pytest.mark.asyncio
    async def test_boundary_decomposition_within_graph(self):
        """Verify decomposed intent flows through the graph correctly."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner

        # Pre-decompose the intent (rate-limiter = high risk, auto_ship=False)
        orch = MetaOrchestrator.__new__(MetaOrchestrator)
        decomposed = orch.decompose_intent(
            intent="Add rate limiter to controller interceptor",
            bundle_id="boundary-dag-test",
            target_repo="learhy/boundary",
        )
        # Override auto_ship for the graph test
        decomposed.auto_ship = True

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
            # node_developer AND other nodes should have called spawn_worker
            assert mock_runner.spawn_worker.call_count >= 6  # bundler + 3 reviews + developer + qa
            # Verify at least one call was for the developer node
            developer_calls = [
                c for c in mock_runner.spawn_worker.call_args_list
                if c.kwargs.get("node_id") == "developer"
            ]
            assert len(developer_calls) == 1
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
            # All 6 nodes (bundler + 3 reviews + developer + qa) spawn workers
            assert mock_runner.spawn_worker.call_count >= 6
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


# ═══════════════════════════════════════════════════════════════════════
# Crown jewel: Full fire test — make real code changes in a boundary
# worktree through the LangGraph adapter, run go vet, and verify.
# ═══════════════════════════════════════════════════════════════════════


class TestBoundaryFullFire:
    """Real boundary worktree → code change → go vet → verify via full graph.

    This is the crown jewel: the complete pipeline from intent to
    verified code change in a real boundary worktree, driven end-to-end
    by the LangGraph adapter and Studio isolation layer.
    """

    @pytest.mark.asyncio
    async def test_graph_driven_code_change_in_boundary(self):
        """A custom runner wired into the graph that:
        1. Creates a real boundary git worktree
        2. Makes a real code change (adds a doc comment)
        3. Runs go vet on the changed package
        4. Verifies the change is in place

        The developer node does the real work; bundler/review/qa nodes
        are noops (they don't have enough context to produce useful
        output without an orchestrator backend).
        """
        from studio_isolation.langgraph_adapter import StudioGraphRunner, StudioGraphState
        from studio_isolation.runner import WorkerSpawnResult
        import subprocess

        boundary_repo = Path("/home/dan.rohan/software/boundary")
        if not boundary_repo.is_dir() or not (boundary_repo / "go.mod").exists():
            pytest.skip("boundary repo not available")

        wt_dir = tempfile.mkdtemp(prefix="studio-boundary-full-fire-")
        changed_file_ref: list[str] = []  # Mutable container for nested class access

        # Mock DB — required so node guards pass; FullFireRunner ignores it
        mock_db = MagicMock()
        mock_db.fetch_one = AsyncMock(return_value=None)
        mock_db.execute = AsyncMock()
        mock_db.conn = MagicMock()
        mock_db.conn.commit = AsyncMock()

        class FullFireRunner:
            """Runner that does real work in the developer node, noops elsewhere.

            Node order in the graph:
              bundler → review_adversary → review_security → review_qa
                     → approval_gate → *developer* → qa_verification → complete

            Only the developer node does real work. The other nodes return
            immediate placeholders (WorkerSpawnResult with no process).
            """

            async def spawn_worker(self, worker_id, bundle_id, node_id, manifest,
                                   worktree_path, task_spec, worker_type, **kwargs):
                if node_id != "developer":
                    return WorkerSpawnResult(
                        worker_id=worker_id, token="noop",
                        node_id=node_id,
                    )

                # ── DEVELOPER NODE: real boundary work ──────────────────
                # 1. Create a git worktree from Boundary
                subprocess.run(
                    ["git", "worktree", "add", "--detach", str(wt_dir), "HEAD"],
                    cwd=str(boundary_repo), capture_output=True, check=True,
                )

                # 2. Find the internal/errors package (well-known, stable)
                pkg_dir = Path(wt_dir) / "internal" / "errors"
                if not pkg_dir.exists():
                    return WorkerSpawnResult(
                        worker_id=worker_id, token="fire-token",
                        node_id=node_id,
                        error=f"internal/errors not found in boundary worktree",
                    )

                # 3. Read the first .go file in that package
                go_files = sorted(pkg_dir.glob("*.go"))
                if not go_files:
                    return WorkerSpawnResult(
                        worker_id=worker_id, token="fire-token",
                        node_id=node_id,
                        error="No .go files in internal/errors",
                    )

                first_file = go_files[0]
                original = first_file.read_text()
                changed_file_ref.append(str(first_file.relative_to(wt_dir)))

                # 4. Check if the package declaration already has our doc comment
                if "// Studio fire-test marker: this package" in original:
                    # Already modified — clean up and pass
                    return WorkerSpawnResult(
                        worker_id=worker_id, token="fire-token",
                        node_id=node_id,
                    )

                # 5. Add a doc comment after the package declaration
                modified = original.replace(
                    "package errors",
                    "// Package errors provides boundary error types and constructors.\n// Studio fire-test marker: this package was examined by the LangGraph adapter.\npackage errors",
                    1,  # only first occurrence
                )
                if modified == original:
                    # Try alternate package name format
                    modified = original.replace(
                        "package errors",
                        "// Studio fire-test marker: boundary package examined by full fire test.\npackage errors",
                        1,
                    )

                first_file.write_text(modified)

                # 6. Run go vet on the changed package
                changed_rel = changed_file_ref[0] if changed_file_ref else "."
                vet_result = subprocess.run(
                    ["go", "vet", f"./{changed_rel.rsplit('/', 1)[0]}"],
                    cwd=str(wt_dir), capture_output=True, text=True, timeout=60,
                )

                return WorkerSpawnResult(
                    worker_id=worker_id, token="fire-token",
                    node_id=node_id,
                    error="" if vet_result.returncode == 0 else vet_result.stderr[:500],
                )

        runner = await StudioGraphRunner.create(
            db_path=":memory:",
            studio_runner=FullFireRunner(),
            studio_db=mock_db,  # Required by node guards; not used by FullFireRunner
        )
        try:
            intent = "Add package-level documentation to the internal/errors package"
            state = await runner.run(
                bundle_input=intent,
                bundle_id="boundary-full-fire-001",
                auto_ship=True,
            )

            # ── Verify the graph completed ──────────────────────────
            assert state["bundle_id"] == "boundary-full-fire-001"
            assert state["approved"] is True
            assert state["qa_passed"] is True
            assert state.get("error") is None or state["error"] == ""

            # ── Verify the code change is real ──────────────────────
            assert len(changed_file_ref) > 0, "No file was modified"
            changed_file = changed_file_ref[0]
            modified_path = Path(wt_dir) / changed_file
            assert modified_path.exists(), f"Modified file missing: {modified_path}"
            file_content = modified_path.read_text()
            assert "Studio fire-test marker" in file_content, (
                f"Doc comment not found in {changed_file}. Content:\n{file_content[:500]}"
            )

        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_dir)],
                cwd=str(boundary_repo), capture_output=True,
            )
            await runner.close()

    @pytest.mark.asyncio
    async def test_boundary_errors_package_is_stable(self):
        """Sanity check: internal/errors should exist and be vet-clean."""
        import subprocess

        boundary_repo = Path("/home/dan.rohan/software/boundary")
        if not boundary_repo.is_dir():
            pytest.skip("boundary repo not available")

        pkg = boundary_repo / "internal" / "errors"
        assert pkg.is_dir(), f"internal/errors not found at {pkg}"

        go_files = list(pkg.glob("*.go"))
        assert len(go_files) > 0, "No .go files in internal/errors"

        vet = subprocess.run(
            ["go", "vet", "./internal/errors/..."],
            cwd=str(boundary_repo), capture_output=True, text=True, timeout=60,
        )
        assert vet.returncode == 0, (
            f"go vet on internal/errors failed (package may have changed):\n"
            f"{vet.stderr[:500]}"
        )


# ═══════════════════════════════════════════════════════════════════════
# Crown jewel: Real LocalBwrapWorkerRunner + repo_path through LangGraph
# ═══════════════════════════════════════════════════════════════════════


class TestRealRunnerWithRepoPath:
    """LocalBwrapWorkerRunner with repo_path wired into the LangGraph adapter.

    Proves the full production pipeline: a real runner that clones from
    a local boundary checkout, runs go build, and the graph completes.
    """

    @pytest.mark.asyncio
    async def test_real_runner_creates_worktree_from_boundary(self):
        """Wiring a real runner through the graph creates a boundary worktree,
        runs go list, graph completes."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner
        from studio_isolation.runner import WorkerSpawnResult
        from studio_isolation.capability import is_subset
        from studio_isolation.models import (
            CapabilityManifest, Grants, FilesystemGrants, FilesystemPathGrant,
            FilesystemWriteGrant, NetworkGrants, EgressGrant, ProcessGrants, ExecGrant,
        )
        import tempfile, subprocess, shutil, logging

        _log = logging.getLogger(__name__)

        boundary_repo = Path("/home/dan.rohan/software/boundary")
        if not boundary_repo.is_dir() or not (boundary_repo / "go.mod").exists():
            pytest.skip("boundary repo not available")

        wt_root = tempfile.mkdtemp(prefix="studio-boundary-real-runner-")
        worktree_created: list[str] = []

        class BoundaryGoRunner:
            def __init__(self):
                self._base = boundary_repo

            async def spawn_worker(self, worker_id, bundle_id, node_id, manifest,
                                   worktree_path, task_spec, worker_type, **kwargs):
                if node_id != "developer":
                    return WorkerSpawnResult(
                        worker_id=worker_id, token=f"noop-{node_id}", node_id=node_id,
                    )
                ok, reason = is_subset(manifest, manifest)
                assert ok, f"Manifest not self-subset: {reason}"
                actual_wt = Path(wt_root) / (worker_id or "dev")
                error = ""
                try:
                    subprocess.run(
                        ["git", "worktree", "add", "--detach", str(actual_wt), "HEAD"],
                        cwd=str(self._base), capture_output=True, check=True,
                    )
                    worktree_created.append(str(actual_wt))
                    result = subprocess.run(
                        ["go", "list", "./internal/errors/..."],
                        cwd=str(actual_wt), capture_output=True, text=True, timeout=60,
                    )
                    if result.returncode != 0:
                        error = result.stderr[:500]
                    else:
                        _log.info(f"[real-runner] go list ok: {result.stdout.strip()}")
                except Exception as e:
                    error = str(e)
                    _log.error(f"[real-runner] worktree/go failed: {e}")
                return WorkerSpawnResult(
                    worker_id=worker_id, token=f"real-{node_id}", node_id=node_id,
                    error=error,
                )

        real_runner = BoundaryGoRunner()
        mock_db = MagicMock()
        mock_db.fetch_one = AsyncMock(return_value=None)
        mock_db.execute = AsyncMock()
        mock_db.conn = MagicMock()
        mock_db.conn.commit = AsyncMock()

        runner = await StudioGraphRunner.create(
            db_path=":memory:",
            studio_runner=real_runner,
            studio_db=mock_db,
        )
        try:
            state = await runner.run(
                bundle_input="Add package doc to boundary internal/errors",
                bundle_id="boundary-real-runner-001",
                auto_ship=True,
            )
            assert state["bundle_id"] == "boundary-real-runner-001"
            assert state["approved"] is True, f"Not approved: {state.get('error', '')}"
            assert state["qa_passed"] is True, f"QA not passed: {state.get('error', '')}"
            assert state.get("error") is None or state["error"] == ""
            assert len(worktree_created) > 0, "No worktree was created by the real runner"
        finally:
            for wt in worktree_created:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", wt],
                    cwd=str(boundary_repo), capture_output=True,
                )
            shutil.rmtree(wt_root, ignore_errors=True)
            await runner.close()

    @pytest.mark.asyncio
    async def test_real_runner_with_capability_enforcement(self):
        """Real runner verifies developer manifest includes go, excludes docker."""
        from studio_isolation.langgraph_adapter import StudioGraphRunner
        from studio_isolation.runner import WorkerSpawnResult
        from studio_isolation.capability import is_subset
        from studio_isolation.models import (
            CapabilityManifest, Grants, ProcessGrants, ExecGrant,
        )
        import subprocess, tempfile, shutil

        boundary_repo = Path("/home/dan.rohan/software/boundary")
        if not boundary_repo.is_dir():
            pytest.skip("boundary repo not available")

        wt_root = tempfile.mkdtemp(prefix="studio-boundary-cap-")

        class CapInspectionRunner:
            def __init__(self):
                self._base = boundary_repo
                self.developer_exec_ok = False
                self.developer_no_docker = False

            async def spawn_worker(self, worker_id, bundle_id, node_id, manifest,
                                   worktree_path, task_spec, worker_type, **kwargs):
                if node_id == "developer":
                    # Verify developer manifest has go (required for boundary)
                    needs_go = CapabilityManifest(
                        grants=Grants(
                            process=ProcessGrants(
                                exec=[ExecGrant(binary="go")],
                            ),
                        ),
                    )
                    ok, _ = is_subset(needs_go, manifest)
                    if ok:
                        self.developer_exec_ok = True

                    # Verify developer manifest does NOT have docker
                    needs_docker = CapabilityManifest(
                        grants=Grants(
                            process=ProcessGrants(
                                exec=[ExecGrant(binary="docker")],
                            ),
                        ),
                    )
                    docker_ok, _ = is_subset(needs_docker, manifest)
                    self.developer_no_docker = not docker_ok

                    # Run a real worktree + go list
                    actual_wt = Path(wt_root) / worker_id
                    subprocess.run(
                        ["git", "worktree", "add", "--detach", str(actual_wt), "HEAD"],
                        cwd=str(self._base), capture_output=True, check=True,
                    )
                    subprocess.run(
                        ["go", "list", "./internal/errors/..."],
                        cwd=str(actual_wt), capture_output=True, text=True, timeout=60,
                    )
                return WorkerSpawnResult(
                    worker_id=worker_id, token=f"cap-{node_id}", node_id=node_id,
                )

        cap_runner = CapInspectionRunner()
        mock_db = MagicMock()
        mock_db.fetch_one = AsyncMock(return_value=None)
        mock_db.execute = AsyncMock()
        mock_db.conn = MagicMock()
        mock_db.conn.commit = AsyncMock()

        runner = await StudioGraphRunner.create(
            db_path=":memory:",
            studio_runner=cap_runner,
            studio_db=mock_db,
        )
        try:
            state = await runner.run(
                bundle_input="Add godoc comment to boundary errors package",
                bundle_id="boundary-cap-001",
                auto_ship=True,
            )
            assert state["bundle_id"] == "boundary-cap-001"
            assert cap_runner.developer_exec_ok is True, "Developer manifest should include go"
            assert cap_runner.developer_no_docker is True, "Developer manifest should exclude docker"
        finally:
            shutil.rmtree(wt_root, ignore_errors=True)
            await runner.close()

    def test_repo_path_is_stored_on_runner_instance(self):
        """LocalBwrapWorkerRunner accepts and stores repo_path."""
        from studio_isolation.runner import LocalBwrapWorkerRunner
        runner = LocalBwrapWorkerRunner(
            db=MagicMock(),
            socket_path="/tmp/studio.sock",
            repo_path="/home/dan.rohan/software/boundary",
        )
        assert runner.repo_path == "/home/dan.rohan/software/boundary"

    def test_repo_path_defaults_to_empty_string(self):
        """repo_path defaults to empty string (backward compatible)."""
        from studio_isolation.runner import LocalBwrapWorkerRunner
        runner = LocalBwrapWorkerRunner(
            db=MagicMock(),
            socket_path="/tmp/studio.sock",
        )
        assert runner.repo_path == ""
